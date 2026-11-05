-------------------------------------------------------------------------------
-- Title      : ddr_validator_bist — self-checking pattern engine
-- Project    : rr-ddr-stream / zybo_z7_20_ddr_validator
-------------------------------------------------------------------------------
-- File       : ddr_validator_bist.vhd
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Standard   : VHDL-2008
-------------------------------------------------------------------------------
-- Description:
--   Drives the user side of ddr_stream_axi with a deterministic counter
--   pattern, reads it back through the DDR ring, and compares word-for-word.
--   The HW analogue of sim/cocotb/tests/test_ddr_stream_axi.py's
--   mem_model_demo / axi_4k_boundary_test, but against REAL PS DDR3 (via
--   S_AXI_HP0) instead of the cocotbext-axi AxiRam model.
--
--   Self-checking, host-independent: the expected value at read index i is
--   simply (i+1) — hard-coded by construction (ROUTERTL-002: compare against
--   hard expectations, never values re-derived from the same runtime input).
--
--   Adversarial gate (feedback_test_success_is_bugs_found): a run only
--   reports `pass` if BOTH words_written and words_read reached n_words AND
--   error_count = 0. A "green" run that moved zero data is a FAIL, mirroring
--   the cocotb stats.*_bursts >= 4 floors.
--
--   Scenarios (see docs/DESIGN.md §5):
--     0 roundtrip      — push n_words, read back, compare. The core test.
--     1 boundary       — identical logic; caller sets n_words large to span
--                        many bursts (4 KiB compliance is structural in
--                        burst*_setter, proven by round-tripping at scale).
--     2 reset_recovery — DDR-P2.9 / DDR-T2.15: pulses ip_rst_o once, halfway
--                        through the write pass (mid-burst, not at an idle
--                        boundary), holds it C_RESET_HOLD_CYCLES, then waits
--                        C_RESET_SETTLE_CYCLES (covering the FIFO backend's
--                        post-reset busy window DDR-T2.15 gates CS on) before
--                        restarting the write pass from word 1 and completing
--                        a normal roundtrip. Exercises the exact silicon-only
--                        regime DDR-T2.15 fixed: CS release racing FIFO
--                        reset-busy while real traffic was in flight.
--     3 pingpong       — STUB: needs DDR-P3.11 ptr-reset CSR. Reports skip
--                        (done, not fail) until the IP ports exist.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity ddr_validator_bist is
  generic (
    user_data_width_g : positive := 32
  );
  port (
    clk_i     : in  std_logic;             -- clk_sys
    rst_i     : in  std_logic;             -- active-high, sync

    -- Control (from CSR). start_i / soft_reset_i are 1-cycle pulses.
    start_i       : in  std_logic;
    soft_reset_i  : in  std_logic;
    scenario_i    : in  std_logic_vector(3 downto 0);
    n_words_i     : in  std_logic_vector(31 downto 0);

    -- Status (to CSR).
    running_o         : out std_logic;
    done_o            : out std_logic;
    pass_o            : out std_logic;
    fail_o            : out std_logic;
    error_count_o     : out std_logic_vector(31 downto 0);
    words_written_o   : out std_logic_vector(31 downto 0);
    words_read_o      : out std_logic_vector(31 downto 0);
    first_err_index_o : out std_logic_vector(31 downto 0);
    first_err_exp_o   : out std_logic_vector(31 downto 0);
    first_err_got_o   : out std_logic_vector(31 downto 0);

    -- ddr_stream_axi user side.
    s_axis_data_o   : out std_logic_vector(user_data_width_g-1 downto 0);
    s_axis_valid_o  : out std_logic;
    s_axis_ready_i  : in  std_logic;
    m_axis_data_i   : in  std_logic_vector(user_data_width_g-1 downto 0);
    m_axis_valid_i  : in  std_logic;
    m_axis_ready_o  : out std_logic;

    -- IP idle flags (from ddr_stream_axi write_idle_o / read_idle_o).
    write_idle_i    : in  std_logic;
    read_idle_i     : in  std_logic;

    -- Soft reset to the IP (top ORs this into the IP's rst_sys_i). Held during
    -- the BIST's own reset and pulsed for scenario 2. Active-high.
    ip_rst_o        : out std_logic
  );
end entity ddr_validator_bist;

architecture rtl of ddr_validator_bist is

  -- After the last write beat, wait this many cycles of write_idle before
  -- starting the read phase, so the AXI write pipeline + B responses drain.
  -- HW analogue of mem_model_demo's `await Timer(200, us)`.
  constant C_DRAIN_CYCLES : natural := 1024;

  -- DDR-P2.9: scenario 2 (reset_recovery) stimulus timing. C_RESET_HOLD_CYCLES
  -- is how long ip_rst_o pulses (well past a single-cycle glitch, so the IP's
  -- own registered rst_sys_i/rst_ddr_i genuinely samples it high). Ideally the
  -- IP's own busy_o/write_idle_o would gate the settle window directly, but
  -- those flags are not routed to this module — so C_RESET_SETTLE_CYCLES is a
  -- generous fixed bound covering the FIFO backend's post-reset busy window
  -- (the exact quantity DDR-T2.15 gates CS on) before resuming traffic.
  constant C_RESET_HOLD_CYCLES   : natural := 32;
  constant C_RESET_SETTLE_CYCLES : natural := 2048;

  type state_t is (S_IDLE, S_WRITE, S_RESET_HOLD, S_RESET_SETTLE, S_DRAIN, S_READ, S_DONE, S_SKIP);
  signal state_r : state_t := S_IDLE;

  signal n_words_r   : unsigned(31 downto 0) := (others => '0');
  signal wr_count_r  : unsigned(31 downto 0) := (others => '0');  -- beats accepted
  signal rd_count_r  : unsigned(31 downto 0) := (others => '0');  -- beats captured
  signal err_count_r : unsigned(31 downto 0) := (others => '0');
  signal drain_r     : unsigned(31 downto 0) := (others => '0');

  -- DDR-P2.9 scenario 2 (reset_recovery) bookkeeping.
  signal reset_armed_r    : std_logic := '0';  -- scenario_i = 2 was requested at start
  signal reset_fired_r    : std_logic := '0';  -- the one-shot mid-write pulse has fired
  signal reset_wait_cnt_r : unsigned(15 downto 0) := (others => '0');

  signal first_err_idx_r : unsigned(31 downto 0) := (others => '1');  -- 0xFFFFFFFF = none
  signal first_err_exp_r : std_logic_vector(31 downto 0) := (others => '0');
  signal first_err_got_r : std_logic_vector(31 downto 0) := (others => '0');

  signal pass_r : std_logic := '0';
  signal fail_r : std_logic := '0';

  -- Combinational handshakes.
  signal s_valid : std_logic;
  signal m_ready : std_logic;

  -- Expected next read word (1-based counter), as wide as the user word.
  -- rd_count_r counts captured beats; expected for beat k (0-based) is k+1.
  function expected_word(idx : unsigned) return std_logic_vector is
    variable e : unsigned(user_data_width_g-1 downto 0);
  begin
    e := resize(idx + 1, user_data_width_g);
    return std_logic_vector(e);
  end function;

begin

  -- Drive S_AXIS only while writing and below the target count.
  s_valid <= '1' when (state_r = S_WRITE) and (wr_count_r < n_words_r) else '0';
  -- Accept M_AXIS only during the read phase.
  m_ready <= '1' when (state_r = S_READ) else '0';

  s_axis_valid_o <= s_valid;
  -- Next word to write is (wr_count_r + 1): the 1-based counter pattern.
  s_axis_data_o  <= std_logic_vector(resize(wr_count_r + 1, user_data_width_g));
  m_axis_ready_o <= m_ready;

  running_o         <= '1' when (state_r = S_WRITE or state_r = S_RESET_HOLD
                                  or state_r = S_RESET_SETTLE or state_r = S_DRAIN
                                  or state_r = S_READ) else '0';
  done_o            <= '1' when (state_r = S_DONE or state_r = S_SKIP) else '0';
  pass_o            <= pass_r;
  fail_o            <= fail_r;
  error_count_o     <= std_logic_vector(err_count_r);
  words_written_o   <= std_logic_vector(wr_count_r);
  words_read_o      <= std_logic_vector(rd_count_r);
  first_err_index_o <= std_logic_vector(first_err_idx_r);
  first_err_exp_o   <= first_err_exp_r;
  first_err_got_o   <= first_err_got_r;

  process (clk_i)
    variable exp_v : std_logic_vector(user_data_width_g-1 downto 0);
  begin
    if rising_edge(clk_i) then
      ip_rst_o <= '0';

      if rst_i = '1' then
        state_r          <= S_IDLE;
        n_words_r        <= (others => '0');
        wr_count_r       <= (others => '0');
        rd_count_r       <= (others => '0');
        err_count_r      <= (others => '0');
        drain_r          <= (others => '0');
        first_err_idx_r  <= (others => '1');
        first_err_exp_r  <= (others => '0');
        first_err_got_r  <= (others => '0');
        pass_r           <= '0';
        fail_r           <= '0';
        reset_armed_r    <= '0';
        reset_fired_r    <= '0';
        reset_wait_cnt_r <= (others => '0');
        ip_rst_o        <= '1';            -- hold IP in reset while we are
      else
        -- soft_reset pulse from the CSR: bounce back to IDLE and reset the IP.
        if soft_reset_i = '1' then
          state_r  <= S_IDLE;
          ip_rst_o <= '1';
        end if;

        case state_r is

          when S_IDLE =>
            if start_i = '1' then
              -- Latch run parameters, clear per-run counters/results.
              n_words_r       <= unsigned(n_words_i);
              wr_count_r      <= (others => '0');
              rd_count_r      <= (others => '0');
              err_count_r     <= (others => '0');
              drain_r         <= (others => '0');
              first_err_idx_r <= (others => '1');
              first_err_exp_r <= (others => '0');
              first_err_got_r <= (others => '0');
              pass_r          <= '0';
              fail_r          <= '0';
              -- DDR-P2.9: scenario 2 (reset_recovery) arms a one-shot mid-write
              -- ip_rst_o pulse; the arming latches here so the pulse timing
              -- below can key off it without re-reading scenario_i (which the
              -- host could change mid-run).
              reset_armed_r   <= '1' when scenario_i = x"2" else '0';
              reset_fired_r   <= '0';
              -- scenario 3 (pingpong) needs DDR-P3.11 ports that don't exist
              -- yet — report skip rather than a misleading pass/fail.
              if scenario_i = x"3" then
                state_r <= S_SKIP;
              elsif unsigned(n_words_i) = 0 then
                state_r <= S_SKIP;          -- nothing to do
              else
                state_r <= S_WRITE;
              end if;
            end if;

          when S_WRITE =>
            -- Count an accepted beat on S_AXIS handshake.
            if (s_valid = '1') and (s_axis_ready_i = '1') then
              wr_count_r <= wr_count_r + 1;
            end if;
            -- DDR-P2.9 / DDR-T2.15: scenario 2's one-shot reset fires once,
            -- halfway through the write pass (SHIFT RIGHT by 1 = n_words/2) —
            -- this is deliberately mid-BURST, not at a drain/idle boundary,
            -- so the reset lands while the up-FIFO genuinely has in-flight
            -- traffic (the DDR-T2.15 wedge only manifests with real mid-stream
            -- state, never on a quiescent IP).
            if (reset_armed_r = '1') and (reset_fired_r = '0')
               and (wr_count_r >= ('0' & n_words_r(31 downto 1))) then
              reset_fired_r    <= '1';
              reset_wait_cnt_r <= (others => '0');
              state_r          <= S_RESET_HOLD;
            elsif (wr_count_r >= n_words_r) then
              state_r <= S_DRAIN;
              drain_r <= (others => '0');
            end if;

          when S_RESET_HOLD =>
            -- Drive ip_rst_o for C_RESET_HOLD_CYCLES — long enough that the
            -- IP's own registered rst_sys_i/rst_ddr_i unambiguously samples it
            -- (this module's own rst_i is NOT this signal, so the BIST state
            -- machine survives the pulse to observe the recovery).
            ip_rst_o <= '1';
            if reset_wait_cnt_r >= to_unsigned(C_RESET_HOLD_CYCLES, reset_wait_cnt_r'length) then
              reset_wait_cnt_r <= (others => '0');
              state_r          <= S_RESET_SETTLE;
            else
              reset_wait_cnt_r <= reset_wait_cnt_r + 1;
            end if;

          when S_RESET_SETTLE =>
            -- Bounded settle window covering the FIFO backend's post-reset
            -- busy window (DDR-T2.15) before traffic resumes. The IP's own
            -- write pointer reset to 0 with the reset, so the write pass
            -- restarts from word 1 rather than resuming mid-count — a partial
            -- pre-reset word is gone, exactly as on real silicon.
            if reset_wait_cnt_r >= to_unsigned(C_RESET_SETTLE_CYCLES, reset_wait_cnt_r'length) then
              wr_count_r <= (others => '0');
              state_r    <= S_WRITE;
            else
              reset_wait_cnt_r <= reset_wait_cnt_r + 1;
            end if;

          when S_DRAIN =>
            -- Wait for the write pipeline to drain (write_idle held) before
            -- reading, bounded by C_DRAIN_CYCLES so a stuck IP can't hang us.
            if (write_idle_i = '1') then
              drain_r <= drain_r + 1;
            else
              drain_r <= (others => '0');
            end if;
            if (drain_r >= to_unsigned(C_DRAIN_CYCLES, drain_r'length)) then
              state_r <= S_READ;
            end if;

          when S_READ =>
            -- Capture + compare on every M_AXIS handshake.
            if (m_axis_valid_i = '1') and (m_ready = '1') then
              exp_v := expected_word(rd_count_r);
              if m_axis_data_i /= exp_v then
                err_count_r <= err_count_r + 1;
                if first_err_idx_r = (first_err_idx_r'range => '1') then
                  first_err_idx_r <= rd_count_r;
                  first_err_exp_r <= std_logic_vector(resize(unsigned(exp_v), 32));
                  first_err_got_r <= std_logic_vector(resize(unsigned(m_axis_data_i), 32));
                end if;
              end if;
              rd_count_r <= rd_count_r + 1;
            end if;
            -- Done once we've read back as many words as we wrote.
            if (rd_count_r >= n_words_r) then
              state_r <= S_DONE;
            end if;

          when S_DONE =>
            -- Latch the verdict. Adversarial floor: pass requires full traffic
            -- in BOTH directions AND zero mismatches.
            if (wr_count_r >= n_words_r) and (rd_count_r >= n_words_r)
               and (err_count_r = 0) then
              pass_r <= '1';
              fail_r <= '0';
            else
              pass_r <= '0';
              fail_r <= '1';
            end if;

          when S_SKIP =>
            -- Defined no-op terminal (scenario unsupported / n=0). Neither
            -- pass nor fail; done_o is asserted so the host can distinguish.
            null;

          when others =>
            state_r <= S_IDLE;
        end case;
      end if;
    end if;
  end process;

end architecture rtl;

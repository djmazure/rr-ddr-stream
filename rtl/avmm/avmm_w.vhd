-------------------------------------------------------------------------------
-- Title      : avmm write master — Avalon-MM peer of axi4w.vhd
-- Project    : rr-ddr-stream
-------------------------------------------------------------------------------
-- File       : avmm_w.vhd
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Standard   : VHDL'93/02 (compatible with NVC --std=2008)
-------------------------------------------------------------------------------
-- Description:
--   Avalon-MM write master mirroring axi4w.vhd's IDLE -> BURST_ACTIVE
--   shape. Pulls one DDR word per beat from an upstream FIFO via a 2-deep
--   skid_buffer (same VFIFO-T1 fix as axi4w) and issues single-beat AVMM
--   writes with byte-address auto-bumped per beat.
--
--   Why single-beat (BURSTCOUNT=1) writes: real Avalon-MM bursts hold
--   ADDRESS constant for all beats and rely on the slave to auto-increment
--   internally. The v0.1 cocotb AvmmRam BFM (sdk/cocotb_helpers/avmm_ram.py)
--   commits beat 0 to ADDRESS and "the rest will arrive as separate writes
--   the loop picks up on later cycles" — i.e. the BFM samples ADDRESS
--   per-cycle. Single-beat-with-bumped-address is fully Avalon-compliant
--   AND works against the BFM. Real DDR controllers honour single-beat
--   writes too; v0.2 may add a true-burst variant guarded by a generic.
--
--   Burst length from the burst_setter (cmd_wlen_i + 1, AXI-style 0-based
--   AWLEN) becomes the count of consecutive single-beat AVMM writes
--   issued before returning to IDLE.
-------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.MATH_REAL.all;
library rr;
use rr.ddr_stream_pkg.all;

entity avmm_w is
  generic (
    ddr_data_width_g : integer                                   := 128;
    ddr_addr_width_g : integer                                   := 32;  -- 4G
    buffer_width_g   : integer                                   := 16;
    fifo_depth_g     : integer                                   := 12   -- 4096 words
  );
  port (
    -- Burst Intf
    cmd_valid_i  : in  std_logic;
    cmd_wlen_i   : in  std_logic_vector(7 downto 0);
    cmd_awaddr_i : in  std_logic_vector(ddr_addr_width_g - 1 downto 0);
    cmd_wdata_i  : in  std_logic_vector(ddr_data_width_g - 1 downto 0);
    cmd_rden_o   : out std_logic;        -- Request new data from FIFO
    cmd_ready_o  : out std_logic;
    -- DDR-P3.9 (2026-05-18): runtime base address — see axi4w.vhd.
    ddr_base_addr_i : in std_logic_vector(ddr_addr_width_g - 1 downto 0);
    -- DDR-P3.11 (2026-08-23): pointer-reset pulse — see axi4w.vhd. Clears the
    -- OFFSET tracker (ddr_addr_r) to 0. Gated to fsm_r=IDLE by the TOP.
    ptr_reset_i : in std_logic := '0';
    ---------------------------------------------------------------------------
    -- Avalon-MM master interface (write-only sub-record).
    ---------------------------------------------------------------------------
    aclk_i  : in std_logic;
    arstn_i : in std_logic;
    h2c_o : out avmm_write_h2c_t;
    c2h_i : in  avmm_write_c2h_t;
    --
    bus_werror_o  : out std_logic;
    ddr_pointer_o : out std_logic_vector(ddr_addr_width_g - 1 downto 0)
  );
end avmm_w;

architecture RTL of avmm_w is


  -- DDR-P3.9 (was RR--P3.4 constant): runtime base address as unsigned
  -- signal — see axi4w.vhd for the rationale.
  signal ddr_base_addr_c : unsigned(ddr_addr_width_g - 1 downto 0);
  -----------------------------------------------------------------------------
  -- Constants
  -----------------------------------------------------------------------------
  constant C_BEAT_BYTES : integer := ddr_data_width_g/8;
  -- Wraparound point for the DDR ring (= 2^ddr_addr_width_g). Same idiom as
  -- axi4w.vhd — NVC --std=2008 rejects the generic-indexed aggregate form.
  constant C_DDR_BUFFER_SIZE : unsigned(ddr_addr_width_g downto 0) :=
    shift_left(to_unsigned(1, ddr_addr_width_g + 1), ddr_addr_width_g);

  -----------------------------------------------------------------------------
  -- FSM — IDLE on reset, BURST_ACTIVE while issuing single-beat writes.
  -----------------------------------------------------------------------------
  type fsm_t is (IDLE, BURST_ACTIVE);
  signal fsm_r, fsm_n : fsm_t := IDLE;

  -----------------------------------------------------------------------------
  -- Address pointer + beat counters.
  -----------------------------------------------------------------------------
  signal ddr_addr_r, ddr_addr_n : unsigned(ddr_addr_width_g - 1 downto 0) :=
    (others => '0');
  signal beats_to_pull_r,  beats_to_pull_n  : unsigned(8 downto 0) :=
    (others => '0');
  signal beats_to_drive_r, beats_to_drive_n : unsigned(8 downto 0) :=
    (others => '0');

  -----------------------------------------------------------------------------
  -- Skid buffer between upstream FIFO and the AVMM bus (mirrors axi4w
  -- VFIFO-T1 fix). 2-deep, decouples FWFT FIFO read latency from per-cycle
  -- WAITREQUEST stalls.
  -----------------------------------------------------------------------------
  signal skid_s_valid : std_logic;
  signal skid_s_ready : std_logic;
  signal skid_m_valid : std_logic;
  signal skid_m_ready : std_logic;
  signal skid_m_data  : std_logic_vector(ddr_data_width_g - 1 downto 0);

  -----------------------------------------------------------------------------
  -- Misc state.
  -----------------------------------------------------------------------------
  signal write_idle_r, write_idle_n : std_logic := '1';
  signal write_active               : std_logic;
  signal beat_commit                : std_logic;

begin

  -- DDR-P3.9: present the runtime base address as unsigned.
  ddr_base_addr_c <= unsigned(ddr_base_addr_i);

  -----------------------------------------------------------------------------
  -- Skid buffer: producer side reads from cmd_wdata_i (FWFT FIFO head),
  -- gated by beats_to_pull_r. Consumer side feeds AVMM WRITEDATA when the
  -- FSM is in BURST_ACTIVE and the slave is ready (waitrequest='0').
  -----------------------------------------------------------------------------
  skid_s_valid <= '1' when beats_to_pull_r /= 0 else '0';
  skid_m_ready <= '1' when (fsm_r = BURST_ACTIVE)
                       and (c2h_i.WAITREQUEST = '0')
                       and (beats_to_drive_r /= 0)
                  else '0';

  i_wdata_skid : entity rr.skid_buffer
    generic map (
      data_width_g => ddr_data_width_g
    )
    port map (
      clk_i     => aclk_i,
      rst_i     => not arstn_i,
      s_data_i  => cmd_wdata_i,
      s_valid_i => skid_s_valid,
      s_ready_o => skid_s_ready,
      m_data_o  => skid_m_data,
      m_valid_o => skid_m_valid,
      m_ready_i => skid_m_ready
    );

  -----------------------------------------------------------------------------
  -- "WRITE asserted this cycle" combinational tap. The bus commits the beat
  -- when WRITE='1' AND WAITREQUEST='0' on the same rising edge. Per-cycle
  -- counters key off `beat_commit`.
  -----------------------------------------------------------------------------
  write_active <= '1' when (fsm_r = BURST_ACTIVE)
                       and (skid_m_valid = '1')
                       and (beats_to_drive_r /= 0)
                  else '0';
  beat_commit  <= write_active and not c2h_i.WAITREQUEST;

  -----------------------------------------------------------------------------
  -- Next-state logic.
  -----------------------------------------------------------------------------
  FSM_NSL : process (all)
  begin
    fsm_n            <= fsm_r;
    ddr_addr_n       <= ddr_addr_r;
    beats_to_pull_n  <= beats_to_pull_r;
    beats_to_drive_n <= beats_to_drive_r;
    write_idle_n     <= '0';

    -- Pull-side decrement on each skid producer commit.
    if (skid_s_valid = '1') and (skid_s_ready = '1') and (beats_to_pull_r /= 0) then
      beats_to_pull_n <= beats_to_pull_r - 1;
    end if;

    -- DDR-P3.9: ddr_addr_r is now OFFSET (not absolute). Per-beat
    -- increment in offset space; wrap-around to offset 0. The absolute
    -- AVMM ADDRESS is offset + base, computed at the bus output below.
    -- Drive-side decrement + offset bump on each AVMM write commit.
    if beat_commit = '1' then
      beats_to_drive_n <= beats_to_drive_r - 1;
      ddr_addr_n       <= ddr_addr_r + C_BEAT_BYTES;
      if ddr_addr_r = C_DDR_BUFFER_SIZE - C_BEAT_BYTES then
        ddr_addr_n <= (others => '0');
      end if;
    end if;

    case fsm_r is
      when IDLE =>
        write_idle_n <= '1';
        if cmd_valid_i = '1' then
          -- DDR-P3.9: cmd_awaddr_i is offset, ddr_addr_n is offset.
          ddr_addr_n       <= unsigned(cmd_awaddr_i);
          beats_to_pull_n  <= resize(unsigned(cmd_wlen_i), beats_to_pull_n'length) + 1;
          beats_to_drive_n <= resize(unsigned(cmd_wlen_i), beats_to_drive_n'length) + 1;
          write_idle_n     <= '0';
          fsm_n            <= BURST_ACTIVE;
        end if;

      when BURST_ACTIVE =>
        -- Last beat of this burst just committed → back to IDLE.
        if (beats_to_drive_r = 1) and (beat_commit = '1') then
          fsm_n <= IDLE;
        end if;

      when others =>
        fsm_n <= IDLE;
    end case;

    -- DDR-P3.11: pointer-reset override, gated to IDLE — see axi4w.vhd for
    -- the priority-over-cmd_valid_i rationale.
    if ptr_reset_i = '1' and fsm_r = IDLE then
      ddr_addr_n <= (others => '0');
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- Registered state.
  -----------------------------------------------------------------------------
  FSM_REG : process (aclk_i)
  begin
    if rising_edge(aclk_i) then
      if arstn_i = '0' then
        fsm_r            <= IDLE;
        -- DDR-P3.9: offset-based, reset to 0.
        ddr_addr_r       <= (others => '0');
        beats_to_pull_r  <= (others => '0');
        beats_to_drive_r <= (others => '0');
        write_idle_r     <= '1';
      else
        fsm_r            <= fsm_n;
        ddr_addr_r       <= ddr_addr_n;
        beats_to_pull_r  <= beats_to_pull_n;
        beats_to_drive_r <= beats_to_drive_n;
        write_idle_r     <= write_idle_n;
      end if;
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- AVMM bus outputs (combinational). Every shared-line driver is gated on
  -- write_active so the TOP can OR-mux this sub-master with avmm_r without
  -- collisions when neither side is asserting a request.
  -----------------------------------------------------------------------------
  -- DDR-P3.9: AVMM ADDRESS is offset + base.
  h2c_o.ADDRESS    <= std_logic_vector(ddr_addr_r + ddr_base_addr_c) when write_active = '1'
                      else (others => '0');
  h2c_o.WRITE      <= write_active;
  -- RR--P3.6: WRITEDATA / BYTEENABLE are C_AVMM_DATA_WIDTH wide in
  -- the record (the bus-width ceiling). Drive only the lower
  -- ddr_data_width_g bits with real data; zero-pad the rest.
  h2c_o.WRITEDATA(ddr_data_width_g - 1 downto 0) <= skid_m_data;
  h2c_o.WRITEDATA(C_AVMM_DATA_WIDTH - 1 downto ddr_data_width_g) <= (others => '0');
  h2c_o.BYTEENABLE(ddr_data_width_g/8 - 1 downto 0) <= (others => '1') when write_active = '1'
                                                       else (others => '0');
  h2c_o.BYTEENABLE(C_AVMM_BYTEEN_WIDTH - 1 downto ddr_data_width_g/8) <= (others => '0');
  -- BURSTCOUNT held at 1: each beat is its own commit. See header notes.
  h2c_o.BURSTCOUNT <= std_logic_vector(to_unsigned(1, C_AVMM_BURSTCOUNT_WIDTH))
                      when write_active = '1'
                      else (others => '0');

  -- Upstream FIFO interface.
  cmd_rden_o  <= skid_s_valid and skid_s_ready;
  cmd_ready_o <= write_idle_r;

  -- Avalon-MM has no per-transfer error response (no BRESP / RRESP). The
  -- error output stays for AXI-shape parity but is tied low; future
  -- variants can expose slave-defined error CSRs.
  bus_werror_o  <= '0';
  ddr_pointer_o <= std_logic_vector(ddr_addr_r);

end RTL;

-- ============================================================================
-- skid_buffer.vhd — 2-deep AXI-Stream-style skid buffer
--
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Date       : 2026-05-15
-- Standard   : VHDL 2008
-- Style      : esa-vhdl-provisional (port _i/_o, generic _g, registered _r,
--                                    type _t, ieee.numeric_std).
--
-- Purpose
-- =======
--
-- A 2-deep skid buffer decouples a producer from a consumer when the
-- producer's underlying source has a non-zero read latency that can't
-- sustain back-to-back consumer pulls. Both sides use the standard
-- AXI-Stream valid/ready protocol; the buffer transparently absorbs a
-- 1-cycle producer stall.
--
-- Use cases (this IP and beyond):
--   * Bridge an XPM_FIFO_ASYNC fwft FIFO (FIFO_READ_LATENCY=1) to a
--     consumer that wants 1 beat per cycle (VFIFO-T1, axi4w W-channel).
--   * AXI register slice — common ARM AMBA pattern.
--   * Forward/backward register slice for timing closure.
--   * Anywhere two valid/ready handshakes need to be decoupled in time.
--
-- State machine
-- =============
--
--    EMPTY ──s_handshake──▶ ONE ──s_handshake──▶ TWO
--      ▲                     │ ▲                  │
--      └──m_handshake────────┘ └─m_handshake──────┘
--
--   Both handshakes simultaneously in ONE keeps state ONE
--   (replace contents of stage A from input).
--
--   EMPTY  : s_ready_o='1', m_valid_o='0', stage A empty
--   ONE    : s_ready_o='1', m_valid_o='1', stage A holds head
--   TWO    : s_ready_o='0', m_valid_o='1', stage A holds head, B holds next
--
-- Behaviour notes
-- ===============
--
--   * No combinational paths from m_ready_i to s_ready_o (registered) —
--     timing-closure-friendly.
--   * Latency: 1 cycle from producer handshake to consumer-visible data
--     when buffer is initially empty. Zero added latency on subsequent
--     beats.
--   * Throughput: 1 beat per cycle sustained even when the producer's
--     source has up to 1-cycle stalls between handshakes.
-- ============================================================================

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity skid_buffer is
  generic (
    data_width_g : integer := 32
  );
  port (
    clk_i     : in  std_logic;
    rst_i     : in  std_logic;                                     -- active-high, sync
    -- Producer (slave) side
    s_data_i  : in  std_logic_vector(data_width_g - 1 downto 0);
    s_valid_i : in  std_logic;
    s_ready_o : out std_logic;
    -- Consumer (master) side
    m_data_o  : out std_logic_vector(data_width_g - 1 downto 0);
    m_valid_o : out std_logic;
    m_ready_i : in  std_logic
  );
end entity skid_buffer;

architecture rtl of skid_buffer is

  type state_t is (EMPTY, ONE, TWO);

  signal state_r  : state_t                                       := EMPTY;
  signal data_a_r : std_logic_vector(data_width_g - 1 downto 0)   := (others => '0');
  signal data_b_r : std_logic_vector(data_width_g - 1 downto 0)   := (others => '0');

  -- Combinational handshake helpers (named for readability).
  signal s_ready_int : std_logic;
  signal m_valid_int : std_logic;
  signal s_handshake : std_logic;
  signal m_handshake : std_logic;

begin

  -----------------------------------------------------------------------------
  -- Combinational: handshake conditions + outputs.
  -- s_ready=1 in EMPTY/ONE; m_valid=1 in ONE/TWO.
  -----------------------------------------------------------------------------
  s_ready_int <= '1' when (state_r = EMPTY or state_r = ONE) else '0';
  m_valid_int <= '1' when (state_r = ONE   or state_r = TWO) else '0';

  s_handshake <= s_valid_i and s_ready_int;
  m_handshake <= m_valid_int and m_ready_i;

  s_ready_o <= s_ready_int;
  m_valid_o <= m_valid_int;
  m_data_o  <= data_a_r;

  -----------------------------------------------------------------------------
  -- Sequential state machine. Single clocked process, sync reset.
  -----------------------------------------------------------------------------
  proc_skid : process (clk_i) is
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        state_r <= EMPTY;
      else
        case state_r is

          when EMPTY =>
            -- Only producer handshake possible.
            if s_handshake = '1' then
              data_a_r <= s_data_i;
              state_r  <= ONE;
            end if;

          when ONE =>
            -- All four combinations of handshakes possible. Order matters:
            -- handle the consumer-only and both-simultaneous cases before
            -- the producer-only case to avoid losing a beat.
            if m_handshake = '1' and s_handshake = '1' then
              -- Drain A and refill A from input in one cycle: stage stays ONE.
              data_a_r <= s_data_i;
            elsif m_handshake = '1' then
              -- Only consumer: A drains, no replacement.
              state_r <= EMPTY;
            elsif s_handshake = '1' then
              -- Only producer: A is still held (consumer didn't take), park
              -- the new beat in B; stage moves to TWO.
              data_b_r <= s_data_i;
              state_r  <= TWO;
            end if;

          when TWO =>
            -- s_ready_o=0 here, so the producer cannot push. Only the
            -- consumer handshake matters.
            if m_handshake = '1' then
              -- Drain A; B shifts forward into A.
              data_a_r <= data_b_r;
              state_r  <= ONE;
            end if;

          when others =>
            -- Defensive: unreachable once state_t is exhaustive, but
            -- ESA-V-005 requires explicit `when others =>`.
            state_r <= EMPTY;

        end case;
      end if;
    end if;
  end process proc_skid;

end architecture rtl;

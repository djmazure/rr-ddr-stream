-------------------------------------------------------------------------------
-- Title      : axi4 write
-- Project    : Tich App
-------------------------------------------------------------------------------
-- File       : axi4w.vhd
-- Author     : Daniel  <dasjimaz@gmail.com>
-- Company    : 
-- Created    : 2024-05-23
-- Last update: 2024-07-06
-- Platform   : 
-- Standard   : VHDL'93/02
-------------------------------------------------------------------------------
-- Description: Program AXI4 transfers based on an input FIFO
-------------------------------------------------------------------------------
-- Copyright (c) 2024 RigTich
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2024-05-23  1.0      dmazure Created
-------------------------------------------------------------------------------
library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use ieee.std_logic_textio.all;
use IEEE.MATH_REAL.all;
library rr;
use rr.ddr_stream_pkg.all;
-- use std.textio.all;
-- library unisim;
-- use unisim.vcomponents.all;
entity axi4w is
  generic (
    ddr_data_width_g   : integer := 512;
    ddr_addr_width_g   : integer := 32; -- 4G
    buffer_width_g     : integer := 16;
    fifo_depth_g       : integer := 12; -- 4096 words
    axi_id_writeport_g : integer := 0
  );
  port (
    -- Burst Intf
    cmd_valid_i  : in std_logic;
    cmd_wlen_i   : in std_logic_vector(7 downto 0);
    cmd_awaddr_i : in std_logic_vector(ddr_addr_width_g - 1 downto 0);
    cmd_wdata_i  : in std_logic_vector(ddr_data_width_g - 1 downto 0);
    cmd_rden_o   : out std_logic; -- Request new data from FIFO
    cmd_ready_o  : out std_logic;
    -- DDR-P3.9 (2026-05-18): runtime base address — see axi4r.vhd for
    -- the rationale. Driven by the TOP from a clk_ddr-side register.
    ddr_base_addr_i : in std_logic_vector(ddr_addr_width_g - 1 downto 0);
    -- DDR-P3.11 (2026-08-23): pointer-reset pulse — clears the OFFSET tracker
    -- (ddr_req_addrA_r / ddr_ptr_r) to 0 so a base-address swap can present a
    -- fresh window at offset 0 (ping-pong buffer switching), independent of
    -- csr_wr_base_load_i (which alone just shifts the ring by a constant).
    -- A single clk_ddr-domain pulse, ALREADY gated to fsm_ddr_write_r=IDLE by
    -- the TOP (mirrors the host's *_idle_o contract for base_load).
    ptr_reset_i : in std_logic := '0';
    ---------------------------------------------------------------------------
    -- AXI Full Interface
    ---------------------------------------------------------------------------
    aclk_i  : in std_logic;
    arstn_i : in std_logic;
    -- AXI4 write-only master interface (VFIFO-P3.4 record-typed cleanup):
    -- h2c_o.AWADDR is constrained to ddr_addr_width_g downstream — only
    -- the lower ddr_addr_width_g-1 downto 0 bits are populated.
    h2c_o : out axi4_write_h2c_t;
    c2h_i : in  axi4_write_c2h_t;
    --
    bus_werror_o  : out std_logic;
    ddr_pointer_o : out std_logic_vector(ddr_addr_width_g - 1 downto 0)
  );
end axi4w;


architecture RTL of axi4w is
  -- DDR-P3.9 (was RR--P3.4 constant): runtime base address presented
  -- as an unsigned signal for the address arithmetic.
  signal ddr_base_addr_c : unsigned(ddr_addr_width_g - 1 downto 0);
  -----------------------------------------------------------------------------
  -- Constants
  -----------------------------------------------------------------------------
  constant C_DDR_MAX_BITS : integer := ddr_addr_width_g - 2; -- divided by 4 = number of words
  --
  constant C_MAX_BURST_LENGTH : integer          := 64;
  constant C_AW_SIZE          : integer          := ddr_data_width_g/8;
  constant C_AW_SIZE_BITS     : integer          := integer(ceil(log2(real(C_AW_SIZE))));
  constant C_AW_SIZE_STDLV    : std_logic_vector := std_logic_vector(to_unsigned(C_AW_SIZE_BITS, C_AXI_SIZE_WIDTH));
  -- DS-T2.23 FIX: the ring wraps at the RING SIZE = 2^buffer_width_g, NOT at
  -- 2^ddr_addr_width_g (the full address space). The old value only coincided
  -- with the ring when ddr_addr_width_g = buffer_width_g (as in the cocotb sims,
  -- which set them equal). On a real part with ddr_addr_width_g (e.g. 32) >
  -- buffer_width_g (e.g. 28) the write OFFSET climbed toward 2^32 before
  -- wrapping, so M_AXI_AWADDR (= base + offset) escaped the reserved
  -- 2^buffer_width_g ring and scribbled PS RAM (kernel panic — DMA-out-of-region
  -- corruption). NVC --std=2008 rejects the generic-indexed aggregate form,
  -- hence shift_left.
  constant C_DDR_BUFFER_SIZE  : unsigned(ddr_addr_width_g downto 0) := shift_left(to_unsigned(1, ddr_addr_width_g + 1), buffer_width_g);
  -- Window-clamp mask = 2^buffer_width_g - 1. ANDing the incremented offset with
  -- this both wraps at the ring boundary AND hard-clamps the offset into the
  -- window, so no burst/CDC/off-by-one regression can ever let AWADDR escape.
  constant C_RING_OFFSET_MASK : unsigned(ddr_addr_width_g - 1 downto 0) := resize(C_DDR_BUFFER_SIZE - 1, ddr_addr_width_g);
  -----------------------------------------------------------------------------
  -- Types
  -----------------------------------------------------------------------------
  -- VFIFO-T1 fix (2026-05-15): added BURST_ACTIVE state. The FSM now
  -- delegates W-channel timing to a 2-deep skid_buffer instantiated below;
  -- BURST_ACTIVE just tracks beat counters and asserts WLAST on the last
  -- beat. The original DDR_DATA_WRITE state had a race against the FWFT
  -- FIFO's read latency under AXI backpressure (see rtl/support/skid_buffer.vhd
  -- header for the full root-cause analysis).
  --
  -- DDR-T3 fix (2026-05-27): restored the WAIT_BRESP state. The burst is NOT
  -- complete (and the reader-visible pointer must NOT advance) until the slave
  -- returns BRESP — a W-beat handshake only means the data is in flight through
  -- the interconnect + DRAM controller, not durable. Without this gate the
  -- downloader (reading on a different AXI ID, no read-after-write ordering)
  -- overtook the in-flight write and returned stale zeros on silicon while sim
  -- stayed green (the BFM committed on W-receipt). This matches the proven
  -- reference virtual_fifo/axi4w design; the skid fix above is orthogonal (it
  -- handles WITHIN-burst FIFO timing, this handles cross-side write→read order).
  type fsm_ddr_write_t is (IDLE, DDR_ADDR_WRITE, BURST_ACTIVE, WAIT_BRESP);

  signal fsm_ddr_write_r : fsm_ddr_write_t := IDLE;
  signal fsm_ddr_write_n : fsm_ddr_write_t;

  -----------------------------------------------------------------------------
  -- AXI Signals
  -----------------------------------------------------------------------------
  -- Address Write Channel
  signal m0_axi_awid_r    : std_logic_vector(C_AXI_ID_WIDTH - 1 downto 0)    := (others => '0');
  signal m0_axi_awaddr_r  : std_logic_vector(ddr_addr_width_g - 1 downto 0)  := (others => '0');
  signal m0_axi_awlen_r   : std_logic_vector(C_AXI_LEN_WIDTH - 1 downto 0)   := (others => '0');
  signal m0_axi_awsize_r  : std_logic_vector(C_AXI_SIZE_WIDTH - 1 downto 0)  := (others => '0');
  signal m0_axi_awburst_r : std_logic_vector(C_AXI_BURST_WIDTH - 1 downto 0) := (others => '0');
  signal m0_axi_awlock_r  : std_logic_vector(C_AXI_LOCK_WIDTH - 1 downto 0)  := (others => '0');
  signal m0_axi_awcache_r : std_logic_vector(C_AXI_CACHE_WIDTH - 1 downto 0) := (others => '0');
  signal m0_axi_awqos_r   : std_logic_vector(C_AXI_QOS_WIDTH - 1 downto 0)   := (others => '0');
  signal m0_axi_awprot_r  : std_logic_vector(C_AXI_PROT_WIDTH - 1 downto 0)  := (others => '0');
  signal m0_axi_awvalid_r : std_logic                                        := '0';

  -- Data Write Channel — only WSTRB is registered now (WDATA, WVALID,
  -- WLAST come from the skid buffer + combinational logic)
  signal m0_axi_wstrb_r  : std_logic_vector(ddr_data_width_g/8 - 1 downto 0) := (others => '0');
  signal m0_axi_bready_r : std_logic                                       := '0';

  -- Address Write Response Channel
  signal m0_axi_awid_n    : std_logic_vector(C_AXI_ID_WIDTH - 1 downto 0);
  signal m0_axi_awaddr_n  : std_logic_vector(ddr_addr_width_g - 1 downto 0);
  signal m0_axi_awlen_n   : std_logic_vector(C_AXI_LEN_WIDTH - 1 downto 0);
  signal m0_axi_awsize_n  : std_logic_vector(C_AXI_SIZE_WIDTH - 1 downto 0);
  signal m0_axi_awburst_n : std_logic_vector(C_AXI_BURST_WIDTH - 1 downto 0);
  signal m0_axi_awlock_n  : std_logic_vector(C_AXI_LOCK_WIDTH - 1 downto 0);
  signal m0_axi_awcache_n : std_logic_vector(C_AXI_CACHE_WIDTH - 1 downto 0);
  signal m0_axi_awqos_n   : std_logic_vector(C_AXI_QOS_WIDTH - 1 downto 0);
  signal m0_axi_awprot_n  : std_logic_vector(C_AXI_PROT_WIDTH - 1 downto 0);
  signal m0_axi_awvalid_n : std_logic;

  -- Data Write Response Channel — same trim as above
  signal m0_axi_wstrb_n  : std_logic_vector(ddr_data_width_g/8 - 1 downto 0);
  signal m0_axi_bready_n : std_logic;

  -------------------------------------------------------------------------------
  -- DDR pointers
  -------------------------------------------------------------------------------
  -- DDR-T3 (2026-05-27): the COMMITTED reader-visible pointer. Advances ONLY
  -- on a good BRESP (in WAIT_BRESP) — never per-beat. This is what
  -- ddr_pointer_o publishes to the downloader. ddr_req_addrA_r below is the
  -- in-flight write head and must stay private to the write side.
  signal ddr_ptr_r       : unsigned(ddr_addr_width_g - 1 downto 0) := (others => '0');
  signal ddr_ptr_n       : unsigned(ddr_addr_width_g - 1 downto 0);
  -- DDR-P3.9 (2026-05-18): ddr_req_addrA_r holds the OFFSET into the
  -- DDR ring, NOT the absolute DDR address. The absolute AWADDR is
  -- computed combinationally as (offset + base) so a runtime base
  -- change re-anchors subsequent bursts cleanly. The legacy
  -- absolute-with-base-accumulation pattern only worked at ddr_base=0
  -- (every burst double-added base; with base=0 the bug was masked).
  signal ddr_req_addrA_r : unsigned(ddr_addr_width_g - 1 downto 0) := (others => '0');
  signal ddr_req_addrA_n : unsigned(ddr_addr_width_g - 1 downto 0);
  signal burst_addr_abs  : unsigned(ddr_addr_width_g - 1 downto 0);  -- offset + base
  -- Absolute addr of the NEXT burst (offset from cmd_awaddr_i + base),
  -- computed combinationally so the FIRST burst out of IDLE picks up
  -- a correct AWADDR without waiting for ddr_req_addra_r to register.
  signal next_burst_addr_abs : unsigned(ddr_addr_width_g - 1 downto 0);

  -- 4KB-boundary AWLEN clamp, computed from the ACTUAL issue-time address.
  signal aw_bytes_to_4k : unsigned(12 downto 0);
  signal aw_len_4k_max  : unsigned(7 downto 0);
  signal aw_len_clamped : std_logic_vector(7 downto 0);

  -----------------------------------------------------------------------------
  -- Skid buffer (between upstream FIFO and AXI W-channel) — VFIFO-T1 fix
  -----------------------------------------------------------------------------
  signal s_skid_s_valid    : std_logic;
  signal s_skid_s_ready    : std_logic;
  signal s_skid_m_valid    : std_logic;
  signal s_skid_m_ready    : std_logic;
  signal s_skid_m_data     : std_logic_vector(ddr_data_width_g - 1 downto 0);

  -----------------------------------------------------------------------------
  -- Beat counters: how many beats are still owed to the FIFO (pull side)
  -- and to the AXI W-channel (drive side). Loaded at AW handshake.
  -----------------------------------------------------------------------------
  signal beats_to_pull_r   : unsigned(C_AXI_LEN_WIDTH downto 0) := (others => '0');
  signal beats_to_pull_n   : unsigned(C_AXI_LEN_WIDTH downto 0);
  signal beats_to_drive_r  : unsigned(C_AXI_LEN_WIDTH downto 0) := (others => '0');
  signal beats_to_drive_n  : unsigned(C_AXI_LEN_WIDTH downto 0);

  -----------------------------------------------------------------------------
  -- Misc
  -----------------------------------------------------------------------------
  signal writeIDle_r : std_logic := '0';
  signal writeIDle_n : std_logic;
  --
  -- signal writeDone_r       : std_logic                              := '0';
  -- signal writeDone_n       : std_logic;
  --
  signal write_error_r : std_logic := '0';
  signal write_error_n : std_logic;
  --
  signal s_local_reset : std_logic := '0';

begin

  -- DDR-P3.9: present the runtime base address as unsigned for the
  -- address arithmetic. Driven by the TOP from a clk_ddr-side register.
  ddr_base_addr_c <= unsigned(ddr_base_addr_i);
  burst_addr_abs      <= ddr_req_addrA_r + ddr_base_addr_c;
  next_burst_addr_abs <= unsigned(cmd_awaddr_i(ddr_addr_width_g - 1 downto 0)) + ddr_base_addr_c;

  -- Re-clamp the requested burst length to the beats remaining before the next
  -- 4KB boundary of the ACTUAL issue-time address. cmd_wlen_i is sized by
  -- burstw_setter against ptr_write_i, which can lag next_burst_addr_abs by a
  -- beat during its sizing pipeline — so a length legal for the earlier pointer
  -- can make this AW straddle a 4KB boundary (AXI-illegal). Surfaced only at
  -- 256-bit (32B beats) with a near-edge address; the 128/512 width sweep missed
  -- it. beats_to_4k = (4096 - addr[11:0]) / bytes_per_beat; AWLEN = that - 1.
  -- min() can only SHORTEN the burst, so it never breaks a legal one.
  aw_bytes_to_4k <= to_unsigned(4096, 13) - resize(next_burst_addr_abs(11 downto 0), 13);
  aw_len_4k_max  <= resize(shift_right(aw_bytes_to_4k, C_AW_SIZE_BITS) - 1, 8);
  aw_len_clamped <= cmd_wlen_i when unsigned(cmd_wlen_i) <= aw_len_4k_max
                    else std_logic_vector(aw_len_4k_max);

  -------------------------------------------------------------------------------
  --  AXI register next state
  -----------------------------------------------------------------------------

  REG_NS_AXI : process (aclk_i)
  begin
    if rising_edge(aclk_i) then
      if arstn_i = '0' then
        m0_axi_awid_r    <= (others => '0');
        m0_axi_awaddr_r  <= std_logic_vector(ddr_base_addr_c);
        m0_axi_awlen_r   <= (others => '0');
        m0_axi_awsize_r  <= (others => '0');
        m0_axi_awburst_r <= (others => '0');
        m0_axi_awlock_r  <= (others => '0');
        m0_axi_awcache_r <= (others => '0');
        m0_axi_awqos_r   <= (others => '0');
        m0_axi_awprot_r  <= (others => '0');
        m0_axi_awvalid_r <= '0';
        -- Data Write Channel
        m0_axi_wstrb_r  <= (others => '0');
        -- Signal Channel
        m0_axi_bready_r <= '0';
      else
        -- Address Write Channel
        m0_axi_awid_r    <= m0_axi_awid_n;
        m0_axi_awaddr_r  <= m0_axi_awaddr_n;
        m0_axi_awlen_r   <= m0_axi_awlen_n;
        m0_axi_awsize_r  <= m0_axi_awsize_n;
        m0_axi_awburst_r <= m0_axi_awburst_n;
        m0_axi_awlock_r  <= m0_axi_awlock_n;
        m0_axi_awcache_r <= m0_axi_awcache_n;
        m0_axi_awqos_r   <= m0_axi_awqos_n;
        m0_axi_awprot_r  <= m0_axi_awprot_n;
        m0_axi_awvalid_r <= m0_axi_awvalid_n;
        -- Data Write Channel — VFIFO-T1 fix: m0_axi_wdata_r / wlast /
        -- wvalid are no longer driven from the FSM. WDATA comes from
        -- the skid buffer's m_data_o; WVALID comes from the skid's
        -- m_valid_o gated on burst-active; WLAST is computed
        -- combinationally from beats_to_drive_r. We still register
        -- WSTRB (set once at AW handshake, held through the burst).
        m0_axi_wstrb_r  <= m0_axi_wstrb_n;
        -- Signal Channel
        m0_axi_bready_r <= m0_axi_bready_n;
      end if;
    end if;
  end process;
  -----------------------------------------------------------------------------
  -- Next State Logic
  -----------------------------------------------------------------------------

  FSM_WRITE_NSL : process (all)
  begin
    --
    fsm_ddr_write_n     <= fsm_ddr_write_r;
    ddr_req_addra_n     <= ddr_req_addra_r;
    ddr_ptr_n           <= ddr_ptr_r;
    beats_to_pull_n     <= beats_to_pull_r;
    beats_to_drive_n    <= beats_to_drive_r;
    -- AXI default
    m0_axi_awid_n    <= m0_axi_awid_r;
    m0_axi_awaddr_n  <= m0_axi_awaddr_r;
    m0_axi_awlen_n   <= m0_axi_awlen_r;
    m0_axi_awsize_n  <= m0_axi_awsize_r;
    m0_axi_awburst_n <= m0_axi_awburst_r;
    m0_axi_awlock_n  <= m0_axi_awlock_r;
    m0_axi_awcache_n <= m0_axi_awcache_r;
    m0_axi_awqos_n   <= m0_axi_awqos_r;
    m0_axi_awprot_n  <= m0_axi_awprot_r;
    m0_axi_awvalid_n <= m0_axi_awvalid_r;
    --
    m0_axi_bready_n <= m0_axi_bready_r;
    m0_axi_wstrb_n  <= m0_axi_wstrb_r;
    --
    write_error_n  <= write_error_r;
    writeIdle_n    <= '0';

    -- Beat counters: decrement on the actual handshakes that drove them.
    -- s_handshake = skid pulled one beat from the FIFO   → pull counter--
    -- m_handshake = AXI W consumed one beat from skid    → drive counter--
    if (s_skid_s_valid = '1') and (s_skid_s_ready = '1') and (beats_to_pull_r /= 0) then
      beats_to_pull_n <= beats_to_pull_r - 1;
    end if;
    if (s_skid_m_valid = '1') and (s_skid_m_ready = '1') and (beats_to_drive_r /= 0) then
      beats_to_drive_n <= beats_to_drive_r - 1;
    end if;

    case fsm_ddr_write_r is
      when IDLE =>
        writeIdle_n <= '1';
        if cmd_valid_i = '1' then
          -- DDR-P3.9: cmd_awaddr_i is the OFFSET from uploader (loops
          -- back from this entity's ddr_pointer_o which is offset-only).
          -- The absolute AWADDR is offset + base, computed in
          -- burst_addr_abs above. ddr_req_addra_r tracks the offset.
          ddr_req_addra_n <= unsigned(cmd_awaddr_i(ddr_addr_width_g - 1 downto 0));
          -----------------------------------------------------------------------
          -- Prepare the AXI transfer to DDR
          -----------------------------------------------------------------------
          m0_axi_awvalid_n <= '1';
          m0_axi_awid_n    <= std_logic_vector(to_unsigned(axi_id_writeport_g, m0_axi_awid_r'length));
          m0_axi_awaddr_n  <= std_logic_vector(next_burst_addr_abs(m0_axi_awaddr_r'length - 1 downto 0));
          m0_axi_awlen_n   <= aw_len_clamped;  -- 4KB-boundary clamp (see aw_len_clamped above)
          m0_axi_awsize_n  <= C_AW_SIZE_STDLV;
          m0_axi_awburst_n <= "01";
          m0_axi_awlock_n  <= (others => '0');
          m0_axi_awcache_n <= "0010";
          m0_axi_awprot_n  <= "000";
          m0_axi_wstrb_n   <= (others => '1');
          m0_axi_bready_n  <= '1';
          --
          fsm_ddr_write_n <= DDR_ADDR_WRITE;
        end if;
        --
      when DDR_ADDR_WRITE =>
        ---------------------------------------------------------------
        -- Write Address Handshake — load beat counters AT this moment
        -- so the skid buffer can start fetching from the FIFO and
        -- driving M_AXI_WDATA without any race against FIFO timing.
        ---------------------------------------------------------------
        if c2h_i.AWREADY = '1' and m0_axi_awvalid_r = '1' then
          m0_axi_awvalid_n <= '0';
          beats_to_pull_n  <= resize(unsigned(m0_axi_awlen_r), beats_to_pull_n'length) + 1;
          beats_to_drive_n <= resize(unsigned(m0_axi_awlen_r), beats_to_drive_n'length) + 1;
          fsm_ddr_write_n  <= BURST_ACTIVE;
        end if;

      when BURST_ACTIVE =>
        ---------------------------------------------------------------
        -- skid_buffer drives M_AXI_WDATA / WVALID / WLAST automatically.
        -- The FSM here just (a) tracks the DDR address pointer for the
        -- NEXT burst as beats drain, and (b) hands off to WAIT_BRESP when
        -- the last beat has handshaked out to AXI.
        ---------------------------------------------------------------
        if (s_skid_m_valid = '1') and (s_skid_m_ready = '1') then
          -- DDR-P3.9: increment offset per beat. DS-T2.23: wrap the offset at
          -- the ring size and hard-clamp it into the window in one step — the
          -- mask wraps at 2^buffer_width_g regardless of how ddr_addr_width_g
          -- relates to buffer_width_g, and guarantees the offset (hence
          -- M_AXI_AWADDR = base + offset) can never escape [0, 2^buffer_width_g).
          -- Replaces the old exact-match wrap against 2^ddr_addr_width_g.
          ddr_req_addrA_n <= (ddr_req_addra_r + C_AW_SIZE) and C_RING_OFFSET_MASK;
          if beats_to_drive_r = 1 then
            -- The just-fired m_handshake was the last beat of this burst.
            -- DDR-T3: do NOT return to IDLE or publish the pointer yet — the
            -- write is in flight, not durable. Wait for BRESP.
            fsm_ddr_write_n <= WAIT_BRESP;
          end if;
        end if;

      when WAIT_BRESP =>
        ---------------------------------------------------------------------
        -- DDR-T3 commit-gate. Hold here until the slave acknowledges the
        -- burst's write response. Only on a good BRESP do we publish the new
        -- committed pointer (ddr_ptr_n) to the reader; a bad response flags
        -- write_error and leaves the pointer unmoved. ddr_req_addra_r already
        -- holds the end-of-burst offset (advanced on the last beat above), so
        -- publishing it means "writes up to here are durable in DDR." cmd_ready_o
        -- stays low (writeIdle_n=0) here, so the next burst cannot start until
        -- this one commits — keeping the write head and committed pointer
        -- coherent for the cmd_awaddr_i loop-back.
        ---------------------------------------------------------------------
        if (c2h_i.BVALID = '1' and m0_axi_bready_r = '1') then
          m0_axi_bready_n <= '0';
          if (c2h_i.BRESP /= "00") then
            write_error_n <= '1';
          else
            ddr_ptr_n <= ddr_req_addra_r;
          end if;
          fsm_ddr_write_n <= IDLE;
        end if;

      when others =>
        fsm_ddr_write_n <= IDLE;
    end case;

    -- DDR-P3.11: pointer-reset override, gated to IDLE (the same instant the
    -- TOP's write_idle_o contract lets a host pulse base_load). Applied AFTER
    -- the case statement so it takes priority over a same-cycle cmd_valid_i —
    -- must NOT race the IDLE->DDR_ADDR_WRITE transition (a burst that starts
    -- this cycle keeps ddr_req_addra_n from the IDLE branch above, which
    -- already samples the OLD offset via cmd_awaddr_i; forcing the reset to
    -- also win here keeps ddr_req_addrA_r and ddr_ptr_r coherent at 0 rather
    -- than leaving one of them reloaded from a stale loop-back).
    if ptr_reset_i = '1' and fsm_ddr_write_r = IDLE then
      ddr_req_addra_n <= (others => '0');
      ddr_ptr_n       <= (others => '0');
    end if;
  end process;
  -----------------------------------------------------------------------------
  -- Register Non-AXI-signals
  -----------------------------------------------------------------------------
  FSM_REG_WRITE_PROC : process (aclk_i)
  begin
    if rising_edge(aclk_i) then
      if arstn_i = '0' then
        fsm_ddr_write_r  <= IDLE;
        -- DDR-P3.9: offset-based tracker resets to 0 (was: base value
        -- under absolute-tracking).
        ddr_req_addra_r  <= (others => '0');
        ddr_ptr_r        <= (others => '0');  -- DDR-T3: committed pointer
        beats_to_pull_r  <= (others => '0');
        beats_to_drive_r <= (others => '0');
        --
        write_error_r  <= '0';
        writeIDle_r    <= '1';
      else
        -----------------------------------------------------------------------
        -- Register next state logic
        -----------------------------------------------------------------------
        fsm_ddr_write_r  <= fsm_ddr_write_n;
        ddr_req_addra_r  <= ddr_req_addra_n;
        ddr_ptr_r        <= ddr_ptr_n;  -- DDR-T3: committed pointer
        beats_to_pull_r  <= beats_to_pull_n;
        beats_to_drive_r <= beats_to_drive_n;
        --
        writeIDle_r    <= writeIdle_n;
        write_error_r  <= write_error_n;
      end if;
    end if;
  end process;
  -----------------------------------------------------------------------------
  -- Skid buffer between upstream FIFO (cmd_wdata_i / cmd_rden_o) and the AXI
  -- W-channel (M0_AXI_W*). 2-deep, decouples FIFO read latency from AXI
  -- consumer back-to-back handshakes. See rtl/support/skid_buffer.vhd
  -- header for the full root-cause analysis (VFIFO-T1).
  --
  -- Producer side (consumes from FIFO):
  --   s_data_i  = live FIFO head (cmd_wdata_i, FWFT)
  --   s_valid_i = "we still owe beats to fetch for this burst"
  --   s_ready_o = "skid has room for one more beat"
  --   ⇒ cmd_rden_o = s_valid AND s_ready (one rd_en pulse per FIFO pop)
  --
  -- Consumer side (drives AXI W-channel):
  --   m_data_o  → M0_AXI_WDATA
  --   m_valid_o → M0_AXI_WVALID (while burst is active)
  --   m_ready_i ← M0_AXI_WREADY (while burst is active)
  -----------------------------------------------------------------------------
  s_skid_s_valid <= '1' when beats_to_pull_r /= 0 else '0';
  s_skid_m_ready <= c2h_i.WREADY when fsm_ddr_write_r = BURST_ACTIVE else '0';

  i_wdata_skid : entity rr.skid_buffer
    generic map (
      data_width_g => ddr_data_width_g
    )
    port map (
      clk_i     => aclk_i,
      rst_i     => not arstn_i,
      s_data_i  => cmd_wdata_i(ddr_data_width_g - 1 downto 0),
      s_valid_i => s_skid_s_valid,
      s_ready_o => s_skid_s_ready,
      m_data_o  => s_skid_m_data,
      m_valid_o => s_skid_m_valid,
      m_ready_i => s_skid_m_ready
    );

  -----------------------------------------------------------------------------
  -- Output — all bundled into the axi4_write_h2c_t record
  -----------------------------------------------------------------------------
  -- Address Write Channel
  h2c_o.AWID    <= m0_axi_awid_r;
  h2c_o.AWADDR  <= std_logic_vector(resize(unsigned(m0_axi_awaddr_r), C_AXI_ADDR_WIDTH));
  h2c_o.AWLEN   <= m0_axi_awlen_r;
  h2c_o.AWSIZE  <= m0_axi_awsize_r;
  h2c_o.AWBURST <= m0_axi_awburst_r;
  h2c_o.AWLOCK  <= m0_axi_awlock_r;
  h2c_o.AWCACHE <= m0_axi_awcache_r;
  h2c_o.AWPROT  <= m0_axi_awprot_r;
  h2c_o.AWQOS   <= m0_axi_awqos_r;
  h2c_o.AWVALID <= m0_axi_awvalid_r;
  -- Write Channel — driven by the skid buffer.
  -- RR--P3.6: WDATA / WSTRB are C_AXI_DATA_WIDTH bits in the record
  -- (the bus-width ceiling, 512 bits). Drive only the lower
  -- ddr_data_width_g bits with real data and zero-pad the rest.
  -- AWSIZE (= log2(ddr_data_width_g/8)) tells the slave the actual
  -- beat width; upper bits are ignored by AXI-spec-compliant slaves.
  h2c_o.WDATA(ddr_data_width_g - 1 downto 0)   <= s_skid_m_data;
  h2c_o.WDATA(C_AXI_DATA_WIDTH - 1 downto ddr_data_width_g) <= (others => '0');
  h2c_o.WSTRB(ddr_data_width_g/8 - 1 downto 0) <= m0_axi_wstrb_r;
  h2c_o.WSTRB(C_AXI_STRB_WIDTH - 1 downto ddr_data_width_g/8) <= (others => '0');
  h2c_o.WVALID  <= s_skid_m_valid when fsm_ddr_write_r = BURST_ACTIVE else '0';
  -- WLAST asserts on the very last beat handshaked out to AXI for this burst.
  h2c_o.WLAST   <= '1' when (fsm_ddr_write_r = BURST_ACTIVE)
                        and (s_skid_m_valid = '1')
                        and (beats_to_drive_r = 1)
                   else '0';
  -- Write Response Channel
  h2c_o.BREADY  <= m0_axi_bready_r;
  -- Upstream FIFO interface
  cmd_rden_o  <= s_skid_s_valid and s_skid_s_ready;
  cmd_ready_o <= writeIdle_r;
  --
  bus_werror_o  <= write_error_r;
  -- DDR-T3: publish the COMMITTED pointer (advanced only on BRESP in
  -- WAIT_BRESP), never the in-flight ddr_req_addra_r write head.
  ddr_pointer_o <= std_logic_vector(ddr_ptr_r(ddr_pointer_o'length - 1 downto 0));

end RTL;

-------------------------------------------------------------------------------
-- Title      : axi4 read
-- Project    : Astrape App
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
use IEEE.MATH_REAL.all;
use ieee.std_logic_textio.all;
use std.textio.all;
library rr;
use rr.ddr_stream_pkg.all;

-- library unisim;
-- use unisim.vcomponents.all;
entity axi4r is
  generic (
    ddr_data_width_g  : integer := 512;
    ddr_addr_width_g  : integer := 32; -- 4G
    buffer_width_g    : integer := 16;
    axi_id_readport_g : integer := 0
  );
  port (
    -- Burst Intf
    cmd_valid_i  : in std_logic; -- FIFO not FULL = internal ready signal
    cmd_rlen_i   : in std_logic_vector(7 downto 0);
    cmd_araddr_i : in std_logic_vector(ddr_addr_width_g - 1 downto 0);
    cmd_rdata_o  : out std_logic_vector(ddr_data_width_g - 1 downto 0);
    cmd_wren_o   : out std_logic; -- Request new data from FIFO
    cmd_ready_o  : out std_logic;
    -- DDR-P3.9 (2026-05-18): runtime base address — was a compile-time
    -- generic. Driven from a clk_ddr-side register inside the TOP
    -- (latched + 2-stage CDC'd from clk_sys CSR). Stable while
    -- bus_rerror_o / bus_busy is asserted; host honors *_idle_o
    -- before pulsing the load.
    ddr_base_addr_i : in std_logic_vector(ddr_addr_width_g - 1 downto 0);
    -- DDR-P3.11 (2026-08-23): pointer-reset pulse — see axi4w.vhd. Clears the
    -- OFFSET tracker (ddr_req_addrA_r, which axi4r publishes directly as
    -- ddr_pointer_o) to 0. Gated to fsm_ddr_read_r=IDLE by the TOP.
    ptr_reset_i : in std_logic := '0';
    ---------------------------------------------------------------------------
    -- AXI Full Interface
    ---------------------------------------------------------------------------
    aclk_i  : in std_logic;
    arstn_i : in std_logic;
    -- AXI4 read-only master interface (VFIFO-P3.4 record-typed cleanup):
    -- collapsed from 17 flat M0_AXI_* ports into two record-typed ports
    -- using axi4_read_h2c_t / axi4_read_c2h_t from vfifo_pkg.
    h2c_o : out axi4_read_h2c_t;
    c2h_i : in  axi4_read_c2h_t;
    --
    bus_rerror_o  : out std_logic;
    ddr_pointer_o : out std_logic_vector(ddr_addr_width_g - 1 downto 0)
  );
end axi4r;
architecture RTL of axi4r is

  -- DDR-P3.9 (was RR--P3.4 constant): runtime base address presented
  -- as an unsigned signal for the address arithmetic. Kept the
  -- ddr_base_addr_c name to minimise diff against the existing FSM.
  signal ddr_base_addr_c : unsigned(ddr_addr_width_g - 1 downto 0);
  -----------------------------------------------------------------------------
  -- Constants
  -----------------------------------------------------------------------------
  constant C_DDR_MAX_BITS : integer := ddr_addr_width_g - 2; -- divided by 4 = number of words
  --
  constant C_MAX_BURST_LENGTH : integer          := 64;
  constant C_AR_SIZE          : integer          := ddr_data_width_g/8;
  constant C_AR_SIZE_BITS     : integer          := integer(ceil(log2(real(C_AR_SIZE))));
  constant C_AR_SIZE_STLV     : std_logic_vector := std_logic_vector(to_unsigned(C_AR_SIZE_BITS, C_AXI_SIZE_WIDTH));
  -- DS-T2.23 FIX (read side, symmetric to axi4w): the ring wraps at the RING
  -- SIZE = 2^buffer_width_g, NOT at 2^ddr_addr_width_g. The old value only
  -- coincided with the ring when ddr_addr_width_g = buffer_width_g (as in the
  -- cocotb sims). On a real part with ddr_addr_width_g > buffer_width_g the read
  -- OFFSET would climb past the ring, so M_AXI_ARADDR (= base + offset) escapes
  -- the reserved ring and reads out-of-region. NVC --std=2008 rejects the
  -- generic-indexed aggregate form, hence shift_left.
  constant C_DDR_BUFFER_SIZE  : unsigned(ddr_addr_width_g downto 0) := shift_left(to_unsigned(1, ddr_addr_width_g + 1), buffer_width_g);
  -- Window-clamp mask = 2^buffer_width_g - 1: wraps at the ring AND hard-clamps
  -- the read offset into the window so ARADDR can never escape.
  constant C_RING_OFFSET_MASK : unsigned(ddr_addr_width_g - 1 downto 0) := resize(C_DDR_BUFFER_SIZE - 1, ddr_addr_width_g);
  -----------------------------------------------------------------------------
  -- Types
  -----------------------------------------------------------------------------
  type fsm_ddr_read_t is (IDLE, DDR_ADDR_ACK, DDR_DATA_READ);

  signal fsm_ddr_read_r : fsm_ddr_read_t := IDLE;
  signal fsm_ddr_read_n : fsm_ddr_read_t;

  -----------------------------------------------------------------------------
  -- AXI Signals
  -----------------------------------------------------------------------------
  -- Read Channel Signals (Updated)
  signal m0_axi_arid_r    : std_logic_vector(C_AXI_ID_WIDTH - 1 downto 0)    := (others => '0');
  signal m0_axi_araddr_r  : std_logic_vector(ddr_addr_width_g - 1 downto 0)  := (others => '0');
  signal m0_axi_arlen_r   : std_logic_vector(C_AXI_LEN_WIDTH - 1 downto 0)   := (others => '0');
  signal m0_axi_arsize_r  : std_logic_vector(C_AXI_SIZE_WIDTH - 1 downto 0)  := (others => '0');
  signal m0_axi_arburst_r : std_logic_vector(C_AXI_BURST_WIDTH - 1 downto 0) := (others => '0');
  signal m0_axi_arlock_r  : std_logic_vector(C_AXI_LOCK_WIDTH - 1 downto 0)  := (others => '0');
  signal m0_axi_arcache_r : std_logic_vector(C_AXI_CACHE_WIDTH - 1 downto 0) := (others => '0');
  signal m0_axi_arprot_r  : std_logic_vector(C_AXI_PROT_WIDTH - 1 downto 0)  := (others => '0');
  signal m0_axi_arqos_r   : std_logic_vector(C_AXI_QOS_WIDTH - 1 downto 0)   := (others => '0');
  signal m0_axi_arvalid_r : std_logic                                        := '0';
  signal m0_axi_rready_r  : std_logic                                        := '0';
  -- 
  signal m0_axi_arid_n    : std_logic_vector(C_AXI_ID_WIDTH - 1 downto 0);
  signal m0_axi_araddr_n  : std_logic_vector(ddr_addr_width_g - 1 downto 0);
  signal m0_axi_arlen_n   : std_logic_vector(C_AXI_LEN_WIDTH - 1 downto 0);
  signal m0_axi_arsize_n  : std_logic_vector(C_AXI_SIZE_WIDTH - 1 downto 0);
  signal m0_axi_arburst_n : std_logic_vector(C_AXI_BURST_WIDTH - 1 downto 0);
  signal m0_axi_arlock_n  : std_logic_vector(C_AXI_LOCK_WIDTH - 1 downto 0);
  signal m0_axi_arcache_n : std_logic_vector(C_AXI_CACHE_WIDTH - 1 downto 0);
  signal m0_axi_arprot_n  : std_logic_vector(C_AXI_PROT_WIDTH - 1 downto 0);
  signal m0_axi_arqos_n   : std_logic_vector(C_AXI_QOS_WIDTH - 1 downto 0);
  signal m0_axi_arvalid_n : std_logic;
  signal m0_axi_rready_n  : std_logic;
  --
  signal rdata_r : std_logic_vector(ddr_data_width_g - 1 downto 0) := (others => '0');
  signal rdata_n : std_logic_vector(ddr_data_width_g - 1 downto 0);

  -------------------------------------------------------------------------------
  -- DDR pointers
  -------------------------------------------------------------------------------
  signal ddr_ptr_r       : unsigned(ddr_addr_width_g - 1 downto 0) := (others => '0');
  -- DDR-P3.9: ddr_req_addrA_r holds the OFFSET into the DDR ring, NOT
  -- the absolute address. See axi4w.vhd for the rationale.
  signal ddr_req_addrA_r : unsigned(ddr_addr_width_g - 1 downto 0) := (others => '0');
  signal ddr_req_addrA_n : unsigned(ddr_addr_width_g - 1 downto 0);
  signal burst_addr_abs      : unsigned(ddr_addr_width_g - 1 downto 0);  -- offset + base
  signal next_burst_addr_abs : unsigned(ddr_addr_width_g - 1 downto 0);  -- next-offset + base

  -- 4KB-boundary ARLEN clamp, computed from the ACTUAL issue-time address.
  signal ar_bytes_to_4k : unsigned(12 downto 0);
  signal ar_len_4k_max  : unsigned(7 downto 0);
  signal ar_len_clamped : std_logic_vector(7 downto 0);

  -----------------------------------------------------------------------------
  -- Upstream FIFO control
  -----------------------------------------------------------------------------
  signal wrEn_r : std_logic := '0';
  signal wrEn_n : std_logic;

  -----------------------------------------------------------------------------
  -- Misc
  -----------------------------------------------------------------------------
  signal cnt_burst_read_r : unsigned(C_AXI_LEN_WIDTH-1 + 1 downto 0) := (others => '0');
  signal cnt_burst_read_n : unsigned(C_AXI_LEN_WIDTH-1 + 1 downto 0);
  --
  signal readIDle_r : std_logic := '0';
  signal readIDle_n : std_logic;
  --
  -- signal readDone_r       : std_logic                              := '0';
  -- signal readDone_n       : std_logic;
  --
  signal readError_r : std_logic := '0';
  signal readError_n : std_logic;
  --
  signal s_local_reset : std_logic := '0';

begin

  -- DDR-P3.9: present the runtime base address as an unsigned for the
  -- address arithmetic. Driven by the TOP from a clk_ddr-side register.
  ddr_base_addr_c <= unsigned(ddr_base_addr_i);
  burst_addr_abs      <= ddr_req_addrA_r + ddr_base_addr_c;
  next_burst_addr_abs <= unsigned(cmd_araddr_i(ddr_addr_width_g - 1 downto 0)) + ddr_base_addr_c;

  -- Re-clamp ARLEN to the beats remaining before the next 4KB boundary of the
  -- ACTUAL issue-time address (mirror of the axi4w write-side clamp). cmd_rlen_i
  -- is sized by burstr_setter against a pointer that can lag this address by a
  -- beat in its sizing pipeline, so the requested length could straddle a 4KB
  -- boundary (AXI-illegal). min() can only SHORTEN the burst → always legal.
  ar_bytes_to_4k <= to_unsigned(4096, 13) - resize(next_burst_addr_abs(11 downto 0), 13);
  ar_len_4k_max  <= resize(shift_right(ar_bytes_to_4k, C_AR_SIZE_BITS) - 1, 8);
  ar_len_clamped <= cmd_rlen_i when unsigned(cmd_rlen_i) <= ar_len_4k_max
                    else std_logic_vector(ar_len_4k_max);

  -------------------------------------------------------------------------------
  --  AXI register next state
  -----------------------------------------------------------------------------

  REG_NS_AXI : process (aclk_i)
  begin
    if rising_edge(aclk_i) then
      if arstn_i = '0' then
        -- Address Read Channel
        m0_axi_arid_r    <= (others => '0');
        m0_axi_araddr_r  <= std_logic_vector(ddr_base_addr_c);
        m0_axi_arlen_r   <= (others => '0');
        m0_axi_arsize_r  <= (others => '0');
        m0_axi_arburst_r <= (others => '0');
        m0_axi_arlock_r  <= (others => '0');
        m0_axi_arcache_r <= (others => '0');
        m0_axi_arprot_r  <= (others => '0');
        m0_axi_arqos_r   <= (others => '0');
        m0_axi_arvalid_r <= '0';
        -- Data Read Channel
        m0_axi_rready_r <= '0';
      else
        -- Address Read Channel
        m0_axi_arid_r    <= m0_axi_arid_n;
        m0_axi_araddr_r  <= m0_axi_araddr_n;
        m0_axi_arlen_r   <= m0_axi_arlen_n;
        m0_axi_arsize_r  <= m0_axi_arsize_n;
        m0_axi_arburst_r <= m0_axi_arburst_n;
        m0_axi_arlock_r  <= m0_axi_arlock_n;
        m0_axi_arcache_r <= m0_axi_arcache_n;
        m0_axi_arprot_r  <= m0_axi_arprot_n;
        m0_axi_arqos_r   <= m0_axi_arqos_n;
        m0_axi_arvalid_r <= m0_axi_arvalid_n;
        -- Data Read Channel
        m0_axi_rready_r <= m0_axi_rready_n;
      end if;
    end if;
  end process;
  -----------------------------------------------------------------------------
  -- Next State Logic
  -----------------------------------------------------------------------------

  FSM_READ_NSL : process (all)
  begin
    --
    fsm_ddr_read_n   <= fsm_ddr_read_r;
    ddr_req_addra_n  <= ddr_req_addra_r;
    cnt_burst_read_n <= cnt_burst_read_r;
    -- AXI default
    m0_axi_arid_n    <= m0_axi_arid_r;
    m0_axi_araddr_n  <= m0_axi_araddr_r;
    m0_axi_arlen_n   <= m0_axi_arlen_r;
    m0_axi_arsize_n  <= m0_axi_arsize_r;
    m0_axi_arburst_n <= m0_axi_arburst_r;
    m0_axi_arlock_n  <= m0_axi_arlock_r;
    m0_axi_arqos_n   <= m0_axi_arqos_r;
    m0_axi_arcache_n <= m0_axi_arcache_r;
    m0_axi_arprot_n  <= m0_axi_arprot_r;
    m0_axi_arvalid_n <= m0_axi_arvalid_r;
    --
    m0_axi_rready_n <= '0';
    readIdle_n     <= '0';
    wrEn_n         <= '0';
    rdata_n        <= (others => '0');
    -- DDR-P3.25: read error is sticky-until-reset (mirror axi4w write_error_r),
    -- so a transient RRESP=SLVERR survives long enough to cross to clk_sys and
    -- promote into ddr_csr_o.fifoStatus.read_error.
    readError_n    <= readError_r;

    case fsm_ddr_read_r is
      when IDLE =>
        readIdle_n <= '1';
        if cmd_valid_i = '1' then
          -- DDR-P3.9: cmd_araddr_i is the OFFSET from downloader; the
          -- absolute ARADDR is offset + base.
          ddr_req_addra_n <= unsigned(cmd_araddr_i(ddr_addr_width_g - 1 downto 0));
          fsm_ddr_read_n  <= DDR_ADDR_ACK;
          m0_axi_arvalid_n <= '1';
          m0_axi_arid_n    <= std_logic_vector(to_unsigned(axi_id_readport_g, m0_axi_arid_r'length));
          m0_axi_araddr_n  <= std_logic_vector(next_burst_addr_abs(m0_axi_araddr_r'length - 1 downto 0));
          m0_axi_arlen_n   <= ar_len_clamped;  -- 4KB-boundary clamp (see ar_len_clamped above)
          m0_axi_arsize_n  <= C_AR_SIZE_STLV; -- "110";    -- 64 bytes in transfer (2^6)
          m0_axi_arburst_n <= "01";
          m0_axi_arlock_n  <= (others => '0'); -- normal access;
          m0_axi_arcache_n <= "0010";
          m0_axi_arprot_n  <= "000";
          -------------------------------------------------------------------
          -- Go to reading bursts
          -------------------------------------------------------------------
          fsm_ddr_read_n <= DDR_ADDR_ACK;
        end if;
        --
      when DDR_ADDR_ACK =>
        ---------------------------------------------------------------
        -- Read Address Handshake
        ---------------------------------------------------------------
        if c2h_i.ARREADY = '1' and m0_axi_arvalid_r = '1' then
          m0_axi_arvalid_n <= '0';
          fsm_ddr_read_n   <= DDR_DATA_READ;
        end if;
      when DDR_DATA_READ =>
        ---------------------------------------------------------------
        -- Read Data Handshake
        ---------------------------------------------------------------
        m0_axi_rready_n <= '1'; -- Whether is space for this complete burst has been calculated beforehand
        if (c2h_i.RVALID = '1' and m0_axi_rready_r = '1') then
          -- RR--P3.6: c2h_i.RDATA is C_AXI_DATA_WIDTH wide; take only
          -- the lower ddr_data_width_g bits (the rest is zero-padding
          -- from the slave per AXI spec when ARSIZE selects a narrower
          -- beat width).
          rdata_n <= c2h_i.RDATA(ddr_data_width_g - 1 downto 0);
          wrEn_n  <= '1'; -- store the data in the Read Domain
          -- FIFO (core clk)
          cnt_burst_read_n <= cnt_burst_read_r + 1;
          -- DDR-P3.9: offset increments per beat. DS-T2.23 (symmetric to axi4w):
          -- wrap at the ring size and hard-clamp into the window via the mask, so
          -- M_AXI_ARADDR (= base + offset) can never escape [0, 2^buffer_width_g)
          -- regardless of how ddr_addr_width_g relates to buffer_width_g.
          ddr_req_addrA_n  <= (ddr_req_addra_r + C_AR_SIZE) and C_RING_OFFSET_MASK;
          if c2h_i.RLAST = '1' then -- Previous state was the only transfer
            m0_axi_rready_n  <= '0';
            cnt_burst_read_n <= (others => '0');
            --readIdle_n       <= '1';
            fsm_ddr_read_n   <= IDLE;
            --
          end if;
          if c2h_i.RRESP /= "00" or cnt_burst_read_r > unsigned(m0_axi_arlen_r) then
            readError_n <= '1';
          end if;
        end if;
      when others =>
        null;
    end case;

    -- DDR-P3.11: pointer-reset override, gated to IDLE — see axi4w.vhd for
    -- the priority-over-cmd_valid_i rationale (applied after the case so it
    -- wins on a same-cycle race, which the host contract shouldn't produce
    -- but the RTL should not depend on that).
    if ptr_reset_i = '1' and fsm_ddr_read_r = IDLE then
      ddr_req_addra_n <= (others => '0');
    end if;
  end process;
  -----------------------------------------------------------------------------
  -- Register Non-AXI-signals
  -----------------------------------------------------------------------------
  FSM_REG_READ_PROC : process (aclk_i)
  begin
    if rising_edge(aclk_i) then
      if arstn_i = '0' then
        fsm_ddr_read_r   <= IDLE;
        -- DDR-P3.9: offset-based tracker resets to 0.
        ddr_req_addra_r  <= (others => '0');
        cnt_burst_read_r <= (others => '0');
        wrEn_r           <= '0';
        --
        readError_r    <= '0';
        readIDle_r     <= '1';
      else
        -----------------------------------------------------------------------
        -- Register next state logic
        -----------------------------------------------------------------------
        fsm_ddr_read_r  <= fsm_ddr_read_n;
        ddr_req_addra_r <= ddr_req_addra_n;
        --
        wrEn_r           <= wrEn_n;
        rdata_r          <= rdata_n;
        cnt_burst_read_r <= cnt_burst_read_n;
        --
        readIDle_r     <= readIdle_n;
        readError_r    <= readError_n;
      end if;
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- Output
  -----------------------------------------------------------------------------
  -- Read Channel
  h2c_o.ARID    <= m0_axi_arid_r;
  h2c_o.ARADDR  <= std_logic_vector(resize(unsigned(m0_axi_araddr_r), C_AXI_ADDR_WIDTH));
  h2c_o.ARLEN   <= m0_axi_arlen_r;
  h2c_o.ARSIZE  <= m0_axi_arsize_r;
  h2c_o.ARBURST <= m0_axi_arburst_r;
  h2c_o.ARLOCK  <= m0_axi_arlock_r;
  h2c_o.ARCACHE <= m0_axi_arcache_r;
  h2c_o.ARPROT  <= m0_axi_arprot_r;
  h2c_o.ARQOS   <= m0_axi_arqos_r;
  h2c_o.ARVALID <= m0_axi_arvalid_r;

  -- Read channel
  h2c_o.RREADY <= m0_axi_rready_r;

  -- FIFO if
  cmd_wren_o  <= wrEn_r;
  cmd_ready_o <= readIdle_r;
  cmd_rdata_o <= rdata_r;
  --
  bus_rerror_o  <= readError_r;
  ddr_pointer_o <= std_logic_vector(ddr_req_addra_r(ddr_pointer_o'length - 1 downto 0));

end RTL;

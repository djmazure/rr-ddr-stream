-- File: wrapper_ddr_stream_axi.vhd
-- Purpose: Flatten AXI4 record ports for cocotb+GHDL friendliness
-- Standard: VHDL-2008

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library rr;
use rr.ddr_stream_pkg.all;

entity wrapper_ddr_stream_axi is
  generic (
    G_AXI_ID                 : integer                               := 0;
    G_FIFO_DEPTH             : integer                               := 9;
    -- RR--P3.6: user-side / DDR-side widths independent (power-of-two ratio).
    G_USER_DATA_WIDTH        : integer                               := 32;
    G_DDR_DATA_WIDTH         : integer                               := 128;
    G_DDR_ADDR_WIDTH         : integer                               := 32;
    -- DDR-P3.39: fixed-width like the public generic, so NVC can elaborate
    -- upper-half addresses such as x"8000_0000" on 32-bit-INTEGER builds.
    G_DDR_BASE_ADDR          : std_logic_vector(31 downto 0)          := (others => '0');
    -- NOTE: dev-branch virtual_fifo dropped the G_MIN_BURST_* and
    -- G_BURST_*_TIMEOUT_RATIO generics — those are now constants
    -- C_MIN_BURST_*_WIDTH and C_BURST_*_TIMEOUT_RATIO in vfifo_pkg.vhd.
    G_BUFFER_WIDTH           : integer                               := 31;
    -- RR--P3.2: pick the FIFO backend across all sites (cache + arb +
    -- ddr_fifo_intf upload/download/pad-event). "olo" = vendor-portable
    -- default; "xpm" = Xilinx golden reference.
    G_FIFO_IMPL              : string                                := "olo";
    -- DDR-P1.1 (2026-05-18): plane-elision pass-through. The cocotb
    -- flat port list stays unconditional so test handles are stable;
    -- only the DUT-internal silicon is elided when these go false.
    G_ENABLE_WRITE_PLANE     : boolean                               := true;
    G_ENABLE_READ_PLANE      : boolean                               := true;
    -- VFIFO-T2.6 / IDP-T2.5: max AXI write-burst length in BEATS, forwarded to
    -- the DUT. AXI4 default 256 keeps every existing test unchanged; the
    -- AXI3 BFM test (test_ddr_stream_axi_axi3) sweeps 256 -> 16 to prove the
    -- clamp catches the AXI3-illegal (>16-beat) burst.
    G_MAX_BURST_LEN          : integer                               := 256
  );
  port (
    -- Clocks & resets
    CLK_SYS  : in  std_logic;
    CLK_DDR  : in  std_logic;
    RST_SYS  : in  std_logic;
    RST_DDR  : in  std_logic;

    -- DDR-P3.10: AXI-Stream handshake on both sides. Flat-port names
    -- map 1:1 to the DUT entity ports (s_axis_*, m_axis_*).
    S_AXIS_DATA  : in  std_logic_vector(G_USER_DATA_WIDTH-1 downto 0);
    S_AXIS_VALID : in  std_logic;
    S_AXIS_READY : out std_logic;
    M_AXIS_DATA  : out std_logic_vector(G_USER_DATA_WIDTH-1 downto 0);
    M_AXIS_VALID : out std_logic;
    M_AXIS_READY : in  std_logic;

    -- DDR-P3.9: runtime base address — defaults so existing tests can
    -- ignore. CSR_*_BASE_ADDR + CSR_*_BASE_LOAD on the producer side,
    -- WRITE_IDLE / READ_IDLE status on the consumer side.
    CSR_WR_BASE_ADDR : in  std_logic_vector(G_DDR_ADDR_WIDTH-1 downto 0) := (others => '0');
    CSR_WR_BASE_LOAD : in  std_logic := '0';
    CSR_RD_BASE_ADDR : in  std_logic_vector(G_DDR_ADDR_WIDTH-1 downto 0) := (others => '0');
    CSR_RD_BASE_LOAD : in  std_logic := '0';
    -- DDR-P3.11: pointer-reset pulse — see ddr_stream_axi.vhd.
    CSR_WR_PTR_RESET : in  std_logic := '0';
    CSR_RD_PTR_RESET : in  std_logic := '0';
    -- DDR-P2.4 / DS-P2.59: software-managed ring read pointer (drain when the
    -- read plane is elided). Defaults let existing tests ignore it.
    CSR_RD_PTR_ADDR  : in  std_logic_vector(G_DDR_ADDR_WIDTH-1 downto 0) := (others => '0');
    CSR_RD_PTR_LOAD  : in  std_logic := '0';
    -- DDR-P3.16: software-managed ring write pointer (source when the write
    -- plane is elided). Defaults let existing tests ignore it.
    CSR_WR_PTR_ADDR  : in  std_logic_vector(G_DDR_ADDR_WIDTH-1 downto 0) := (others => '0');
    CSR_WR_PTR_LOAD  : in  std_logic := '0';
    WRITE_IDLE       : out std_logic;
    READ_IDLE        : out std_logic;
    -- DS-T2.20 / DDR-P2.19: live DMA write (commit) address. Exposed flat so the
    -- port sim can watch it advance as the software write pointer is loaded when
    -- the write plane is elided.
    WR_COMMIT_ADDR   : out std_logic_vector(G_DDR_ADDR_WIDTH-1 downto 0);

    -- CSR / status
    DDR_CSR  : out fifo_csr_t := C_INIT_FIFO_CSR;

    --------------------------------------------------------------------------
    -- FLAT AXI4 MASTER (from DUT master to memory)
    -- Write address channel
    M_AXI_AWID    : out std_logic_vector(C_AXI_ID_WIDTH-1    downto 0);
    M_AXI_AWADDR  : out std_logic_vector(C_AXI_ADDR_WIDTH-1  downto 0);
    M_AXI_AWLEN   : out std_logic_vector(C_AXI_LEN_WIDTH-1   downto 0);
    M_AXI_AWSIZE  : out std_logic_vector(C_AXI_SIZE_WIDTH-1  downto 0);
    M_AXI_AWBURST : out std_logic_vector(C_AXI_BURST_WIDTH-1 downto 0);
    M_AXI_AWLOCK  : out std_logic_vector(C_AXI_LOCK_WIDTH-1  downto 0);
    M_AXI_AWCACHE : out std_logic_vector(C_AXI_CACHE_WIDTH-1 downto 0);
    M_AXI_AWPROT  : out std_logic_vector(C_AXI_PROT_WIDTH-1  downto 0);
    M_AXI_AWQOS   : out std_logic_vector(C_AXI_QOS_WIDTH-1   downto 0);
    M_AXI_AWVALID : out std_logic;

    -- Write data channel
    -- RR--P3.6: wrapper-port widths track the active G_DDR_DATA_WIDTH.
    M_AXI_WDATA   : out std_logic_vector(G_DDR_DATA_WIDTH-1   downto 0);
    M_AXI_WSTRB   : out std_logic_vector(G_DDR_DATA_WIDTH/8-1 downto 0);
    M_AXI_WLAST   : out std_logic;
    M_AXI_WVALID  : out std_logic;

    -- Write response channel (master side)
    M_AXI_BREADY  : out std_logic;

    -- Read address channel
    M_AXI_ARID    : out std_logic_vector(C_AXI_ID_WIDTH-1    downto 0);
    M_AXI_ARADDR  : out std_logic_vector(C_AXI_ADDR_WIDTH-1  downto 0);
    M_AXI_ARLEN   : out std_logic_vector(C_AXI_LEN_WIDTH-1   downto 0);
    M_AXI_ARSIZE  : out std_logic_vector(C_AXI_SIZE_WIDTH-1  downto 0);
    M_AXI_ARBURST : out std_logic_vector(C_AXI_BURST_WIDTH-1 downto 0);
    M_AXI_ARLOCK  : out std_logic_vector(C_AXI_LOCK_WIDTH-1  downto 0);
    M_AXI_ARCACHE : out std_logic_vector(C_AXI_CACHE_WIDTH-1 downto 0);
    M_AXI_ARPROT  : out std_logic_vector(C_AXI_PROT_WIDTH-1  downto 0);
    M_AXI_ARQOS   : out std_logic_vector(C_AXI_QOS_WIDTH-1   downto 0);
    M_AXI_ARVALID : out std_logic;

    -- Read data channel (master side)
    M_AXI_RREADY  : out std_logic;

    --------------------------------------------------------------------------
    -- FLAT AXI4 SLAVE RESPONSE (from memory back to master/DUT)
    -- Write address channel
    M_AXI_AWREADY : in  std_logic;

    -- Write data channel
    M_AXI_WREADY  : in  std_logic;

    -- Write response channel
    M_AXI_BID     : in  std_logic_vector(C_AXI_ID_WIDTH-1   downto 0);
    M_AXI_BRESP   : in  std_logic_vector(C_AXI_RESP_WIDTH-1 downto 0);
    M_AXI_BVALID  : in  std_logic;

    -- Read address channel
    M_AXI_ARREADY : in  std_logic;

    -- Read data channel
    M_AXI_RID     : in  std_logic_vector(C_AXI_ID_WIDTH-1   downto 0);
    M_AXI_RDATA   : in  std_logic_vector(G_DDR_DATA_WIDTH-1 downto 0);
    M_AXI_RRESP   : in  std_logic_vector(C_AXI_RESP_WIDTH-1 downto 0);
    M_AXI_RLAST   : in  std_logic;
    M_AXI_RVALID  : in  std_logic
  );
end entity wrapper_ddr_stream_axi;

architecture rtl of wrapper_ddr_stream_axi is

  -- Internal record signals for the AXI bus (still recordised at the
  -- bus boundary; the user-data path now flat per DDR-P3.4).
  signal axi_h2c : axi4_h2c_bus_t;
  signal axi_c2h : axi4_c2h_bus_t;

  -- ──────────────────────────────────────────────────────────────────────
  -- VFIFO-T1 debug markers — driven from the cocotb cleaver_n20 test so
  -- RouteWave can show exactly where the WDATA race fires. All start at
  -- '0' / zero; cocotb pulses them at the relevant simulation moments.
  -- The signals are unconnected to RTL logic — they exist only to appear
  -- in the FST waveform for easy visual correlation.
  -- ──────────────────────────────────────────────────────────────────────
  signal t1_marker_aw_handshake     : std_logic                     := '0';
  signal t1_marker_w_beat           : std_logic                     := '0';
  signal t1_marker_w_correct        : std_logic                     := '0';
  signal t1_marker_w_wrong          : std_logic                     := '0';
  signal t1_marker_back_to_back     : std_logic                     := '0';
  signal t1_expected_lane0          : std_logic_vector(31 downto 0) := (others => '0');
  signal t1_actual_lane0            : std_logic_vector(31 downto 0) := (others => '0');
  signal t1_beat_index              : std_logic_vector( 7 downto 0) := (others => '0');
  signal t1_burst_index             : std_logic_vector( 7 downto 0) := (others => '0');

begin

  -----------------------------------------------------------------------------
  -- RECORD -> FLAT : drive flat outputs from the master (h2c)
  -----------------------------------------------------------------------------
  -- Write address
  M_AXI_AWID    <= axi_h2c.AWID;
  M_AXI_AWADDR  <= axi_h2c.AWADDR;
  M_AXI_AWLEN   <= axi_h2c.AWLEN;
  M_AXI_AWSIZE  <= axi_h2c.AWSIZE;
  M_AXI_AWBURST <= axi_h2c.AWBURST;
  M_AXI_AWLOCK  <= axi_h2c.AWLOCK;
  M_AXI_AWCACHE <= axi_h2c.AWCACHE;
  M_AXI_AWPROT  <= axi_h2c.AWPROT;
  M_AXI_AWQOS   <= axi_h2c.AWQOS;
  M_AXI_AWVALID <= axi_h2c.AWVALID;

  -- Write data
  -- RR--P3.6: axi_h2c.WDATA / WSTRB are C_AXI_DATA_WIDTH wide (the
  -- bus ceiling); slice down to the active ddr-side width on the way
  -- to the AxiRam slave.
  M_AXI_WDATA   <= axi_h2c.WDATA(G_DDR_DATA_WIDTH-1 downto 0);
  M_AXI_WSTRB   <= axi_h2c.WSTRB(G_DDR_DATA_WIDTH/8-1 downto 0);
  M_AXI_WLAST   <= axi_h2c.WLAST;
  M_AXI_WVALID  <= axi_h2c.WVALID;

  -- Write response readiness
  M_AXI_BREADY  <= axi_h2c.BREADY;

  -- Read address
  M_AXI_ARID    <= axi_h2c.ARID;
  M_AXI_ARADDR  <= axi_h2c.ARADDR;
  M_AXI_ARLEN   <= axi_h2c.ARLEN;
  M_AXI_ARSIZE  <= axi_h2c.ARSIZE;
  M_AXI_ARBURST <= axi_h2c.ARBURST;
  M_AXI_ARLOCK  <= axi_h2c.ARLOCK;
  M_AXI_ARCACHE <= axi_h2c.ARCACHE;
  M_AXI_ARPROT  <= axi_h2c.ARPROT;
  M_AXI_ARQOS   <= axi_h2c.ARQOS;
  M_AXI_ARVALID <= axi_h2c.ARVALID;

  -- Read data readiness
  M_AXI_RREADY  <= axi_h2c.RREADY;

  -----------------------------------------------------------------------------
  -- FLAT -> RECORD : capture slave responses (c2h) back into the record
  -----------------------------------------------------------------------------
  -- Write address / data ready from slave
  axi_c2h.AWREADY <= M_AXI_AWREADY;
  axi_c2h.WREADY  <= M_AXI_WREADY;

  -- Write response
  axi_c2h.BID     <= M_AXI_BID;
  axi_c2h.BRESP   <= M_AXI_BRESP;
  axi_c2h.BVALID  <= M_AXI_BVALID;

  -- Read address ready
  axi_c2h.ARREADY <= M_AXI_ARREADY;

  -- Read data
  axi_c2h.RID     <= M_AXI_RID;
  -- RR--P3.6: pad RDATA from the active ddr-side width up to
  -- C_AXI_DATA_WIDTH (the record's slot width).
  axi_c2h.RDATA(G_DDR_DATA_WIDTH-1 downto 0)              <= M_AXI_RDATA;
  axi_c2h.RDATA(C_AXI_DATA_WIDTH-1 downto G_DDR_DATA_WIDTH) <= (others => '0');
  axi_c2h.RRESP   <= M_AXI_RRESP;
  axi_c2h.RLAST   <= M_AXI_RLAST;
  axi_c2h.RVALID  <= M_AXI_RVALID;

  -----------------------------------------------------------------------------
  -- Original entity instantiation (keeps your exact generics/ports)
  -----------------------------------------------------------------------------
  u_ddr_stream_axi : entity rr.ddr_stream_axi
    generic map (
      axi_id_g          => G_AXI_ID,
      fifo_depth_g      => G_FIFO_DEPTH,
      user_data_width_g => G_USER_DATA_WIDTH,
      ddr_data_width_g  => G_DDR_DATA_WIDTH,
      ddr_addr_width_g  => G_DDR_ADDR_WIDTH,
      ddr_base_addr_g  => G_DDR_BASE_ADDR,
      buffer_width_g   => G_BUFFER_WIDTH,
      fifo_impl_g      => G_FIFO_IMPL,
      enable_write_plane_g => G_ENABLE_WRITE_PLANE,
      enable_read_plane_g  => G_ENABLE_READ_PLANE,
      max_burst_len_g      => G_MAX_BURST_LEN
    )
    port map (
      clk_sys_i        => CLK_SYS,
      clk_ddr_i        => CLK_DDR,
      rst_sys_i        => RST_SYS,
      rst_ddr_i        => RST_DDR,
      s_axis_data_i    => S_AXIS_DATA,
      s_axis_valid_i   => S_AXIS_VALID,
      s_axis_ready_o   => S_AXIS_READY,
      m_axis_data_o    => M_AXIS_DATA,
      m_axis_valid_o   => M_AXIS_VALID,
      m_axis_ready_i   => M_AXIS_READY,
      csr_wr_base_addr_i => CSR_WR_BASE_ADDR,
      csr_wr_base_load_i => CSR_WR_BASE_LOAD,
      csr_rd_base_addr_i => CSR_RD_BASE_ADDR,
      csr_rd_base_load_i => CSR_RD_BASE_LOAD,
      csr_wr_ptr_reset_i => CSR_WR_PTR_RESET,
      csr_rd_ptr_reset_i => CSR_RD_PTR_RESET,
      csr_rd_ptr_addr_i  => CSR_RD_PTR_ADDR,
      csr_rd_ptr_load_i  => CSR_RD_PTR_LOAD,
      csr_wr_ptr_addr_i  => CSR_WR_PTR_ADDR,
      csr_wr_ptr_load_i  => CSR_WR_PTR_LOAD,
      write_idle_o       => WRITE_IDLE,
      read_idle_o        => READ_IDLE,
      wr_commit_addr_o   => WR_COMMIT_ADDR,
      ddr_csr_o        => DDR_CSR,
      m_axi4_h2c_bus_o => axi_h2c,
      m_axi4_c2h_bus_i => axi_c2h
    );

end architecture rtl;

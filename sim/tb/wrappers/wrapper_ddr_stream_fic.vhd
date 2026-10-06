-- SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
-------------------------------------------------------------------------------
-- Title      : wrapper_ddr_stream_fic — Flat-port cocotb wrapper for ddr_stream_fic
-- Project    : rr-ddr-stream
-------------------------------------------------------------------------------
-- File       : wrapper_ddr_stream_fic.vhd
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Flattens AXI4 bus records to flat std_logic / std_logic_vector ports for
--   cocotb test attachment, targeting the Microchip PolarFire SoC FIC backend.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library rr;
use rr.ddr_stream_pkg.all;

entity wrapper_ddr_stream_fic is
  generic (
    G_AXI_ID             : integer                               := 0;
    G_FIFO_DEPTH         : integer                               := 9;
    G_USER_DATA_WIDTH    : integer                               := 32;
    G_DDR_DATA_WIDTH     : integer                               := C_FIC_DATA_WIDTH;      -- 64
    G_DDR_ADDR_WIDTH     : integer                               := 32;
    G_DDR_BASE_ADDR      : std_logic_vector(31 downto 0)         := (others => '0');
    G_BUFFER_WIDTH       : integer                               := 31;
    G_FIFO_IMPL          : string                                := "olo";
    G_ENABLE_WRITE_PLANE : boolean                               := true;
    G_ENABLE_READ_PLANE  : boolean                               := true;
    G_MAX_BURST_LEN      : integer                               := C_PFSOC_FIC_MAX_BURST  -- 64
  );
  port (
    CLK_SYS  : in  std_logic;
    CLK_DDR  : in  std_logic;
    RST_SYS  : in  std_logic;
    RST_DDR  : in  std_logic;

    -- User streaming
    S_AXIS_DATA  : in  std_logic_vector(G_USER_DATA_WIDTH-1 downto 0);
    S_AXIS_VALID : in  std_logic;
    S_AXIS_READY : out std_logic;
    M_AXIS_DATA  : out std_logic_vector(G_USER_DATA_WIDTH-1 downto 0);
    M_AXIS_VALID : out std_logic;
    M_AXIS_READY : in  std_logic;

    -- CSR runtime base address
    CSR_WR_BASE_ADDR : in  std_logic_vector(G_DDR_ADDR_WIDTH-1 downto 0) := (others => '0');
    CSR_WR_BASE_LOAD : in  std_logic := '0';
    CSR_RD_BASE_ADDR : in  std_logic_vector(G_DDR_ADDR_WIDTH-1 downto 0) := (others => '0');
    CSR_RD_BASE_LOAD : in  std_logic := '0';

    -- Pointer reset
    CSR_WR_PTR_RESET : in  std_logic := '0';
    CSR_RD_PTR_RESET : in  std_logic := '0';

    -- Software pointers
    CSR_RD_PTR_ADDR  : in  std_logic_vector(G_DDR_ADDR_WIDTH-1 downto 0) := (others => '0');
    CSR_RD_PTR_LOAD  : in  std_logic := '0';
    CSR_WR_PTR_ADDR  : in  std_logic_vector(G_DDR_ADDR_WIDTH-1 downto 0) := (others => '0');
    CSR_WR_PTR_LOAD  : in  std_logic := '0';

    WRITE_IDLE       : out std_logic;
    READ_IDLE        : out std_logic;

    -- CSR & status telemetry
    DDR_CSR          : out fifo_csr_t := C_INIT_FIFO_CSR;
    VERSION          : out std_logic_vector(31 downto 0);
    WR_COMMIT_ADDR   : out std_logic_vector(G_DDR_ADDR_WIDTH-1 downto 0);
    DBG_RESET_STATUS : out std_logic_vector(3 downto 0);

    --------------------------------------------------------------------------
    -- FLAT AXI4 MASTER (from DUT master to memory/FIC target)
    --------------------------------------------------------------------------
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
    -- FLAT AXI4 SLAVE RESPONSE (from FIC target memory back to master/DUT)
    --------------------------------------------------------------------------
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
end entity wrapper_ddr_stream_fic;

architecture rtl of wrapper_ddr_stream_fic is

  signal axi_h2c : axi4_h2c_bus_t;
  signal axi_c2h : axi4_c2h_bus_t;

begin

  -----------------------------------------------------------------------------
  -- RECORD -> FLAT : drive flat outputs from the master (h2c)
  -----------------------------------------------------------------------------
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

  M_AXI_WDATA   <= axi_h2c.WDATA(G_DDR_DATA_WIDTH-1 downto 0);
  M_AXI_WSTRB   <= axi_h2c.WSTRB(G_DDR_DATA_WIDTH/8-1 downto 0);
  M_AXI_WLAST   <= axi_h2c.WLAST;
  M_AXI_WVALID  <= axi_h2c.WVALID;

  M_AXI_BREADY  <= axi_h2c.BREADY;

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

  M_AXI_RREADY  <= axi_h2c.RREADY;

  -----------------------------------------------------------------------------
  -- FLAT -> RECORD : capture slave responses (c2h) back into the record
  -----------------------------------------------------------------------------
  axi_c2h.AWREADY <= M_AXI_AWREADY;
  axi_c2h.WREADY  <= M_AXI_WREADY;

  axi_c2h.BID     <= M_AXI_BID;
  axi_c2h.BRESP   <= M_AXI_BRESP;
  axi_c2h.BVALID  <= M_AXI_BVALID;

  axi_c2h.ARREADY <= M_AXI_ARREADY;

  axi_c2h.RID     <= M_AXI_RID;
  axi_c2h.RDATA(G_DDR_DATA_WIDTH-1 downto 0)              <= M_AXI_RDATA;
  axi_c2h.RDATA(C_AXI_DATA_WIDTH-1 downto G_DDR_DATA_WIDTH) <= (others => '0');
  axi_c2h.RRESP   <= M_AXI_RRESP;
  axi_c2h.RLAST   <= M_AXI_RLAST;
  axi_c2h.RVALID  <= M_AXI_RVALID;

  -----------------------------------------------------------------------------
  -- DUT instantiation
  -----------------------------------------------------------------------------
  u_ddr_stream_fic : entity rr.ddr_stream_fic
    generic map (
      axi_id_g             => G_AXI_ID,
      fifo_depth_g         => G_FIFO_DEPTH,
      user_data_width_g    => G_USER_DATA_WIDTH,
      ddr_data_width_g     => G_DDR_DATA_WIDTH,
      ddr_addr_width_g     => G_DDR_ADDR_WIDTH,
      ddr_base_addr_g      => G_DDR_BASE_ADDR,
      buffer_width_g       => G_BUFFER_WIDTH,
      fifo_impl_g          => G_FIFO_IMPL,
      enable_write_plane_g => G_ENABLE_WRITE_PLANE,
      enable_read_plane_g  => G_ENABLE_READ_PLANE,
      max_burst_len_g      => G_MAX_BURST_LEN
    )
    port map (
      clk_sys_i          => CLK_SYS,
      clk_ddr_i          => CLK_DDR,
      rst_sys_i          => RST_SYS,
      rst_ddr_i          => RST_DDR,
      s_axis_data_i      => S_AXIS_DATA,
      s_axis_valid_i     => S_AXIS_VALID,
      s_axis_ready_o     => S_AXIS_READY,
      m_axis_data_o      => M_AXIS_DATA,
      m_axis_valid_o     => M_AXIS_VALID,
      m_axis_ready_i     => M_AXIS_READY,
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
      ddr_csr_o          => DDR_CSR,
      version_o          => VERSION,
      wr_commit_addr_o   => WR_COMMIT_ADDR,
      dbg_reset_status_o => DBG_RESET_STATUS,
      m_axi4_h2c_bus_o   => axi_h2c,
      m_axi4_c2h_bus_i   => axi_c2h
    );

end architecture rtl;

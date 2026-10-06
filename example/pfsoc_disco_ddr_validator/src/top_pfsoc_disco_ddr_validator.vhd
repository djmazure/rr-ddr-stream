-- SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
-------------------------------------------------------------------------------
-- Title      : top_pfsoc_disco_ddr_validator — top entity for PolarFire SoC Discovery Kit
-- Project    : rr-ddr-stream / pfsoc_disco_ddr_validator
-- Standard   : VHDL-2008
-------------------------------------------------------------------------------
-- Description:
--   Top entity wiring the rr-ddr-stream validator core (AVMM/AXI via
--   ddr_stream_fic) to the Microchip PolarFire SoC MSS DDR4 controller + FIC.
--   Instantiates the pfsoc_mss SmartDesign component and pfsoc_ddr_validator_core,
--   routing FIC1 AXI4 target traffic directly to the DDR4 subsystem and passing
--   through the dedicated MSS DDR4/REFCLK package balls.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library rr;
use rr.ddr_stream_pkg.all;

entity top_pfsoc_disco_ddr_validator is
  port (
    -- On-board 50 MHz fabric oscillator (ref_clk_50mhz, pin R18, HSIO Bank 0).
    ref_clk_50mhz : in std_logic;

    -- UJTAG TAP boundary for REA (dedicated JTAG pads).
    jtag_tck      : in  std_logic := '0';
    jtag_tms      : in  std_logic := '0';
    jtag_tdi      : in  std_logic := '0';
    jtag_trstb    : in  std_logic := '1';
    jtag_tdo      : out std_logic;

    -- MSS dedicated-pad passthrough ports (auto-promoted by Libero for MSS dedicated package balls).
    CAN_1_RXBUS                 : in    std_logic;
    EMMC_STRB_SD_CD             : in    std_logic;
    MMUART_0_RXD                : in    std_logic;
    REFCLK                      : in    std_logic;
    REFCLK_N                    : in    std_logic;
    SGMII_RX0_N                 : in    std_logic;
    SGMII_RX0_P                 : in    std_logic;
    SGMII_RX1_N                 : in    std_logic;
    SGMII_RX1_P                 : in    std_logic;
    USB_CLK                     : in    std_logic;
    USB_DIR                     : in    std_logic;
    USB_NXT                     : in    std_logic;
    A                           : out   std_logic_vector(13 downto 0);
    ACT_N                       : out   std_logic;
    BA                          : out   std_logic_vector(1 downto 0);
    BG0                         : out   std_logic;
    CAN_1_TXBUS                 : out   std_logic;
    CAN_1_TX_EBL_N              : out   std_logic;
    CAS_N                       : out   std_logic;
    CK0                         : out   std_logic;
    CK0_N                       : out   std_logic;
    CKE0                        : out   std_logic;
    CS0_N                       : out   std_logic;
    DM                          : out   std_logic_vector(1 downto 0);
    EMMC_CLK_SD_CLK             : out   std_logic;
    EMMC_RSTN_SD_WP             : out   std_logic;
    EMMC_UNUSED_SD_VOLT_DIR_0   : out   std_logic;
    EMMC_UNUSED_SD_VOLT_DIR_1_3 : out   std_logic;
    MMUART_0_TXD                : out   std_logic;
    ODT0                        : out   std_logic;
    QSPI_CLK                    : out   std_logic;
    QSPI_SS0                    : out   std_logic;
    RAS_N                       : out   std_logic;
    RESET_N                     : out   std_logic;
    SGMII_TX0_N                 : out   std_logic;
    SGMII_TX0_P                 : out   std_logic;
    SGMII_TX1_N                 : out   std_logic;
    SGMII_TX1_P                 : out   std_logic;
    USB_STP                     : out   std_logic;
    WE_N                        : out   std_logic;
    DQ                          : inout std_logic_vector(15 downto 0);
    DQS                         : inout std_logic_vector(1 downto 0);
    DQS_N                       : inout std_logic_vector(1 downto 0);
    EMMC_CMD_SD_CMD             : inout std_logic;
    EMMC_DATA0_SD_DATA0         : inout std_logic;
    EMMC_DATA1_SD_DATA1         : inout std_logic;
    EMMC_DATA2_SD_DATA2         : inout std_logic;
    EMMC_DATA3_SD_DATA3         : inout std_logic;
    EMMC_DATA4_SD_POW           : inout std_logic;
    EMMC_DATA5_SD_VOLT_SEL      : inout std_logic;
    EMMC_DATA6_SD_VOLT_EN       : inout std_logic;
    EMMC_DATA7_SD_VOLT_CMD_DIR  : inout std_logic;
    I2C_1_SCL                   : inout std_logic;
    I2C_1_SDA                   : inout std_logic;
    QSPI_DATA0                  : inout std_logic;
    QSPI_DATA1                  : inout std_logic;
    USB_DATA0                   : inout std_logic;
    USB_DATA1                   : inout std_logic;
    USB_DATA2                   : inout std_logic;
    USB_DATA3                   : inout std_logic;
    USB_DATA4                   : inout std_logic;
    USB_DATA5                   : inout std_logic;
    USB_DATA6                   : inout std_logic;
    USB_DATA7                   : inout std_logic
  );
end entity top_pfsoc_disco_ddr_validator;

architecture rtl of top_pfsoc_disco_ddr_validator is

  -- ── pfsoc_mss component declaration ───────────────────────────────────────
  component pfsoc_mss is
    port (
      ref_clk_50mhz_i  : in  std_logic;
      clk_fabric_o     : out std_logic;
      mss_reset_n_o    : out std_logic;

      fic0_awid_o      : out std_logic_vector(7 downto 0);
      fic0_awaddr_o    : out std_logic_vector(37 downto 0);
      fic0_awlen_o     : out std_logic_vector(7 downto 0);
      fic0_awsize_o    : out std_logic_vector(2 downto 0);
      fic0_awburst_o   : out std_logic_vector(1 downto 0);
      fic0_awlock_o    : out std_logic;
      fic0_awcache_o   : out std_logic_vector(3 downto 0);
      fic0_awprot_o    : out std_logic_vector(2 downto 0);
      fic0_awqos_o     : out std_logic_vector(3 downto 0);
      fic0_awvalid_o   : out std_logic;
      fic0_awready_i   : in  std_logic;
      fic0_wdata_o     : out std_logic_vector(63 downto 0);
      fic0_wstrb_o     : out std_logic_vector(7 downto 0);
      fic0_wlast_o     : out std_logic;
      fic0_wvalid_o    : out std_logic;
      fic0_wready_i    : in  std_logic;
      fic0_bid_i       : in  std_logic_vector(7 downto 0);
      fic0_bresp_i     : in  std_logic_vector(1 downto 0);
      fic0_bvalid_i    : in  std_logic;
      fic0_bready_o    : out std_logic;
      fic0_arid_o      : out std_logic_vector(7 downto 0);
      fic0_araddr_o    : out std_logic_vector(37 downto 0);
      fic0_arlen_o     : out std_logic_vector(7 downto 0);
      fic0_arsize_o    : out std_logic_vector(2 downto 0);
      fic0_arburst_o   : out std_logic_vector(1 downto 0);
      fic0_arlock_o    : out std_logic;
      fic0_arcache_o   : out std_logic_vector(3 downto 0);
      fic0_arprot_o    : out std_logic_vector(2 downto 0);
      fic0_arqos_o     : out std_logic_vector(3 downto 0);
      fic0_arvalid_o   : out std_logic;
      fic0_arready_i   : in  std_logic;
      fic0_rid_i       : in  std_logic_vector(7 downto 0);
      fic0_rdata_i     : in  std_logic_vector(63 downto 0);
      fic0_rresp_i     : in  std_logic_vector(1 downto 0);
      fic0_rlast_i     : in  std_logic;
      fic0_rvalid_i    : in  std_logic;
      fic0_rready_o    : out std_logic;

      fic1_awid_i      : in  std_logic_vector(3 downto 0);
      fic1_awaddr_i    : in  std_logic_vector(37 downto 0);
      fic1_awlen_i     : in  std_logic_vector(7 downto 0);
      fic1_awsize_i    : in  std_logic_vector(2 downto 0);
      fic1_awburst_i   : in  std_logic_vector(1 downto 0);
      fic1_awlock_i    : in  std_logic;
      fic1_awcache_i   : in  std_logic_vector(3 downto 0);
      fic1_awprot_i    : in  std_logic_vector(2 downto 0);
      fic1_awqos_i     : in  std_logic_vector(3 downto 0);
      fic1_awvalid_i   : in  std_logic;
      fic1_awready_o   : out std_logic;
      fic1_wdata_i     : in  std_logic_vector(63 downto 0);
      fic1_wstrb_i     : in  std_logic_vector(7 downto 0);
      fic1_wlast_i     : in  std_logic;
      fic1_wvalid_i    : in  std_logic;
      fic1_wready_o    : out std_logic;
      fic1_bid_o       : out std_logic_vector(3 downto 0);
      fic1_bresp_o     : out std_logic_vector(1 downto 0);
      fic1_bvalid_o    : out std_logic;
      fic1_bready_i    : in  std_logic;
      fic1_arid_i      : in  std_logic_vector(3 downto 0);
      fic1_araddr_i    : in  std_logic_vector(37 downto 0);
      fic1_arlen_i     : in  std_logic_vector(7 downto 0);
      fic1_arsize_i    : in  std_logic_vector(2 downto 0);
      fic1_arburst_i   : in  std_logic_vector(1 downto 0);
      fic1_arlock_i    : in  std_logic;
      fic1_arcache_i   : in  std_logic_vector(3 downto 0);
      fic1_arprot_i    : in  std_logic_vector(2 downto 0);
      fic1_arqos_i     : in  std_logic_vector(3 downto 0);
      fic1_arvalid_i   : in  std_logic;
      fic1_arready_o   : out std_logic;
      fic1_rid_o       : out std_logic_vector(3 downto 0);
      fic1_rdata_o     : out std_logic_vector(63 downto 0);
      fic1_rresp_o     : out std_logic_vector(1 downto 0);
      fic1_rlast_o     : out std_logic;
      fic1_rvalid_o    : out std_logic;
      fic1_rready_i    : in  std_logic;

      mss_f2m_i        : in  std_logic_vector(63 downto 0);

      CAN_1_RXBUS                 : in    std_logic;
      EMMC_STRB_SD_CD             : in    std_logic;
      MMUART_0_RXD                : in    std_logic;
      REFCLK                      : in    std_logic;
      REFCLK_N                    : in    std_logic;
      SGMII_RX0_N                 : in    std_logic;
      SGMII_RX0_P                 : in    std_logic;
      SGMII_RX1_N                 : in    std_logic;
      SGMII_RX1_P                 : in    std_logic;
      USB_CLK                     : in    std_logic;
      USB_DIR                     : in    std_logic;
      USB_NXT                     : in    std_logic;
      A                           : out   std_logic_vector(13 downto 0);
      ACT_N                       : out   std_logic;
      BA                          : out   std_logic_vector(1 downto 0);
      BG0                         : out   std_logic;
      CAN_1_TXBUS                 : out   std_logic;
      CAN_1_TX_EBL_N              : out   std_logic;
      CAS_N                       : out   std_logic;
      CK0                         : out   std_logic;
      CK0_N                       : out   std_logic;
      CKE0                        : out   std_logic;
      CS0_N                       : out   std_logic;
      DM                          : out   std_logic_vector(1 downto 0);
      EMMC_CLK_SD_CLK             : out   std_logic;
      EMMC_RSTN_SD_WP             : out   std_logic;
      EMMC_UNUSED_SD_VOLT_DIR_0   : out   std_logic;
      EMMC_UNUSED_SD_VOLT_DIR_1_3 : out   std_logic;
      MMUART_0_TXD                : out   std_logic;
      ODT0                        : out   std_logic;
      QSPI_CLK                    : out   std_logic;
      QSPI_SS0                    : out   std_logic;
      RAS_N                       : out   std_logic;
      RESET_N                     : out   std_logic;
      SGMII_TX0_N                 : out   std_logic;
      SGMII_TX0_P                 : out   std_logic;
      SGMII_TX1_N                 : out   std_logic;
      SGMII_TX1_P                 : out   std_logic;
      USB_STP                     : out   std_logic;
      WE_N                        : out   std_logic;
      DQ                          : inout std_logic_vector(15 downto 0);
      DQS                         : inout std_logic_vector(1 downto 0);
      DQS_N                       : inout std_logic_vector(1 downto 0);
      EMMC_CMD_SD_CMD             : inout std_logic;
      EMMC_DATA0_SD_DATA0         : inout std_logic;
      EMMC_DATA1_SD_DATA1         : inout std_logic;
      EMMC_DATA2_SD_DATA2         : inout std_logic;
      EMMC_DATA3_SD_DATA3         : inout std_logic;
      EMMC_DATA4_SD_POW           : inout std_logic;
      EMMC_DATA5_SD_VOLT_SEL      : inout std_logic;
      EMMC_DATA6_SD_VOLT_EN       : inout std_logic;
      EMMC_DATA7_SD_VOLT_CMD_DIR  : inout std_logic;
      I2C_1_SCL                   : inout std_logic;
      I2C_1_SDA                   : inout std_logic;
      QSPI_DATA0                  : inout std_logic;
      QSPI_DATA1                  : inout std_logic;
      USB_DATA0                   : inout std_logic;
      USB_DATA1                   : inout std_logic;
      USB_DATA2                   : inout std_logic;
      USB_DATA3                   : inout std_logic;
      USB_DATA4                   : inout std_logic;
      USB_DATA5                   : inout std_logic;
      USB_DATA6                   : inout std_logic;
      USB_DATA7                   : inout std_logic
    );
  end component pfsoc_mss;

  -- ── pfsoc_ddr_validator_core component declaration ───────────────────────
  component pfsoc_ddr_validator_core is
    generic (
      ddr_data_width_g : integer                       := C_FIC_DATA_WIDTH;
      ddr_addr_width_g : integer                       := 32;
      ddr_base_addr_g  : std_logic_vector(31 downto 0) := x"C000_0000";
      buffer_width_g   : integer                       := 24;
      csr_addr_width_g : positive                      := 12
    );
    port (
      clk_i   : in  std_logic;
      rst_n_i : in  std_logic;

      s_axil_awaddr_i  : in  std_logic_vector(csr_addr_width_g-1 downto 0);
      s_axil_awprot_i  : in  std_logic_vector(2 downto 0);
      s_axil_awvalid_i : in  std_logic;
      s_axil_awready_o : out std_logic;
      s_axil_wdata_i   : in  std_logic_vector(31 downto 0);
      s_axil_wstrb_i   : in  std_logic_vector(3 downto 0);
      s_axil_wvalid_i  : in  std_logic;
      s_axil_wready_o  : out std_logic;
      s_axil_bresp_o   : out std_logic_vector(1 downto 0);
      s_axil_bvalid_o  : out std_logic;
      s_axil_bready_i  : in  std_logic;
      s_axil_araddr_i  : in  std_logic_vector(csr_addr_width_g-1 downto 0);
      s_axil_arprot_i  : in  std_logic_vector(2 downto 0);
      s_axil_arvalid_i : in  std_logic;
      s_axil_arready_o : out std_logic;
      s_axil_rdata_o   : out std_logic_vector(31 downto 0);
      s_axil_rresp_o   : out std_logic_vector(1 downto 0);
      s_axil_rvalid_o  : out std_logic;
      s_axil_rready_i  : in  std_logic;

      m_axi_awid_o    : out std_logic_vector(3 downto 0);
      m_axi_awaddr_o  : out std_logic_vector(37 downto 0);
      m_axi_awlen_o   : out std_logic_vector(7 downto 0);
      m_axi_awsize_o  : out std_logic_vector(2 downto 0);
      m_axi_awburst_o : out std_logic_vector(1 downto 0);
      m_axi_awlock_o  : out std_logic_vector(0 downto 0);
      m_axi_awcache_o : out std_logic_vector(3 downto 0);
      m_axi_awprot_o  : out std_logic_vector(2 downto 0);
      m_axi_awqos_o   : out std_logic_vector(3 downto 0);
      m_axi_awvalid_o : out std_logic;
      m_axi_awready_i : in  std_logic;

      m_axi_wdata_o   : out std_logic_vector(ddr_data_width_g-1 downto 0);
      m_axi_wstrb_o   : out std_logic_vector(ddr_data_width_g/8-1 downto 0);
      m_axi_wlast_o   : out std_logic;
      m_axi_wvalid_o  : out std_logic;
      m_axi_wready_i  : in  std_logic;

      m_axi_bid_i     : in  std_logic_vector(3 downto 0);
      m_axi_bresp_i   : in  std_logic_vector(1 downto 0);
      m_axi_bvalid_i  : in  std_logic;
      m_axi_bready_o  : out std_logic;

      m_axi_arid_o    : out std_logic_vector(3 downto 0);
      m_axi_araddr_o  : out std_logic_vector(37 downto 0);
      m_axi_arlen_o   : out std_logic_vector(7 downto 0);
      m_axi_arsize_o  : out std_logic_vector(2 downto 0);
      m_axi_arburst_o : out std_logic_vector(1 downto 0);
      m_axi_arlock_o  : out std_logic_vector(0 downto 0);
      m_axi_arcache_o : out std_logic_vector(3 downto 0);
      m_axi_arprot_o  : out std_logic_vector(2 downto 0);
      m_axi_arqos_o   : out std_logic_vector(3 downto 0);
      m_axi_arvalid_o : out std_logic;
      m_axi_arready_i : in  std_logic;

      m_axi_rid_i     : in  std_logic_vector(3 downto 0);
      m_axi_rdata_i   : in  std_logic_vector(ddr_data_width_g-1 downto 0);
      m_axi_rresp_i   : in  std_logic_vector(1 downto 0);
      m_axi_rlast_i   : in  std_logic;
      m_axi_rvalid_i  : in  std_logic;
      m_axi_rready_o  : out std_logic;

      jtag_tck_i      : in  std_logic;
      jtag_tdi_i      : in  std_logic;
      jtag_tms_i      : in  std_logic;
      jtag_trstb_i    : in  std_logic;
      jtag_tdo_o      : out std_logic;

      led_o           : out std_logic_vector(3 downto 0)
    );
  end component pfsoc_ddr_validator_core;

  -- ── Internal interconnections ──────────────────────────────────────────
  signal clk_fabric  : std_logic;
  signal mss_reset_n : std_logic;

  -- FIC1 AXI4 interconnect between u_validator and u_mss
  signal fic1_awid    : std_logic_vector(3 downto 0);
  signal fic1_awaddr  : std_logic_vector(37 downto 0);
  signal fic1_awlen   : std_logic_vector(7 downto 0);
  signal fic1_awsize  : std_logic_vector(2 downto 0);
  signal fic1_awburst : std_logic_vector(1 downto 0);
  signal fic1_awlock  : std_logic;
  signal fic1_awcache : std_logic_vector(3 downto 0);
  signal fic1_awprot  : std_logic_vector(2 downto 0);
  signal fic1_awqos   : std_logic_vector(3 downto 0);
  signal fic1_awvalid : std_logic;
  signal fic1_awready : std_logic;

  signal fic1_wdata   : std_logic_vector(63 downto 0);
  signal fic1_wstrb   : std_logic_vector(7 downto 0);
  signal fic1_wlast   : std_logic;
  signal fic1_wvalid  : std_logic;
  signal fic1_wready  : std_logic;

  signal fic1_bid     : std_logic_vector(3 downto 0);
  signal fic1_bresp   : std_logic_vector(1 downto 0);
  signal fic1_bvalid  : std_logic;
  signal fic1_bready  : std_logic;

  signal fic1_arid    : std_logic_vector(3 downto 0);
  signal fic1_araddr  : std_logic_vector(37 downto 0);
  signal fic1_arlen   : std_logic_vector(7 downto 0);
  signal fic1_arsize  : std_logic_vector(2 downto 0);
  signal fic1_arburst : std_logic_vector(1 downto 0);
  signal fic1_arlock  : std_logic;
  signal fic1_arcache : std_logic_vector(3 downto 0);
  signal fic1_arprot  : std_logic_vector(2 downto 0);
  signal fic1_arqos   : std_logic_vector(3 downto 0);
  signal fic1_arvalid : std_logic;
  signal fic1_arready : std_logic;

  signal fic1_rid     : std_logic_vector(3 downto 0);
  signal fic1_rdata   : std_logic_vector(63 downto 0);
  signal fic1_rresp   : std_logic_vector(1 downto 0);
  signal fic1_rlast   : std_logic;
  signal fic1_rvalid  : std_logic;
  signal fic1_rready  : std_logic;

  signal core_awlock : std_logic_vector(0 downto 0);
  signal core_arlock : std_logic_vector(0 downto 0);

begin

  fic1_awlock <= core_awlock(0);
  fic1_arlock <= core_arlock(0);

  -- ── PolarFire SoC MSS Subsystem ──────────────────────────────────────────
  u_mss : pfsoc_mss
    port map (
      ref_clk_50mhz_i  => ref_clk_50mhz,
      clk_fabric_o     => clk_fabric,
      mss_reset_n_o    => mss_reset_n,

      -- FIC0 (MSS initiator) is unused by autonomous validator witness
      fic0_awid_o      => open,
      fic0_awaddr_o    => open,
      fic0_awlen_o     => open,
      fic0_awsize_o    => open,
      fic0_awburst_o   => open,
      fic0_awlock_o    => open,
      fic0_awcache_o   => open,
      fic0_awprot_o    => open,
      fic0_awqos_o     => open,
      fic0_awvalid_o   => open,
      fic0_awready_i   => '0',
      fic0_wdata_o     => open,
      fic0_wstrb_o     => open,
      fic0_wlast_o     => open,
      fic0_wvalid_o    => open,
      fic0_wready_i    => '0',
      fic0_bid_i       => (others => '0'),
      fic0_bresp_i     => (others => '0'),
      fic0_bvalid_i    => '0',
      fic0_bready_o    => open,
      fic0_arid_o      => open,
      fic0_araddr_o    => open,
      fic0_arlen_o     => open,
      fic0_arsize_o    => open,
      fic0_arburst_o   => open,
      fic0_arlock_o    => open,
      fic0_arcache_o   => open,
      fic0_arprot_o    => open,
      fic0_arqos_o     => open,
      fic0_arvalid_o   => open,
      fic0_arready_i   => '0',
      fic0_rid_i       => (others => '0'),
      fic0_rdata_i     => (others => '0'),
      fic0_rresp_i     => (others => '0'),
      fic0_rlast_i     => '0',
      fic0_rvalid_i    => '0',
      fic0_rready_o    => open,

      -- FIC1 (MSS DDR4 target port)
      fic1_awid_i      => fic1_awid,
      fic1_awaddr_i    => fic1_awaddr,
      fic1_awlen_i     => fic1_awlen,
      fic1_awsize_i    => fic1_awsize,
      fic1_awburst_i   => fic1_awburst,
      fic1_awlock_i    => fic1_awlock,
      fic1_awcache_i   => fic1_awcache,
      fic1_awprot_i    => fic1_awprot,
      fic1_awqos_i     => fic1_awqos,
      fic1_awvalid_i   => fic1_awvalid,
      fic1_awready_o   => fic1_awready,
      fic1_wdata_i     => fic1_wdata,
      fic1_wstrb_i     => fic1_wstrb,
      fic1_wlast_i     => fic1_wlast,
      fic1_wvalid_i    => fic1_wvalid,
      fic1_wready_o    => fic1_wready,
      fic1_bid_o       => fic1_bid,
      fic1_bresp_o     => fic1_bresp,
      fic1_bvalid_o    => fic1_bvalid,
      fic1_bready_i    => fic1_bready,
      fic1_arid_i      => fic1_arid,
      fic1_araddr_i    => fic1_araddr,
      fic1_arlen_i     => fic1_arlen,
      fic1_arsize_i    => fic1_arsize,
      fic1_arburst_i   => fic1_arburst,
      fic1_arlock_i    => fic1_arlock,
      fic1_arcache_i   => fic1_arcache,
      fic1_arprot_i    => fic1_arprot,
      fic1_arqos_i     => fic1_arqos,
      fic1_arvalid_i   => fic1_arvalid,
      fic1_arready_o   => fic1_arready,
      fic1_rid_o       => fic1_rid,
      fic1_rdata_o     => fic1_rdata,
      fic1_rresp_o     => fic1_rresp,
      fic1_rlast_o     => fic1_rlast,
      fic1_rvalid_o    => fic1_rvalid,
      fic1_rready_i    => fic1_rready,

      mss_f2m_i        => (others => '0'),

      -- Dedicated pad passthroughs
      CAN_1_RXBUS                 => CAN_1_RXBUS,
      EMMC_STRB_SD_CD             => EMMC_STRB_SD_CD,
      MMUART_0_RXD                => MMUART_0_RXD,
      REFCLK                      => REFCLK,
      REFCLK_N                    => REFCLK_N,
      SGMII_RX0_N                 => SGMII_RX0_N,
      SGMII_RX0_P                 => SGMII_RX0_P,
      SGMII_RX1_N                 => SGMII_RX1_N,
      SGMII_RX1_P                 => SGMII_RX1_P,
      USB_CLK                     => USB_CLK,
      USB_DIR                     => USB_DIR,
      USB_NXT                     => USB_NXT,
      A                           => A,
      ACT_N                       => ACT_N,
      BA                          => BA,
      BG0                         => BG0,
      CAN_1_TXBUS                 => CAN_1_TXBUS,
      CAN_1_TX_EBL_N              => CAN_1_TX_EBL_N,
      CAS_N                       => CAS_N,
      CK0                         => CK0,
      CK0_N                       => CK0_N,
      CKE0                        => CKE0,
      CS0_N                       => CS0_N,
      DM                          => DM,
      EMMC_CLK_SD_CLK             => EMMC_CLK_SD_CLK,
      EMMC_RSTN_SD_WP             => EMMC_RSTN_SD_WP,
      EMMC_UNUSED_SD_VOLT_DIR_0   => EMMC_UNUSED_SD_VOLT_DIR_0,
      EMMC_UNUSED_SD_VOLT_DIR_1_3 => EMMC_UNUSED_SD_VOLT_DIR_1_3,
      MMUART_0_TXD                => MMUART_0_TXD,
      ODT0                        => ODT0,
      QSPI_CLK                    => QSPI_CLK,
      QSPI_SS0                    => QSPI_SS0,
      RAS_N                       => RAS_N,
      RESET_N                     => RESET_N,
      SGMII_TX0_N                 => SGMII_TX0_N,
      SGMII_TX0_P                 => SGMII_TX0_P,
      SGMII_TX1_N                 => SGMII_TX1_N,
      SGMII_TX1_P                 => SGMII_TX1_P,
      USB_STP                     => USB_STP,
      WE_N                        => WE_N,
      DQ                          => DQ,
      DQS                         => DQS,
      DQS_N                       => DQS_N,
      EMMC_CMD_SD_CMD             => EMMC_CMD_SD_CMD,
      EMMC_DATA0_SD_DATA0         => EMMC_DATA0_SD_DATA0,
      EMMC_DATA1_SD_DATA1         => EMMC_DATA1_SD_DATA1,
      EMMC_DATA2_SD_DATA2         => EMMC_DATA2_SD_DATA2,
      EMMC_DATA3_SD_DATA3         => EMMC_DATA3_SD_DATA3,
      EMMC_DATA4_SD_POW           => EMMC_DATA4_SD_POW,
      EMMC_DATA5_SD_VOLT_SEL      => EMMC_DATA5_SD_VOLT_SEL,
      EMMC_DATA6_SD_VOLT_EN       => EMMC_DATA6_SD_VOLT_EN,
      EMMC_DATA7_SD_VOLT_CMD_DIR  => EMMC_DATA7_SD_VOLT_CMD_DIR,
      I2C_1_SCL                   => I2C_1_SCL,
      I2C_1_SDA                   => I2C_1_SDA,
      QSPI_DATA0                  => QSPI_DATA0,
      QSPI_DATA1                  => QSPI_DATA1,
      USB_DATA0                   => USB_DATA0,
      USB_DATA1                   => USB_DATA1,
      USB_DATA2                   => USB_DATA2,
      USB_DATA3                   => USB_DATA3,
      USB_DATA4                   => USB_DATA4,
      USB_DATA5                   => USB_DATA5,
      USB_DATA6                   => USB_DATA6,
      USB_DATA7                   => USB_DATA7
    );

  -- ── DDR Validator Core ───────────────────────────────────────────────────
  u_validator : pfsoc_ddr_validator_core
    port map (
      clk_i   => clk_fabric,
      rst_n_i => mss_reset_n,

      -- AXI4-Lite CSR slave (autonomous witness: idle ties)
      s_axil_awaddr_i  => (others => '0'),
      s_axil_awprot_i  => (others => '0'),
      s_axil_awvalid_i => '0',
      s_axil_awready_o => open,
      s_axil_wdata_i   => (others => '0'),
      s_axil_wstrb_i   => (others => '0'),
      s_axil_wvalid_i  => '0',
      s_axil_wready_o  => open,
      s_axil_bresp_o   => open,
      s_axil_bvalid_o  => open,
      s_axil_bready_i  => '1',
      s_axil_araddr_i  => (others => '0'),
      s_axil_arprot_i  => (others => '0'),
      s_axil_arvalid_i => '0',
      s_axil_arready_o => open,
      s_axil_rdata_o   => open,
      s_axil_rresp_o   => open,
      s_axil_rvalid_o  => open,
      s_axil_rready_i  => '1',

      -- AXI4 master -> MSS FIC1 target
      m_axi_awid_o    => fic1_awid,
      m_axi_awaddr_o  => fic1_awaddr,
      m_axi_awlen_o   => fic1_awlen,
      m_axi_awsize_o  => fic1_awsize,
      m_axi_awburst_o => fic1_awburst,
      m_axi_awlock_o  => core_awlock,
      m_axi_awcache_o => fic1_awcache,
      m_axi_awprot_o  => fic1_awprot,
      m_axi_awqos_o   => fic1_awqos,
      m_axi_awvalid_o => fic1_awvalid,
      m_axi_awready_i => fic1_awready,

      m_axi_wdata_o   => fic1_wdata,
      m_axi_wstrb_o   => fic1_wstrb,
      m_axi_wlast_o   => fic1_wlast,
      m_axi_wvalid_o  => fic1_wvalid,
      m_axi_wready_i  => fic1_wready,

      m_axi_bid_i     => fic1_bid,
      m_axi_bresp_i   => fic1_bresp,
      m_axi_bvalid_i  => fic1_bvalid,
      m_axi_bready_o  => fic1_bready,

      m_axi_arid_o    => fic1_arid,
      m_axi_araddr_o  => fic1_araddr,
      m_axi_arlen_o   => fic1_arlen,
      m_axi_arsize_o  => fic1_arsize,
      m_axi_arburst_o => fic1_arburst,
      m_axi_arlock_o  => core_arlock,
      m_axi_arcache_o => fic1_arcache,
      m_axi_arprot_o  => fic1_arprot,
      m_axi_arqos_o   => fic1_arqos,
      m_axi_arvalid_o => fic1_arvalid,
      m_axi_arready_i => fic1_arready,

      m_axi_rid_i     => fic1_rid,
      m_axi_rdata_i   => fic1_rdata,
      m_axi_rresp_i   => fic1_rresp,
      m_axi_rlast_i   => fic1_rlast,
      m_axi_rvalid_i  => fic1_rvalid,
      m_axi_rready_o  => fic1_rready,

      -- Dedicated JTAG boundary
      jtag_tck_i      => jtag_tck,
      jtag_tdi_i      => jtag_tdi,
      jtag_tms_i      => jtag_tms,
      jtag_trstb_i    => jtag_trstb,
      jtag_tdo_o      => jtag_tdo,

      led_o           => open
    );

end architecture rtl;

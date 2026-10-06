-- =============================================================================
-- Title      : tb_lwh2f_csr — sim harness: full-AXI4 master ↔ lwh2f glue ↔ CSR
-- Project    : rr-ddr-stream / de25_agilex5_ddr_validator
-- -----------------------------------------------------------------------------
-- Description:
--   Top-level DUT for DDR-T2.4. Exposes the FULL AXI4 slave signal set the
--   rr_cocotb_tb Axi4Master BFM expects (prefix `s_axi`), runs it through the
--   production lwh2f_axilite_bridge, and into ddr_validator_csr — the exact path
--   the silicon lwhps2fpga master takes but that the standalone CSR test bypasses.
--
--   BIST status inputs are tied to constants so register reads are deterministic
--   (IDENT @0x3C = 0x0DD50200 is a CSR constant; STATUS reflects the tied values).
--   Control outputs are left open. Driven via `rr sim run test_lwh2f_csr_bridge`.
-- =============================================================================
library ieee;
use ieee.std_logic_1164.all;
library rr;

entity tb_lwh2f_csr is
  port (
    clk_i : in std_logic;
    rst_i : in std_logic;                                   -- active-high, sync

    -- Full AXI4 slave (Axi4Master BFM, prefix "s_axi"). Burst/cache/qos/last
    -- inputs are present for the BFM but ignored by the single-beat glue.
    s_axi_awid    : in  std_logic_vector(3 downto 0);
    s_axi_awaddr  : in  std_logic_vector(28 downto 0);
    s_axi_awlen   : in  std_logic_vector(7 downto 0);
    s_axi_awsize  : in  std_logic_vector(2 downto 0);
    s_axi_awburst : in  std_logic_vector(1 downto 0);
    s_axi_awcache : in  std_logic_vector(3 downto 0);
    s_axi_awprot  : in  std_logic_vector(2 downto 0);
    s_axi_awqos   : in  std_logic_vector(3 downto 0);
    s_axi_awvalid : in  std_logic;
    s_axi_awready : out std_logic;
    s_axi_wdata   : in  std_logic_vector(31 downto 0);
    s_axi_wstrb   : in  std_logic_vector(3 downto 0);
    s_axi_wlast   : in  std_logic;
    s_axi_wvalid  : in  std_logic;
    s_axi_wready  : out std_logic;
    s_axi_bid     : out std_logic_vector(3 downto 0);
    s_axi_bresp   : out std_logic_vector(1 downto 0);
    s_axi_bvalid  : out std_logic;
    s_axi_bready  : in  std_logic;
    s_axi_arid    : in  std_logic_vector(3 downto 0);
    s_axi_araddr  : in  std_logic_vector(28 downto 0);
    s_axi_arlen   : in  std_logic_vector(7 downto 0);
    s_axi_arsize  : in  std_logic_vector(2 downto 0);
    s_axi_arburst : in  std_logic_vector(1 downto 0);
    s_axi_arcache : in  std_logic_vector(3 downto 0);
    s_axi_arprot  : in  std_logic_vector(2 downto 0);
    s_axi_arqos   : in  std_logic_vector(3 downto 0);
    s_axi_arvalid : in  std_logic;
    s_axi_arready : out std_logic;
    s_axi_rid     : out std_logic_vector(3 downto 0);
    s_axi_rdata   : out std_logic_vector(31 downto 0);
    s_axi_rresp   : out std_logic_vector(1 downto 0);
    s_axi_rlast   : out std_logic;
    s_axi_rvalid  : out std_logic;
    s_axi_rready  : in  std_logic
  );
end entity tb_lwh2f_csr;

architecture tb of tb_lwh2f_csr is
  -- AXI-Lite between the bridge and the CSR
  signal l_awaddr  : std_logic_vector(11 downto 0);
  signal l_awprot  : std_logic_vector(2 downto 0);
  signal l_awvalid : std_logic;
  signal l_awready : std_logic;
  signal l_wdata   : std_logic_vector(31 downto 0);
  signal l_wstrb   : std_logic_vector(3 downto 0);
  signal l_wvalid  : std_logic;
  signal l_wready  : std_logic;
  signal l_bresp   : std_logic_vector(1 downto 0);
  signal l_bvalid  : std_logic;
  signal l_bready  : std_logic;
  signal l_araddr  : std_logic_vector(11 downto 0);
  signal l_arprot  : std_logic_vector(2 downto 0);
  signal l_arvalid : std_logic;
  signal l_arready : std_logic;
  signal l_rdata   : std_logic_vector(31 downto 0);
  signal l_rresp   : std_logic_vector(1 downto 0);
  signal l_rvalid  : std_logic;
  signal l_rready  : std_logic;

  constant ZERO32 : std_logic_vector(31 downto 0) := (others => '0');
begin
  u_bridge : entity rr.lwh2f_axilite_bridge
    generic map (id_width_g => 4, addr_width_g => 29, lite_addr_g => 12)
    port map (
      clk_i     => clk_i,
      rst_i     => rst_i,
      s_awid    => s_axi_awid,
      s_awaddr  => s_axi_awaddr,
      s_awprot  => s_axi_awprot,
      s_awvalid => s_axi_awvalid,
      s_awready => s_axi_awready,
      s_wdata   => s_axi_wdata,
      s_wstrb   => s_axi_wstrb,
      s_wvalid  => s_axi_wvalid,
      s_wready  => s_axi_wready,
      s_bid     => s_axi_bid,
      s_bresp   => s_axi_bresp,
      s_bvalid  => s_axi_bvalid,
      s_bready  => s_axi_bready,
      s_arid    => s_axi_arid,
      s_araddr  => s_axi_araddr,
      s_arprot  => s_axi_arprot,
      s_arvalid => s_axi_arvalid,
      s_arready => s_axi_arready,
      s_rid     => s_axi_rid,
      s_rdata   => s_axi_rdata,
      s_rresp   => s_axi_rresp,
      s_rlast   => s_axi_rlast,
      s_rvalid  => s_axi_rvalid,
      s_rready  => s_axi_rready,
      m_awaddr  => l_awaddr,
      m_awprot  => l_awprot,
      m_awvalid => l_awvalid,
      m_awready => l_awready,
      m_wdata   => l_wdata,
      m_wstrb   => l_wstrb,
      m_wvalid  => l_wvalid,
      m_wready  => l_wready,
      m_bresp   => l_bresp,
      m_bvalid  => l_bvalid,
      m_bready  => l_bready,
      m_araddr  => l_araddr,
      m_arprot  => l_arprot,
      m_arvalid => l_arvalid,
      m_arready => l_arready,
      m_rdata   => l_rdata,
      m_rresp   => l_rresp,
      m_rvalid  => l_rvalid,
      m_rready  => l_rready
    );

  u_csr : entity rr.ddr_validator_csr
    generic map (addr_width_g => 12)
    port map (
      clk_i           => clk_i,
      rst_i           => rst_i,
      s_axi_awaddr_i  => l_awaddr,
      s_axi_awprot_i  => l_awprot,
      s_axi_awvalid_i => l_awvalid,
      s_axi_awready_o => l_awready,
      s_axi_wdata_i   => l_wdata,
      s_axi_wstrb_i   => l_wstrb,
      s_axi_wvalid_i  => l_wvalid,
      s_axi_wready_o  => l_wready,
      s_axi_bresp_o   => l_bresp,
      s_axi_bvalid_o  => l_bvalid,
      s_axi_bready_i  => l_bready,
      s_axi_araddr_i  => l_araddr,
      s_axi_arprot_i  => l_arprot,
      s_axi_arvalid_i => l_arvalid,
      s_axi_arready_o => l_arready,
      s_axi_rdata_o   => l_rdata,
      s_axi_rresp_o   => l_rresp,
      s_axi_rvalid_o  => l_rvalid,
      s_axi_rready_i  => l_rready,
      -- control (open)
      start_o         => open,
      soft_reset_o    => open,
      continuous_o    => open,
      scenario_o      => open,
      n_words_o       => open,
      wr_base_o       => open,
      rd_base_o       => open,
      wr_base_load_o  => open,
      rd_base_load_o  => open,
      wr_ptr_reset_o  => open,
      rd_ptr_reset_o  => open,
      -- status (tied constants → deterministic STATUS reads)
      running_i         => '0',
      done_i            => '0',
      pass_i            => '0',
      fail_i            => '0',
      write_idle_i      => '1',
      read_idle_i       => '1',
      error_count_i     => ZERO32,
      words_written_i   => ZERO32,
      words_read_i      => ZERO32,
      first_err_index_i => ZERO32,
      first_err_exp_i   => ZERO32,
      first_err_got_i   => ZERO32
    );
end architecture tb;

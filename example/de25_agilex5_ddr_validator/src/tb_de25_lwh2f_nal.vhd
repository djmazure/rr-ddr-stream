-- =============================================================================
-- Title      : tb_de25_lwh2f_nal — lwh2f → demux → {CSR, NAL ROM} integration tb
-- Project    : rr-ddr-stream / de25_agilex5_ddr_validator
-- -----------------------------------------------------------------------------
-- Description:
--   The lwh2f read/write SUBSYSTEM the DE25 top will use once the synthetic NAL
--   generator is mapped in: full-AXI4 master (Axi4Master BFM, prefix `s_axi`) →
--   lwh2f_axilite_bridge (12-bit window — the REAL 4 KiB Agilex 5 lwh2f
--   aperture, DDR-T2.7) → de25_axil_demux2 (sel bit 11) →
--   ddr_validator_csr (m0, 0x000..0x7FF) + de25_nal_rom (m1, 0x800..0xFFF).
--   Proves the validated CSR path SURVIVES the demux (IDENT 0x0DD50200 still
--   reads, RW registers still round-trip) AND the NAL ROM is reachable at
--   0x800+ — de-risking the top integration before any synth/HW run.
--   Driven via `rr sim run test_de25_lwh2f_nal`.
-- =============================================================================
library ieee;
use ieee.std_logic_1164.all;
library rr;

entity tb_de25_lwh2f_nal is
  port (
    clk_i : in std_logic;
    rst_i : in std_logic;                                   -- active-high, sync

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
end entity tb_de25_lwh2f_nal;

architecture tb of tb_de25_lwh2f_nal is
  constant ZERO32 : std_logic_vector(31 downto 0) := (others => '0');

  -- Bridge → demux (12-bit lwh2f window — real 4 KiB aperture)
  signal b_awaddr  : std_logic_vector(11 downto 0);
  signal b_awprot  : std_logic_vector(2 downto 0);
  signal b_awvalid : std_logic;
  signal b_awready : std_logic;
  signal b_wdata   : std_logic_vector(31 downto 0);
  signal b_wstrb   : std_logic_vector(3 downto 0);
  signal b_wvalid  : std_logic;
  signal b_wready  : std_logic;
  signal b_bresp   : std_logic_vector(1 downto 0);
  signal b_bvalid  : std_logic;
  signal b_bready  : std_logic;
  signal b_araddr  : std_logic_vector(11 downto 0);
  signal b_arprot  : std_logic_vector(2 downto 0);
  signal b_arvalid : std_logic;
  signal b_arready : std_logic;
  signal b_rdata   : std_logic_vector(31 downto 0);
  signal b_rresp   : std_logic_vector(1 downto 0);
  signal b_rvalid  : std_logic;
  signal b_rready  : std_logic;

  -- demux → slave m{0,1} (11-bit slave window). Per-slave records.
  type axil11_t is record
    awaddr  : std_logic_vector(10 downto 0);
    awprot  : std_logic_vector(2 downto 0);
    awvalid : std_logic;
    awready : std_logic;
    wdata   : std_logic_vector(31 downto 0);
    wstrb   : std_logic_vector(3 downto 0);
    wvalid  : std_logic;
    wready  : std_logic;
    bresp   : std_logic_vector(1 downto 0);
    bvalid  : std_logic;
    bready  : std_logic;
    araddr  : std_logic_vector(10 downto 0);
    arprot  : std_logic_vector(2 downto 0);
    arvalid : std_logic;
    arready : std_logic;
    rdata   : std_logic_vector(31 downto 0);
    rresp   : std_logic_vector(1 downto 0);
    rvalid  : std_logic;
    rready  : std_logic;
  end record;
  signal m0, m1 : axil11_t;
begin
  u_bridge : entity rr.lwh2f_axilite_bridge
    generic map (id_width_g => 4, addr_width_g => 29, lite_addr_g => 12)
    port map (
      clk_i => clk_i, rst_i => rst_i,
      s_awid => s_axi_awid, s_awaddr => s_axi_awaddr, s_awprot => s_axi_awprot,
      s_awvalid => s_axi_awvalid, s_awready => s_axi_awready,
      s_wdata => s_axi_wdata, s_wstrb => s_axi_wstrb, s_wvalid => s_axi_wvalid,
      s_wready => s_axi_wready,
      s_bid => s_axi_bid, s_bresp => s_axi_bresp, s_bvalid => s_axi_bvalid,
      s_bready => s_axi_bready,
      s_arid => s_axi_arid, s_araddr => s_axi_araddr, s_arprot => s_axi_arprot,
      s_arvalid => s_axi_arvalid, s_arready => s_axi_arready,
      s_rid => s_axi_rid, s_rdata => s_axi_rdata, s_rresp => s_axi_rresp,
      s_rlast => s_axi_rlast, s_rvalid => s_axi_rvalid, s_rready => s_axi_rready,
      m_awaddr => b_awaddr, m_awprot => b_awprot, m_awvalid => b_awvalid,
      m_awready => b_awready,
      m_wdata => b_wdata, m_wstrb => b_wstrb, m_wvalid => b_wvalid,
      m_wready => b_wready,
      m_bresp => b_bresp, m_bvalid => b_bvalid, m_bready => b_bready,
      m_araddr => b_araddr, m_arprot => b_arprot, m_arvalid => b_arvalid,
      m_arready => b_arready,
      m_rdata => b_rdata, m_rresp => b_rresp, m_rvalid => b_rvalid,
      m_rready => b_rready
    );

  u_demux : entity rr.de25_axil_demux2
    generic map (addr_width_g => 12, sel_bit_g => 11)
    port map (
      clk_i => clk_i, rst_i => rst_i,
      s_awaddr_i => b_awaddr, s_awprot_i => b_awprot, s_awvalid_i => b_awvalid,
      s_awready_o => b_awready,
      s_wdata_i => b_wdata, s_wstrb_i => b_wstrb, s_wvalid_i => b_wvalid,
      s_wready_o => b_wready,
      s_bresp_o => b_bresp, s_bvalid_o => b_bvalid, s_bready_i => b_bready,
      s_araddr_i => b_araddr, s_arprot_i => b_arprot, s_arvalid_i => b_arvalid,
      s_arready_o => b_arready,
      s_rdata_o => b_rdata, s_rresp_o => b_rresp, s_rvalid_o => b_rvalid,
      s_rready_i => b_rready,
      m0_awaddr_o => m0.awaddr, m0_awprot_o => m0.awprot, m0_awvalid_o => m0.awvalid,
      m0_awready_i => m0.awready,
      m0_wdata_o => m0.wdata, m0_wstrb_o => m0.wstrb, m0_wvalid_o => m0.wvalid,
      m0_wready_i => m0.wready,
      m0_bresp_i => m0.bresp, m0_bvalid_i => m0.bvalid, m0_bready_o => m0.bready,
      m0_araddr_o => m0.araddr, m0_arprot_o => m0.arprot, m0_arvalid_o => m0.arvalid,
      m0_arready_i => m0.arready,
      m0_rdata_i => m0.rdata, m0_rresp_i => m0.rresp, m0_rvalid_i => m0.rvalid,
      m0_rready_o => m0.rready,
      m1_awaddr_o => m1.awaddr, m1_awprot_o => m1.awprot, m1_awvalid_o => m1.awvalid,
      m1_awready_i => m1.awready,
      m1_wdata_o => m1.wdata, m1_wstrb_o => m1.wstrb, m1_wvalid_o => m1.wvalid,
      m1_wready_i => m1.wready,
      m1_bresp_i => m1.bresp, m1_bvalid_i => m1.bvalid, m1_bready_o => m1.bready,
      m1_araddr_o => m1.araddr, m1_arprot_o => m1.arprot, m1_arvalid_o => m1.arvalid,
      m1_arready_i => m1.arready,
      m1_rdata_i => m1.rdata, m1_rresp_i => m1.rresp, m1_rvalid_i => m1.rvalid,
      m1_rready_o => m1.rready
    );

  -- m0 = the shared validator CSR (status tied → deterministic reads)
  u_csr : entity rr.ddr_validator_csr
    generic map (addr_width_g => 11)
    port map (
      clk_i => clk_i, rst_i => rst_i,
      s_axi_awaddr_i => m0.awaddr, s_axi_awprot_i => m0.awprot,
      s_axi_awvalid_i => m0.awvalid, s_axi_awready_o => m0.awready,
      s_axi_wdata_i => m0.wdata, s_axi_wstrb_i => m0.wstrb,
      s_axi_wvalid_i => m0.wvalid, s_axi_wready_o => m0.wready,
      s_axi_bresp_o => m0.bresp, s_axi_bvalid_o => m0.bvalid, s_axi_bready_i => m0.bready,
      s_axi_araddr_i => m0.araddr, s_axi_arprot_i => m0.arprot,
      s_axi_arvalid_i => m0.arvalid, s_axi_arready_o => m0.arready,
      s_axi_rdata_o => m0.rdata, s_axi_rresp_o => m0.rresp,
      s_axi_rvalid_o => m0.rvalid, s_axi_rready_i => m0.rready,
      start_o => open, soft_reset_o => open, continuous_o => open,
      scenario_o => open, n_words_o => open, wr_base_o => open, rd_base_o => open,
      wr_base_load_o => open, rd_base_load_o => open,
      wr_ptr_reset_o => open, rd_ptr_reset_o => open,
      running_i => '0', done_i => '0', pass_i => '0', fail_i => '0',
      write_idle_i => '1', read_idle_i => '1',
      error_count_i => ZERO32, words_written_i => ZERO32, words_read_i => ZERO32,
      first_err_index_i => ZERO32, first_err_exp_i => ZERO32, first_err_got_i => ZERO32
    );

  -- m1 = the synthetic fabric NAL generator
  u_nal_rom : entity rr.de25_nal_rom
    generic map (addr_width_g => 11)
    port map (
      clk_i => clk_i, rst_i => rst_i,
      s_axi_awaddr_i => m1.awaddr, s_axi_awprot_i => m1.awprot,
      s_axi_awvalid_i => m1.awvalid, s_axi_awready_o => m1.awready,
      s_axi_wdata_i => m1.wdata, s_axi_wstrb_i => m1.wstrb,
      s_axi_wvalid_i => m1.wvalid, s_axi_wready_o => m1.wready,
      s_axi_bresp_o => m1.bresp, s_axi_bvalid_o => m1.bvalid, s_axi_bready_i => m1.bready,
      s_axi_araddr_i => m1.araddr, s_axi_arprot_i => m1.arprot,
      s_axi_arvalid_i => m1.arvalid, s_axi_arready_o => m1.arready,
      s_axi_rdata_o => m1.rdata, s_axi_rresp_o => m1.rresp,
      s_axi_rvalid_o => m1.rvalid, s_axi_rready_i => m1.rready
    );
end architecture tb;

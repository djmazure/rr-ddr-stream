-- SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
-------------------------------------------------------------------------------
-- Title      : pfsoc_ddr_validator_core — fabric top (PolarFire SoC MSS FIC)
-- Project    : rr-ddr-stream / pfsoc_disco_ddr_validator
-- Standard   : VHDL-2008
-------------------------------------------------------------------------------
-- Description:
--   Fabric top for the hardware validator of ddr_stream on the Microchip
--   PolarFire SoC Discovery Kit (MPFS095T-1FCSG325E). The PolarFire SoC sibling
--   of top_de25_ddr_validator (Agilex 5 HPS) and top_kv260_ddr_validator
--   (ZynqMP): same in-fabric round-trip, same CSR/BIST/IP composition, with the
--   vendor DDR subsystem swapped for the MSS DDR4 controller reached over a
--   Fabric Interface Controller (FIC0/FIC1) AXI4 target port.
--
--   DDR-P2.17 — the DESK/RTL half of the first deepskopion PolarFire line. The
--   backend TOP is `ddr_stream_fic` (the MSS-FIC AXI4 master, DDR-P2.15), NOT
--   `ddr_stream_axi` directly: it clamps bursts to the FIC 64-beat cap
--   (C_PFSOC_FIC_MAX_BURST) and defaults to the 64-bit FIC data lane. On-silicon
--   read/write proof is the sibling hardware ticket DDR-P2.18.
--
--   Composition (mirrors the DDR-P3.30 declarative validator machinery):
--     - ddr_validator_csr : AXI4-Lite slave, the MSS drives it over a fabric
--                           AXI-Lite master (FIC1 / an APB-AXI bridge). CSR map
--                           §4 — same register file as the Zybo/DE25 validators.
--     - ddr_validator_bist: self-checking counter pattern engine (vendor-neutral).
--     - ddr_stream_fic    : the IP under test, BOTH planes enabled, 64-bit
--                           FIC-native DDR side.
--
--   Validation is a FULL IN-FABRIC ROUND TRIP: the BIST pushes a counter
--   pattern through the IP's write plane into MSS DDR4 (m_axi4 -> FIC target),
--   the IP reads it back through its read plane (FIC -> m_axi4 -> M_AXIS), and
--   the BIST compares word-for-word in fabric. The payload never touches the U54
--   L2 cache — the ring lands in the NON-cached DDR4 window (ddr_base_addr_g =
--   0xC000_0000), so a fabric DMA write is coherent to a PS consumer without a
--   cache flush. Linux/HSS only pokes/polls the CSR.
--
--   BOUNDARY: the FIC AXI4 master and the CSR AXI4-Lite slave are FLAT
--   top-level ports (record->flat shim inline, wrapper_ddr_stream_fic pattern).
--   The MSS FIC target and the CSR AXI-Lite master are Libero SmartDesign
--   components wired to these ports at gen_project time — NOT instantiated in
--   this RTL, so this top ELABORATES at the desk (nvc) with no unbound vendor
--   component, exactly the DDR-P2.17 done-when.
--
--   Single fabric clock domain for the first build (clk_i drives both clk_sys
--   and clk_ddr; rst_i active-high, synchronous). Splitting clk_ddr onto a
--   faster FIC clock is a deferred follow-up (its own ticket), same as the
--   DE25/Zybo second-clock split.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library rr;
use rr.ddr_stream_pkg.all;

entity pfsoc_ddr_validator_core is
  generic (
    -- FIC-native 64-bit MSS DDR4 data lane; 64-beat burst cap enforced by
    -- ddr_stream_fic regardless. Non-cached DDR4 ring window base + size.
    ddr_data_width_g : integer                       := C_FIC_DATA_WIDTH;      -- 64
    ddr_addr_width_g : integer                       := 32;
    ddr_base_addr_g  : std_logic_vector(31 downto 0) := x"C000_0000";          -- non-cached DDR4 window
    buffer_width_g   : integer                       := 24;                     -- 16 MiB ring
    csr_addr_width_g : positive                      := 12                      -- 4 KiB CSR window
  );
  port (
    -- Fabric clock / reset (from the MSS fabric clock + a fabric reset).
    clk_i   : in  std_logic;
    rst_n_i : in  std_logic;                          -- active-low from MSS

    -- ── CSR AXI4-Lite SLAVE (MSS fabric AXI-Lite master -> validator CSR) ──
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

    -- ── FIC AXI4 MASTER (validator -> MSS FIC target -> DDR4) ─────────────
    -- Write address channel
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
    -- Write data channel
    m_axi_wdata_o   : out std_logic_vector(ddr_data_width_g-1   downto 0);
    m_axi_wstrb_o   : out std_logic_vector(ddr_data_width_g/8-1 downto 0);
    m_axi_wlast_o   : out std_logic;
    m_axi_wvalid_o  : out std_logic;
    m_axi_wready_i  : in  std_logic;
    -- Write response channel
    m_axi_bid_i     : in  std_logic_vector(3 downto 0);
    m_axi_bresp_i   : in  std_logic_vector(1 downto 0);
    m_axi_bvalid_i  : in  std_logic;
    m_axi_bready_o  : out std_logic;
    -- Read address channel
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
    -- Read data channel
    m_axi_rid_i     : in  std_logic_vector(3 downto 0);
    m_axi_rdata_i   : in  std_logic_vector(ddr_data_width_g-1 downto 0);
    m_axi_rresp_i   : in  std_logic_vector(1 downto 0);
    m_axi_rlast_i   : in  std_logic;
    m_axi_rvalid_i  : in  std_logic;
    m_axi_rready_o  : out std_logic;

    -- Dedicated UJTAG TAP boundary for REA (no user package pins).
    jtag_tck_i      : in  std_logic;
    jtag_tdi_i      : in  std_logic;
    jtag_tms_i      : in  std_logic;
    jtag_trstb_i    : in  std_logic;
    jtag_tdo_o      : out std_logic;

    -- ── Verdict LEDs (LED_RUNNING / LED_DONE / LED_PASS / LED_FAIL) ───────
    led_o           : out std_logic_vector(3 downto 0)
  );
end entity pfsoc_ddr_validator_core;

architecture rtl of pfsoc_ddr_validator_core is

  -- Single fabric clock domain (first build).
  signal clk_sys : std_logic;
  signal clk_ddr : std_logic;
  signal rst_sys : std_logic;
  signal rst_ddr : std_logic;

  -- CSR -> control
  signal csr_start        : std_logic;
  signal csr_soft_reset   : std_logic;
  signal csr_scenario     : std_logic_vector(3 downto 0);
  signal csr_n_words      : std_logic_vector(31 downto 0);
  signal csr_wr_base      : std_logic_vector(31 downto 0);
  signal csr_rd_base      : std_logic_vector(31 downto 0);
  signal csr_wr_base_load : std_logic;
  signal csr_rd_base_load : std_logic;
  signal csr_wr_ptr_reset : std_logic;
  signal csr_rd_ptr_reset : std_logic;

  -- BIST -> status
  signal bist_running       : std_logic;
  signal bist_done          : std_logic;
  signal bist_pass          : std_logic;
  signal bist_fail          : std_logic;
  signal bist_error_count   : std_logic_vector(31 downto 0);
  signal bist_words_written : std_logic_vector(31 downto 0);
  signal bist_words_read    : std_logic_vector(31 downto 0);
  signal bist_first_err_idx : std_logic_vector(31 downto 0);
  signal bist_first_err_exp : std_logic_vector(31 downto 0);
  signal bist_first_err_got : std_logic_vector(31 downto 0);
  signal bist_ip_rst        : std_logic;

  -- IP status
  signal ip_write_idle : std_logic;
  signal ip_read_idle  : std_logic;
  signal ip_rst_sys    : std_logic;
  signal wr_commit_addr : std_logic_vector(31 downto 0);
  signal auto_start, auto_soft_reset : std_logic := '0';
  signal auto_delay : unsigned(15 downto 0) := (others => '0');

  -- User streaming (BIST <-> IP)
  signal s_axis_data  : std_logic_vector(31 downto 0);
  signal s_axis_valid : std_logic;
  signal s_axis_ready : std_logic;
  signal m_axis_data  : std_logic_vector(31 downto 0);
  signal m_axis_valid : std_logic;
  signal m_axis_ready : std_logic;

  -- FIC AXI4 master record boundary (record<->flat shim below).
  signal axi_h2c : axi4_h2c_bus_t;
  signal axi_c2h : axi4_c2h_bus_t;
  signal rea_probe : std_logic_vector(159 downto 0);

begin

  -- Single fabric clock / reset fan-out.
  clk_sys <= clk_i;
  clk_ddr <= clk_i;
  rst_sys <= not rst_n_i;
  rst_ddr <= not rst_n_i;

  -- The BIST resets ONLY the IP (soft-reset / restart) — never the CSR/BIST.
  ip_rst_sys <= rst_sys or bist_ip_rst;

  -- Repeat the witness forever so REA can be armed after programming and still
  -- trigger on a fresh transaction. Each run starts only after a quiet gap.
  p_auto : process (clk_sys)
  begin
    if rising_edge(clk_sys) then
      auto_start <= '0';
      auto_soft_reset <= '0';
      if rst_sys = '1' then
        auto_delay <= (others => '0');
      elsif bist_running = '0' then
        if auto_delay = to_unsigned(50000, auto_delay'length) then
          if bist_done = '1' then
            auto_soft_reset <= '1';
          else
            auto_start <= '1';
          end if;
          auto_delay <= (others => '0');
        else
          auto_delay <= auto_delay + 1;
        end if;
      else
        auto_delay <= (others => '0');
      end if;
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- CSR — AXI4-Lite slave (flat top ports straight onto the CSR slave).
  -----------------------------------------------------------------------------
  u_csr : entity rr.ddr_validator_csr
    generic map (
      addr_width_g => csr_addr_width_g
    )
    port map (
      clk_i           => clk_sys,
      rst_i           => rst_sys,
      s_axi_awaddr_i  => s_axil_awaddr_i,
      s_axi_awprot_i  => s_axil_awprot_i,
      s_axi_awvalid_i => s_axil_awvalid_i,
      s_axi_awready_o => s_axil_awready_o,
      s_axi_wdata_i   => s_axil_wdata_i,
      s_axi_wstrb_i   => s_axil_wstrb_i,
      s_axi_wvalid_i  => s_axil_wvalid_i,
      s_axi_wready_o  => s_axil_wready_o,
      s_axi_bresp_o   => s_axil_bresp_o,
      s_axi_bvalid_o  => s_axil_bvalid_o,
      s_axi_bready_i  => s_axil_bready_i,
      s_axi_araddr_i  => s_axil_araddr_i,
      s_axi_arprot_i  => s_axil_arprot_i,
      s_axi_arvalid_i => s_axil_arvalid_i,
      s_axi_arready_o => s_axil_arready_o,
      s_axi_rdata_o   => s_axil_rdata_o,
      s_axi_rresp_o   => s_axil_rresp_o,
      s_axi_rvalid_o  => s_axil_rvalid_o,
      s_axi_rready_i  => s_axil_rready_i,
      -- control
      start_o         => csr_start,
      soft_reset_o    => csr_soft_reset,
      continuous_o    => open,          -- reserved (single-shot BIST today)
      scenario_o      => csr_scenario,
      n_words_o       => csr_n_words,
      wr_base_o       => csr_wr_base,
      rd_base_o       => csr_rd_base,
      wr_base_load_o  => csr_wr_base_load,
      rd_base_load_o  => csr_rd_base_load,
      wr_ptr_reset_o  => csr_wr_ptr_reset,
      rd_ptr_reset_o  => csr_rd_ptr_reset,
      -- status
      running_i         => bist_running,
      done_i            => bist_done,
      pass_i            => bist_pass,
      fail_i            => bist_fail,
      write_idle_i      => ip_write_idle,
      read_idle_i       => ip_read_idle,
      error_count_i     => bist_error_count,
      words_written_i   => bist_words_written,
      words_read_i      => bist_words_read,
      first_err_index_i => bist_first_err_idx,
      first_err_exp_i   => bist_first_err_exp,
      first_err_got_i   => bist_first_err_got,
      wr_commit_addr_i  => wr_commit_addr
    );

  -----------------------------------------------------------------------------
  -- BIST — self-checking pattern engine.
  -----------------------------------------------------------------------------
  u_bist : entity rr.ddr_validator_bist
    generic map (
      user_data_width_g => 32
    )
    port map (
      clk_i             => clk_sys,
      rst_i             => rst_sys,
      start_i           => csr_start or auto_start,
      soft_reset_i      => csr_soft_reset or auto_soft_reset,
      scenario_i        => csr_scenario,
      n_words_i         => csr_n_words,
      running_o         => bist_running,
      done_o            => bist_done,
      pass_o            => bist_pass,
      fail_o            => bist_fail,
      error_count_o     => bist_error_count,
      words_written_o   => bist_words_written,
      words_read_o      => bist_words_read,
      first_err_index_o => bist_first_err_idx,
      first_err_exp_o   => bist_first_err_exp,
      first_err_got_o   => bist_first_err_got,
      s_axis_data_o     => s_axis_data,
      s_axis_valid_o    => s_axis_valid,
      s_axis_ready_i    => s_axis_ready,
      m_axis_data_i     => m_axis_data,
      m_axis_valid_i    => m_axis_valid,
      m_axis_ready_o    => m_axis_ready,
      write_idle_i      => ip_write_idle,
      read_idle_i       => ip_read_idle,
      wr_commit_addr_i  => wr_commit_addr,
      ip_rst_o          => bist_ip_rst
    );

  -----------------------------------------------------------------------------
  -- IP under test — ddr_stream_fic (MSS-FIC backend, both planes, 64-bit).
  -----------------------------------------------------------------------------
  u_ip : entity rr.ddr_stream_fic
    generic map (
      axi_id_g             => 0,
      fifo_depth_g         => 9,
      user_data_width_g    => 32,
      ddr_data_width_g     => ddr_data_width_g,
      ddr_addr_width_g     => ddr_addr_width_g,
      ddr_base_addr_g      => ddr_base_addr_g,
      buffer_width_g       => buffer_width_g,
      fifo_impl_g          => "olo",
      enable_write_plane_g => true,
      enable_read_plane_g  => true,
      max_burst_len_g      => C_PFSOC_FIC_MAX_BURST
    )
    port map (
      clk_sys_i          => clk_sys,
      clk_ddr_i          => clk_ddr,
      rst_sys_i          => ip_rst_sys,
      rst_ddr_i          => rst_ddr,
      s_axis_data_i      => s_axis_data,
      s_axis_valid_i     => s_axis_valid,
      s_axis_ready_o     => s_axis_ready,
      m_axis_data_o      => m_axis_data,
      m_axis_valid_o     => m_axis_valid,
      m_axis_ready_i     => m_axis_ready,
      csr_wr_base_addr_i => csr_wr_base(ddr_addr_width_g-1 downto 0),
      csr_wr_base_load_i => csr_wr_base_load,
      csr_rd_base_addr_i => csr_rd_base(ddr_addr_width_g-1 downto 0),
      csr_rd_base_load_i => csr_rd_base_load,
      csr_wr_ptr_reset_i => csr_wr_ptr_reset,
      csr_rd_ptr_reset_i => csr_rd_ptr_reset,
      write_idle_o       => ip_write_idle,
      read_idle_o        => ip_read_idle,
      ddr_csr_o          => open,
      version_o          => open,
      wr_commit_addr_o   => wr_commit_addr,
      dbg_reset_status_o => open,
      m_axi4_h2c_bus_o   => axi_h2c,
      m_axi4_c2h_bus_i   => axi_c2h
    );

  -----------------------------------------------------------------------------
  -- FIC AXI4 master record -> flat top ports (wrapper_ddr_stream_fic pattern).
  -----------------------------------------------------------------------------
  m_axi_awid_o    <= "00" & axi_h2c.AWID;
  m_axi_awaddr_o  <= "000000" & axi_h2c.AWADDR;
  m_axi_awlen_o   <= axi_h2c.AWLEN;
  m_axi_awsize_o  <= axi_h2c.AWSIZE;
  m_axi_awburst_o <= axi_h2c.AWBURST;
  m_axi_awlock_o  <= axi_h2c.AWLOCK;
  m_axi_awcache_o <= axi_h2c.AWCACHE;
  m_axi_awprot_o  <= axi_h2c.AWPROT;
  m_axi_awqos_o   <= axi_h2c.AWQOS;
  m_axi_awvalid_o <= axi_h2c.AWVALID;

  m_axi_wdata_o   <= axi_h2c.WDATA(ddr_data_width_g-1 downto 0);
  m_axi_wstrb_o   <= axi_h2c.WSTRB(ddr_data_width_g/8-1 downto 0);
  m_axi_wlast_o   <= axi_h2c.WLAST;
  m_axi_wvalid_o  <= axi_h2c.WVALID;

  m_axi_bready_o  <= axi_h2c.BREADY;

  m_axi_arid_o    <= "00" & axi_h2c.ARID;
  m_axi_araddr_o  <= "000000" & axi_h2c.ARADDR;
  m_axi_arlen_o   <= axi_h2c.ARLEN;
  m_axi_arsize_o  <= axi_h2c.ARSIZE;
  m_axi_arburst_o <= axi_h2c.ARBURST;
  m_axi_arlock_o  <= axi_h2c.ARLOCK;
  m_axi_arcache_o <= axi_h2c.ARCACHE;
  m_axi_arprot_o  <= axi_h2c.ARPROT;
  m_axi_arqos_o   <= axi_h2c.ARQOS;
  m_axi_arvalid_o <= axi_h2c.ARVALID;

  m_axi_rready_o  <= axi_h2c.RREADY;

  -- FIC target responses -> record.
  axi_c2h.AWREADY <= m_axi_awready_i;
  axi_c2h.WREADY  <= m_axi_wready_i;
  axi_c2h.BID     <= m_axi_bid_i(1 downto 0);
  axi_c2h.BRESP   <= m_axi_bresp_i;
  axi_c2h.BVALID  <= m_axi_bvalid_i;
  axi_c2h.ARREADY <= m_axi_arready_i;
  axi_c2h.RID     <= m_axi_rid_i(1 downto 0);
  axi_c2h.RDATA(ddr_data_width_g-1 downto 0)              <= m_axi_rdata_i;
  axi_c2h.RDATA(C_AXI_DATA_WIDTH-1 downto ddr_data_width_g) <= (others => '0');
  axi_c2h.RRESP   <= m_axi_rresp_i;
  axi_c2h.RLAST   <= m_axi_rlast_i;
  axi_c2h.RVALID  <= m_axi_rvalid_i;

  -----------------------------------------------------------------------------
  -- Verdict LEDs.
  -----------------------------------------------------------------------------
  led_o(0) <= bist_running;
  led_o(1) <= bist_done;
  led_o(2) <= bist_pass;
  led_o(3) <= bist_fail;

  -- LSB-first map: returned word, issued word, public durable pointer, counters,
  -- then handshakes/verdict. This is the REA evidence surface for DDR-P2.18.
  rea_probe <= (159 downto 135 => '0') & ip_read_idle & ip_write_idle &
               bist_fail & bist_pass & bist_done & m_axis_ready & m_axis_valid &
               bist_words_written(15 downto 0) & bist_words_read(15 downto 0) &
               wr_commit_addr & s_axis_data & m_axis_data;

  u_rea : entity work.rr_rea_jtag_microchip
    generic map (G_SAMPLE_W => 160, G_DEPTH => 4096,
                 G_TIMESTAMP_W => 0, G_NUM_CHAN => 1, G_NUM_SOURCE => 1,
                 G_CTRL_CHAIN => 1)
    port map (
      sample_clk_i => clk_sys, sample_rst_i => rst_sys, probe_i => rea_probe,
      ext_trigger_i => '0', source_o => open, trigger_o => open,
      jtag_tck_i => jtag_tck_i, jtag_tdi_i => jtag_tdi_i,
      jtag_tms_i => jtag_tms_i, jtag_trstb_i => jtag_trstb_i,
      jtag_tdo_o => jtag_tdo_o
    );

end architecture rtl;

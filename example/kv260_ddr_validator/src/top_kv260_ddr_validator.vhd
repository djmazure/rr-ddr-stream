-------------------------------------------------------------------------------
-- Title      : top_kv260_ddr_validator — PL top-level
-- Project    : rr-ddr-stream / kv260_ddr_validator
-------------------------------------------------------------------------------
-- File       : top_kv260_ddr_validator.vhd
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Standard   : VHDL-2008
-------------------------------------------------------------------------------
-- Description:
--   PL top for the hardware validator of ddr_stream_axi on the AMD Kria KV260
--   (Zynq UltraScale+ MPSoC, xck26). KV260 sibling of
--   ../zybo_z7_20_ddr_validator/src/top_zybo_z7_20_ddr_validator.vhd. Composes:
--     - ddr_validator_bd  : ZynqMP PS subsystem (BD recipe, tcl_hooks/gen_bd.tcl)
--                           — zynq_ultra_ps_e + 2× smartconnect + proc_sys_reset.
--                           Exposes M_AXI_GP0 (AXI-Lite master → CSR, via
--                           M_AXI_HPM0_FPD @ 0xA000_0000), S_AXI_HP0 (AXI4
--                           slave ← IP DMA master, via S_AXI_HP0_FPD), and
--                           fab_clk (pl_clk0, 100 MHz) + active-low fab_aresetn.
--     - ddr_validator_csr : AXI-Lite slave on M_AXI_GP0 (CSR map §4).
--     - ddr_validator_bist: self-checking counter pattern engine (§5).
--     - ddr_stream_axi    : the IP under test, BOTH planes enabled.
--
--   Validation is a FULL IN-FABRIC ROUND TRIP (docs/DESIGN.md §1): the BIST
--   pushes a counter pattern through the IP's write plane into PS DDR4
--   (m_axi4 → S_AXI_HP0_FPD), the IP reads it back through its read plane
--   (S_AXI_HP0_FPD → m_axi4 → M_AXIS), and the BIST compares word-for-word in
--   PL. The PS payload never touches the APU, so no cache-coherency surface
--   exists on the data path — S_AXI_HP0_FPD is a non-coherent HP port that
--   bypasses the A53 caches and goes straight to the DDR controller. The PS
--   only pokes/polls the CSR. (The ring's physical DDR window must be reserved
--   from the kernel — see docs/BRING_UP.md.)
--
--   ── Two differences from the Zynq-7 validator ─────────────────────────────
--   1. PORT-LESS top. On ZynqMP the DDR + MIO are PS-internal (not brought to
--      the PL), and the KV260 SOM has NO user LEDs on PL pins — so unlike the
--      Zybo's led[3:0], the verdict is CSR-only (STATUS reg, read over /dev/mem
--      or JTAG). This entity therefore has no external ports.
--   2. AUTO-START. Because there are no LEDs AND the KV260 Linux bring-up can
--      be flaky, the BIST optionally self-runs once on configuration
--      (auto_start_g): a one-shot start pulse fires after reset release, using
--      auto_n_words_g words of the default (roundtrip) scenario. The verdict
--      then latches in the CSR with NO host interaction — a JTAG `mrd
--      0xA0000004` (STATUS) reads PASS/FAIL straight after programming. A host
--      that drives the CSR (writes N_WORDS / START) overrides the auto run.
--
--   Record→flat AXI shim is inline (deepskopion/z7_20 pattern): the IP's
--   m_axi4_*_bus record fields map directly onto the BD's flat S_AXI_HP0_*
--   ports; the 512-bit record data lanes (RR--P3.6) are sliced to the BD's
--   native 128-bit HP0 width at the boundary.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library rr;
use rr.ddr_stream_pkg.all;

entity top_kv260_ddr_validator is
  generic (
    -- Self-run the BIST once on configuration (no host needed). The verdict
    -- latches in the CSR STATUS register and is JTAG/Linux readable.
    auto_start_g  : boolean := true;
    -- Words pushed by the auto run, when the host has not written N_WORDS.
    auto_n_words_g : natural := 4096
  );
  -- PORT-LESS: ZynqMP DDR/MIO are PS-internal; KV260 has no user-LED PL pins.
  -- (A VHDL entity with no ports is legal and synthesises — all I/O is inside
  -- the PS block design.)
end entity top_kv260_ddr_validator;

architecture rtl of top_kv260_ddr_validator is

  ---------------------------------------------------------------------------
  -- Block-design wrapper. Ports match tcl_hooks/gen_bd.tcl exactly.
  -- proc_sys_reset emits peripheral_aresetn as std_logic_vector(0 to 0).
  ---------------------------------------------------------------------------
  component ddr_validator_bd is
    port (
      -- S_AXI_HP0 (slave in, AXI4, 128-bit data / 32-bit addr / 2-bit id)
      S_AXI_HP0_araddr  : in    std_logic_vector(31 downto 0);
      S_AXI_HP0_arburst : in    std_logic_vector(1 downto 0);
      S_AXI_HP0_arcache : in    std_logic_vector(3 downto 0);
      S_AXI_HP0_arid    : in    std_logic_vector(1 downto 0);
      S_AXI_HP0_arlen   : in    std_logic_vector(7 downto 0);
      S_AXI_HP0_arlock  : in    std_logic_vector(0 downto 0);
      S_AXI_HP0_arprot  : in    std_logic_vector(2 downto 0);
      S_AXI_HP0_arqos   : in    std_logic_vector(3 downto 0);
      S_AXI_HP0_arready : out   std_logic;
      S_AXI_HP0_arsize  : in    std_logic_vector(2 downto 0);
      S_AXI_HP0_arvalid : in    std_logic;
      S_AXI_HP0_awaddr  : in    std_logic_vector(31 downto 0);
      S_AXI_HP0_awburst : in    std_logic_vector(1 downto 0);
      S_AXI_HP0_awcache : in    std_logic_vector(3 downto 0);
      S_AXI_HP0_awid    : in    std_logic_vector(1 downto 0);
      S_AXI_HP0_awlen   : in    std_logic_vector(7 downto 0);
      S_AXI_HP0_awlock  : in    std_logic_vector(0 downto 0);
      S_AXI_HP0_awprot  : in    std_logic_vector(2 downto 0);
      S_AXI_HP0_awqos   : in    std_logic_vector(3 downto 0);
      S_AXI_HP0_awready : out   std_logic;
      S_AXI_HP0_awsize  : in    std_logic_vector(2 downto 0);
      S_AXI_HP0_awvalid : in    std_logic;
      S_AXI_HP0_bid     : out   std_logic_vector(1 downto 0);
      S_AXI_HP0_bready  : in    std_logic;
      S_AXI_HP0_bresp   : out   std_logic_vector(1 downto 0);
      S_AXI_HP0_bvalid  : out   std_logic;
      S_AXI_HP0_rdata   : out   std_logic_vector(127 downto 0);
      S_AXI_HP0_rid     : out   std_logic_vector(1 downto 0);
      S_AXI_HP0_rlast   : out   std_logic;
      S_AXI_HP0_rready  : in    std_logic;
      S_AXI_HP0_rresp   : out   std_logic_vector(1 downto 0);
      S_AXI_HP0_rvalid  : out   std_logic;
      S_AXI_HP0_wdata   : in    std_logic_vector(127 downto 0);
      S_AXI_HP0_wlast   : in    std_logic;
      S_AXI_HP0_wready  : out   std_logic;
      S_AXI_HP0_wstrb   : in    std_logic_vector(15 downto 0);
      S_AXI_HP0_wvalid  : in    std_logic;
      -- M_AXI_GP0 (master out, AXI4-Lite, 32-bit data / 32-bit addr)
      M_AXI_GP0_araddr  : out   std_logic_vector(31 downto 0);
      M_AXI_GP0_arprot  : out   std_logic_vector(2 downto 0);
      M_AXI_GP0_arready : in    std_logic;
      M_AXI_GP0_arvalid : out   std_logic;
      M_AXI_GP0_awaddr  : out   std_logic_vector(31 downto 0);
      M_AXI_GP0_awprot  : out   std_logic_vector(2 downto 0);
      M_AXI_GP0_awready : in    std_logic;
      M_AXI_GP0_awvalid : out   std_logic;
      M_AXI_GP0_bready  : out   std_logic;
      M_AXI_GP0_bresp   : in    std_logic_vector(1 downto 0);
      M_AXI_GP0_bvalid  : in    std_logic;
      M_AXI_GP0_rdata   : in    std_logic_vector(31 downto 0);
      M_AXI_GP0_rready  : out   std_logic;
      M_AXI_GP0_rresp   : in    std_logic_vector(1 downto 0);
      M_AXI_GP0_rvalid  : in    std_logic;
      M_AXI_GP0_wdata   : out   std_logic_vector(31 downto 0);
      M_AXI_GP0_wready  : in    std_logic;
      M_AXI_GP0_wstrb   : out   std_logic_vector(3 downto 0);
      M_AXI_GP0_wvalid  : out   std_logic;
      -- Fabric clock + synchronised active-low reset (single 100 MHz domain).
      fab_clk           : out std_logic;
      fab_aresetn       : out std_logic_vector(0 to 0)
    );
  end component ddr_validator_bd;

  -- ── Clock + reset (single domain) ──────────────────────────────────────
  signal clk        : std_logic;                    -- 100 MHz (BD pl_clk0)
  signal aresetn    : std_logic_vector(0 to 0);
  signal rst        : std_logic;                    -- active-high, clk
  signal ip_rst_sys : std_logic;                    -- rst OR BIST soft reset

  -- ── AXI4 master record to/from the IP (DDR side) ───────────────────────
  signal axi_h2c : axi4_h2c_bus_t;
  signal axi_c2h : axi4_c2h_bus_t;

  -- RR--P3.6 widened the record data lanes to 512; S_AXI_HP0 is 128-bit
  -- (16-bit WSTRB). Slice to the BD width at the boundary; the upper record
  -- bits stay constant zero (the IP drives only [ddr_data_width_g-1:0]).
  signal hp0_rdata : std_logic_vector(127 downto 0);
  signal hp0_wdata : std_logic_vector(127 downto 0);
  signal hp0_wstrb : std_logic_vector(15 downto 0);

  -- ── AXI-Lite slave wiring (flat, from BD M_AXI_GP0) ────────────────────
  signal gp0_awaddr  : std_logic_vector(31 downto 0);
  signal gp0_awprot  : std_logic_vector(2 downto 0);
  signal gp0_awvalid : std_logic;
  signal gp0_awready : std_logic;
  signal gp0_wdata   : std_logic_vector(31 downto 0);
  signal gp0_wstrb   : std_logic_vector(3 downto 0);
  signal gp0_wvalid  : std_logic;
  signal gp0_wready  : std_logic;
  signal gp0_bresp   : std_logic_vector(1 downto 0);
  signal gp0_bvalid  : std_logic;
  signal gp0_bready  : std_logic;
  signal gp0_araddr  : std_logic_vector(31 downto 0);
  signal gp0_arprot  : std_logic_vector(2 downto 0);
  signal gp0_arvalid : std_logic;
  signal gp0_arready : std_logic;
  signal gp0_rdata   : std_logic_vector(31 downto 0);
  signal gp0_rresp   : std_logic_vector(1 downto 0);
  signal gp0_rvalid  : std_logic;
  signal gp0_rready  : std_logic;

  -- ── CSR → BIST/IP control ──────────────────────────────────────────────
  signal csr_start        : std_logic;
  signal csr_soft_reset   : std_logic;
  signal csr_continuous   : std_logic;
  signal csr_scenario     : std_logic_vector(3 downto 0);
  signal csr_n_words      : std_logic_vector(31 downto 0);
  signal csr_wr_base      : std_logic_vector(31 downto 0);
  signal csr_rd_base      : std_logic_vector(31 downto 0);
  signal csr_wr_base_load : std_logic;
  signal csr_rd_base_load : std_logic;
  signal csr_wr_ptr_reset : std_logic;
  signal csr_rd_ptr_reset : std_logic;

  -- ── BIST → CSR status ──────────────────────────────────────────────────
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

  -- ── BIST ↔ IP user-side streaming ──────────────────────────────────────
  signal s_axis_data   : std_logic_vector(31 downto 0);
  signal s_axis_valid  : std_logic;
  signal s_axis_ready  : std_logic;
  signal m_axis_data   : std_logic_vector(31 downto 0);
  signal m_axis_valid  : std_logic;
  signal m_axis_ready  : std_logic;
  signal ip_write_idle : std_logic;
  signal ip_read_idle  : std_logic;

  -- ── Effective start / n_words (host CSR vs auto-start) ─────────────────
  signal eff_start   : std_logic;
  signal eff_n_words : std_logic_vector(31 downto 0);

  -- Auto-start one-shot state.
  signal auto_pulse  : std_logic := '0';
  signal auto_armed  : std_logic := '1';
  signal auto_cnt    : unsigned(7 downto 0) := (others => '0');

begin

  -- ── Clock / reset derivation ───────────────────────────────────────────
  rst        <= not aresetn(0);
  ip_rst_sys <= rst or bist_ip_rst;

  -- ── Block design ───────────────────────────────────────────────────────
  u_bd : component ddr_validator_bd
    port map (
      -- AXI4 slave in ← IP master record (record→flat shim)
      S_AXI_HP0_araddr  => axi_h2c.ARADDR,
      S_AXI_HP0_arburst => axi_h2c.ARBURST,
      S_AXI_HP0_arcache => axi_h2c.ARCACHE,
      S_AXI_HP0_arid    => axi_h2c.ARID,
      S_AXI_HP0_arlen   => axi_h2c.ARLEN,
      S_AXI_HP0_arlock  => axi_h2c.ARLOCK,
      S_AXI_HP0_arprot  => axi_h2c.ARPROT,
      S_AXI_HP0_arqos   => axi_h2c.ARQOS,
      S_AXI_HP0_arready => axi_c2h.ARREADY,
      S_AXI_HP0_arsize  => axi_h2c.ARSIZE,
      S_AXI_HP0_arvalid => axi_h2c.ARVALID,
      S_AXI_HP0_awaddr  => axi_h2c.AWADDR,
      S_AXI_HP0_awburst => axi_h2c.AWBURST,
      S_AXI_HP0_awcache => axi_h2c.AWCACHE,
      S_AXI_HP0_awid    => axi_h2c.AWID,
      S_AXI_HP0_awlen   => axi_h2c.AWLEN,
      S_AXI_HP0_awlock  => axi_h2c.AWLOCK,
      S_AXI_HP0_awprot  => axi_h2c.AWPROT,
      S_AXI_HP0_awqos   => axi_h2c.AWQOS,
      S_AXI_HP0_awready => axi_c2h.AWREADY,
      S_AXI_HP0_awsize  => axi_h2c.AWSIZE,
      S_AXI_HP0_awvalid => axi_h2c.AWVALID,
      S_AXI_HP0_bid     => axi_c2h.BID,
      S_AXI_HP0_bready  => axi_h2c.BREADY,
      S_AXI_HP0_bresp   => axi_c2h.BRESP,
      S_AXI_HP0_bvalid  => axi_c2h.BVALID,
      S_AXI_HP0_rdata   => hp0_rdata,
      S_AXI_HP0_rid     => axi_c2h.RID,
      S_AXI_HP0_rlast   => axi_c2h.RLAST,
      S_AXI_HP0_rready  => axi_h2c.RREADY,
      S_AXI_HP0_rresp   => axi_c2h.RRESP,
      S_AXI_HP0_rvalid  => axi_c2h.RVALID,
      S_AXI_HP0_wdata   => hp0_wdata,
      S_AXI_HP0_wlast   => axi_h2c.WLAST,
      S_AXI_HP0_wready  => axi_c2h.WREADY,
      S_AXI_HP0_wstrb   => hp0_wstrb,
      S_AXI_HP0_wvalid  => axi_h2c.WVALID,
      -- AXI-Lite master out → CSR slave
      M_AXI_GP0_araddr  => gp0_araddr,
      M_AXI_GP0_arprot  => gp0_arprot,
      M_AXI_GP0_arready => gp0_arready,
      M_AXI_GP0_arvalid => gp0_arvalid,
      M_AXI_GP0_awaddr  => gp0_awaddr,
      M_AXI_GP0_awprot  => gp0_awprot,
      M_AXI_GP0_awready => gp0_awready,
      M_AXI_GP0_awvalid => gp0_awvalid,
      M_AXI_GP0_bready  => gp0_bready,
      M_AXI_GP0_bresp   => gp0_bresp,
      M_AXI_GP0_bvalid  => gp0_bvalid,
      M_AXI_GP0_rdata   => gp0_rdata,
      M_AXI_GP0_rready  => gp0_rready,
      M_AXI_GP0_rresp   => gp0_rresp,
      M_AXI_GP0_rvalid  => gp0_rvalid,
      M_AXI_GP0_wdata   => gp0_wdata,
      M_AXI_GP0_wready  => gp0_wready,
      M_AXI_GP0_wstrb   => gp0_wstrb,
      M_AXI_GP0_wvalid  => gp0_wvalid,
      -- Clock + reset
      fab_clk           => clk,
      fab_aresetn       => aresetn
    );

  -- ── HP0 data-lane slicing (512-bit record ↔ 128-bit BD) ────────────────
  axi_c2h.RDATA(127 downto 0)                  <= hp0_rdata;
  axi_c2h.RDATA(C_AXI_DATA_WIDTH-1 downto 128) <= (others => '0');
  hp0_wdata <= axi_h2c.WDATA(127 downto 0);
  hp0_wstrb <= axi_h2c.WSTRB(15 downto 0);

  -- ── Auto-start one-shot (docs/DESIGN.md §6) ────────────────────────────
  -- After reset release, once both IP planes report idle, emit a single-cycle
  -- start pulse. Re-arms only on reset. A host CSR START also starts the BIST;
  -- the two are OR'd. n_words: the host's N_WORDS register wins once written
  -- (non-zero); before any host write it is 0, so the auto run substitutes
  -- auto_n_words_g.
  auto_start_gen : if auto_start_g generate
    process (clk) is
    begin
      if rising_edge(clk) then
        auto_pulse <= '0';
        if rst = '1' then
          auto_armed <= '1';
          auto_cnt   <= (others => '0');
        elsif auto_armed = '1' then
          if ip_write_idle = '1' and ip_read_idle = '1' then
            if auto_cnt = to_unsigned(31, auto_cnt'length) then
              auto_pulse <= '1';
              auto_armed <= '0';
            else
              auto_cnt <= auto_cnt + 1;
            end if;
          end if;
        end if;
      end if;
    end process;
  end generate auto_start_gen;

  eff_start   <= csr_start or auto_pulse;
  eff_n_words <= csr_n_words when (unsigned(csr_n_words) /= 0)
                 else std_logic_vector(to_unsigned(auto_n_words_g, eff_n_words'length));

  -- ── CSR (AXI-Lite slave on M_AXI_GP0 @ 0xA000_0000) ─────────────────────
  -- The smartconnect routes 0xA000_0000..0xA000_FFFF here; the slave decodes
  -- the low 12 address bits (4 KiB). Upper bits are don't-care.
  u_csr : entity rr.ddr_validator_csr
    generic map (
      addr_width_g => 12
    )
    port map (
      clk_i           => clk,
      rst_i           => rst,
      s_axi_awaddr_i  => gp0_awaddr(11 downto 0),
      s_axi_awprot_i  => gp0_awprot,
      s_axi_awvalid_i => gp0_awvalid,
      s_axi_awready_o => gp0_awready,
      s_axi_wdata_i   => gp0_wdata,
      s_axi_wstrb_i   => gp0_wstrb,
      s_axi_wvalid_i  => gp0_wvalid,
      s_axi_wready_o  => gp0_wready,
      s_axi_bresp_o   => gp0_bresp,
      s_axi_bvalid_o  => gp0_bvalid,
      s_axi_bready_i  => gp0_bready,
      s_axi_araddr_i  => gp0_araddr(11 downto 0),
      s_axi_arprot_i  => gp0_arprot,
      s_axi_arvalid_i => gp0_arvalid,
      s_axi_arready_o => gp0_arready,
      s_axi_rdata_o   => gp0_rdata,
      s_axi_rresp_o   => gp0_rresp,
      s_axi_rvalid_o  => gp0_rvalid,
      s_axi_rready_i  => gp0_rready,
      -- control
      start_o         => csr_start,
      soft_reset_o    => csr_soft_reset,
      continuous_o    => csr_continuous,
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
      first_err_got_i   => bist_first_err_got
    );

  -- ── BIST (self-checking pattern engine, clk) ───────────────────────────
  u_bist : entity rr.ddr_validator_bist
    generic map (
      user_data_width_g => 32
    )
    port map (
      clk_i             => clk,
      rst_i             => rst,
      start_i           => eff_start,
      soft_reset_i      => csr_soft_reset,
      scenario_i        => csr_scenario,
      n_words_i         => eff_n_words,
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
      ip_rst_o          => bist_ip_rst
    );

  -- ── IP under test — ddr_stream_axi (both planes, single clock) ─────────
  -- KV260 S_AXI_HP is AXI4 (no Zynq-7 AXI3 16-beat clamp), so max_burst_len_g
  -- keeps the IP's AXI4 default (256). clk_sys == clk_ddr here (single 100 MHz
  -- pl_clk0) — the IP's CDC FIFO is degenerate but functionally valid.
  u_ip : entity rr.ddr_stream_axi
    generic map (
      axi_id_g             => 0,
      fifo_depth_g         => 9,             -- 512-word CDC FIFO
      user_data_width_g    => 32,
      ddr_data_width_g     => 128,           -- matches S_AXI_HP0_FPD 128-bit
      ddr_addr_width_g     => 32,
      ddr_base_addr_g      => x"7000_0000", -- reserved 16 MiB DDR window
      buffer_width_g       => 24,            -- 2^24 = 16 MiB ring
      fifo_impl_g          => "olo",
      enable_write_plane_g => true,
      enable_read_plane_g  => true
    )
    port map (
      clk_sys_i          => clk,
      clk_ddr_i          => clk,
      rst_sys_i          => ip_rst_sys,
      rst_ddr_i          => ip_rst_sys,
      s_axis_data_i      => s_axis_data,
      s_axis_valid_i     => s_axis_valid,
      s_axis_ready_o     => s_axis_ready,
      m_axis_data_o      => m_axis_data,
      m_axis_valid_o     => m_axis_valid,
      m_axis_ready_i     => m_axis_ready,
      csr_wr_base_addr_i => csr_wr_base,
      csr_wr_base_load_i => csr_wr_base_load,
      csr_rd_base_addr_i => csr_rd_base,
      csr_rd_base_load_i => csr_rd_base_load,
      write_idle_o       => ip_write_idle,
      read_idle_o        => ip_read_idle,
      ddr_csr_o          => open,
      m_axi4_h2c_bus_o   => axi_h2c,
      m_axi4_c2h_bus_i   => axi_c2h
    );

  -- ptr-reset CSR strobes (csr_wr_ptr_reset / csr_rd_ptr_reset) and continuous
  -- mode are captured by the CSR but the IP has no ptr-reset ports yet, so they
  -- are intentionally unconsumed today (scenario-3 stub, mirrors the Zynq-7
  -- validator). The verdict is CSR-only — KV260 has no user LEDs on PL pins.

end architecture rtl;

-------------------------------------------------------------------------------
-- Title      : top_de25_ddr_validator — fabric top-level (Agilex 5 HPS)
-- Project    : rr-ddr-stream / de25_agilex5_ddr_validator
-------------------------------------------------------------------------------
-- File       : top_de25_ddr_validator.vhd
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Standard   : VHDL-2008
-------------------------------------------------------------------------------
-- Description:
--   Fabric top for the hardware validator of ddr_stream_axi on the Terasic
--   DE25-Standard (Intel Agilex 5 SoC). This is the HPS analog of the Zynq-7
--   top (top_zybo_z7_20_ddr_validator.vhd) — same in-fabric round-trip, same
--   CSR/BIST/IP composition, with the Zynq PS BD swapped for the rr-generated
--   `hps_subsys` Platform Designer system. Composes:
--     - hps_subsys        : Agilex 5 HPS hard-IP subsystem (rr-generated qsys,
--                           RTL-P3.682 + RTL-P3.722 bridge re-export). Exposes
--                           fabric_clk / fabric_reset / ninit_done, the
--                           f2sdram_adapter AXI4 SLAVE (FPGA→HPS-DDR, 256-bit)
--                           and the lwhps2fpga AXI-Lite MASTER (Linux→fabric
--                           CSRs, 32-bit). The HPS hard pins (DDR4/EMAC/SDMMC)
--                           are inside the hard IP — NOT fabric top ports.
--     - ddr_validator_csr : AXI-Lite slave on lwhps2fpga (CSR map §4).
--     - ddr_validator_bist: self-checking counter pattern engine (§5).
--     - ddr_stream_axi    : the IP under test, BOTH planes enabled, 256-bit
--                           DDR-side to match the f2sdram slave natively.
--
--   Validation is a FULL IN-FABRIC ROUND TRIP (docs/DESIGN.md §1, mirrors the
--   Zybo design): the BIST pushes a counter pattern through the IP's write
--   plane into HPS DDR (m_axi4 → f2sdram), the IP reads it back through its
--   read plane (f2sdram → m_axi4 → M_AXIS), and the BIST compares word-for-word
--   in PL. The payload never touches the A55/A76 CPU, so no data-path cache
--   coherency surface exists — f2sdram lands straight in the HPS DDR
--   controller. Linux only pokes/polls the CSR over lwhps2fpga.
--
--   Two boundary shims are inline (deepskopion_top.vhd record→flat pattern,
--   DS-P2.45):
--     - f2sdram: the IP's m_axi4_*_bus record fields map onto the flat
--       f2sdram_adapter_axi4_sub_* slave ports; the 256-bit IP data lanes map
--       1:1 (ddr_data_width_g=256 here, so NO slicing — unlike the Zybo's
--       512→128 slice), and the IP's 2-bit AXI ID is zero-extended to the
--       slave's 5-bit ID (the HPS ignores upper ID bits, contract §2).
--     - lwh2f: the lwhps2fpga AXI-Lite MASTER drives the CSR AXI-Lite slave;
--       a small ID-ECHO latches awid/arid on their handshakes to drive
--       bid/rid, and the single-beat bursts ignore the master's awlen/awsize/
--       awburst/awlock/awcache outputs (the CSR is AXI4-Lite by construction).
--
--   Single clock domain for the first build (contract §4 permits one fabric
--   user clock to drive fabric_clk / f2sdram_adapter_clk / lwhps2fpga_clk):
--   clk_sys = clk_ddr = CLOCK2_50 (the proven de25_hps_smoke / de25_led_smoke
--   board oscillator, pin BM71). All three bridge clocks + the fabric clock
--   take CLOCK2_50; all three resets take the ninit_done-gated active-high
--   reset. (Splitting clk_ddr onto the HPS f2h_free_clk @125 MHz is a deferred
--   follow-up — needs that clock exported, same gate as the Zybo's fclk_clk1.)
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library rr;
use rr.ddr_stream_pkg.all;

entity top_de25_ddr_validator is
  port (
    -- Board oscillator (CLOCK2_50, LVCMOS12, pin BM71) — the proven
    -- de25_hps_smoke / de25_led_smoke fabric clock. Drives every fabric-side
    -- bridge clock sink (fabric_clk / f2sdram_adapter_clk / lwhps2fpga_clk).
    CLOCK2_50 : in  std_logic;

    -- ── User LEDs (autonomous verdict, §6) ────────────────────────────────
    -- FABRIC-driveable user LEDs (LEDR bank, the de25_hps_smoke / de25_led_smoke
    -- canary pins — NOT HPS_LED Y127, which is in the HPS-dedicated I/O bank and
    -- cannot be driven by fabric logic). Pins bound from boards/de25-standard
    -- by the board pinout subscription (no hand-written QSF).
    -- LD0 running · LD1 done · LD2 pass (latched) · LD3 fail (latched).
    LEDR : out std_logic_vector(3 downto 0);

    -- ── HPS DDR4 (io96b EMIF) physical pins (RTL-P3.722) ──────────────────
    -- TRIVIAL CONDUIT PASS-THROUGH: the EMIF (inside hps_subsys) owns all DDR4
    -- logic; the top merely exposes its mem/ck/reset/oct/refclk conduits as
    -- board pins and passes them straight through. Widths transcribed from the
    -- regenerated hps_subsys wrapper (EMIF_TOPOLOGY=1x32 → 32-bit bus). Bound to
    -- the DE25 board DDR4_* / DDR4_REFCLK / DDR4_RZQ pins by the board pinout
    -- subscription (no hand-written QSF). The HPS cannot boot without these:
    -- they ARE the DRAM bus the SPL/Linux runs from.
    DDR4_CKE     : out   std_logic;
    DDR4_ODT     : out   std_logic;
    DDR4_CS_n    : out   std_logic;
    DDR4_A       : out   std_logic_vector(16 downto 0);
    DDR4_BA      : out   std_logic_vector(1 downto 0);
    DDR4_BG      : out   std_logic_vector(0 downto 0);
    DDR4_ACT_n   : out   std_logic;
    DDR4_PAR     : out   std_logic;
    DDR4_DQ      : inout std_logic_vector(31 downto 0);
    DDR4_DQS     : inout std_logic_vector(3 downto 0);   -- mem_dqs_t
    DDR4_DQS_n   : inout std_logic_vector(3 downto 0);   -- mem_dqs_c
    -- Read-DBI data-bus-inversion strobes (RTL-P3.733). The harvested EMIF .ip
    -- has CTRL_RD_DBI_EN=true (x9 byte lane), so the regenerated wrapper exposes
    -- mem_dbi_n[3:0]; bound to the board DDR4_DBI_n[3:0] pins (pinout already
    -- lists them). One per byte lane (4 lanes for the 1x32 bus).
    DDR4_DBI_n   : inout std_logic_vector(3 downto 0);   -- mem_dbi_n (read-DBI)
    DDR4_ALERT_n  : in    std_logic;
    DDR4_CK       : out   std_logic;                       -- mem_ck_t (board scalar pin)
    DDR4_CK_n     : out   std_logic;                       -- mem_ck_c (board scalar pin)
    DDR4_RESET_n  : out   std_logic;
    DDR4_RZQ      : in    std_logic;                       -- oct_rzqin
    DDR4_REFCLK_p : in    std_logic;                       -- ref_clk (150 MHz, board pin name)

    -- ── HPS dedicated I/O (frozen HPS IO48 pin-mux, RTL-P3.722 Option A) ────
    -- The 48 HPS-dedicated board pins the frozen agilex_hps.ip routes via its
    -- baked-in pin-mux (board names match boards/de25-standard + golden_top.qsf).
    -- HPS_UART_TX (M124) is the SPL/Linux console (ttyS1) — the silent-boot fix.
    -- HPS_SD_* is the SD boot device; HPS_ENET_* the EMAC0 dev loop. Bound to the
    -- HPS-bank pins by the board pinout subscription (no hand-written QSF).
    HPS_CLK_25       : in    std_logic;                    -- hps_io_hps_osc_clk
    HPS_SD_CLK       : out   std_logic;                    -- hps_io_sdmmc_cclk
    HPS_SD_CMD       : inout std_logic;                    -- hps_io_sdmmc_cmd
    HPS_SD_DATA      : inout std_logic_vector(3 downto 0); -- hps_io_sdmmc_data0..3
    HPS_UART_TX      : out   std_logic;                    -- hps_io_uart1_tx (console)
    HPS_UART_RX      : in    std_logic;                    -- hps_io_uart1_rx
    HPS_ENET_MDC     : out   std_logic;                    -- hps_io_mdio0_mdc
    HPS_ENET_MDIO    : inout std_logic;                    -- hps_io_mdio0_mdio
    HPS_ENET_RX_CLK  : in    std_logic;                    -- hps_io_emac0_rx_clk
    HPS_ENET_RX_CTL  : in    std_logic;                    -- hps_io_emac0_rx_ctl
    HPS_ENET_RX_DATA : in    std_logic_vector(3 downto 0); -- hps_io_emac0_rxd0..3
    HPS_ENET_TX_CLK  : out   std_logic;                    -- hps_io_emac0_tx_clk
    HPS_ENET_TX_CTL  : out   std_logic;                    -- hps_io_emac0_tx_ctl
    HPS_ENET_TX_DATA : out   std_logic_vector(3 downto 0); -- hps_io_emac0_txd0..3
    HPS_USB_CLK      : in    std_logic;                    -- hps_io_usb0_clk
    HPS_USB_STP      : out   std_logic;                    -- hps_io_usb0_stp
    HPS_USB_DIR      : in    std_logic;                    -- hps_io_usb0_dir
    HPS_USB_NXT      : in    std_logic;                    -- hps_io_usb0_nxt
    HPS_USB_DATA     : inout std_logic_vector(7 downto 0); -- hps_io_usb0_data0..7
    HPS_I2C_SCL      : inout std_logic;                    -- hps_io_i2c1_scl
    HPS_I2C_SDA      : inout std_logic;                    -- hps_io_i2c1_sda
    HPS_LCM_SPIM_CLK : out   std_logic;                    -- hps_io_spim0_clk
    HPS_LCM_SPIM_MOSI : out  std_logic;                    -- hps_io_spim0_mosi
    HPS_LCM_SPIM_SS  : out   std_logic;                    -- hps_io_spim0_ss0_n
    -- The 8 HPS GPIO-mapped pins (LCM control, sensor, key, LED, …) the frozen
    -- pin-mux routes as GPIO1 — passed straight to their board pins.
    HPS_LCM_D_C      : inout std_logic;                    -- hps_io_gpio28
    HPS_GPIO         : inout std_logic_vector(1 downto 0); -- hps_io_gpio32/33
    HPS_LCM_RST_n    : inout std_logic;                    -- hps_io_gpio34
    HPS_LCM_BK       : inout std_logic;                    -- hps_io_gpio35
    HPS_KEY          : inout std_logic;                    -- hps_io_gpio40
    HPS_LED          : inout std_logic;                    -- hps_io_gpio41
    HPS_GSENSOR_INT  : inout std_logic                     -- hps_io_gpio42
  );
end entity top_de25_ddr_validator;

architecture rtl of top_de25_ddr_validator is

  ---------------------------------------------------------------------------
  -- rr-generated Agilex 5 HPS subsystem with the FPGA↔HPS bridges enabled +
  -- re-exported to the fabric top (export_f2sdram / export_lwh2f in ip.yml).
  -- Ports + widths + directions transcribed EXACTLY from the contract
  -- (docs/HPS_SUBSYS_PORT_CONTRACT.md): directions are relative to hps_subsys,
  -- so the f2sdram AXI4 SLAVE has AW/AR/W as inputs and B/R as outputs, while
  -- the lwhps2fpga AXI-Lite MASTER has AW/AR/W as outputs and B/R as inputs.
  ---------------------------------------------------------------------------
  component hps_subsys is
    port (
      -- Always-present flat exports (contract §1).
      fabric_clk_clk            : in  std_logic := 'X';  -- fabric clock-bridge input
      fabric_reset_reset        : in  std_logic := 'X';  -- fabric reset-bridge input (active-high)
      ninit_done_ninit_done     : out std_logic;          -- Reset Release IP output

      -- ── f2sdram: FPGA→HPS-DDR AXI4 SLAVE, 256-bit data / 32 addr / 5 id (§2) ──
      -- NB: the regenerated hps_subsys.v drives the f2sdram bridge clock/reset
      -- INTERNALLY (from fabric_clk + fabric_reset via mm_interconnect) — there
      -- are NO f2sdram_adapter_clk/rst top ports. The slave is the AXI4 custom
      -- f2sdram_adapter (RTL-P3.682): 8-bit AxLEN, 1-bit AxLOCK, no WID, and it
      -- DOES carry qos/region/user — the adapter ignores the fabric AxUSER and
      -- drives the HPS-side AxUSER=0xE0 itself (the ACE5-Lite attr that routes
      -- the write to DDR). Confirmed against hps_subsys/synth/hps_subsys.v.
      -- Write address channel (slave inputs).
      f2sdram_adapter_axi4_sub_awid     : in  std_logic_vector(4 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awaddr   : in  std_logic_vector(31 downto 0) := (others => 'X');
      f2sdram_adapter_axi4_sub_awlen    : in  std_logic_vector(7 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awsize   : in  std_logic_vector(2 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awburst  : in  std_logic_vector(1 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awlock   : in  std_logic := 'X';
      f2sdram_adapter_axi4_sub_awcache  : in  std_logic_vector(3 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awprot   : in  std_logic_vector(2 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awqos    : in  std_logic_vector(3 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awregion : in  std_logic_vector(3 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awuser   : in  std_logic_vector(7 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awvalid  : in  std_logic := 'X';
      f2sdram_adapter_axi4_sub_awready  : out std_logic;
      -- Write data channel (slave inputs). AXI4: no WID; carries WUSER.
      f2sdram_adapter_axi4_sub_wdata    : in  std_logic_vector(255 downto 0) := (others => 'X');
      f2sdram_adapter_axi4_sub_wstrb    : in  std_logic_vector(31 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_wlast    : in  std_logic := 'X';
      f2sdram_adapter_axi4_sub_wuser    : in  std_logic_vector(7 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_wvalid   : in  std_logic := 'X';
      f2sdram_adapter_axi4_sub_wready   : out std_logic;
      -- Write response channel (slave outputs).
      f2sdram_adapter_axi4_sub_bid      : out std_logic_vector(4 downto 0);
      f2sdram_adapter_axi4_sub_bresp    : out std_logic_vector(1 downto 0);
      f2sdram_adapter_axi4_sub_buser    : out std_logic_vector(7 downto 0);
      f2sdram_adapter_axi4_sub_bvalid   : out std_logic;
      f2sdram_adapter_axi4_sub_bready   : in  std_logic := 'X';
      -- Read address channel (slave inputs).
      f2sdram_adapter_axi4_sub_arid     : in  std_logic_vector(4 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_araddr   : in  std_logic_vector(31 downto 0) := (others => 'X');
      f2sdram_adapter_axi4_sub_arlen    : in  std_logic_vector(7 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arsize   : in  std_logic_vector(2 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arburst  : in  std_logic_vector(1 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arlock   : in  std_logic := 'X';
      f2sdram_adapter_axi4_sub_arcache  : in  std_logic_vector(3 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arprot   : in  std_logic_vector(2 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arqos    : in  std_logic_vector(3 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arregion : in  std_logic_vector(3 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_aruser   : in  std_logic_vector(7 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arvalid  : in  std_logic := 'X';
      f2sdram_adapter_axi4_sub_arready  : out std_logic;
      -- Read data channel (slave outputs).
      f2sdram_adapter_axi4_sub_rid      : out std_logic_vector(4 downto 0);
      f2sdram_adapter_axi4_sub_rdata    : out std_logic_vector(255 downto 0);
      f2sdram_adapter_axi4_sub_rresp    : out std_logic_vector(1 downto 0);
      f2sdram_adapter_axi4_sub_rlast    : out std_logic;
      f2sdram_adapter_axi4_sub_ruser    : out std_logic_vector(7 downto 0);
      f2sdram_adapter_axi4_sub_rvalid   : out std_logic;
      f2sdram_adapter_axi4_sub_rready   : in  std_logic := 'X';

      -- ── lwh2f: HPS→fabric AXI-Lite MASTER, 32-bit data / 29 addr / 4 id (§3) ──
      -- The lwhps2fpga bridge clock/reset are ALSO driven internally (no
      -- lwhps2fpga_clk/rst top ports in the regenerated wrapper).
      -- Write address channel (master outputs).
      lwhps2fpga_awid           : out std_logic_vector(3 downto 0);
      lwhps2fpga_awaddr         : out std_logic_vector(28 downto 0);
      lwhps2fpga_awlen          : out std_logic_vector(7 downto 0);
      lwhps2fpga_awsize         : out std_logic_vector(2 downto 0);
      lwhps2fpga_awburst        : out std_logic_vector(1 downto 0);
      lwhps2fpga_awlock         : out std_logic_vector(0 downto 0);
      lwhps2fpga_awcache        : out std_logic_vector(3 downto 0);
      lwhps2fpga_awprot         : out std_logic_vector(2 downto 0);
      lwhps2fpga_awvalid        : out std_logic;
      lwhps2fpga_awready        : in  std_logic := 'X';
      -- Write data channel (master outputs).
      lwhps2fpga_wdata          : out std_logic_vector(31 downto 0);
      lwhps2fpga_wstrb          : out std_logic_vector(3 downto 0);
      lwhps2fpga_wlast          : out std_logic;
      lwhps2fpga_wvalid         : out std_logic;
      lwhps2fpga_wready         : in  std_logic := 'X';
      -- Write response channel (master inputs).
      lwhps2fpga_bid            : in  std_logic_vector(3 downto 0) := (others => 'X');
      lwhps2fpga_bresp          : in  std_logic_vector(1 downto 0) := (others => 'X');
      lwhps2fpga_bvalid         : in  std_logic := 'X';
      lwhps2fpga_bready         : out std_logic;
      -- Read address channel (master outputs).
      lwhps2fpga_arid           : out std_logic_vector(3 downto 0);
      lwhps2fpga_araddr         : out std_logic_vector(28 downto 0);
      lwhps2fpga_arlen          : out std_logic_vector(7 downto 0);
      lwhps2fpga_arsize         : out std_logic_vector(2 downto 0);
      lwhps2fpga_arburst        : out std_logic_vector(1 downto 0);
      lwhps2fpga_arlock         : out std_logic_vector(0 downto 0);
      lwhps2fpga_arcache        : out std_logic_vector(3 downto 0);
      lwhps2fpga_arprot         : out std_logic_vector(2 downto 0);
      lwhps2fpga_arvalid        : out std_logic;
      lwhps2fpga_arready        : in  std_logic := 'X';
      -- Read data channel (master inputs).
      lwhps2fpga_rid            : in  std_logic_vector(3 downto 0) := (others => 'X');
      lwhps2fpga_rdata          : in  std_logic_vector(31 downto 0) := (others => 'X');
      lwhps2fpga_rresp          : in  std_logic_vector(1 downto 0) := (others => 'X');
      lwhps2fpga_rlast          : in  std_logic := 'X';
      lwhps2fpga_rvalid         : in  std_logic := 'X';
      lwhps2fpga_rready         : out std_logic;

      -- NB: the frozen HPS's enabled-but-unused h2f + f2s coherent MPFE bridges
      -- are TERMINATED entirely INSIDE hps_subsys as a self-contained HPS→HPS
      -- coherent loop (hps2fpga → ACE5-Lite translator → fpga2hps), so there are
      -- NO h2f/f2s top ports here — the validator datapath rides f2sdram +
      -- lwhps2fpga only.

      -- ── HPS DDR4 (io96b EMIF) conduits + refclk (§5, RTL-P3.722) ─────────
      -- Names + widths transcribed from the regenerated hps_subsys wrapper
      -- (EMIF_TOPOLOGY=1x32). These reach the board DDR4_* pins straight through.
      emif_mem_0_mem_cke           : out   std_logic_vector(0 downto 0);
      emif_mem_0_mem_odt           : out   std_logic_vector(0 downto 0);
      emif_mem_0_mem_cs_n          : out   std_logic_vector(0 downto 0);
      emif_mem_0_mem_a             : out   std_logic_vector(16 downto 0);
      emif_mem_0_mem_ba            : out   std_logic_vector(1 downto 0);
      emif_mem_0_mem_bg            : out   std_logic_vector(0 downto 0);
      emif_mem_0_mem_act_n         : out   std_logic;
      emif_mem_0_mem_par           : out   std_logic;
      emif_mem_0_mem_dq            : inout std_logic_vector(31 downto 0) := (others => 'X');
      emif_mem_0_mem_dqs_t         : inout std_logic_vector(3 downto 0)  := (others => 'X');
      emif_mem_0_mem_dqs_c         : inout std_logic_vector(3 downto 0)  := (others => 'X');
      emif_mem_0_mem_alert_n       : in    std_logic := 'X';
      emif_mem_0_mem_dbi_n         : inout std_logic_vector(3 downto 0)  := (others => 'X');
      emif_mem_ck_0_mem_ck_t       : out   std_logic_vector(0 downto 0);
      emif_mem_ck_0_mem_ck_c       : out   std_logic_vector(0 downto 0);
      emif_mem_reset_n_mem_reset_n : out   std_logic;
      emif_oct_0_oct_rzqin         : in    std_logic := 'X';
      emif_ref_clk_clk             : in    std_logic := 'X';

      -- ── HPS IO48 physical-pin conduit (RTL-P3.722 Option A, frozen HPS) ──
      -- The 48 dedicated HPS I/O signals the FROZEN agilex_hps.ip routes via its
      -- baked-in pin-mux. Wired straight through to the board HPS-dedicated pins.
      -- hps_io_uart1_tx reaching pin M124 is what makes the SPL console alive.
      -- Directions transcribed from hps_subsys/synth/hps_subsys.v.
      hps_io_hps_osc_clk : in    std_logic := 'X';
      hps_io_sdmmc_data0 : inout std_logic := 'X';
      hps_io_sdmmc_data1 : inout std_logic := 'X';
      hps_io_sdmmc_cclk  : out   std_logic;
      hps_io_sdmmc_data2 : inout std_logic := 'X';
      hps_io_sdmmc_data3 : inout std_logic := 'X';
      hps_io_sdmmc_cmd   : inout std_logic := 'X';
      hps_io_usb0_clk    : in    std_logic := 'X';
      hps_io_usb0_stp    : out   std_logic;
      hps_io_usb0_dir    : in    std_logic := 'X';
      hps_io_usb0_data0  : inout std_logic := 'X';
      hps_io_usb0_data1  : inout std_logic := 'X';
      hps_io_usb0_nxt    : in    std_logic := 'X';
      hps_io_usb0_data2  : inout std_logic := 'X';
      hps_io_usb0_data3  : inout std_logic := 'X';
      hps_io_usb0_data4  : inout std_logic := 'X';
      hps_io_usb0_data5  : inout std_logic := 'X';
      hps_io_usb0_data6  : inout std_logic := 'X';
      hps_io_usb0_data7  : inout std_logic := 'X';
      hps_io_emac0_tx_clk : out   std_logic;
      hps_io_emac0_tx_ctl : out   std_logic;
      hps_io_emac0_rx_clk : in    std_logic := 'X';
      hps_io_emac0_rx_ctl : in    std_logic := 'X';
      hps_io_emac0_txd0   : out   std_logic;
      hps_io_emac0_txd1   : out   std_logic;
      hps_io_emac0_rxd0   : in    std_logic := 'X';
      hps_io_emac0_rxd1   : in    std_logic := 'X';
      hps_io_emac0_txd2   : out   std_logic;
      hps_io_emac0_txd3   : out   std_logic;
      hps_io_emac0_rxd2   : in    std_logic := 'X';
      hps_io_emac0_rxd3   : in    std_logic := 'X';
      hps_io_mdio0_mdio  : inout std_logic := 'X';
      hps_io_mdio0_mdc   : out   std_logic;
      hps_io_spim0_clk   : out   std_logic;
      hps_io_spim0_mosi  : out   std_logic;
      hps_io_spim0_ss0_n : out   std_logic;
      hps_io_uart1_tx    : out   std_logic;
      hps_io_uart1_rx    : in    std_logic := 'X';
      hps_io_i2c1_sda    : inout std_logic := 'X';
      hps_io_i2c1_scl    : inout std_logic := 'X';
      hps_io_gpio28      : inout std_logic := 'X';
      hps_io_gpio32      : inout std_logic := 'X';
      hps_io_gpio33      : inout std_logic := 'X';
      hps_io_gpio34      : inout std_logic := 'X';
      hps_io_gpio35      : inout std_logic := 'X';
      hps_io_gpio40      : inout std_logic := 'X';
      hps_io_gpio41      : inout std_logic := 'X';
      hps_io_gpio42      : inout std_logic := 'X'
    );
  end component hps_subsys;

  -- ── Clock / reset ──────────────────────────────────────────────────────
  -- Single fabric clock domain: clk_sys = clk_ddr = CLOCK2_50 (contract §4
  -- permits one user clock for all fabric bridge sinks).
  signal clk_sys     : std_logic;
  signal clk_ddr     : std_logic;
  signal ninit_done  : std_logic;                  -- HIGH while still configuring
  signal config_busy : std_logic;                  -- = ninit_done (active-high HPS fabric reset)
  signal rst_sync_r  : std_logic_vector(2 downto 0) := (others => '1');
  signal rst_sys     : std_logic;                  -- active-high, clk_sys
  signal rst_ddr     : std_logic;                  -- active-high, clk_ddr (= rst_sys, one domain)

  -- ── AXI4 master record to/from the IP (DDR side, drives f2sdram) ────────
  signal axi_h2c : axi4_h2c_bus_t;
  signal axi_c2h : axi4_c2h_bus_t;

  -- ── f2sdram flat slave wiring (256-bit data, 5-bit id) ──────────────────
  signal f2s_awid     : std_logic_vector(4 downto 0);
  signal f2s_awready  : std_logic;
  signal f2s_wready   : std_logic;
  signal f2s_bid      : std_logic_vector(4 downto 0);
  signal f2s_bresp    : std_logic_vector(1 downto 0);
  signal f2s_bvalid   : std_logic;
  signal f2s_arid     : std_logic_vector(4 downto 0);
  signal f2s_arready  : std_logic;
  signal f2s_rid      : std_logic_vector(4 downto 0);
  signal f2s_rdata    : std_logic_vector(255 downto 0);
  signal f2s_rresp    : std_logic_vector(1 downto 0);
  signal f2s_rlast    : std_logic;
  signal f2s_rvalid   : std_logic;

  -- ── lwh2f AXI-Lite master wiring (flat, 32-bit data / 29 addr / 4 id) ───
  signal lw_awid    : std_logic_vector(3 downto 0);
  signal lw_awaddr  : std_logic_vector(28 downto 0);
  signal lw_awprot  : std_logic_vector(2 downto 0);
  signal lw_awvalid : std_logic;
  signal lw_awready : std_logic;
  signal lw_wdata   : std_logic_vector(31 downto 0);
  signal lw_wstrb   : std_logic_vector(3 downto 0);
  signal lw_wvalid  : std_logic;
  signal lw_wready  : std_logic;
  signal lw_bid     : std_logic_vector(3 downto 0);
  signal lw_bresp   : std_logic_vector(1 downto 0);
  signal lw_bvalid  : std_logic;
  signal lw_bready  : std_logic;
  signal lw_arid    : std_logic_vector(3 downto 0);
  signal lw_araddr  : std_logic_vector(28 downto 0);
  signal lw_arprot  : std_logic_vector(2 downto 0);
  signal lw_arvalid : std_logic;
  signal lw_arready : std_logic;
  signal lw_rid     : std_logic_vector(3 downto 0);
  signal lw_rdata   : std_logic_vector(31 downto 0);
  signal lw_rresp   : std_logic_vector(1 downto 0);
  signal lw_rvalid  : std_logic;
  signal lw_rready  : std_logic;

  -- ── lwh2f ID-ECHO registers ─────────────────────────────────────────────
  -- The CSR is AXI4-Lite (no IDs); lwh2f carries 4-bit awid/arid and expects
  -- bid/rid echoed back. Latch the request ID on its address handshake and
  -- drive the matching response ID. The IP-side CSR drives bvalid/rvalid one
  -- cycle later, by which point the latched ID is stable.
  signal bid_r : std_logic_vector(3 downto 0) := (others => '0');
  signal rid_r : std_logic_vector(3 downto 0) := (others => '0');

  -- The CSR is single-beat AXI-Lite; lwh2f's RLAST master input is always '1'.
  signal lw_rlast_tie : std_logic;

  -- ── lwh2f 1→2 AXI-Lite demux (Phase 1b) ────────────────────────────────
  -- de25_axil_demux2 splits the lwh2f window (sel bit 11): m0 = validator CSR
  -- (offset 0x000..0x7FF), m1 = de25_nal_rom synthetic NAL generator (offset
  -- 0x800..0xFFF). The sel bit MUST live inside the lwh2f aperture: the HPS
  -- lightweight HPS→FPGA bridge is only 4 KiB / 12-bit on Agilex 5
  -- (system_memory_map.yml span=0x1000, base 0x2000_0000) — bit 12 is NOT
  -- decoded on silicon (DDR-T2.7: an earlier sel_bit_g=12 mapped the ROM at
  -- 0x1000, outside the window, so 0x2000_1000 aliased back to the CSR). Both
  -- slaves comfortably fit their 2 KiB half (CSR uses <0x80, ROM uses <0x120).
  -- The lwh2f master supplies a 29-bit address; the demux subscribes its low 12
  -- bits (11:0) so the select bit reaches the demux and each slave decodes its
  -- low 11. Sim-proven by tb_de25_lwh2f_nal / test_de25_lwh2f_nal (the BFM
  -- bridge is now lite_addr_g=12, matching the real aperture).
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
  signal m0, m1 : axil11_t;   -- m0 = CSR, m1 = NAL ROM

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
  signal s_axis_data  : std_logic_vector(31 downto 0);
  signal s_axis_valid : std_logic;
  signal s_axis_ready : std_logic;
  signal m_axis_data  : std_logic_vector(31 downto 0);
  signal m_axis_valid : std_logic;
  signal m_axis_ready : std_logic;
  signal ip_write_idle : std_logic;
  signal ip_read_idle  : std_logic;

  -- IP user-side reset (active-high). Folds in the BIST's soft reset; the DDR
  -- plane uses the global reset only (same as the Zybo top — resetting the
  -- clk_ddr plane + pointer rings on soft_reset is the scenario-2 un-stub TODO,
  -- docs/DESIGN.md §5).
  signal ip_rst_sys : std_logic;

  -- ── REA (RouteRTL Embedded Analyzer) bring-up probe — RTL-P3.739 ─────────
  -- On-chip ILA to crack the on-silicon lwh2f CSR read abort: observe the
  -- HPS↔fabric boundary during a /dev/mem read and split firewall-vs-fabric.
  -- `rea_rst` is a LOCAL power-up reset INDEPENDENT of ninit_done/rst_sys, so
  -- REA survives a stuck fabric reset and can sample ninit_done as DATA.
  signal rea_rst     : std_logic := '1';
  signal rea_rst_cnt : unsigned(3 downto 0)         := (others => '0');
  signal rea_probe   : std_logic_vector(15 downto 0);
  signal rea_trig    : std_logic;
  signal rea_trig_sticky : std_logic := '0';
  -- RTL-P3.739: sticky latch of lw_arvalid for a JTAG-free LED indicator that an
  -- HPS read crossed the lwh2f bridge into the fabric.
  signal lw_arvalid_sticky : std_logic := '0';
  -- keep the REA hierarchy + probe net from being optimised away
  attribute keep : boolean;
  attribute keep of rea_probe : signal is true;
  attribute keep of rea_trig_sticky : signal is true;
  attribute keep of lw_arvalid_sticky : signal is true;

begin

  -- ── Clock / reset derivation ───────────────────────────────────────────
  -- One fabric clock domain: clk_sys and clk_ddr are both CLOCK2_50.
  clk_sys <= CLOCK2_50;
  clk_ddr <= CLOCK2_50;

  -- ninit_done is HIGH while the SDM is still initialising; it drives the HPS
  -- fabric reset bridge (active-high) AND gates the user-logic reset. Mirror
  -- de25_hps_smoke. A 3-stage synchroniser deasserts the active-high user
  -- reset cleanly into the clk_sys domain after config completes.
  config_busy <= ninit_done;

  p_rst_sync : process (clk_sys)
  begin
    if rising_edge(clk_sys) then
      rst_sync_r <= rst_sync_r(1 downto 0) & config_busy;
    end if;
  end process;
  rst_sys    <= rst_sync_r(2);
  ip_rst_sys <= rst_sys or bist_ip_rst;
  -- DDR-P2.9: single domain (clk_ddr = clk_sys, both CLOCK2_50) — rst_ddr must
  -- track ip_rst_sys, not bare rst_sys, or the BIST's scenario-2 reset_recovery
  -- ip_rst_o pulse never actually reaches the IP's DDR-side reset input, and
  -- the CS-gate / reset-busy path (DDR-T2.15) the scenario exists to exercise
  -- would silently never fire on this board.
  rst_ddr    <= ip_rst_sys;              -- single domain

  -- ── HPS subsystem ──────────────────────────────────────────────────────
  -- fabric_clk + both bridge clocks ride CLOCK2_50; all three resets take the
  -- active-high config_busy (the fabric reset bridge held until config done).
  u_hps : component hps_subsys
    port map (
      fabric_clk_clk            => CLOCK2_50,
      fabric_reset_reset        => config_busy,
      ninit_done_ninit_done     => ninit_done,

      -- ── f2sdram AXI4 slave ← IP m_axi4 master (record→flat shim) ────────
      -- Bridge clock/reset are internal to hps_subsys — not driven here.
      -- Write address (slave in ← master out).
      f2sdram_adapter_axi4_sub_awid     => f2s_awid,
      f2sdram_adapter_axi4_sub_awaddr   => axi_h2c.AWADDR,
      f2sdram_adapter_axi4_sub_awlen    => axi_h2c.AWLEN,
      f2sdram_adapter_axi4_sub_awsize   => axi_h2c.AWSIZE,
      f2sdram_adapter_axi4_sub_awburst  => axi_h2c.AWBURST,
      f2sdram_adapter_axi4_sub_awlock   => axi_h2c.AWLOCK(0),
      f2sdram_adapter_axi4_sub_awcache  => axi_h2c.AWCACHE,
      f2sdram_adapter_axi4_sub_awprot   => axi_h2c.AWPROT,
      f2sdram_adapter_axi4_sub_awqos    => axi_h2c.AWQOS,
      f2sdram_adapter_axi4_sub_awregion => "0000",
      f2sdram_adapter_axi4_sub_awuser   => x"00",
      f2sdram_adapter_axi4_sub_awvalid  => axi_h2c.AWVALID,
      f2sdram_adapter_axi4_sub_awready  => f2s_awready,
      -- Write data (slave in ← master out). AXI4: no WID; WUSER tied 0 — the
      -- adapter forces the HPS-side AxUSER=0xE0 itself (RTL-P3.682).
      f2sdram_adapter_axi4_sub_wdata    => axi_h2c.WDATA(255 downto 0),
      f2sdram_adapter_axi4_sub_wstrb    => axi_h2c.WSTRB(31 downto 0),
      f2sdram_adapter_axi4_sub_wlast    => axi_h2c.WLAST,
      f2sdram_adapter_axi4_sub_wuser    => x"00",
      f2sdram_adapter_axi4_sub_wvalid   => axi_h2c.WVALID,
      f2sdram_adapter_axi4_sub_wready   => f2s_wready,
      -- Write response (slave out → master in).
      f2sdram_adapter_axi4_sub_bid      => f2s_bid,
      f2sdram_adapter_axi4_sub_bresp    => f2s_bresp,
      f2sdram_adapter_axi4_sub_buser    => open,
      f2sdram_adapter_axi4_sub_bvalid   => f2s_bvalid,
      f2sdram_adapter_axi4_sub_bready   => axi_h2c.BREADY,
      -- Read address (slave in ← master out).
      f2sdram_adapter_axi4_sub_arid     => f2s_arid,
      f2sdram_adapter_axi4_sub_araddr   => axi_h2c.ARADDR,
      f2sdram_adapter_axi4_sub_arlen    => axi_h2c.ARLEN,
      f2sdram_adapter_axi4_sub_arsize   => axi_h2c.ARSIZE,
      f2sdram_adapter_axi4_sub_arburst  => axi_h2c.ARBURST,
      f2sdram_adapter_axi4_sub_arlock   => axi_h2c.ARLOCK(0),
      f2sdram_adapter_axi4_sub_arcache  => axi_h2c.ARCACHE,
      f2sdram_adapter_axi4_sub_arprot   => axi_h2c.ARPROT,
      f2sdram_adapter_axi4_sub_arqos    => axi_h2c.ARQOS,
      f2sdram_adapter_axi4_sub_arregion => "0000",
      f2sdram_adapter_axi4_sub_aruser   => x"00",
      f2sdram_adapter_axi4_sub_arvalid  => axi_h2c.ARVALID,
      f2sdram_adapter_axi4_sub_arready  => f2s_arready,
      -- Read data (slave out → master in).
      f2sdram_adapter_axi4_sub_rid      => f2s_rid,
      f2sdram_adapter_axi4_sub_rdata    => f2s_rdata,
      f2sdram_adapter_axi4_sub_rresp    => f2s_rresp,
      f2sdram_adapter_axi4_sub_rlast    => f2s_rlast,
      f2sdram_adapter_axi4_sub_ruser    => open,
      f2sdram_adapter_axi4_sub_rvalid   => f2s_rvalid,
      f2sdram_adapter_axi4_sub_rready   => axi_h2c.RREADY,

      -- ── lwh2f AXI-Lite master → CSR slave (flat wiring) ─────────────────
      -- Bridge clock/reset internal to hps_subsys — not driven here.
      lwhps2fpga_awid           => lw_awid,
      lwhps2fpga_awaddr         => lw_awaddr,
      lwhps2fpga_awlen          => open,   -- single-beat CSR — burst outputs ignored
      lwhps2fpga_awsize         => open,
      lwhps2fpga_awburst        => open,
      lwhps2fpga_awlock         => open,
      lwhps2fpga_awcache        => open,
      lwhps2fpga_awprot         => lw_awprot,
      lwhps2fpga_awvalid        => lw_awvalid,
      lwhps2fpga_awready        => lw_awready,
      lwhps2fpga_wdata          => lw_wdata,
      lwhps2fpga_wstrb          => lw_wstrb,
      lwhps2fpga_wlast          => open,   -- single-beat — WLAST ignored
      lwhps2fpga_wvalid         => lw_wvalid,
      lwhps2fpga_wready         => lw_wready,
      lwhps2fpga_bid            => lw_bid,
      lwhps2fpga_bresp          => lw_bresp,
      lwhps2fpga_bvalid         => lw_bvalid,
      lwhps2fpga_bready         => lw_bready,
      lwhps2fpga_arid           => lw_arid,
      lwhps2fpga_araddr         => lw_araddr,
      lwhps2fpga_arlen          => open,   -- single-beat CSR — burst outputs ignored
      lwhps2fpga_arsize         => open,
      lwhps2fpga_arburst        => open,
      lwhps2fpga_arlock         => open,
      lwhps2fpga_arcache        => open,
      lwhps2fpga_arprot         => lw_arprot,
      lwhps2fpga_arvalid        => lw_arvalid,
      lwhps2fpga_arready        => lw_arready,
      lwhps2fpga_rid            => lw_rid,
      lwhps2fpga_rdata          => lw_rdata,
      lwhps2fpga_rresp          => lw_rresp,
      lwhps2fpga_rlast          => lw_rlast_tie,  -- tied below
      lwhps2fpga_rvalid         => lw_rvalid,
      lwhps2fpga_rready         => lw_rready,

      -- NB: h2f + f2s coherent bridges are looped internally in hps_subsys —
      -- no top ports to wire.

      -- ── HPS DDR4 (io96b EMIF) conduits → board pins (pass-through) ───────
      emif_mem_0_mem_cke(0)        => DDR4_CKE,
      emif_mem_0_mem_odt(0)        => DDR4_ODT,
      emif_mem_0_mem_cs_n(0)       => DDR4_CS_n,
      emif_mem_0_mem_a             => DDR4_A,
      emif_mem_0_mem_ba            => DDR4_BA,
      emif_mem_0_mem_bg            => DDR4_BG,
      emif_mem_0_mem_act_n         => DDR4_ACT_n,
      emif_mem_0_mem_par           => DDR4_PAR,
      emif_mem_0_mem_dq            => DDR4_DQ,
      emif_mem_0_mem_dqs_t         => DDR4_DQS,
      emif_mem_0_mem_dqs_c         => DDR4_DQS_n,
      emif_mem_0_mem_alert_n       => DDR4_ALERT_n,
      emif_mem_0_mem_dbi_n         => DDR4_DBI_n,
      emif_mem_ck_0_mem_ck_t(0)    => DDR4_CK,
      emif_mem_ck_0_mem_ck_c(0)    => DDR4_CK_n,
      emif_mem_reset_n_mem_reset_n => DDR4_RESET_n,
      emif_oct_0_oct_rzqin         => DDR4_RZQ,
      emif_ref_clk_clk             => DDR4_REFCLK_p,

      -- ── HPS IO48 conduit → HPS dedicated board pins (frozen pin-mux) ────
      -- 1:1 pass-through, board-name↔conduit-signal mapping harvested from the
      -- golden GHRD golden_top.v (the GPIO1 assignments are board-specific).
      -- hps_io_uart1_tx → HPS_UART_TX (M124) = the SPL/Linux console.
      hps_io_hps_osc_clk  => HPS_CLK_25,
      hps_io_sdmmc_cclk   => HPS_SD_CLK,
      hps_io_sdmmc_cmd    => HPS_SD_CMD,
      hps_io_sdmmc_data0  => HPS_SD_DATA(0),
      hps_io_sdmmc_data1  => HPS_SD_DATA(1),
      hps_io_sdmmc_data2  => HPS_SD_DATA(2),
      hps_io_sdmmc_data3  => HPS_SD_DATA(3),
      hps_io_uart1_tx     => HPS_UART_TX,
      hps_io_uart1_rx     => HPS_UART_RX,
      hps_io_emac0_tx_clk => HPS_ENET_TX_CLK,
      hps_io_emac0_tx_ctl => HPS_ENET_TX_CTL,
      hps_io_emac0_rx_clk => HPS_ENET_RX_CLK,
      hps_io_emac0_rx_ctl => HPS_ENET_RX_CTL,
      hps_io_emac0_txd0   => HPS_ENET_TX_DATA(0),
      hps_io_emac0_txd1   => HPS_ENET_TX_DATA(1),
      hps_io_emac0_txd2   => HPS_ENET_TX_DATA(2),
      hps_io_emac0_txd3   => HPS_ENET_TX_DATA(3),
      hps_io_emac0_rxd0   => HPS_ENET_RX_DATA(0),
      hps_io_emac0_rxd1   => HPS_ENET_RX_DATA(1),
      hps_io_emac0_rxd2   => HPS_ENET_RX_DATA(2),
      hps_io_emac0_rxd3   => HPS_ENET_RX_DATA(3),
      hps_io_mdio0_mdio   => HPS_ENET_MDIO,
      hps_io_mdio0_mdc    => HPS_ENET_MDC,
      hps_io_usb0_clk     => HPS_USB_CLK,
      hps_io_usb0_stp     => HPS_USB_STP,
      hps_io_usb0_dir     => HPS_USB_DIR,
      hps_io_usb0_nxt     => HPS_USB_NXT,
      hps_io_usb0_data0   => HPS_USB_DATA(0),
      hps_io_usb0_data1   => HPS_USB_DATA(1),
      hps_io_usb0_data2   => HPS_USB_DATA(2),
      hps_io_usb0_data3   => HPS_USB_DATA(3),
      hps_io_usb0_data4   => HPS_USB_DATA(4),
      hps_io_usb0_data5   => HPS_USB_DATA(5),
      hps_io_usb0_data6   => HPS_USB_DATA(6),
      hps_io_usb0_data7   => HPS_USB_DATA(7),
      hps_io_i2c1_sda     => HPS_I2C_SDA,
      hps_io_i2c1_scl     => HPS_I2C_SCL,
      hps_io_spim0_clk    => HPS_LCM_SPIM_CLK,
      hps_io_spim0_mosi   => HPS_LCM_SPIM_MOSI,
      hps_io_spim0_ss0_n  => HPS_LCM_SPIM_SS,
      -- GPIO1 board mapping (golden_top.v): 28→GSENSOR_INT, 32→GPIO0, 33→GPIO1,
      -- 34→LCM_RST_n, 35→LCM_D_C, 40→KEY, 41→LED, 42→LCM_BK.
      hps_io_gpio28       => HPS_GSENSOR_INT,
      hps_io_gpio32       => HPS_GPIO(0),
      hps_io_gpio33       => HPS_GPIO(1),
      hps_io_gpio34       => HPS_LCM_RST_n,
      hps_io_gpio35       => HPS_LCM_D_C,
      hps_io_gpio40       => HPS_KEY,
      hps_io_gpio41       => HPS_LED,
      hps_io_gpio42       => HPS_LCM_BK
    );

  -- ── f2sdram record→flat shim (256-bit data, 2-bit IP id ↔ 5-bit slave) ──
  -- The IP is instantiated with ddr_data_width_g=256, so the WDATA/RDATA lanes
  -- map 1:1 onto the 256-bit f2sdram slave — NO upper-bit slicing (unlike the
  -- Zybo's 512→128 HP0 slice). The record's WDATA/RDATA are still 512 wide
  -- (C_AXI_DATA_WIDTH), of which the IP drives only [255:0]; the upper bits
  -- stay constant zero (the IP never asserts them at ddr_data_width_g=256).
  f2s_awid <= std_logic_vector(resize(unsigned(axi_h2c.AWID), 5));  -- 2→5 id widen
  f2s_arid <= std_logic_vector(resize(unsigned(axi_h2c.ARID), 5));  -- 2→5 id widen

  axi_c2h.AWREADY                              <= f2s_awready;
  axi_c2h.WREADY                               <= f2s_wready;
  axi_c2h.BID                                  <= f2s_bid(1 downto 0);  -- 5→2 id narrow
  axi_c2h.BRESP                                <= f2s_bresp;
  axi_c2h.BVALID                               <= f2s_bvalid;
  axi_c2h.ARREADY                              <= f2s_arready;
  axi_c2h.RID                                  <= f2s_rid(1 downto 0);  -- 5→2 id narrow
  axi_c2h.RDATA(255 downto 0)                  <= f2s_rdata;
  axi_c2h.RDATA(C_AXI_DATA_WIDTH-1 downto 256) <= (others => '0');
  axi_c2h.RRESP                                <= f2s_rresp;
  axi_c2h.RLAST                                <= f2s_rlast;
  axi_c2h.RVALID                               <= f2s_rvalid;

  -- ── lwh2f 1→2 demux: lw_* master → {CSR (m0), NAL ROM (m1)} ─────────────
  -- The lwhps2fpga master decodes the HPS-fixed lwh2f aperture (base
  -- 0x2000_0000, 4 KiB / 12-bit on Agilex 5). The demux subscribes its low 12
  -- bits: sel bit 11 picks the slave, the low 11 reach each slave's window.
  -- CSR @ lwh2f offset 0x000..0x7FF, NAL ROM @ 0x800..0xFFF (DDR-T2.7).
  u_lwh2f_demux : entity rr.de25_axil_demux2
    generic map (
      addr_width_g => 12,
      sel_bit_g    => 11
    )
    port map (
      clk_i       => clk_sys,
      rst_i       => rst_sys,
      -- upstream ← lwh2f master (low 12 address bits — the 4 KiB aperture)
      s_awaddr_i  => lw_awaddr(11 downto 0),
      s_awprot_i  => lw_awprot,
      s_awvalid_i => lw_awvalid,
      s_awready_o => lw_awready,
      s_wdata_i   => lw_wdata,
      s_wstrb_i   => lw_wstrb,
      s_wvalid_i  => lw_wvalid,
      s_wready_o  => lw_wready,
      s_bresp_o   => lw_bresp,
      s_bvalid_o  => lw_bvalid,
      s_bready_i  => lw_bready,
      s_araddr_i  => lw_araddr(11 downto 0),
      s_arprot_i  => lw_arprot,
      s_arvalid_i => lw_arvalid,
      s_arready_o => lw_arready,
      s_rdata_o   => lw_rdata,
      s_rresp_o   => lw_rresp,
      s_rvalid_o  => lw_rvalid,
      s_rready_i  => lw_rready,
      -- m0 → validator CSR
      m0_awaddr_o  => m0.awaddr,  m0_awprot_o  => m0.awprot,  m0_awvalid_o => m0.awvalid,
      m0_awready_i => m0.awready,
      m0_wdata_o   => m0.wdata,   m0_wstrb_o   => m0.wstrb,   m0_wvalid_o  => m0.wvalid,
      m0_wready_i  => m0.wready,
      m0_bresp_i   => m0.bresp,   m0_bvalid_i  => m0.bvalid,  m0_bready_o  => m0.bready,
      m0_araddr_o  => m0.araddr,  m0_arprot_o  => m0.arprot,  m0_arvalid_o => m0.arvalid,
      m0_arready_i => m0.arready,
      m0_rdata_i   => m0.rdata,   m0_rresp_i   => m0.rresp,   m0_rvalid_i  => m0.rvalid,
      m0_rready_o  => m0.rready,
      -- m1 → synthetic NAL ROM
      m1_awaddr_o  => m1.awaddr,  m1_awprot_o  => m1.awprot,  m1_awvalid_o => m1.awvalid,
      m1_awready_i => m1.awready,
      m1_wdata_o   => m1.wdata,   m1_wstrb_o   => m1.wstrb,   m1_wvalid_o  => m1.wvalid,
      m1_wready_i  => m1.wready,
      m1_bresp_i   => m1.bresp,   m1_bvalid_i  => m1.bvalid,  m1_bready_o  => m1.bready,
      m1_araddr_o  => m1.araddr,  m1_arprot_o  => m1.arprot,  m1_arvalid_o => m1.arvalid,
      m1_arready_i => m1.arready,
      m1_rdata_i   => m1.rdata,   m1_rresp_i   => m1.rresp,   m1_rvalid_i  => m1.rvalid,
      m1_rready_o  => m1.rready
    );

  -- ── CSR (AXI-Lite slave on lwh2f demux m0) ──────────────────────────────
  -- The CSR's 2 KiB half subscribes the low 11 address bits (demux-stripped).
  u_csr : entity rr.ddr_validator_csr
    generic map (
      addr_width_g => 11
    )
    port map (
      clk_i           => clk_sys,
      rst_i           => rst_sys,
      s_axi_awaddr_i  => m0.awaddr,
      s_axi_awprot_i  => m0.awprot,
      s_axi_awvalid_i => m0.awvalid,
      s_axi_awready_o => m0.awready,
      s_axi_wdata_i   => m0.wdata,
      s_axi_wstrb_i   => m0.wstrb,
      s_axi_wvalid_i  => m0.wvalid,
      s_axi_wready_o  => m0.wready,
      s_axi_bresp_o   => m0.bresp,
      s_axi_bvalid_o  => m0.bvalid,
      s_axi_bready_i  => m0.bready,
      s_axi_araddr_i  => m0.araddr,
      s_axi_arprot_i  => m0.arprot,
      s_axi_arvalid_i => m0.arvalid,
      s_axi_arready_o => m0.arready,
      s_axi_rdata_o   => m0.rdata,
      s_axi_rresp_o   => m0.rresp,
      s_axi_rvalid_o  => m0.rvalid,
      s_axi_rready_i  => m0.rready,
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

  -- ── Synthetic fabric NAL generator (AXI-Lite slave on lwh2f demux m1) ───
  -- Phase 1b first-light source: a read-only ROM serving an Annex-B SPS+PPS+IDR
  -- NAL stream at lwh2f offset 0x800 (IDENT 0x0DD50A10 @ HPS 0x2000_0800, ROM
  -- window @ 0x2000_0900). Linux reads it over lwh2f and streams it to the host
  -- NAL analyzer. Does NOT touch the validated CSR/BIST/f2sdram datapath.
  u_nal_rom : entity rr.de25_nal_rom
    generic map (
      addr_width_g => 11
    )
    port map (
      clk_i           => clk_sys,
      rst_i           => rst_sys,
      s_axi_awaddr_i  => m1.awaddr,
      s_axi_awprot_i  => m1.awprot,
      s_axi_awvalid_i => m1.awvalid,
      s_axi_awready_o => m1.awready,
      s_axi_wdata_i   => m1.wdata,
      s_axi_wstrb_i   => m1.wstrb,
      s_axi_wvalid_i  => m1.wvalid,
      s_axi_wready_o  => m1.wready,
      s_axi_bresp_o   => m1.bresp,
      s_axi_bvalid_o  => m1.bvalid,
      s_axi_bready_i  => m1.bready,
      s_axi_araddr_i  => m1.araddr,
      s_axi_arprot_i  => m1.arprot,
      s_axi_arvalid_i => m1.arvalid,
      s_axi_arready_o => m1.arready,
      s_axi_rdata_o   => m1.rdata,
      s_axi_rresp_o   => m1.rresp,
      s_axi_rvalid_o  => m1.rvalid,
      s_axi_rready_i  => m1.rready
    );

  -- ── lwh2f ID-ECHO + single-beat RLAST tie ──────────────────────────────
  -- Latch awid/arid on their address handshakes (valid & ready) and drive the
  -- matching bid/rid back. The CSR is single-beat AXI-Lite, so RLAST is always
  -- '1'.
  p_id_echo : process (clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        bid_r <= (others => '0');
        rid_r <= (others => '0');
      else
        if lw_awvalid = '1' and lw_awready = '1' then
          bid_r <= lw_awid;
        end if;
        if lw_arvalid = '1' and lw_arready = '1' then
          rid_r <= lw_arid;
        end if;
      end if;
    end if;
  end process;
  lw_bid       <= bid_r;
  lw_rid       <= rid_r;
  lw_rlast_tie <= '1';   -- single-beat CSR → RLAST always asserted

  -- ── BIST (self-checking pattern engine, clk_sys) ───────────────────────
  u_bist : entity rr.ddr_validator_bist
    generic map (
      user_data_width_g => 32
    )
    port map (
      clk_i             => clk_sys,
      rst_i             => rst_sys,
      start_i           => csr_start,
      soft_reset_i      => csr_soft_reset,
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
      ip_rst_o          => bist_ip_rst
    );

  -- ── IP under test — ddr_stream_axi (both planes, 256-bit DDR side) ──────
  -- ddr_data_width_g=256 to match the f2sdram 256-bit slave natively (no
  -- width adapter, vs the Zybo's 128-bit HP0). buffer_width_g=28 → a 256 MiB
  -- ring at ddr_base_addr_g=0x0. The HPS DDR window for f2sdram is 0x0 ..
  -- 0x1_0000_0000 (4 GB at offset 0, contract §2 + ip.yml f2sdram_address_width
  -- 32), so a 256 MiB ring at base 0 sits safely inside it. (The Linux side
  -- must reserve this window — see docs/BRING_UP.md `mem=`/reserved-memory.)
  u_ip : entity rr.ddr_stream_axi
    generic map (
      axi_id_g             => 0,
      fifo_depth_g         => 9,          -- 512-word CDC FIFO
      user_data_width_g    => 32,
      ddr_data_width_g     => 256,        -- matches f2sdram 256-bit slave
      ddr_addr_width_g     => 32,
      ddr_base_addr_g      => x"0000_0000",  -- f2sdram window starts at 0x0
      buffer_width_g       => 28,         -- 256 MiB ring window
      fifo_impl_g          => "olo",
      enable_write_plane_g => true,
      enable_read_plane_g  => true
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

  -- ptr-reset CSR strobes are wired for DDR-P3.11; the IP has no ptr-reset
  -- ports yet, so they are intentionally unconsumed today (scenario 3 stub).
  -- continuous mode (CTRL[8]) is captured by the CSR but the single-shot
  -- BIST does not consume it yet — both surfaced for the host, no-op in PL.

  -- ── LED verdict (autonomous, §6) ───────────────────────────────────────
  -- LD0 running · LD1 done · LD2 pass (latched) · LD3 fail XOR rea-trigger.
  -- LD3 also dances on every REA capture trigger — anchors the REA trigger
  -- path against optimiser pruning + gives an eyeball capture confirmation.
  LEDR(0) <= bist_running;
  LEDR(1) <= bist_done;
  LEDR(2) <= bist_pass;
  -- RTL-P3.739: LEDR(3) = "HPS read crossed the lwh2f bridge into the fabric"
  -- (sticky lw_arvalid). bist_fail is still observable as LEDR(1)&not-LEDR(2).
  LEDR(3) <= lw_arvalid_sticky;

  -- ── REA bring-up probe (RTL-P3.739) ─────────────────────────────────────
  -- sample_rst: local power-up reset, released after 16 CLOCK2_50 edges,
  -- INDEPENDENT of the suspect ninit_done/rst_sys (so REA can observe them).
  p_rea_rst : process (CLOCK2_50)
  begin
    if rising_edge(CLOCK2_50) then
      if rea_rst_cnt /= "1111" then
        rea_rst_cnt <= rea_rst_cnt + 1;
      else
        rea_rst <= '0';
      end if;
    end if;
  end process;

  -- Probe word — LSB-first (must match debug/u_dbg_rea.yml):
  --  [0] ninit_done  [1] rst_sys  [2] lw_arvalid [3] lw_arready [4] lw_rvalid
  --  [5] lw_awvalid  [6] lw_wvalid [7] lw_awready [8] lw_wready  [9] lw_bvalid
  --  [15:10] lw_araddr[5:0]
  rea_probe <= lw_araddr(5 downto 0) & lw_bvalid & lw_wready & lw_awready &
               lw_wvalid & lw_awvalid & lw_rvalid & lw_arready & lw_arvalid &
               rst_sys & ninit_done;

  -- Sticky toggle on every trigger fire → anchors trigger_out + visible on LD3.
  p_rea_trig : process (CLOCK2_50)
  begin
    if rising_edge(CLOCK2_50) then
      if rea_trig = '1' then
        rea_trig_sticky <= not rea_trig_sticky;
      end if;
    end if;
  end process;

  -- RTL-P3.739: latch the first lw_arvalid (HPS read reaching the fabric across
  -- the lwh2f bridge) and hold it until fabric reset → LEDR(3) is a power-cycle-
  -- and HPS-abort-survivable visual verdict, independent of REA arming. Same
  -- clk_sys/CLOCK2_50 domain as lw_arvalid (the lwhps2fpga bridge clock), no CDC.
  p_lw_arvalid_sticky : process (CLOCK2_50)
  begin
    if rising_edge(CLOCK2_50) then
      if rst_sys = '1' then
        lw_arvalid_sticky <= '0';
      elsif lw_arvalid = '1' then
        lw_arvalid_sticky <= '1';
      end if;
    end if;
  end process;

  u_dbg_rea : entity work.rr_rea_intel
    generic map (
      G_SAMPLE_W    => 16,
      G_DEPTH       => 4096,
      G_TIMESTAMP_W => 0,
      G_NUM_CHAN    => 1,
      G_CTRL_CHAIN  => 1   -- sld_instance_index = 1
    )
    port map (
      sample_clk  => CLOCK2_50,
      sample_rst  => rea_rst,
      probe_in    => rea_probe,
      trigger_out => rea_trig
    );

end architecture rtl;

# `hps_subsys` exported top-level port contract (DE25 Phase B)

This is the **fabric-facing port contract** of the `hps_subsys` Platform
Designer system when `rr` generates it with the composed-subsystem bridge
re-export enabled (RTL-P3.722). A fabric top instantiates `hps_subsys` and wires:

- `ddr_stream_axi`'s `m_axi4` AXI master → the **f2sdram** AXI4 slave (FPGA→HPS-DDR),
- a CSR **AXI-Lite slave** → the **lwh2f** AXI-Lite master (Linux→fabric CSRs).

The interface/port names + widths below are harvested from the DE25 golden GHRD
generated black box (`…/GHRD/hps_subsys/hps_subsys/hps_subsys_inst.v`) and the
`f2sdram_adapter.ip`. They are the names `rr`'s `render_hps_qsys_script`
re-exports via `set_interface_property <top> EXPORT_OF <internal>`.

> **How to enable.** In the project's `ip.yml`, under the HPS IP `config:` block,
> set `export_f2sdram: true` and/or `export_lwh2f: true`. These are real
> `HpsConfig` fields, so `hps_gen._build_hps_config` forwards them with no extra
> plumbing. Default is OFF → the flat 3-port heartbeat behaviour
> (`de25_hps_smoke`) is unchanged. Exporting a bridge whose width-is-enable knob
> is 0 (`f2sdram_width`/`lwh2f_width`) is rejected at render time
> (no-silent-failures).

---

## 1. Always-present flat exports (RTL-P3.682, both modes)

| Top interface | qsys type | Dir | Port(s) | Width | Notes |
|---|---|---|---|---|---|
| `fabric_clk` | clock sink | in | `fabric_clk_clk` | 1 | board top drives the fabric clock (GHRD `clk_100`, 100 MHz) |
| `fabric_reset` | reset sink | in | `fabric_reset_reset` | 1 | board top drives the fabric reset (GHRD `rst_in`, active-high async) |
| `ninit_done` | reset source | out | `ninit_done_ninit_done` | 1 | Reset Release IP; deasserts LOW at config-complete — gate user logic on it |

---

## 2. `export_f2sdram: true` — FPGA→HPS-DDR AXI4 **slave** (256-bit)

Exported through the GHRD `f2sdram_adapter` (an `altera_axi_bridge`); the
adapter's AXI4 **slave** (`s0`) is the fabric-facing port, its master (`m0`) is
wired into `hps.f2sdram` over the qsys interconnect. The fabric AXI **master**
(`ddr_stream_axi.m_axi4`) drives this slave.

**AXI4, 256-bit data, 32-bit address (4 GB window, range `0x1_0000_0000`), 5-bit ID.**
Top interface name: `f2sdram_adapter_axi4_sub` (a **slave** on `hps_subsys`).

Clock/reset for this datapath: **driven INTERNALLY** by `hps_subsys` (from
`fabric_clk` + `fabric_reset` through `mm_interconnect_0`). The regenerated
wrapper exposes **NO** `f2sdram_adapter_clk_clk` / `f2sdram_adapter_rst_reset`
top ports — verified against `hps_subsys/synth/hps_subsys.v`.

AXI slave channel ports (prefix `f2sdram_adapter_axi4_sub_`). Direction is
**relative to `hps_subsys`** (it is a slave, so AW/AR/W are inputs, B/R outputs).
This is an **AXI3-style** slave: it carries a separate **`wid`** write-ID strobe
and exposes **no** `qos` / `region` / `user` sidebands. `awlen`/`arlen` are
**4-bit** and `awlock`/`arlock` **2-bit** (verified against the generated wrapper):

| Port | Dir | Width | | Port | Dir | Width |
|---|---|---|---|---|---|---|
| `…_awid` | in | 5 | | `…_arid` | in | 5 |
| `…_awaddr` | in | 32 | | `…_araddr` | in | 32 |
| `…_awlen` | in | 4 | | `…_arlen` | in | 4 |
| `…_awsize` | in | 3 | | `…_arsize` | in | 3 |
| `…_awburst` | in | 2 | | `…_arburst` | in | 2 |
| `…_awlock` | in | 2 | | `…_arlock` | in | 2 |
| `…_awcache` | in | 4 | | `…_arcache` | in | 4 |
| `…_awprot` | in | 3 | | `…_arprot` | in | 3 |
| `…_awvalid` | in | 1 | | `…_arvalid` | in | 1 |
| `…_awready` | out | 1 | | `…_arready` | out | 1 |
| `…_wid` | in | 5 | | `…_rdata` | out | 256 |
| `…_wdata` | in | 256 | | `…_rid` | out | 5 |
| `…_wstrb` | in | 32 | | `…_rresp` | out | 2 |
| `…_wlast` | in | 1 | | `…_rlast` | out | 1 |
| `…_wvalid` | in | 1 | | `…_rvalid` | out | 1 |
| `…_wready` | out | 1 | | `…_rready` | in | 1 |
| `…_bid` | out | 5 | | | | |
| `…_bresp` | out | 2 | | | | |
| `…_bvalid` | out | 1 | | | | |
| `…_bready` | in | 1 | | | | |

> Wiring `ddr_stream_axi.m_axi4` (a 256-bit AXI master): its `aw*/ar*/w*` master
> outputs drive the corresponding `f2sdram_adapter_axi4_sub_*` inputs, its
> `b*/r*` ready/inputs take the slave outputs. ID width 5 — if the fabric master
> uses a narrower ID, zero-extend; the HPS side ignores upper ID bits per the
> GHRD `addressMap` (4 GB at offset 0).

---

## 3. `export_lwh2f: true` — lightweight HPS→FPGA AXI-Lite **master** (32-bit)

Re-exported directly off the HPS instance (it is already a master toward the
fabric). **AXI-Lite, 32-bit data, 29-bit address (512 MB window), 4-bit ID.** The
HPS-fixed CSR aperture base is **`0xF900_0000`** (an HPS address-map fact). A
fabric CSR AXI-Lite **slave** is driven by this master.

Top interface name: `lwhps2fpga` (a **master** on `hps_subsys`).

Clock/reset for this master are **driven INTERNALLY** by `hps_subsys` — the
regenerated wrapper exposes **NO** `lwhps2fpga_clk_clk` / `lwhps2fpga_rst_reset`
top ports.

AXI-Lite master channel ports (prefix `lwhps2fpga_`). Direction is **relative to
`hps_subsys`** (it is a master, so AW/AR/W are outputs, B/R inputs):

| Port | Dir | Width | | Port | Dir | Width |
|---|---|---|---|---|---|---|
| `…_awid` | out | 4 | | `…_arid` | out | 4 |
| `…_awaddr` | out | 29 | | `…_araddr` | out | 29 |
| `…_awlen` | out | 8 | | `…_arlen` | out | 8 |
| `…_awsize` | out | 3 | | `…_arsize` | out | 3 |
| `…_awburst` | out | 2 | | `…_arburst` | out | 2 |
| `…_awlock` | out | 1 | | `…_arlock` | out | 1 |
| `…_awcache` | out | 4 | | `…_arcache` | out | 4 |
| `…_awprot` | out | 3 | | `…_arprot` | out | 3 |
| `…_awvalid` | out | 1 | | `…_arvalid` | out | 1 |
| `…_awready` | in | 1 | | `…_arready` | in | 1 |
| `…_wdata` | out | 32 | | `…_rid` | in | 4 |
| `…_wstrb` | out | 4 | | `…_rdata` | in | 32 |
| `…_wlast` | out | 1 | | `…_rresp` | in | 2 |
| `…_wvalid` | out | 1 | | `…_rlast` | in | 1 |
| `…_wready` | in | 1 | | `…_rvalid` | in | 1 |
| `…_bid` | in | 4 | | `…_rready` | out | 1 |
| `…_bresp` | in | 2 | | | | |
| `…_bvalid` | in | 1 | | | | |
| `…_bready` | out | 1 | | | | |

> Although the HPS exposes the lwh2f port with full AXI4 channel signals
> (`awlen/awsize/awburst/wlast/…`), it is an AXI-Lite-class CSR bridge: drive
> single-beat transfers (`awlen=0`, `awsize=0b010` for 32-bit). A standard
> AXI-Lite fabric slave can ignore the burst-related outputs and tie `*last`
> high.

---

## 4. Clocking note

In the GHRD, `f2sdram_adapter_clk` / `lwhps2fpga_clk` / `fpga2hps_clk` /
`hps2fpga_clk` are separate fabric-supplied clocks (the user clocks each bridge
domain). For the validator a single fabric user clock can drive
`fabric_clk`, `f2sdram_adapter_clk` and `lwhps2fpga_clk` (they are all
fabric-side sinks). The `*_rst` sinks take the matching synchronous reset for
that clock domain.

## 5. Provenance

- Top ports + widths + directions: `…/GHRD/hps_subsys/hps_subsys/hps_subsys_inst.v`
- f2sdram adapter (256-bit `axi4_sub`, slave start `0x0` end `0x1_0000_0000`):
  `…/GHRD/hps_subsys/ip/hps_subsys/f2sdram_adapter.ip`
- Export mechanism (`altera_interface_boundary` / `interface_mapping`):
  `…/GHRD/hps_subsys/hps_subsys.qsys` (the `<altera:altera_interface_boundary>` block)
- Generated by `routertl/sdk/engine/hps_preset.py` `_render_bridge_exports`.

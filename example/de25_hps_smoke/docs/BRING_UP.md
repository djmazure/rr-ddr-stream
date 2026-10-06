# DE25-Standard (Agilex 5) — minimal HPS + heartbeat-LED bring-up

First-milestone landing for the Agilex 5 HPS platform/preset (RTL-P3.682). This
example isolates the **HPS platform generation** itself (the PS7/PS8 analog) from
the full DDR datapath (that lives in the sibling `de25_agilex5_ddr_validator`).

Pure-rr discipline: everything is declarative. The Agilex 5 HPS is a VENDOR IP,
declared in `ip.yml` by name/type; `rr` OWNS its qsys generation. There is **no**
hand-authored `gen_bd.tcl` / qsys-script and **no** hand-curated source list.

## What rr generates

`rr` reads `ip.yml` → builds an HPS platform request through the SoC vendor seam
(`sdk/engine/soc_backend.generate_hps_platform`, vendor-keyed) → renders the
routertl-owned qsys-script (`sdk/engine/hps_preset.render_hps_qsys_script`) →
runs `qsys-generate --synthesis` to emit the HPS synthesis HDL + the `.sopcinfo`
system-info handoff. This is the Altera analog of the auto-generated Xilinx
`ps_preset.tcl` → BD cell → `.xsa`.

## Pure-rr flow (intended)

```bash
rr synth run            # generate HPS (qsys) + fabric, then Quartus Pro synth
rr bitstream run        # → bitstream/de25_hps_smoke.sof
rr program run          # program the DE25 (IDCODE 4364C0DD)
# heartbeat: HPS_LED (pin Y127) toggles ~1.5 Hz → fabric clock alive
```

`rr linux ...` for the SPL→ATF→U-Boot→kernel boot is **out of scope** here — that
backend is RTL-P3.681 (the `altera` SocLinuxProfile is `implemented=False`).

## Config facts (harvested from the DE25 GHRD `agilex_hps.ip`)

| Knob | Value | Source |
|---|---|---|
| Boot | SDMMC 4-bit | `SDMMC_Mode`/`SDMMC_PinMuxing` |
| Console | UART1, no flow control | `UART1_Mode` |
| EMAC0 | RGMII_with_MDIO, 125 MHz GTX | `EMAC0_Mode` |
| h2f / lwh2f / f2sdram width | 128 / 32 / 256 | `H2F_Width`/`LWH2F_Width`/`f2sdram_data_width` |
| f2sdram mode | ACE5-Lite | `f2s_mode` |
| clocks | EOSC 25 MHz, F2H free 125 MHz | `eosc1_clk_mhz`/`F2H_free_clk_mhz` |

Addresses: f2h→HPS-DDR window 0x0–0x1_0000_0000 (4 GiB, 256-bit); lwh2f CSRs at
+0x40000 (sysid +0x10000, led_pio +0x10080) — see `sw/linux/system_memory_map.yml`.

## Validated 2026-06-04 (qsys-generate first attempt)

Ran the rr-generated qsys-script + `qsys-generate` against real Quartus Pro 25.3:

- **HPS component version is a TOOL fact, not a board fact.** The GHRD authored
  `intel_agilex_5_soc` **9.0.0** (Quartus 25.1); Pro 25.3 rejects it ("Version
  9.0.0 of Module intel_agilex_5_soc does not exist. Latest valid version of the
  module is 13.0.0"). `rr` now defaults to **13.0.0** and the system builds.
- **`qsys-generate` has NO `--hps-handoff` switch** on Pro 25.3 ("Unrecognized
  switch hps-handoff"). The HPS handoff is the **synthesis-generation side
  effect**: `qsys-generate --synthesis=VERILOG` emits the HPS synthesis HDL
  (`intel_agilex_5_soc_1300`, `intel_sundancemesa_hps_100`,
  `intel_sundancemesa_mpfe_100` — 27 files) + the `.sopcinfo` system-info
  handoff. The **U-Boot-SPL isw handoff** proper is a downstream BSP-generator
  step — that wiring is RTL-P3.681, not this milestone.
- With version 13.0.0 + `--synthesis`, generation **succeeded** (no fatal
  errors), producing the full HPS + led_pio HDL.

## Top integration + Reset Release (RTL-P3.682c, validated 2026-06-04)

`rr synth run` fires the HPS pre-synth gen, then synthesises a top that now
**instantiates the rr-generated `hps_subsys`**. The generated flat-system wrapper
exposes exactly three top-level ports (from `hps_subsys/hps_subsys_inst.vhd`):

| Port | Dir | Driven by |
|---|---|---|
| `fabric_clk_clk` | in | board `CLOCK2_50` (BM71, LVCMOS12) |
| `fabric_reset_reset` | in | `ninit_done` (active-high fabric reset bridge) |
| `ninit_done_ninit_done` | out | Reset Release IP → top gate |

**The flat `hps_subsys` exports NO HPS-sourced clock.** The HPS hard-IP bridge
clock interfaces (`f2h_free_clk`, `emif_usr_clk`, the f2h/lwh2f/f2sdram AXI
clocks) only materialise when the HPS is composed into a re-exporting
sub-system (the heavier GHRD flow this minimal milestone does not adopt — see the
note in `sdk/engine/hps_preset.render_hps_qsys_script`). So the heartbeat is
driven from the **board oscillator CLOCK2_50, not an HPS clock.**

**The heartbeat lights LEDR[0], NOT HPS_LED.** `HPS_LED` (pin Y127, LVCMOS18) is
in the **HPS-dedicated I/O bank** — owned by the HPS hard IP, so fabric logic
cannot drive it (the Fitter rejects it: *"Error 175019: Illegal constraint of I/O
pad to location PIN_Y127 … no routing connectivity between source I/O output
buffer and the I/O pad"*). Driving HPS_LED would require an HPS GPIO under
software (out of scope for this fabric-only smoke). So the heartbeat goes to a
**fabric-driveable user LED — LEDR[0] (pin CC71, LVCMOS12)** — the same pin class
the de25_led_smoke canary proved (RTL-P3.682f).

> **Honest status contract.** A toggling `LEDR[0]` on this build proves
> **configuration + fabric clock + the Reset Release gate** are alive. It does
> **NOT** prove the HPS clock manager / CPU came out of reset — that needs an
> exported HPS clock (composed-subsystem HPS flow, a follow-up ticket).

### Reset Release IP (Critical Warning 20759)

Agilex 5 needs the **Reset Release Intel FPGA IP** for a successful functional
configuration. `rr` now instantiates it (`altera_s10_user_rst_clkgate`, the
active "Reset Release IP"; `intel_user_rst_clkgate` is the legacy alias) INSIDE
the generated `hps_subsys` and exports its `ninit_done`. `ninit_done` is HIGH
while the SDM initialises the device and deasserts LOW at config-complete; the
top (a) drives the HPS fabric reset bridge from it and (b) holds the heartbeat
counter in reset until it clears — so the LED stays dark until the device reports
configured, then visibly toggles.

## Pure-rr build + program

```bash
rr synth run        # HPS gen (qsys-generate) + Quartus Pro synth + fit
rr bitstream run    # → bitstream/de25_hps_smoke.sof
rr program run      # program the DE25 (IDCODE 4364C0DD)
# HPS_LED (pin Y127) toggles ~1.5 Hz once configured → config + fabric clock alive
```

`hps_subsys/hps_subsys.qsys` is declared in `project.yml` under `sources.ip` so
the Quartus project picks it up as a `QSYS_FILE` (the file itself is rr-OWNED /
generated, never hand-authored).

## Remaining (follow-up)

1. **Prove HPS-alive:** export an HPS-sourced free-running clock (compose the HPS
   into a re-exporting sub-system) and drive the heartbeat from it, so a toggling
   LED proves the HPS clock manager — not just config — is alive.
2. SPL isw `hps-handoff` export step (RTL-P3.681) from the `.sopcinfo`.
3. Wire the f2h/lwh2f/f2sdram AXI bridges + led_pio CSR into the datapath (the
   sibling `de25_agilex5_ddr_validator`).

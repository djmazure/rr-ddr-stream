# DE25-Standard (Agilex 5 SoC) DDR validator — bring-up plan

> **✅ DONE — BIST passes on silicon (2026-06-09).** This was the *plan*; the
> validated as-built architecture, the f2sdram `AxUSER=0xE0` fix, the exact
> reproduce-it `rr` flow, and the bench gotchas are in
> [`SHARED_DDR4_F2SDRAM.md`](SHARED_DDR4_F2SDRAM.md). NB: the data path is the
> **f2sdram** port (FPGA→shared HPS DDR4 via the MPFE/io96b EMIF), not "f2h" as
> the original sketch below says.

Our own reference design (TerASIC gave no usable RTL). Goal: **boot Linux on the
Agilex 5 HPS and replicate the Zybo `ddr_stream_axi` DDR-validator** — the
Linux-on-HPS twin of `example/zybo_z7_20_ddr_validator/`.

Pure-`rr` only: declarative `project.yml` + `rr` commands, no PetaLinux/bitbake/
vendor-TCL. Where `rr` can't do it yet, we filed tickets + use an interim
hardware contract (serial console / LED), never a vendor workaround.

## Architecture (= the Zybo design, PS→HPS)

The PL BIST writes a counter pattern through `ddr_stream_axi` into **HPS DDR via
the f2h AXI bridge**, reads it back, and compares **in fabric** (the payload
never reaches the CPU → no data-path cache surface). Linux on the HPS (A55/A76)
drives the AXI-Lite **CSR via the h2f/lwh2f bridge** and reads the verdict
registers. `ddr_validator.c` (reused verbatim) is the userspace orchestrator.

Basic landing = **HPS DDR via f2h**. Exercising the **fabric DDR4 EMIF** (AVMM)
is a deferred follow-up (its own ticket) once the basic lands.

## 1:1 mapping from the Zybo original

| Zybo (Zynq-7 PS) | DE25 (Agilex 5 HPS) | status |
|---|---|---|
| `platform: zybo-z7-20` + `ps7_hooks` + `gen_bd.tcl` | `platform: de25-standard` + HPS BD | **GAP RTL-P3.682** |
| AXI-HP slave → PS DDR | f2h AXI → HPS DDR | RTL-P3.682 |
| GP master → PL AXI-Lite CSR | h2f/lwh2f AXI → fabric CSR | RTL-P3.682 |
| `ddr_stream_axi`, `ddr_validator_bist.vhd`, `ddr_validator_csr.vhd` | **reused as-is** (vendor-neutral) | ✓ available |
| FSBL + u-boot-xlnx + linux-xlnx | SPL + ATF BL31 + U-Boot + linux-socfpga | **GAP RTL-P3.681** |
| `first_stage: xilinx-fsbl` | `first_stage: altera-spl` | RTL-P3.681 |
| `ddr_validator.c`, `system_memory_map.yml` | **reused / mirrored** | ✓ (CSR base TBD, RTL-P3.682) |

## Open `rr` gaps — now CHILDREN of the landed SoC seam

The RouteRTL agent landed **RTL-P3.684 CHILD-0**: the vendor-keyed SoC/Linux
backend seam (`routertl sdk/engine/soc_backend.py`) — `rr linux` + the PS/HPS
presets now dispatch by `hardware.vendor` through one seam, with Xilinx routed
behind it and a clean `SocBackendNotImplementedError` for un-wired vendors. It
**already registers the `altera` `SocLinuxProfile`** (implemented=False,
ticket=RTL-P3.681) with the vendor defaults: `first_stage: uboot-spl`,
uboot=`altera-opensource/u-boot-socfpga`, kernel=`altera-opensource/linux-socfpga`,
atf=`ARM-software/arm-trusted-firmware`, `hardware_handoff: hps-handoff`. So our
two gaps are no longer build-from-scratch — they **implement against the seam**:

- **RTL-P3.681 (CHILD-1)** — wire the altera Linux boot backend: flip the profile
  `implemented→True` + implement the `uboot-spl → ATF BL31 → U-Boot →
  linux-socfpga` build/assembly behind the seam, consuming the `hps-handoff`.
  *(Note: `first_stage` is `uboot-spl`, not the earlier `altera-spl` guess.)*
- **RTL-P3.682 (CHILD-2)** — the Agilex 5 HPS platform/BD preset
  (`intel_agilex_5_soc_hps`, ps7/ps8 analog) that **emits the `hps-handoff`**
  CHILD-1 consumes (h2f/f2h AXI bridges + HPS DDR).

Because the seam owns the vendor defaults, this design's `project.yml` `linux:`
block **inherits** them (no duplicated repos/first_stage) — it only sets
`board`, `bootargs`, `apps`, `memory_map`.

## Phased plan

- **Phase A — HPS Linux boot** (hard new part, isolated). Minimal/empty fabric;
  get the Agilex 5 HPS to a Linux serial console + login. Proves RTL-P3.681
  (socfpga boot chain) end-to-end. *Interim contract: UART console + login.*
- **Phase B — fabric datapath + HPS BD**. Bring up `ddr_stream_axi` + the HPS
  f2h/h2f bridges (RTL-P3.682); CSR reachable from Linux (`devmem` the CSR base,
  read the ID/version register). *Interim contract: CSR read-back + an LED.*
- **Phase C — replicate the validator**. Wire stream→HPS-DDR, reuse
  `ddr_validator_bist`/`_csr` + `ddr_validator.c`, finalise `system_memory_map.yml`
  addresses, run `./ddr_validator --scenario roundtrip` → exit 0. The DE25 twin
  of the Zybo design.
- **Follow-up** — port the datapath to the **fabric DDR4 EMIF** (AVMM); reuse the
  `de25_agilex5_smoke` learnings (the AVMM top already synthesises on Agilex 5).

## What's in this skeleton today

- `project.yml` — declarative intent + the 1:1 mapping; fields that need the gaps
  are marked `(GAP …)`. **Does not build until RTL-P3.681 + RTL-P3.682 land.**
- `sw/linux/ddr_validator.c` — reused verbatim from the Zybo example.
- `sw/linux/system_memory_map.yml` — mirrored; CSR/DDR bases are placeholders
  pending the HPS BD (RTL-P3.682).
- (Phase B/C will add `src/top_de25_ddr_validator.vhd` + the reused
  `ddr_validator_bist.vhd` / `ddr_validator_csr.vhd` + the HPS BD tcl.)

## Board facts in hand
`boards/de25-standard/pinout.yml` (437 pins, vendor-seed) already carries the HPS
nets (HPS_ENET/SD/USB/UART/I2C/…), DDR4, clocks (3× 50 MHz + HPS_CLK_25). Part
`A5ED013BB32AE4SCS` (confirm vs chip marking). Quartus Pro 25.3.1 + patch 1.02 at
`/opt/altera_pro/25.3`; Agilex 5 licensed (LR-159839).

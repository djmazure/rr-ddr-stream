# DE25 bench runbook — CHILD‑2 (P3.682) + CHILD‑1 (P3.681)

Turnkey playbook for the DE25-Standard bench session (board in hand). The
RTL‑P3.684 seam (CHILD‑0/Phase‑2a) is DONE and stable; P3.681 (altera/socfpga
boot backend) and P3.682 (Agilex 5 HPS BD/preset) are **bench-coupled** — they
need Quartus Pro + the real HPS build/boot, so we implement+validate them here,
against hardware, not as unit-only software. Inter-agent handoff from the
RouteRTL agent → downstream integration agent, 2026-06-03.

**Bench order: P3.682 (produce the `hps-handoff` + `.sof`) → P3.681 (consume it,
boot Linux) → Phase C (validator).** Pure-`rr` only; every step has an interim
hardware contract so we never need a vendor-TCL/PetaLinux workaround.

## Bench-host JTAG setup (one-time, learned 2026-06-03)
- The Quartus **Pro** toolchain at `/opt/altera_pro/25.3` has its own `jtagd`.
  If the board doesn't enumerate (or enumerates under a Std install), kill the
  running `jtagd` and let the Pro `jtagconfig` respawn its own:
  `pkill jtagd; /opt/altera_pro/25.3/quartus/bin/jtagconfig`.
- Enumeration + IDCODE check (proves the cable + part before any build):
  `PATH=/opt/altera_pro/25.3/quartus/bin:$PATH jtagconfig` →
  expect `DE25-Standard … 4364C0DD  A5EC013(AB32A|BB32A)/..` — IDCODE **`4364C0DD`**
  is the Agilex 5 `A5ED013BB32A…` family marker. Mismatch ⇒ wrong cable/part.
- **LED smoke (`example/de25_led_smoke/`) is the proven bench-path canary** — pure
  fabric, programs in seconds via `rr program run`. Run it first on any fresh host:
  it validated Quartus Pro synth→fit→bitstream→program end-to-end on real silicon
  (2026-06-03). Two rr gaps it surfaced are filed: **RTL-P3.687** (stale
  `board_pins.tcl` not regenerated when top ports change — workaround:
  `rm -rf .routertl_cache/staging/<proj>`) and **RTL-P3.688** (`gen_bitstream.tcl`
  exits non-zero on memory-less designs despite a valid `.sof` — the `.sof` is
  still good, `rr program run` works).

## lwh2f aperture is 4 KiB / 12-bit — bench-critical (DDR-T2.7, 2026-06-11)

**The HPS lightweight HPS→FPGA (lwh2f / lwsoc2fpga) bridge presents only a 4 KiB
window to the fabric on Agilex 5.** Silicon-validated: `sw/linux/system_memory_map.yml`
`span: 0x1000`, base `0x2000_0000`. **Address bit 12 is NOT decoded** — a Linux
read of `0x2000_1000` aliases back to fabric offset `0x000`. Proven on the bench:
with a fabric slave mapped at offset `0x1000`, `devmem 0x2000103C` returned the
CSR IDENT `0x0DD50200` (the CSR at `0x2000003C`), not the intended slave.

**Implication for ANY fabric slave on lwh2f:** it must live within offsets
`0x000..0xFFF`. To host more than one slave, split *inside* the 4 KiB with a
demux whose select bit is **≤ 11** (e.g. `de25_axil_demux2` sel_bit_g=11 →
CSR `0x000..0x7FF`, second slave `0x800..0xFFF`). A select bit ≥ 12 is silently
unreachable — and sim will NOT catch it unless the testbench's
`lwh2f_axilite_bridge` is instantiated with `lite_addr_g=12` (the real aperture).
DDR-T2.7 shipped sim-green with `lite_addr_g=13` and failed on silicon.

**Bench discipline that caught it (apply every fabric-map change):**
- A source edit needs the FULL chain `rr synth run && rr implementation run &&
  rr bitstream run`. `rr bitstream run` alone only re-ASSEMBLES the cached
  netlist → a byte-identical `.sof`. After synth regenerates the qsys IPs the
  bitstream IP-drift gate (RTL-P3.661) trips — finish with `rr bitstream run
  --force` (benign same-run drift).
- **Confirm the PL runs the NEW bit** before trusting any `devmem`: `cmp` the
  fresh `.sof` against `bitstream/history/<latest>/...` (must DIFFER) and check
  the `quartus_pgm` checksum changed. A stale `.sof` reads like the old design.
- **Console:** pyserial to `/dev/ttyUSB2` (09fb:6022, interface 1) reads NOTHING
  unless you assert `s.dtr=True; s.rts=True` (+ `reset_input_buffer`, send `\r\n`).
- Re-JTAG-programming **under a running Linux WEDGES the HPS** (silent console);
  recover with a voltcraft power-cycle, then a single clean program from cold.

This resolves the "Remaining unknown: absolute lwh2f base/window" note below —
base `0x2000_0000`, span `0x1000`, silicon-confirmed.

## Prerequisites (verify FIRST, on the bench host)
- [ ] DE25-Standard powered + JTAG/USB-C enumerates; **confirm the chip marking
      = `A5ED013BB32AE4SCS`** (vs the manual's SR1 ambiguity) and fix the part if not.
      (IDCODE `4364C0DD` already confirms the `A5ED013BB32A…` family — 2026-06-03.)
- [ ] Quartus Pro **25.3.1 + Patch 1.02** at `/opt/altera_pro/25.3`; Agilex 5
      licensed (`LR-159839`). Front-load the **RTL-P3.663 empty-`get_family_list`
      license path** — it's on the critical path and unvalidated in a real HPS
      build (it passed for the synth-smoke; confirm for the HPS/qsys flow).
- [ ] `boards/de25-standard/pinout.yml` (437 pins) loads; the HPS_* nets present.
- [ ] aarch64 cross-toolchain available (or let `rr linux` fetch it).

## Implementation surface (code-grounded, confirmed 2026-06-03)
Read of the live routertl seam, so P3.682/681 implement against facts not guesses:
- **Seam gate:** `sdk/cli/commands/linux_steps/_common.py::gate_linux_backend_implemented()`
  raises `SocBackendNotImplementedError` while `_ALTERA_SOCFPGA.implemented is False`
  (`sdk/engine/soc_backend.py`). **P3.681 step 1 = flip that flag** once the steps exist.
- **fetch_sw is UNGATED** (`fetch-sw.py` reads `SW_REPOS` env → `linux.sw_repos`; never
  checks `implemented`). socfpga repos clone pre-bench. **P3.681 TODO:** wire `fetch_sw`
  to fall back to the profile's `uboot_repo`/`kernel_repo`/`atf_repo` when `linux.sw_repos`
  is unset (today it errors "no repos"). Repos are cloning now under `sw/linux/src/`.
- **Xilinx handoff analog:** `rr linux xsa` = Vivado `write_hw_platform` → `build/<proj>.xsa`
  (`linux_steps/xsa.py`). **P3.682 analog:** `qsys-generate` (toolchain already on PATH via
  `sdk/cli/env_setup.py`) on an HPS qsys system → the `hps-handoff` dir; `qsys_scanner.py`
  already parses qsys output XML for interface widths/bases.
- **Xilinx PS-BD analog:** per-project `tcl_hooks/gen_bd.tcl` (create_bd_*) + the platform's
  `ps_preset.tcl` sidecar. **P3.682:** an `intel_agilex_5_soc_hps` qsys (boot=SD, 1×UART for
  console, EMAC, SD/MMC, HPS-DDR, **f2h** AXI + **lwh2f** AXI bridges), HPS pin-mux sourced
  from the `HPS_*` nets already in `boards/de25-standard/pinout.yml` (golden_top.qsf-seeded).
- **FSBL/bsp are Zynq-only** (`require_zynq_fsbl_chain` raises for `uboot-spl`). The altera
  chain needs its OWN steps: SPL (from u-boot-socfpga + handoff) → ATF BL31 → U-Boot → DT →
  bootscr → kernel(linux-socfpga) → sdcard; `crosscheck` = handoff vs `system_memory_map.yml`.
- **GOLDEN HPS REFERENCE — found 2026-06-03 (de-risks P3.682).** The TerASIC DE25
  ResourcePackage (`~/Documents/terasic/DE25-Standard_revD_v1.2.0_ResourcePackage.zip`)
  ships a complete HPS GHRD at `Demonstration/SoC_FPGA/GHRD/` (extracted to
  `~/Documents/terasic/DE25_revD_extracted/...`). It is our datapath **1:1**:
  `golden_top.v` + `qsys_top.qsys`; `hps_subsys/` with `agilex_hps.ip` (HPS hard-IP:
  EMAC0 RGMII, SDMMC boot, UART console, f2sdram + lwh2f enabled), `emif_io96b_hps.ip`
  (HPS DDR4, 32-bit DQ + 4 DBI), `f2sdram_adapter.ip` (**f2h → HPS DDR**);
  `peripheral_subsys/` (led_pio/button/dipsw/sysid on **lwh2f**). Address map harvested:
  f2sdram window 0x0–0x1_0000_0000 (4 GiB, 256-bit); lwh2f CSRs at +0x40000
  (sysid+0x10000, led+0x10080…). HPS Linux apps in `Demonstration/SoC/` (hps_led_key,
  hps_lcd, hps_gsensor) confirm the board boots Linux. **HARVEST FACTS ONLY** (HPS/DDR/
  clock/pin-mux config — board facts, like the pinout) — do NOT copy their `.qpf`/
  PetaLinux/vendor-TCL flow (reference-only, the thing rr replaces). Author the
  Agilex 5 HPS config declaratively for the rr flow from these facts.
- **RESOLVED (2026-06-11):** lwh2f base = `0x2000_0000`, span = `0x1000` (4 KiB /
  12-bit), silicon-confirmed — see the "lwh2f aperture" section above (DDR-T2.7). Promote
  `boards/de25-standard` vendor-seed→schematic-verified after a schematic cross-check
  (the GHRD `.qsf`/`.sdc` + datasheets are a strong corroborating source).

## CHILD‑2 (RTL‑P3.682) — Agilex 5 HPS BD/preset → handoff + bitstream
Goal: a declarative HPS config (qsys `intel_agilex_5_soc_hps`) exposing **f2h**
AXI (→ HPS DDR), **lwh2f** AXI (→ fabric CSR), HPS DDR EMIF, clocks/resets, that
**emits the `hps-handoff`** (the SocLinuxProfile `hardware_handoff` for altera).
1. Implement the HPS preset behind the vendor-keyed dispatch (the ps7/ps8 analog;
   new module + surgical hook, per the seam's pattern — NOT inline `vendor==`).
2. Generate the HPS qsys IP + the handoff; build a **minimal/empty-fabric** top
   (just HPS + a heartbeat LED on a known pin from the pinout).
3. **Interim contract:** `.sof` programs; handoff files generated; HPS exits reset
   (heartbeat LED toggles). File the lwh2f/f2h base addresses back into
   `sw/linux/system_memory_map.yml` (replace the placeholders).

## CHILD‑1 (RTL‑P3.681) — altera/socfpga boot backend → Linux console
Goal: wire the altera `SocLinuxProfile` backend (flip `implemented=False→True` in
`soc_backend.py`) + implement the steps behind the seam, consuming CHILD‑2's handoff.
1. **fetch_sw** → clone the profile repos (u-boot-socfpga, linux-socfpga, ATF).
   *(board-independent; the one step doable pre-bench if desired.)*
2. **SPL + ATF BL31** — build U-Boot SPL from u-boot-socfpga **+ the hps-handoff**;
   build ATF BL31 (ARM-software/arm-trusted-firmware) → FIT.
3. **U-Boot** proper + **socfpga DT** (Agilex 5) + **bootscr** (extlinux/boot.scr).
4. **kernel** = linux-socfpga; **sdcard** = socfpga SD partition layout
   (SPL/ATF/FIT in the boot partition + rootfs).
5. **crosscheck** = `hps-handoff` vs `system_memory_map.yml` (altera analog of the
   XSA crosscheck).
6. **Interim contract:** boot to a **serial console + login** (`console=ttyS0,115200`
   — confirm ttyS0 vs ttyAMA0 at first boot) on the **empty/minimal fabric** (Phase A
   complete). This is the headline milestone.

## Phase C — replicate the validator (the DE25 twin of the Zybo design)
1. Bring `ddr_stream_axi` + `ddr_validator_bist.vhd` + `ddr_validator_csr.vhd`
   (reused as-is) into the top; wire the AXI master → **f2h → HPS DDR**, the
   AXI-Lite CSR → **lwh2f**.
2. Finalise `system_memory_map.yml` (real CSR base + the HPS-DDR ring carve).
3. On target: `./ddr_validator --scenario roundtrip --n 8192` → **exit 0**.

## Definition of done / discipline
- A bench session that finds real defects is success — file **T-tickets** for each
  (per the bug-finding-is-success rule); a clean run that found nothing is a
  yellow flag. Never weaken an assertion or hack the BFM/DUT to make it pass.
- Record per-step PASS/console-lines + any misfire inline; promote
  `boards/de25-standard` from `vendor-seed` → `schematic-verified` only after a
  schematic cross-check (we don't have the schematic yet).
- Shared-checkout hygiene: path-scoped commits, submodule lock path
  (`$(git rev-parse --git-dir)/index.lock`), coordinate with the RouteRTL agent.

## Follow-up (after basic lands)
Port the datapath to the **fabric DDR4 EMIF** (AVMM) — the `de25_agilex5_smoke`
target already proved the AVMM top synthesises on Agilex 5.

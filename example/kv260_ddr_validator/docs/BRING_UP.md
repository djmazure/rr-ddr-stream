# kv260_ddr_validator — bring-up ladder

All steps are `rr` commands (pure-routertl flow). Hardware steps require an
`rr lease` on the KV260 board (shared bench — HARD RULE).

---

## 0. Build (no board)

```bash
cd rr-ddr-stream/example/kv260_ddr_validator
rr ws update                 # subscribe src/ + vendored libs/ (IP + scoped CDC xdc)
rr lint                      # ESA VHDL baseline (esa-vhdl-baseline-provisional)
rr synth run                 # gen_project sources tcl_hooks/gen_bd.tcl (zynq_ultra_ps_e)
rr implementation run        # routed sign-off
rr bitstream run             # -> out/.../rr_ddr_stream_kv260_ddr_validator.bit
```

The bench EDA admission gate SIGKILLs an *uncontained* Vivado (~2 s, exit 247).
Build cgroup-contained: ensure `XDG_RUNTIME_DIR=/run/user/1000` and
`DBUS_SESSION_BUS_ADDRESS` are exported and do **not** set `RR_DISABLE_CGROUP` /
`RR_EDA_CGROUP` (see `reference_bench_magic_eda_admission_requires_contained_build`).

---

## 1. Silicon — primary path: autonomous auto-start + JTAG readback

This is the robust KV260 path: it needs **no Linux**, no LEDs, no UART. The
in-fabric BIST self-runs on configuration (`auto_start_g`, DESIGN.md §7) and
latches the verdict in the CSR; JTAG reads it.

**PROVEN recipe (xsct, 2026-06-24 — verified PASS on KV260 silicon).** The
KV260 SOM boots from on-SOM QSPI, whose stock FSBL `psu_init` leaves
`M_AXI_HPM0_FPD` DISABLED — so the PL CSR @ 0xA0000000 reads 0 from the stock
U-Boot (`md.l`) or any access using that running `psu_init`
([[hazard_kv260_pl_csr_hpm0_needs_design_fsbl]]). The fix is to run **this
project's OWN `psu_init`** (its BD sets `PSU__USE__M_AXI_GP0=1`) over JTAG, which
enables HPM0 — *no QSPI reflash, no Linux*. Sequence (multi-board bench):

```tcl
# psu_init.tcl + .bit live under project/.../ddr_validator_bd_zynq_ultra_ps_e_0_0/
# and bitstream/ . Run with: xsct <thisfile>
connect
targets -set -filter {name == "PSU"}          ;# reset on the PSU node →
rst -type system                              ;#   leaves A53 #0 in reset-catch
after 3000
targets -set -filter {name == "Cortex-A53 #0"}
stop                                          ;# psu_init/mrd need a STOPPED core
source <project>/.../psu_init.tcl
psu_init                                       ;# ENABLES M_AXI_HPM0_FPD (+ DDR4/clocks)
targets -set -filter {name == "PS TAP"}        ;# KV260's TAP (fpga part-checks xck26)
fpga -file <bitstream>/rr_ddr_stream_kv260_ddr_validator.bit
targets -set -filter {name == "Cortex-A53 #0"}
psu_ps_pl_isolation_removal                    ;# release PL reset → auto_start BIST runs
psu_ps_pl_reset_config
after 3000
# mrd -force bypasses xsdb's "PL AXI not in memory map" guard (DS-P3.115):
puts "IDENT  [mrd -value -force 0xA000003C]"    ;# 232063488 = 0x0DD50200
puts "STATUS [mrd -value -force 0xA0000004]"    ;# 54 = 0x36 → done+PASS+idle, no fail
```

**Verified silicon result (auto-start roundtrip, N=8192):**

| Reg | Addr | Value | Meaning |
|-----|------|-------|---------|
| IDENT | 0xA000003C | `0x0DD50200` | CSR reached — HPM0 route good |
| STATUS | 0xA0000004 | `0x00000036` | done=1 **PASS=1** fail=0 write_idle=1 read_idle=1 |
| ERR_COUNT | 0xA0000010 | `0x00000000` | zero mismatches |
| WORDS_WRITTEN | 0xA0000014 | `0x00002000` | 8192 = N (full traffic) |
| WORDS_READ | 0xA0000018 | `0x00002000` | 8192 = N |

`STATUS=0x36 / ERR=0` is the canonical rr-ddr-stream PASS code (matches the
Classic xc7z010 baseline). **The full in-fabric round-trip — write plane →
DDR4 (S_AXI_HP0_FPD) → read plane → in-fabric compare — passes on KV260.**

> **Order of checks (no hand-waving):** read `IDENT` first. `0x0DD50200` proves
> the GP0/HPM0 route reaches the CSR. Only then trust `STATUS`. `done & pass &
> write_idle & read_idle` with `WORDS_WRITTEN==WORDS_READ==N` and
> `ERROR_COUNT==0` is the end-to-end PASS.

To re-run a different scenario over JTAG (`mwr -force`, no Linux):
```tcl
mwr -force 0xA0000000 0x2     ;# CTRL.soft_reset
mwr -force 0xA0000008 0x1     ;# SCENARIO (0 roundtrip, 1 boundary, 2 reset_recovery)
mwr -force 0xA000000C 0x2000  ;# N_WORDS = 8192
mwr -force 0xA0000000 0x1     ;# CTRL.start
# poll STATUS bit1 (done), read ERROR_COUNT @ 0xA0000010
```

> **Gotchas (all hit + resolved 2026-06-24):** reset on the **PSU** node (not
> the A53) to land the core in reset-catch; the core must be `stop`-ped before
> `psu_init`/`mrd`; program via the **PS TAP** node (`fpga` part-checks xck26,
> so a wrong-board pick is rejected); `mrd -force` is mandatory (xsdb blocks
> PL-AXI addresses not in its memory map); on the multi-board chain the debug
> target tree only populates *after* the first reset (RTL-P3.916). A
> lease-aware `rr`-native version of this whole flow is RTL-P3.927.

---

## 2. Silicon — full app path: Linux + ddr_validator

The designed end-user path: boot Linux (ZynqMP boot chain — see the
`zynqmp-kria-linux-bringup` recipe), then run the userspace app.

```bash
rr linux xsa --refresh
rr linux fsbl && rr linux pmufw && rr linux atf && rr linux uboot
rr linux dtb && rr linux kernel && rr linux rootfs && rr linux sdcard
# on target:
./ddr_validator --scenario roundtrip --n 8192     # exit 0 = pass
```

The userspace app `mmap`s the CSR at `0xA000_0000` via `/dev/mem` (`O_SYNC`,
uncached), probes `IDENT`, runs the scenario, and prints the verdict.

> **The one thing that can sabotage it.** The IP's DMA ring lives at physical
> `0x7000_0000` (16 MiB). The kernel must NOT cache or speculatively touch that
> window or it corrupts the in-fabric round-trip and presents as a phantom DUT
> mismatch. `project.yml` `bootargs` carries `mem=1792M`, clamping kernel RAM to
> the low `0x7000_0000` so the ring is outside it. (A `reserved-memory` DT node
> is the cleaner ZynqMP idiom — fold it in via `rr linux dt_nodes` when the ring
> moves into the high DDR aperture.) The data path is in-fabric
> (PL↔S_AXI_HP0_FPD↔DDR4); the CPU is never a producer/consumer of the payload.

> **KV260 Linux is the bench-flaky path** (see the `kv260_linux` context: no-
> login timeouts, board hammering). Prefer §1 for a quick, deterministic
> IP-validation verdict; use §2 for the full end-user demonstration once the
> board boots cleanly from a cold power-cycle.

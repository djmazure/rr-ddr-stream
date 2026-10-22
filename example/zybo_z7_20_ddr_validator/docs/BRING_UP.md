# Zybo Z7-20 ddr_stream_axi validator — bring-up ladder

Pure-`rr`-flow bring-up for the hardware validator. Two tiers:

- **v0 — autonomous LED verdict** (no host): build → program → read the LEDs.
- **v1 — Linux validator app**: SD-boot Linux → run scenarios over the CSR,
  rich pass/fail with first-mismatch detail.

Everything goes through `rr`. There is no `build_project.tcl`, no hand-listed
`sources.syn`, no `create_ip`. The block design is a generation recipe
(`tcl_hooks/gen_bd.tcl`); the LED pins come from the board registry
(`routertl/boards/zybo-z7-20/pinout.yml`) via inferred pinout subscription —
no hand-written pin XDC.

---

## What this validates, and the one thing that can sabotage it

The BIST pushes a counter pattern `1..N` through the IP's **write** plane into
PS DDR3 (`m_axi4` → `S_AXI_HP0`), the IP reads it back through its **read**
plane (`S_AXI_HP0` → `m_axi4` → `M_AXIS`), and the BIST compares word-for-word
**in fabric**. The payload never reaches the CPU.

`S_AXI_HP0` is a **non-coherent High-Performance port** — it bypasses the APU
L1/L2 caches and goes straight to the DDR controller. Because both ends of the
loop are PL-side through HP0, the round trip is self-consistent regardless of
CPU cache state. The classic Zynq "CPU dirty cache vs DMA stale DDR" trap does
**not** apply here, because the CPU is never a producer or consumer of the
payload.

The **only** caching requirement: the ring's physical DDR window
(`ddr_base 0x1000_0000`, 256 MiB) must be **carved out of the kernel's RAM**,
so the CPU never caches or speculatively touches it. We do that with
`mem=256M` in `sw/linux/boot.txt` (kernel keeps the low 256 MiB; the ring sits
at 256–512 MiB, untouched). If you ever see a phantom mismatch under Linux that
the standalone v0 run doesn't show, suspect the clamp first.

---

## v0 — autonomous LED verdict

### Stage 0 — prerequisites

```bash
cd example/zybo_z7_20_ddr_validator
rr status                       # workspace + toolchain sanity
rr deps tree top_zybo_z7_20_ddr_validator   # confirm the resolver orders src/ + ../../rtl + ../../libs

# Register bank schema validation and HTML map generation
rr regbank validate project_regbank.yml
rr regbank html project_regbank.yml --output register-map.html
```

> **Register schema note (DDR-P2.16):**
> `project_regbank.yml` is the single source of truth for the AXI-Lite CSR decode.
> It defines 15 registers within the 4 KiB system window (`0x4000_0000`, span `0x1000`,
> matching `sw/linux/system_memory_map.yml`). Word indices 0..15 correspond to byte
> offsets `0x00`..`0x3C` (active footprint 64 bytes); unused addresses (words 16..1023)
> read as zero. Rendered `register-map.html` documents all fields, enums, and bit
> positions. Note: pulse fields (`CTRL.start`, `CTRL.soft_reset`, `BASE_LOAD.*`,
> `PTR_RESET.*`) appear with `RW` badges in HTML because RouteRTL currently lacks
> native `W1P` schema support (see `DESIGN.md` §4.3).
>
> **Cross-board variants:** `zybo_classic` shares this identical 15-register map;
> `kv260` shares the layout at base `0xA000_0000` (span `0x10000`); `de25` shares the
> layout at base `0x2000_0000` (span `0x1000`); `pfsoc` extends it with `C_WR_COMMIT`
> at `0x38`; `axe5` userspace uses an alternative map (`REG_IDENT` at `0x00`).
> This schema is authoritative ONLY for `zybo_z7_20_ddr_validator` (and verified identical clones).


### Stage 1 — build the bitstream

```bash
rr ws update                    # subscribe sources/constraints (board_pins auto-materialized)
rr synth run                    # ← RR--P3.8 acceptance harness — see "CDC note" below
rr implementation run
rr bitstream run
```

### Stage 2 — program

```bash
rr hardware doctor              # cable + board present
rr program run
rr program verify
```

> Zybo Z7-20 stays powered between runs (reference_zybo_no_poweroff) — the
> Arria-10 power-off-every-test rule does NOT apply here.

### Stage 3 — read the verdict

The design runs the default scenario at power-on only if `CTRL.start` is
pulsed. With no host, drive `start` from a button-press build, or move
straight to v1. The LED contract while running (docs/DESIGN.md §6):

| LED | meaning |
|-----|---------|
| LD0 | running |
| LD1 | done |
| LD2 | pass (latched until next start) |
| LD3 | fail (latched until next start) |

A clean run ends with **LD1 + LD2** lit and LD3 dark.

---

## CDC note — RR--P3.8 acceptance (the reason `rr synth run` is the harness)

The IP's only fabric clock-domain crossing is `i_ddr_fifo_intf`'s olo async
FIFOs (clk_sys 100 MHz ↔ clk_ddr 125 MHz). Out of the box Vivado times these
as related clocks and reports a **false** negative WNS (deepskopion saw
`-0.621 ns`). This example is RR--P3.8's acceptance harness:

1. First `rr synth run` will show the false WNS on the FIFO crossing — that is
   expected, it is the "before" state.
2. Develop the IP-side scoped constraint synth-in-the-loop: `report_cdc` /
   `report_timing` on the crossing → refine a cell-relative
   `set_max_delay -datapath_only` (NOT `clock_groups -asynchronous`, NOT
   consumer clock names) until it cleanly matches the real FIFO cells.
3. Ship it via the IP's `ip.yml build.sources.xdc` so a fresh consumer inherits
   correct CDC timing with **zero hand-added constraints** — at which point
   this example's `rr synth run` closes timing with no project XDC.

**Acceptance gate 1:** `rr synth run` closes timing on the async-FIFO CDC with
no consumer hand-added constraint → closes RR--P3.8.

---

## v1 — Linux validator app

Linux SD-boot follows the standard routertl Zynq-7 flow. If anything in the
boot chain misbehaves (silent UART, ILLEGAL_BOOT_MODE, kernel panic, garbled
u-boot, `unknown-block(0,0)`), work the **zynq7-sd-boot-bringup** skill's
8-gate ladder — every symptom collapses onto one of eight root causes.

### Stage 4 — build Linux from the freshly built impl

```bash
rr linux xsa --refresh          # export XSA from the impl DCP just built
rr linux dtb
rr linux fsbl
rr linux uboot
rr linux kernel
rr linux rootfs
rr linux bootscr                # compiles sw/linux/boot.txt → boot.scr (carries mem=256M)
rr linux sdcard                 # stage BOOT.bin + uImage + devicetree.dtb + boot.scr
rr linux lock                   # freeze the validated build set
```

`rr linux mmap` keeps the BD address segment for `ddr_validator_csr` in sync
with `sw/linux/system_memory_map.yml` (CSR @ 0x4000_0000, 4 KiB). `rr linux
sync` regenerates the device tree + UIO headers after an XSA export.

### Stage 5 — run the validator on target

Build the app on target (or cross-compile and drop it on the rootfs):

```bash
gcc -O2 -Wall -o ddr_validator ddr_validator.c
```

Then:

```bash
./ddr_validator --scenario roundtrip --n 8192
./ddr_validator --scenario boundary  --n 1048576   # span many bursts at scale
./ddr_validator --scenario reset_recovery          # stub today (behaves as roundtrip)
```

The app mmaps the CSR via `/dev/mem` (O_SYNC, uncached), probes `IDENT`
(`0x0DD50200`), kicks the scenario, polls `STATUS.done`, and prints the
verdict. Exit code: `0` pass, `1` fail, `2` skip/probe-error. On failure it
prints the first mismatch (`FIRST_ERR_INDEX/EXP/GOT`).

**Acceptance gate 2:** scenarios 0–2 report `pass` with non-zero traffic on
real DDR; LEDs encode the same verdict.

**Acceptance gate 3:** scenario 3 (`pingpong`) unstubs and passes once
DDR-P3.11 ships the IP's ptr-reset CSR ports.

---

## Repo layout

```
example/zybo_z7_20_ddr_validator/
  project.yml                         declarative descriptor (board, auto_sources, linux, BD hook, mmap, lint)
  src/
    top_zybo_z7_20_ddr_validator.vhd  PL top: BD wrapper + IP + BIST + CSR + record→flat AXI shim + LEDs
    ddr_validator_bist.vhd            self-checking pattern engine (clk_sys), scenarios per §5
    ddr_validator_csr.vhd             AXI-Lite slave, CSR map per §4
  tcl_hooks/
    gen_bd.tcl                        BD recipe: PS7 + 2× smartconnect + 2× proc_sys_reset, GP0 + HP0
  sw/linux/
    ddr_validator.c                   userspace app: mmap CSR, run scenarios, print verdict
    system_memory_map.yml             0x4000_0000 CSR window for `rr linux mmap`
    boot.txt                          u-boot boot script (carries mem=256M ring clamp)
  docs/
    DESIGN.md                         design spec
    BRING_UP.md                       this file
```

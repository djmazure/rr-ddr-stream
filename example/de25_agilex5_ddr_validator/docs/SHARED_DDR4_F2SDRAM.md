<!--
SPDX-FileCopyrightText: 2026 Daniel J. Mazure
SPDX-License-Identifier: MIT
-->
# DE25 Shared DDR4 over f2sdram — architecture, the AxUSER fix, and the validated bring-up

**Status: PASSING ON SILICON (2026-06-09).** The fabric BIST round-trips data
through the HPS DDR4 via f2sdram with zero errors — see
[Silicon validation](#silicon-validation).

This is the as-built reference for how the FPGA fabric reaches the **shared HPS
DDR4** on the Terasic DE25-Standard (Agilex 5 SoC), the bug that blocked it, the
one-line root cause, and the exact pure-`rr` flow to reproduce it.

---

## 1. What "Shared DDR4" actually is

The DE25 has **one DDR4 bank, attached to the HPS** and driven by the **single
io96b EMIF** (the HPS hard memory controller — `EMIF_PROTOCOL=DDR4_COMP`). There
is **no separate FPGA-fabric memory controller** on this design. "Shared" =
*that one DDR4 is reached by two masters*:

- the **HPS / PS** (the A55/A76 ARM cores — this is the RAM Linux runs out of), and
- the **FPGA fabric**, via the **f2sdram** ("FPGA-to-SDRAM") AXI port.

Both are arbitrated by the HPS **MPFE** (Multi-Port Front End) / **CCU** (Cache
Coherency Unit) in front of the io96b EMIF. Terasic's "Shared DDR4" *is* this —
the GHRD's own demo is just a JTAG-Avalon master poking the HPS DDR4 through
f2sdram; our validator drives the same path with a real self-checking BIST.

`f2sdram` is **not** a memory controller — it is the fabric's *entry port* into
the HPS memory subsystem. The bytes land in DRAM via the **shared** io96b EMIF,
the same controller (and the same DRAM) the CPU uses.

### The data path (fabric → DDR4)

```
  ddr_stream_axi.m_axi4            fabric AXI4 master (the IP under test, BIST)
        │  256-bit, AXI4-native (LEN=8b, LOCK=1b, QOS=4b)
        ▼
  f2sdram_adapter.axi4_sub         custom adapter — FORCES AxUSER=0xE0 (the fix)
        │  axi4_man
        ▼
  hps.f2sdram                      HPS hard-IP f2sdram AXI slave
        │
        ▼
  CCU / MPFE                       coherency + multi-port arbitration  ← shares
        │                          the DDR4 with the CPU's traffic here
        ▼
  io96b EMIF (emif_io96b_hps)      the HPS DDR4 controller + PHY
        │
        ▼
  external DDR4                    the ONE shared bank
```

The CSR side is independent: Linux pokes the validator's control/status
registers over **lwh2f** (lightweight HPS→FPGA AXI-Lite) at `0x2000_0000`; the
data never touches the CPU (compared in fabric).

---

## 2. The bug, and the one-line root cause (RTL-P3.682 / DDR-P3.21)

On first silicon the BIST **deadlocked**: `WORDS_WRITTEN` counted up to N, but
`WORDS_READ=0` and `STATUS=0x31` (RUNNING + both engines idle) — stuck in the
write-drain phase, data never landing in DRAM.

It was **not** a firewall, a missing bridge connection, or the BD wiring — all of
those matched the golden GHRD. The cause:

> **The Agilex 5 HPS f2sdram (ACE5-Lite) port only routes a transaction to DDR
> through the CCU/MPFE when the AXI sideband carries the right coherency
> attributes in `AxUSER`.** rr was using a stock `altera_axi_bridge`, which
> passes the fabric master's `AWUSER` (`0x00`) straight through — so the CCU
> never recognised the write as a DDR access. It was accepted (no backpressure)
> but never completed (no `BRESP`) → write-drain deadlock.

Found by diffing the **Agilex 5 GSRD** (`altera-opensource/ghrd-socfpga`), whose
custom `f2sdram_adapter` hard-codes the HPS-facing sideband:

| signal | value | meaning |
|---|---|---|
| `man_awuser` / `man_aruser` | **`0xE0`** | the ACE5-Lite domain/cache/coherency encoding the CCU/MPFE need |
| `man_awcache` / `man_arcache` | `0x2` | Normal Non-cacheable Bufferable |
| `man_awprot` / `man_arprot` | `0b011` | privileged, non-secure, data |
| `man_awlock` / `man_arlock`, qos, region | `0` | — |

**The fix:** rr now **ships** that proven adapter
(`routertl/sdk/engine/hps_ip/f2sdram_adapter/`, MIT-0) and the HPS generator
wires it (`f2sdram_adapter.axi4_man → hps.f2sdram`) instead of the stock bridge —
for **every** rr Agilex 5 project. The adapter exposes a proper **AXI4** slave
(8-bit `AxLEN`, 1-bit `AxLOCK`, no `WID`, +qos/region/user); the example top
connects the AXI4-native `ddr_stream_axi` master to it directly (no AXI3
down-conversion).

---

## 3. The pure-`rr` bring-up flow (reproduce it)

All steps are `rr` + declarative `project.yml`/`ip.yml` — no vendor TCL.

```bash
# 1. Build the fabric (HPS gen regenerates the qsys with the custom f2sdram_adapter).
rr synth run && rr implementation run && rr bitstream run    # → bare .sof
#    (closure preflight false-positives on the library-overridden olo packages —
#     RTL-P3.777, fixed; if it resurfaces use RR_SKIP_CLOSURE_PREFLIGHT=1.
#     If `rr bitstream run` trips the IP-vs-.qsf regen staleness guard, --force.)

# 2. Build U-Boot/SPL (stages out/u-boot-spl-dtb.hex).
rr linux uboot

# 3. Embed the SPL into the .sof → HPS-bootable *_hps.sof  (RTL-P3.778, CLI.202).
rr linux sof
#    The bare .sof has NO bootloader (JTAG program → Error 19192). `rr linux sof`
#    runs quartus_pfg -o hps_path=... → out/<stem>_hps.sof. JTAG-program THAT.

# 4. On the board: program the bootable SOF, reach U-Boot, boot clamped.
quartus_pgm -c "DE25-Standard [3-4]" -m jtag -o "p;out/<stem>_hps.sof"
pkill -9 jtagd            # free the shared 09fb:6022 UART (NOT reset-blaster)
#    interrupt autoboot (Enter spam) → U-Boot, then:
setenv bootargs "console=ttyS1,115200 ... mem=512M"   # carve a BIST window
load mmc 0:1 ${kernel_addr_r} Image
load mmc 0:1 ${fdt_addr_r} socfpga_agilex5_socdk.dtb
bridge enable 0x3f        # release ALL bridges incl f2sdram (bits 3-5) + sideband
booti ${kernel_addr_r} - ${fdt_addr_r}

# 5. In Linux, drive the BIST via devmem (CSR @ 0x2000_0000, base in the carved window).
busybox devmem 0x20000000 32 0x2          # CTRL = SOFT_RESET
busybox devmem 0x20000008 32 0x0          # SCENARIO = roundtrip
busybox devmem 0x2000000C 32 256          # N_WORDS
busybox devmem 0x20000028 32 0xa0000000   # WR_BASE (in the mem=512M free window)
busybox devmem 0x2000002C 32 0xa0000000   # RD_BASE
busybox devmem 0x20000030 32 0x3          # BASE_LOAD (latch wr+rd)
busybox devmem 0x20000000 32 0x1          # START
busybox devmem 0x20000004 32              # STATUS  → 0x36 = DONE|PASS
busybox devmem 0x20000010 32              # ERROR_COUNT → 0
```

### Why `mem=512M`
The BIST writes real DDR; the kernel must not own that region. `mem=512M` clamps
Linux to `0x8000_0000–0x9FFF_FFFF` and frees `0xA000_0000–0xBFFF_FFFF` for the
BIST (`WR/RD_BASE = 0xA000_0000`). The app never sets the base; the RTL default
`0x0` would target the bottom of DRAM = the kernel — always set it.

---

## 4. Silicon validation

DE25, fingerprinted **aarch64 / "SoCFPGA Agilex5 SoCDK"**, f2sdram-fix SOF:

| N | STATUS | WORDS_WRITTEN | WORDS_READ | ERROR_COUNT | DDR @0xA0000000 |
|---|---|---|---|---|---|
| 256 | `0x36` (DONE+PASS) | 256 | **256** | **0** | `1,2,3,…` (counter pattern) |
| 8192 | `0x36` (DONE+PASS) | 8192 | **8192** | **0** | — |

CSR IDENT `0x0DD50200` on the new fabric. The write-drain deadlock is gone —
f2sdram→DDR4 round-trips cleanly. Build timing met (setup slack **+15.071 ns**).

---

## 5. Bench gotchas (hard-won)

- **One DDR4, shared, coherent.** Because it's the *shared* HPS DDR4 (not a
  private FPGA window), the FPGA path runs through the CCU/MPFE — which is exactly
  why `AxUSER` mattered. A private/direct memory would never have hit this.
- **Bootable SOF ≠ synth SOF.** The bare `rr bitstream` `.sof` has no SPL →
  JTAG program fails Error 19192. Use `rr linux sof` (or the SPL is on the SD via
  `u-boot.itb` for SD-boot).
- **Console is the 09fb:6022 "DE25-Standard" device, interface 1.** It carries
  JTAG (if0) + the HPS UART (if1) on one cable. After a Quartus JTAG op,
  `pkill -9 jtagd` restores the UART — **do NOT** use `reset-blaster` (its USB
  re-enumeration eats the autoboot interrupt window).
- **Interrupt autoboot with Enter, not Ctrl-C.** A `\x03` spam wedged the SPL's
  SD init once ("Card did not respond to voltage select"); a clean power-cycle
  clears that. `bootdelay=5`.
- **≥3 boards share this host; ttyUSB indices shift.** Always fingerprint by
  `uname -m` (aarch64 = DE25) + `/proc/device-tree/model` before trusting a
  `devmem` — a `0x2000003C` read on a wrong (Zynq) board returns DRAM that mimics
  a CSR value.
- **U-Boot `reset` reconfigures the fabric from the SD/SDM source**, dropping a
  JTAG-loaded SOF; the HPS won't `reboot` from Linux. To re-test, re-JTAG-program.

---

## 6. Tickets / capabilities

| ref | what |
|---|---|
| DDR-P3.20 | declarative `linux.hps_fpga_bridges` → `bridge enable` in the BOOTCMD (CLI.200) |
| RTL-P3.682 / DDR-P3.21 | sideband-forcing custom f2sdram_adapter (`AxUSER=0xE0`) — silicon-validated (HPS.2) |
| RTL-P3.778 | `rr linux sof` — embed the SPL into the HPS `.sof` (CLI.202) |
| RTL-P3.777 | closure-preflight false-positive on library-overridden (`olo`) packages |
| RTL-P3.722 | io96b EMIF as a frozen-by-file board fact (`hps_emif`) |

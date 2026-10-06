# Zybo Classic (xc7z010) ddr_stream_axi validator — bring-up ladder

Pure-`rr`-flow bring-up for the **in-fabric** hardware validator on the original
Digilent Zybo (PB200-279, `xc7z010`). Sibling of
`example/zybo_z7_20_ddr_validator/docs/BRING_UP.md`.

The **primary deliverable** of this example is the **in-fabric round-trip
verdict** (does `ddr_stream_axi`'s write→DDR→read loop pass word-for-word on the
`xc7z010`?), read over **JTAG/XSDB** — it needs **no Linux**. The Linux path
(v1, mirrors the z020) is optional and secondary.

Everything goes through `rr`. No `build_project.tcl`, no hand-listed
`sources.syn`, no `create_ip`. The block design is a generation recipe
(`tcl_hooks/gen_bd.tcl`); LED pins come from `routertl/boards/zybo/pinout.yml`
via inferred pinout subscription — no hand-written pin XDC.

---

## What this validates (and why in-fabric pins the IDP-T2.5 finding)

The BIST pushes a counter pattern `1..N` through the IP's **write** plane into
PS DDR3 (`m_axi4` → `S_AXI_HP0`), the IP reads it back through its **read**
plane (`S_AXI_HP0` → `m_axi4` → `M_AXIS`), and the BIST compares word-for-word
**in fabric**. The payload **never reaches the CPU** — `S_AXI_HP0` is a
non-coherent HP port straight to the DDR controller. So the in-fabric verdict
isolates the IP's DDR datapath from the PS-readback path that IDP-T2.5 saw
scattered. (No `mem=` clamp is needed for the in-fabric XSDB run — nothing on
the CPU touches the ring; the clamp only matters for the optional Linux path.)

---

## Bench facts (Classic on host `magic`)

- Board: original Digilent Zybo, JTAG cable serial (from rr hardware doctor). The
  bench has 3 boards — scope every `rr program` / XSDB to this serial.
- Stays powered between runs (`reference_zybo_no_poweroff`).
- A wedged DAP recovers by selecting the FPGA TAP then `rst -srst` (no
  power-cycle).
- Builds MUST be cgroup-contained (RTL-P2.737 admission daemon SIGKILLs
  uncontained vivado at ~2 s, exit 247). Preflight:
  `systemd-run --user --scope --quiet true`. Retry a stage on exit 3
  (admission-refused) or 247 (PSI oomd kill) — both are transient contention.

---

## v0 — in-fabric verdict over JTAG/XSDB (primary)

### Stage 1 — build the bitstream (contained, staged)

```bash
source /tools/Xilinx/Vivado/2024.1/settings64.sh
export XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
cd example/zybo_classic_ddr_validator
rr pkg add --local ../.. --name ddr_stream_axi   # one-time: vendors IP + scoped CDC xdc
rr ws update
rr synth run            # gen_bd enables GP0/HP0, sets FCLK1=100, applies read_xdc -ref
rr implementation run   # routed sign-off; confirm WNS >= 0 at 100 MHz on the z010
rr bitstream run
```

### Stage 2 — program over JTAG (scope to the Classic)

```bash
rr hardware doctor                       # confirm cable serial present
rr program run --jtag-serial <serial>
rr program verify
```

### Stage 3 — drive a run and read the verdict via XSDB

To run the packaged script instead of typing the commands below, set the board
cable serial: `RR_JTAG_SERIAL=<serial> xsdb scripts/run_verdict_xsdb.tcl`
(or pass it as the first argument). Without it the script refuses to start, so
it can never act on another board on the chain.

The design only starts when `CTRL.start` is pulsed. With no host UART, drive it
straight from XSDB through the PS `M_AXI_GP0` master (the CSR is at
`0x4000_0000`). XSDB reaches PL-AXI through the APU after `ps7_init`:

```tcl
# xsdb
connect
targets -set -filter {name =~ "*Cortex-A9 #0*"}
rst -system
after 200
# bring up the PS so M_AXI_GP0 can reach the CSR
source <impl>/ps7_init.tcl ; ps7_init ; ps7_post_config
# (the .bit is already loaded by `rr program run`)
targets -set -filter {name =~ "*Cortex-A9 #0*"}

# --- IDENT sanity probe (expect 0x0DD50200) ---
mrd -force 0x4000003C

# --- configure + launch scenario 0 (roundtrip), N = 8192 words ---
mwr -force 0x4000000C 8192        ;# N_WORDS
mwr -force 0x40000008 0           ;# SCENARIO = 0 (roundtrip)
mwr -force 0x40000000 0x1         ;# CTRL.start (W1P)

# --- poll STATUS (0x04): bit1 done, bit2 pass, bit3 fail ---
after 50
mrd -force 0x40000004             ;# STATUS — expect 0x...6 (done|pass) on PASS
mrd -force 0x40000010             ;# ERROR_COUNT  — expect 0 on PASS
mrd -force 0x40000014             ;# WORDS_WRITTEN — expect 8192
mrd -force 0x40000018             ;# WORDS_READ    — expect 8192
# on FAIL, the first-mismatch triple tells you WHERE it diverged:
mrd -force 0x4000001C             ;# FIRST_ERR_INDEX
mrd -force 0x40000020             ;# FIRST_ERR_EXP
mrd -force 0x40000024             ;# FIRST_ERR_GOT
```

**The verdict:**

| Observation (LEDs / STATUS) | Meaning |
|---|---|
| LD1+LD2 lit, LD3 dark; STATUS bit2=1, ERROR_COUNT=0, WORDS_*=N | **PASS in-fabric** → the IP's DDR datapath is correct on the `z010`; the IDP-T2.5 scatter is specific to the **PS-readback path**. |
| LD1+LD3 lit, LD2 dark; STATUS bit3=1, ERROR_COUNT>0 | **FAIL in-fabric** → the bug is in the **IP/HP/DDR/part interaction itself**; FIRST_ERR_* localizes the first divergent word. |
| WORDS_WRITTEN or WORDS_READ == 0 even with "pass" | treat as FAIL — the BIST's adversarial floor catches a no-traffic green. |

Record the verdict per `hw-bitstream-confirm`: confirm the PL runs the built bit
(`rr hw bit-sha`) before trusting the readback.

---

## RESULT — 2026-06-22, board xc7z010clg400-1

**The in-fabric round trip PASSES on the Classic xc7z010.** Run via
`RR_JTAG_SERIAL=<serial> xsdb scripts/run_verdict_xsdb.tcl` (the serial is
required, from `rr hardware doctor`; the script refuses without it) after
`rr program run --cable <serial>`
(build stamp 0x145cd9ab confirmed live via USR_ACCESS):

| Scenario | N | STATUS | ERROR_COUNT | WORDS_WR / WORDS_RD | FIRST_ERR_INDEX | Verdict |
|---|---|---|---|---|---|---|
| 0 roundtrip | 256 / 4096 / 8192 | 0x36 (done+pass+idles) | 0 | N / N | 0xFFFFFFFF | **PASS** |
| 1 boundary  | 65536 / 262144    | 0x36                   | 0 | N / N | 0xFFFFFFFF | **PASS** |
| (N=0)       | 0                 | 0x32 (done, no pass/fail) | 0 | 0 / 0 | — | SKIP |
| 3 pingpong stub | 1024          | 0x32                   | 0 | 0 / 0 | — | SKIP |

IDENT read back `0x0DD50200` (CSR alive). The N=0 and scenario-3 SKIP cases
return `done=1, pass=0, fail=0` — proving the `pass` latch is **traffic-gated,
not stuck high** (the adversarial floor works: pass requires WORDS_WRITTEN ==
WORDS_READ == N AND ERROR_COUNT == 0). Build: WNS **+0.811 ns** @ 100 MHz,
WHS +0.033 ns, 4436 LUTs (25.2% of the z010), 4 BRAM, 2 BUFG.

**Conclusion (the IDP-T2.5 question):** the IP's write→DDR→read datapath is
**correct on the xc7z010**. The DDR corruption IDP-T2.5 saw (scattered,
64-bit-half-interleaved) is therefore **specific to the PS-readback path**
(the CPU/cache/address-view reading DDR back), NOT the `ddr_stream_axi` write
plane or the HP0/DDR/part interaction. The 16-beat AXI3 burst cap
(`max_burst_len_g`) was correctly found irrelevant to that scatter.

---

## v1 — Linux validator app (optional, mirrors z020)

Identical to the z020 sibling's v1 (`rr linux xsa --refresh` → `dtb` → `fsbl` →
`uboot` → `kernel` → `rootfs` → `bootscr` → `sdcard` → `lock`, then
`./ddr_validator --scenario roundtrip --n 8192` on target). For the Classic the
`mem=256M` clamp in `sw/linux/boot.txt` carves the ring window out of kernel
RAM. Work any boot-chain symptom through the **zynq7-sd-boot-bringup** 8-gate
ladder. Note: on the Classic the L16 PL clock + PS ENET0 both require
`eth_rst_b (E17)` driven HIGH (RTL-P3.880) — not relevant to this PS-DDR /
PS-UART design, but relevant if you add PL-clocked or Ethernet features.
```

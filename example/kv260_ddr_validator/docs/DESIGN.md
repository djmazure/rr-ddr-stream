# kv260_ddr_validator — design spec

A **hardware validator** for `ddr_stream_axi` on the AMD Kria KV260 (Zynq
UltraScale+ MPSoC, `xck26-sfvc784-2LV-c`). It exercises, on real PS DDR4, the
same scenarios the cocotb suite drives against the `cocotbext-axi` `AxiRam`
model — round-trip integrity, burst-scale stress, and reset recovery — and
reports pass/fail over an AXI-Lite CSR readable from Linux (`/dev/mem`) **or**
JTAG (`mrd 0xA0000004`).

This is the ZynqMP sibling of `../zybo_z7_20_ddr_validator`: a **declarative,
pure-`rr`-flow** example (no `build_project.tcl`, no hand-listed `sources.syn`,
no `create_ip`). The block design is a *generation recipe*
(`tcl_hooks/gen_bd.tcl`, RTL-P3.506 model), not a committed `.bd`. The BD recipe
is distilled from the silicon-proven `kv260_linux/tcl/hooks/kv260_ps_bd.tcl`.

---

## 1. Why PS DDR via S_AXI_HP0_FPD

The IP is a DDR-mastering DMA. To validate it on silicon we need a real memory
controller. On the KV260 the PS *is* the DDR4 controller; the PL reaches it
through a Full-Power-Domain High-Performance AXI slave port
(`S_AXI_HP0_FPD`). So `ddr_stream_axi`'s `m_axi4_*` master drives
`S_AXI_HP0_FPD`, and the PS DDR4 is the ring-buffer backing store. No MIG, no
external memory IP. HP (not HPC) — the ring is non-coherent and the in-fabric
BIST is the only producer/consumer, so the A53 caches are bypassed entirely.

## 2. Three things that differ from the Zynq-7 validator

ZynqMP is not a drop-in retarget of the PS7 example. The deltas:

| | Zynq-7 (`zybo_z7_20`) | KV260 (ZynqMP) |
|---|---|---|
| PS cell | `processing_system7` | `zynq_ultra_ps_e` |
| DDR / MIO pins | `DDR_*`/`FIXED_IO_*` **inout top ports** | PS-**internal** (not on PL top) |
| HP slave | `S_AXI_HP0` (knob `PCW_USE_S_AXI_HP0`) | `S_AXI_HP0_FPD` (knob `PSU__USE__S_AXI_GP2`) |
| AXI-Lite master | `M_AXI_GP0` | `M_AXI_HPM0_FPD` (knob `PSU__USE__M_AXI_GP0`) |
| CSR base | `0x4000_0000` | `0xA000_0000` |
| HP burst | AXI3 → 16-beat clamp | AXI4 → 256-beat (IP default) |
| Clocks | FCLK0 100 + FCLK1 125 (2 domains) | `pl_clk0` 100 (1 domain) |
| Verdict | `led[3:0]` (autonomous) | **CSR-only** — KV260 SOM has no user LEDs on PL pins |

Consequences captured in the RTL:

1. **The top is PORT-LESS.** All I/O (DDR4, MIO, peripherals) is inside the PS
   block design; there are no PL pins to bring out. `top_kv260_ddr_validator`
   has no `port` clause — legal VHDL, synthesises fine.
2. **The verdict is CSR-only.** No LEDs. STATUS bits `[2]=pass / [3]=fail` are
   read over `/dev/mem` (Linux) or `mrd 0xA0000004` (JTAG).
3. **The BD always exposes HP0 + GP0.** In `kv260_linux` that wiring sits behind
   the `KV260_FBDMA` h264 flag; a bit built without it has an *unwired* PS-only
   BD whose CSR reads HANG (the `hazard_kv260_ps_only_bd_unwired_csr_hang`
   scar). Here the route is unconditional — a validator that can't reach its CSR
   is useless.

## 3. Clock / reset topology (single domain)

```
zynq_ultra_ps_e  pl_clk0 = 100 MHz  -> fab_clk  -> clk_sys == clk_ddr
                 pl_resetn0         -> proc_sys_reset -> fab_aresetn -> rst (active-high to the IP)
```

The IP's only fabric clock-domain crossing (`i_ddr_fifo_intf`'s olo async
FIFOs) is degenerate here (`clk_sys == clk_ddr`) but functionally valid — the
async FIFO simply never sees a phase difference. The IP-shipped scoped
RR--P3.8 CDC constraint still applies (and is benign at single-clock).

## 4. Block diagram

```
        ┌──────────────────────── PL (this example) ────────────────────────┐
 ZynqMP │ M_AXI_HPM0_FPD ─► smartconnect ─► M_AXI_GP0 ─► ddr_validator_csr   │
 PS (BD)│                                         │  ctrl/status/scenario    │
        │                                         ▼                          │
        │                                 ddr_validator_bist  (fab_clk)      │
        │                                  │ S_AXIS (pattern)  ▲ M_AXIS (rd)  │
        │                                  ▼                   │            │
        │                               ddr_stream_axi ── m_axi4 ──┐        │
        │      auto_start ─► (one-shot) ─► start                    │        │
        │                                                          ▼        │
 ZynqMP │ S_AXI_HP0_FPD ◄─ smartconnect ◄─ record→flat shim ◄──── m_axi4    │
 PS DDR4│                                                                   │
        │  verdict ─► CSR STATUS[2:3]  (no LEDs — read over /dev/mem | JTAG) │
        └────────────────────────────────────────────────────────────────────┘
```

- **PS + smartconnects + HP/GP wiring** live in the BD recipe (`gen_bd.tcl`).
- **Everything custom** (`ddr_validator_csr`, `ddr_validator_bist`,
  `ddr_stream_axi`, the record→flat AXI shim, the auto-start one-shot) lives in
  `src/top_kv260_ddr_validator.vhd`.

## 5. CSR map (AXI-Lite, 64 KiB @ `0xA000_0000`)

Identical register layout to the Zynq-7 validator (the CSR RTL is byte-identical
logic). Word-addressed, offsets in bytes. `W1P` = write-1-to-pulse.

| Off  | Name              | Acc | Bits / meaning |
|------|-------------------|-----|----------------|
| 0x00 | `CTRL`            | RW  | [0] start (W1P) · [1] soft_reset (W1P) · [8] continuous |
| 0x04 | `STATUS`          | R   | [0] running · [1] done · [2] pass · [3] fail · [4] write_idle · [5] read_idle |
| 0x08 | `SCENARIO`        | RW  | [3:0] scenario id (see §6) |
| 0x0C | `N_WORDS`         | RW  | user-words to push this run |
| 0x10 | `ERROR_COUNT`     | R   | mismatches observed this run |
| 0x14 | `WORDS_WRITTEN`   | R   | S_AXIS beats accepted |
| 0x18 | `WORDS_READ`      | R   | M_AXIS beats captured |
| 0x1C | `FIRST_ERR_INDEX` | R   | index of first mismatch (else 0xFFFFFFFF) |
| 0x20 | `FIRST_ERR_EXP`   | R   | expected word at first mismatch |
| 0x24 | `FIRST_ERR_GOT`   | R   | observed word at first mismatch |
| 0x28 | `WR_BASE`         | RW  | DDR offset base, write ring (ping-pong, DDR-P3.11) |
| 0x2C | `RD_BASE`         | RW  | DDR offset base, read ring |
| 0x30 | `BASE_LOAD`       | RW  | [0] load WR_BASE (W1P) · [1] load RD_BASE (W1P) |
| 0x34 | `PTR_RESET`       | RW  | [0] wr ptr reset · [1] rd ptr reset (W1P) — wired when DDR-P3.11 lands |
| 0x3C | `IDENT`           | R   | `0x0DD5_0200` (magic `DDS` + IP version 0.2.0) — host sanity probe |

## 6. Scenarios (mirror the cocotb suite)

| id | name             | what it does on HW |
|----|------------------|--------------------|
| 0  | `roundtrip`      | push counter 1..N through S_AXIS to DDR, read back via M_AXIS, compare word-for-word |
| 1  | `boundary`       | N large enough to span many bursts; same compare (4 KiB compliance is structural in `burst*_setter`) |
| 2  | `reset_recovery` | start a run, pulse `CTRL.soft_reset` mid-traffic, confirm STATUS drains clean and a fresh run passes |
| 3  | `pingpong`       | two DDR windows via WR_BASE/RD_BASE + PTR_RESET. **Stubbed until DDR-P3.11 lands.** |

> Adversarial intent carries over (`feedback_test_success_is_bugs_found`): a run
> that reports `pass` with `WORDS_WRITTEN==0` or `WORDS_READ==0` is a **fail**.
> The BIST asserts non-zero traffic floors before declaring pass; the host app
> re-asserts the same floor so a wiring regression can't fake a pass.

## 7. Autonomous verdict — auto-start (the KV260 LED replacement)

The Zybo encodes its verdict on `led[3:0]` so a bring-up needs no host. KV260
has no user LEDs, **and** its Linux bring-up is bench-flaky — so this example
makes the BIST **self-run on configuration**:

- `auto_start_g` (default `true`): after `pl_resetn0` releases and both IP
  planes report idle, a one-shot fires a single-cycle `start`. The default
  `roundtrip` scenario (SCENARIO reset value 0) runs `eff_n_words` words. Note
  the CSR's `N_WORDS` register **resets to 8192** (a non-zero, sensible
  default), so the auto run uses 8192; `auto_n_words_g` (4096) is only the
  fallback for the degenerate case where a host has written `N_WORDS=0`. The
  verdict latches in CSR STATUS with **no host interaction** — silicon-verified
  PASS (STATUS=0x36, ERR=0, 8192/8192 words; see BRING_UP.md §1).
- A host that drives the CSR overrides it: `eff_start = csr_start | auto_pulse`,
  and `eff_n_words` takes the host's `N_WORDS` register once it is written
  non-zero (else the auto default).

So straight after `rr program run`, a JTAG `mrd 0xA0000004` reads the verdict:
`bit2=1` → PASS, `bit3=1` → FAIL. This is the primary silicon-validation path
when Linux is unavailable (see BRING_UP.md).

## 8. RR--P3.8 — scoped CDC constraint shipped with the IP

The project carries **zero timing XDC**. The IP's only fabric CDC
(`i_ddr_fifo_intf`'s olo async FIFOs) is constrained by a scoped, cell-relative
`set_max_delay -datapath_only` + reset `set_false_path` that the IP itself
ships (`../../constraints/ddr_stream_cdc_scoped.xdc`, declared in the IP's
`ip.yml build.sources.xdc`). `rr pkg add --local` vendors it into `libs/` and
records it in `ip.lock`; the build emits `read_xdc -ref ddr_stream_axi`. At
single-clock it is benign; it is retained so the example stays a faithful
consumer of the IP's shipped constraints.

## 9. Files

```
example/kv260_ddr_validator/
  project.yml                       declarative: board, auto_sources, linux, BD hook, mmap, lint
  src/
    top_kv260_ddr_validator.vhd     PL top (PORT-LESS): BD + IP + BIST + CSR + record→flat shim + auto-start
    ddr_validator_bist.vhd          self-checking pattern engine (byte-identical to the Zynq-7 BIST)
    ddr_validator_csr.vhd           AXI-Lite slave, CSR map per §5 (byte-identical to the Zynq-7 CSR)
  tcl_hooks/
    gen_bd.tcl                      BD recipe: zynq_ultra_ps_e (kv260 preset) + proc_sys_reset + 2× smartconnect, HP0 + GP0
  sw/linux/
    ddr_validator.c                 userspace app: mmap CSR @ 0xA000_0000, run scenarios, print verdict
    system_memory_map.yml           0xA000_0000 CSR window for `rr linux mmap` (target: zynqmp)
  docs/
    DESIGN.md                       (this file)
    BRING_UP.md                     rr-command-driven bring-up ladder (build + JTAG + Linux)
```

## 10. Build / bring-up flow (all `rr`)

```bash
rr ws update                       # subscribe sources; vendored IP + its scoped CDC xdc
rr synth run                       # gen_project runs gen_bd.tcl (zynq_ultra_ps_e + HP0 + GP0)
rr implementation run              # routed sign-off
rr bitstream run
# Silicon — primary (autonomous, no Linux):
rr program run                     # configure PL; auto-start BIST self-runs
#   then JTAG:  mrd 0xA0000004   ->  bit2 set = PASS
# Silicon — full app path (when Linux boots): see BRING_UP.md
./ddr_validator --scenario roundtrip --n 8192   # exits 0 on pass
```

**Acceptance gates:**
1. `rr implementation run` closes timing with the IP-shipped scoped RR--P3.8
   constraint and zero project XDC.
2. On hardware, the auto-start `roundtrip` run latches STATUS `pass` with
   non-zero `WORDS_WRITTEN/READ` → validates the IP end-to-end on real DDR4.
3. Scenarios 0–2 driven over CSR (JTAG or Linux) report `pass`.
4. Scenario 3 (`pingpong`) unstubs once DDR-P3.11 ships the ptr-reset CSR.

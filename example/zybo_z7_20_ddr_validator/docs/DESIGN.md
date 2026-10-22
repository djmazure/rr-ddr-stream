# zybo_z7_20_ddr_validator — design spec

A **hardware validator** for `ddr_stream_axi` on the Digilent Zybo Z7-20
(`xc7z020clg400-1`). It exercises, on real PS DDR3, the same scenarios the
cocotb suite (`sim/cocotb/tests/test_ddr_stream_axi.py`) drives against the
`cocotbext-axi` `AxiRam` model — round-trip integrity, 4 KiB-boundary stress,
and reset recovery — and reports pass/fail both via on-board LEDs (autonomous)
and a Linux userspace app over an AXI-Lite CSR (rich verdict).

This is the `rr-ddr-stream` peer of `vhdl-ethernet/example/zybo_rmii`: a
**declarative, pure-`rr`-flow** example (no `build_project.tcl`, no hand-listed
`sources.syn`, no `create_ip`). The block design is a *generation recipe*
(`tcl_hooks/gen_bd.tcl`, RTL-P3.506 model), not a committed `.bd`.

---

## 1. Why PS DDR via S_AXI_HP0

The IP is a DDR-mastering DMA. To validate it on silicon we need a real memory
controller. On Zynq-7000 the PS *is* the DDR3 controller; the PL reaches it
through a High-Performance AXI slave port (`S_AXI_HP0`). So `ddr_stream_axi`'s
`m_axi4_*` master drives `S_AXI_HP0`, and the PS DDR is the ring-buffer backing
store. No MIG, no external memory IP — matches deepskopion's proven topology
(the first fielded consumer, `DS-P1.9` / `DS-T10`).

## 2. Clock / reset topology (mirrors deepskopion)

```
PS7  FCLK_CLK0 = 100 MHz  -> clk_sys  (S_AXIS/M_AXIS user side + AXI-Lite CSR)
     FCLK_CLK1 = 125 MHz  -> clk_ddr  (m_axi4 master + S_AXI_HP0 DMA)
     FCLK_RESET0_N        -> proc_sys_reset -> rst_sys (active-high to the IP)
```

The only fabric clock-domain crossing is **inside the IP** (`i_ddr_fifo_intf`'s
olo async FIFOs). That crossing is exactly what RR--P3.8 must constrain — see §7.

## 3. Block diagram

```
        ┌──────────────────────── PL (this example) ────────────────────────┐
        │                                                                    │
 PS7 ───┤ M_AXI_GP0 ─► axi_interconnect ─► ddr_validator_csr (AXI-Lite slave)│
 (BD)   │                                      │  ctrl/status/scenario        │
        │                                      ▼                              │
        │                              ddr_validator_bist  (clk_sys)          │
        │                                 │ S_AXIS (pattern)   ▲ M_AXIS (rdbk) │
        │                                 ▼                    │              │
        │                              ddr_stream_axi  ── m_axi4 ──┐          │
        │                                 (clk_sys + clk_ddr)      │          │
        │                                                          ▼          │
 PS7 ◄──┤ S_AXI_HP0 ◄──────────── axi record→flat shim ───────── m_axi4       │
 (DDR3) │                                                                     │
        │  LEDs ◄── led_status (LD0..3 + RGB)                                 │
        └────────────────────────────────────────────────────────────────────┘
```

- **PS7 + interconnect + HP/GP wiring** live in the BD recipe (`gen_bd.tcl`).
- **Everything custom** (`ddr_validator_csr`, `ddr_validator_bist`,
  `ddr_stream_axi`, the record→flat AXI shim, LED logic) lives in
  `src/top_zybo_z7_20_ddr_validator.vhd` — RTL-first, BD stays minimal.

## 4. CSR map (AXI-Lite, 4 KiB @ `0x4000_0000`)

The single source of truth for the register bank is `project_regbank.yml` at the
example root. Addresses in the schema and RTL are word indices (`address_mode: words`,
32-bit registers). Byte offsets are obtained by multiplying the word index by 4
(e.g., `STATUS` byte `0x04` is word index 1; `IDENT` byte `0x3C` is word index 15).
`W1P` = write-1-to-pulse (self-clearing strobe, reads 0).

| Off  | Word | Name              | Acc | Bits / meaning |
|------|------|-------------------|-----|----------------|
| 0x00 | 0    | `CTRL`            | RW  | [0] start (W1P) · [1] soft_reset (W1P) · [8] continuous |
| 0x04 | 1    | `STATUS`          | R   | [0] running · [1] done · [2] pass · [3] fail · [4] write_idle · [5] read_idle |
| 0x08 | 2    | `SCENARIO`        | RW  | [3:0] scenario id (see §5) |
| 0x0C | 3    | `N_WORDS`         | RW  | user-words to push this run (default 8192) |
| 0x10 | 4    | `ERROR_COUNT`     | R   | mismatches observed this run |
| 0x14 | 5    | `WORDS_WRITTEN`   | R   | S_AXIS beats accepted |
| 0x18 | 6    | `WORDS_READ`      | R   | M_AXIS beats captured |
| 0x1C | 7    | `FIRST_ERR_INDEX` | R   | index of first mismatch (else 0xFFFFFFFF) |
| 0x20 | 8    | `FIRST_ERR_EXP`   | R   | expected word at first mismatch |
| 0x24 | 9    | `FIRST_ERR_GOT`   | R   | observed word at first mismatch |
| 0x28 | 10   | `WR_BASE`         | RW  | DDR offset base, write ring (ping-pong, DDR-P3.11) |
| 0x2C | 11   | `RD_BASE`         | RW  | DDR offset base, read ring |
| 0x30 | 12   | `BASE_LOAD`       | RW  | [0] load WR_BASE (W1P) · [1] load RD_BASE (W1P) |
| 0x34 | 13   | `PTR_RESET`       | RW  | [0] wr ptr reset · [1] rd ptr reset (W1P) — wired when DDR-P3.11 lands |
| 0x3C | 15   | `IDENT`           | R   | `0x0DD50200` (magic `DDS` + IP version 0.2.0) — host sanity probe |

### 4.1 Relationship to the 4 KiB System Window

The system memory map (`sw/linux/system_memory_map.yml`) assigns the validator CSR
a 4 KiB subordinate aperture on PS M_AXI_GP0:
- `base_addr: 0x40000000`
- `span: 0x1000` (4096 bytes = 1024 32-bit words)

In `project_regbank.yml`:
- `base_address: 0x40000000`
- `addr_width: 12` (representing the 12-bit address bus of the 4 KiB aperture, $2^{12} = 4096$ bytes = 1024 words)

The 15 decoded registers occupy word indices 0 through 15 (byte offsets `0x00` through `0x3C`,
active footprint 64 bytes). Word indices 16 through 1023 (byte offsets `0x040` through `0xFFC`)
within the 4 KiB window are unmapped in RTL and return 0 on read (`when others => r_data_r <= (others => '0')`).

### 4.2 CLI Commands and Rendered HTML Inspection

To validate the schema and generate the HTML register documentation from the example root:

```bash
cd example/zybo_z7_20_ddr_validator
rr regbank validate project_regbank.yml
rr regbank html project_regbank.yml --output register-map.html
```

**Inspection of rendered `register-map.html`:**
- Displays all 15 registers spanning offsets `0x00` to `0x3C` under base `0x40000000` (`version: 0.2.0`).
- Bit-level layouts accurately render fields for:
  - `CTRL`: `START` [0], `SOFT_RESET` [1], `CONTINUOUS` [8]
  - `STATUS`: `RUNNING` [0], `DONE` [1], `PASS` [2], `FAIL` [3], `WRITE_IDLE` [4], `READ_IDLE` [5]
    *(Note: reset value `0x00000030` reflects the initial combinational state of `write_idle_i` and `read_idle_i` being high when the IP is idle).*
  - `SCENARIO`: `ID` [3:0] with enum table (0: `ROUNDTRIP`, 1: `BOUNDARY`, 2: `RESET_RECOVERY`, 3: `PINGPONG`)
  - `N_WORDS`, `ERROR_COUNT`, `WORDS_WRITTEN`, `WORDS_READ`, `FIRST_ERR_INDEX`, `FIRST_ERR_EXP`, `FIRST_ERR_GOT`, `WR_BASE`, `RD_BASE`: [31:0]
  - `BASE_LOAD`: `WR_BASE_LOAD` [0], `RD_BASE_LOAD` [1]
  - `PTR_RESET`: `WR_PTR_RESET` [0], `RD_PTR_RESET` [1]
  - `IDENT`: `VERSION` [15:0] (`0x0200`) and `MAGIC` [31:16] (`0x0DD5`)
- Due to RouteRTL schema limitation (§4.3), pulse fields show "RW" badges in the table, with write-1-to-pulse semantics noted in their descriptions.

### 4.3 RouteRTL Schema Gap / Limitation: W1P Pulse Fields

RouteRTL regbank schema (`SIDE_EFFECT_KINDS: w1c | rc | latch_clear | none`) currently lacks
native support for write-1-to-pulse (`w1p` / strobe) fields.
In RTL (`src/ddr_validator_csr.vhd`), pulse fields:
- `CTRL.START` [bit 0]
- `CTRL.SOFT_RESET` [bit 1]
- `BASE_LOAD.WR_BASE_LOAD` [bit 0]
- `BASE_LOAD.RD_BASE_LOAD` [bit 1]
- `PTR_RESET.WR_PTR_RESET` [bit 0]
- `PTR_RESET.RD_PTR_RESET` [bit 1]

are single-cycle self-clearing strobes: writing 1 asserts the pulse for one clock cycle;
reads return 0 (`CTRL` reads `start_p_r`/`soft_reset_p_r` as 0; `BASE_LOAD` and `PTR_RESET`
fall through to `when others => r_data_r <= (others => '0')`).

Because `w1p` cannot be encoded in the RouteRTL schema, these fields are declared with
`access: rw` without fake side-effect declarations (`side_effect: none` or `latch_clear`
would be misleading). Consequently, generated HTML and C headers render them as RW.
**RouteRTL gap report:** RouteRTL regbank schema and generator should add first-class `w1p`
support (or `access: w1p` / `side_effect: w1p`) to properly generate strobe logic and
accurate documentation.

### 4.4 Cross-Board Example Register Map Relationships

Relationship between this schema and other board examples in `rr-ddr-stream`:
- `zybo_classic` (`example/zybo_classic_ddr_validator`):
  Shares the identical 15-register CSR map (same offsets, decode logic, base `0x40000000`, span `0x1000`).
- `kv260` (`example/kv260_ddr_validator`):
  Shares the same 15-register CSR decode/layout, but mapped at base `0xA0000000`,
  span `0x10000` (PS S_AXI_HP0_FPD DDR4).
- `de25` (`example/de25_agilex5_ddr_validator`):
  Shares the same 15-register CSR decode/layout, mapped at Agilex 5 HPS lwh2f aperture
  base `0x20000000`, span `0x1000`, with independent AW/W handshakes (DDR-T2.5).
- `pfsoc` (`example/pfsoc_disco_ddr_validator`):
  Extends this CSR map with `C_WR_COMMIT` at word index 14 (byte `0x38`) for durable write
  pointer tracking (`wr_commit_addr_i`).
- `axe5` (`example/axe5_eagle_lpddr4_validator`):
  Userspace app uses an alternative/different register map (`REG_IDENT 0x00`,
  `REG_CONTROL 0x04`, `REG_SCENARIO 0x08`, `REG_N_WORDS 0x0C`, `REG_STATUS 0x10`, `CSR_BASE 0x20000000`).

**Authority Notice:** `project_regbank.yml` is authoritative ONLY for `zybo_z7_20_ddr_validator`
(and verified identical clones like `zybo_classic`). It must not be presented as authoritative
for unverified board variants without target verification.
**Core Notice:** The reusable streaming core `ddr_stream_axi` itself does NOT carry a register
bank; it exposes raw telemetry via the `ddr_csr_o : out fifo_csr_t` port (see [`docs/CSR_CONTRACT.md`](../../docs/CSR_CONTRACT.md)).


## 5. Scenarios (mirror the cocotb suite)

| id | name             | cocotb analogue            | what it does on HW |
|----|------------------|----------------------------|--------------------|
| 0  | `roundtrip`      | `mem_model_demo`           | push counter 1..N through S_AXIS to DDR, read back via M_AXIS, compare word-for-word |
| 1  | `boundary`       | `axi_4k_boundary_test`     | N large enough to span many bursts; same compare. (4 KiB compliance is structural in `burst*_setter`; HW just proves it round-trips at scale) |
| 2  | `reset_recovery` | `reset_attack_test`        | start a run, pulse `CTRL.soft_reset` mid-traffic, confirm STATUS drains clean (no `fail`, no hang) and a fresh run after reset passes |
| 3  | `pingpong`       | (new, DDR-P3.11)           | two DDR windows via WR_BASE/RD_BASE + PTR_RESET; producer fills A while consumer drains B, swap, zero drops. **Stubbed until DDR-P3.11 lands.** |

> Adversarial intent carries over (`feedback_test_success_is_bugs_found`): a run
> that reports `pass` with `WORDS_WRITTEN==0` or `WORDS_READ==0` is a **fail**,
> not a pass — the BIST asserts non-zero traffic floors before declaring pass,
> exactly like the cocotb `stats.*_bursts >= 4` gates.

## 6. LED encoding (autonomous, no host)

Per `rr hardware doctor` interim contract. The four user LEDs LD0–LD3
(`led[0..3]` = M14/M15/G14/D18) carry the verdict:

| LED | meaning |
|-----|---------|
| LD0 | running |
| LD1 | done |
| LD2 | pass (latched until next start) |
| LD3 | fail (latched until next start) |

> Only `led[0..3]` are used. They are sourced from
> `routertl/boards/zybo-z7-20/pinout.yml` by inferred pinout subscription —
> the top's `led` port name matches the registry, so `rr` materializes
> `board_pins.xdc`; **no hand-written pin XDC** (rr-board-pinout-first). The
> RGB LEDs (LD5/LD6) are deliberately NOT used: they are not in the board
> registry yet, and inventing pins violates exact-provenance. Add them to
> `pinout.yml` (from Digilent's Zybo-Z7-Master.xdc, with provenance) before
> wiring an RGB verdict.

## 7. RR--P3.8 — scoped CDC constraint shipped with the IP

The IP's only fabric CDC is `i_ddr_fifo_intf`'s olo async FIFOs
(`i_uploader_fifo` / `i_downloader_fifo` → `olo_base_fifo_async` → `i_rst_cc`
+ gray-pointer `olo_base_cc_bits`). Out of the box Vivado times these as related
clocks and reports a false `WNS` (deepskopion saw `-0.621`; this board saw
`-0.443`). Every consumer hits this. RR--P3.8 is a **scoped, cell-relative**
`set_max_delay -datapath_only` + a `set_false_path` on the olo reset async-preset
(NOT `clock_groups -asynchronous`, NOT clock-name-based), so the IP carries
correct CDC timing into any consumer regardless of its clock names.

**PROVEN here** (this example is RR--P3.8's acceptance harness), developed
synth-in-the-loop against the routed checkpoint: place-and-route `WNS -0.443 →
+0.021` ns, hold met (`WHS +0.052`), `Fmax 95.8 → 125.3` MHz. The selectors are
fixed instance-hierarchy `get_cells` (olo tags `async_reg` as a VHDL boolean, so
the elegant `-filter {ASYNC_REG}` idiom silently misses olo's sync FFs — see
hint H.78 / RTL-P3.602). The constraint does NOT assume consumer clock names
(replaces deepskopion's interim `clk_fpga_0/1` block).

**Canonical home:** `../../constraints/ddr_stream_cdc_scoped.xdc` (IP repo root),
declared in the IP's `ip.yml build.sources.xdc`.
**Shipping (RTL-P2.636, live):** `rr pkg add --local` vendors the file into this
example's `libs/` and records it in `ip.lock` as a `scoped_constraint`
(`ref=ddr_stream_axi`); the build emits `read_xdc -ref ddr_stream_axi <file>`, so
every `get_*` is scoped to the IP instance. **This project carries ZERO timing
XDC** — no `sources.xdc`, no `xdc/` dir. Verified end-to-end (Vivado 2024.1,
routed): `read_xdc -ref` matched **174** endpoint cells; `report_exceptions`
shows `max_dpo=8` on the data crossings + the reset `false_path` applied;
inter-clock paths carry `REQUIREMENT=8.000` with **+6.4 ns** slack; worst path is
a benign same-domain `clk_fpga_1` datapath (+0.001). See §9.

## 8. Files

```
example/zybo_z7_20_ddr_validator/
  project.yml                         declarative: board, auto_sources, linux, BD hook, mmap, lint
  src/
    top_zybo_z7_20_ddr_validator.vhd  PL top: BD wrapper + IP + BIST + CSR + record→flat AXI shim + LEDs
    ddr_validator_bist.vhd            self-checking pattern engine (clk_sys), scenarios per §5
    ddr_validator_csr.vhd             AXI-Lite slave, CSR map per §4
  tcl_hooks/
    gen_bd.tcl                        BD recipe: PS7 (zybo-z7-20 preset) + 2× proc_sys_reset + 2× smartconnect + GP0/HP0
  sw/linux/
    ddr_validator.c                   userspace app: mmap CSR, run scenarios, print verdict
    system_memory_map.yml             0x4000_0000 CSR window for `rr linux mmap`
    boot.txt                          u-boot boot script source (carries mem=256M ring clamp)
  docs/
    DESIGN.md                         (this file)
    BRING_UP.md                       rr-command-driven bring-up ladder
```

> No `constraints/` or `xdc/` dir, no `sources.xdc`: the IP's RR--P3.8 CDC
> constraint is auto-inherited (vendored into `libs/` + applied `read_xdc -ref`
> from `ip.lock`, RTL-P2.636), and LED pins are materialized from the board
> registry by pinout subscription (§6). **The project carries zero XDC.**

## 9. Build / bring-up flow (all `rr`)

```bash
rr pkg add --local ../.. --name ddr_stream_axi   # vendors IP + its scoped CDC xdc (RTL-P2.636)
rr ws update                       # subscribe sources; build_env.tcl gets g_scoped_xdc from ip.lock
rr synth run                       # gen_project applies read_xdc -ref ddr_stream_axi (zero project XDC)
rr implementation run              # routed sign-off: WNS +0.001 ns, CDC paths +6.4 ns, Fmax 125 MHz
rr bitstream run
# Linux (full validator app path — see BRING_UP.md + zynq7-sd-boot-bringup skill)
rr linux xsa --refresh && rr linux <dtb|fsbl|uboot|kernel|rootfs|bootscr|sdcard>
# on target:
./ddr_validator --scenario roundtrip --n 8192   # exits 0 on pass, prints verdict
```

**Acceptance gates:**
1. `rr implementation run` closes timing on the IP's async-FIFO CDC with the
   IP-shipped scoped RR--P3.8 constraint and **zero project XDC** (WNS +0.001 ns;
   CDC crossings governed by REQUIREMENT=8.000 with +6.4 ns slack) — **done**.
   RR--P3.8 closed in full: constraint inherited from `ip.yml` via RTL-P2.636.
2. On hardware, scenarios 0–2 report `pass` with non-zero traffic → validates the
   IP end-to-end on real DDR; LEDs encode the same verdict.
3. Scenario 3 (`pingpong`) unstubs and passes once DDR-P3.11 ships the ptr-reset CSR.

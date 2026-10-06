# zybo_classic_ddr_validator — design spec

A **hardware validator** for `ddr_stream_axi` on the **ORIGINAL Digilent Zybo**
(Classic, PB200-279 Rev B, `xc7z010clg400-1`). It is the `xc7z010` sibling of
`example/zybo_z7_20_ddr_validator` (`xc7z020`): same IP, same in-fabric BIST,
same record→flat HP0 shim, same CSR + LED verdict — only the part, board preset,
and the HP0 clock frequency differ.

## 0. Why a Classic sibling exists (this is a live diagnostic)

The ring/HP0 write path of `ddr_stream_axi` had **never been validated on the
`xc7z010`**. A sibling effort (**IDP-T2.5**, repo `rr-idp`) found that the IP's
**write plane** onto a Zynq-7 `S_AXI_HP0` **corrupts DDR on the Classic**
(DDR reads back a scattered, 64-bit-half-interleaved image) while the SAME IP
works on the `z020`. Capping the IP's bursts at 16 (AXI3/HP legal max, the
rr-idp `max_burst_len_g` generic, DDR-P2.5) did **not** fix the scatter.

This validator is the **cleanest correctness check** for that finding, because
it compares **word-for-word IN FABRIC** and **avoids the PS-readback path**
entirely:

- If the in-fabric round trip **FAILS** on the Classic → the corruption is in
  the IP / HP / DDR / part interaction itself (pins the bug in the datapath).
- If the in-fabric round trip **PASSES** on the Classic → the corruption is
  specific to the **PS-readback path** (CPU/cache/coherency/address-view on the
  `z010`), not the IP's write into DDR.

Either way the verdict is decisive. See `docs/BRING_UP.md` for the exact
in-fabric (LED + XSDB CSR) readout that produces it.

---

## 1. Why PS DDR via S_AXI_HP0

The IP is a DDR-mastering DMA. On Zynq-7000 the PS *is* the DDR3 controller;
the PL reaches it through a High-Performance AXI slave port (`S_AXI_HP0`). So
`ddr_stream_axi`'s `m_axi4_*` master drives `S_AXI_HP0`, and the PS DDR3
(`MT41K128M16`, 32-bit, 525 MHz on the Classic) is the ring backing store. No
MIG, no external memory IP — matches deepskopion's proven topology.

## 2. Clock / reset topology

```
PS7  FCLK_CLK0 = 100 MHz  -> clk_sys  (S_AXIS/M_AXIS user side + AXI-Lite CSR)
     FCLK_CLK1 = 100 MHz  -> clk_ddr  (m_axi4 master + S_AXI_HP0 DMA)   ← 100, NOT 125
     FCLK_RESET{0,1}_N    -> proc_sys_reset -> rst_{sys,ddr} (active-high to the IP)
```

**The single Classic-specific RTL/BD delta: `FCLK1 = 100 MHz`, not 125.** The
`xc7z010` is the small part (~17 600 LUTs / ~4 400 slices). The ring's
`burstw_setter` / `axi4w` / `ptr_mgr` paths top out around ~120 MHz, so the
z020's 125 MHz HP0 clock is marginal here. 100 MHz is the known-closing
single-rate. The async-FIFO CDC inside the IP still crosses two physically
distinct FCLK outputs, so the crossing is exercised end-to-end — it is just a
1:1 ratio. (If a future P&R proves 125 closes on the z010, bump
`tcl_hooks/gen_bd.tcl` `PCW_FPGA1_PERIPHERAL_FREQMHZ` back to 125.)

## 3. Block diagram

Identical to the z020 sibling (see its DESIGN.md §3): PS7 (BD recipe) drives
`M_AXI_GP0` → `ddr_validator_csr`; the CSR + `ddr_validator_bist` drive
`ddr_stream_axi` (both planes); the IP's `m_axi4` record maps onto the BD's
flat `S_AXI_HP0` (record→flat shim in the top, 512-bit lanes sliced to 128);
LEDs LD0–LD3 carry the verdict.

## 4. CSR map (AXI-Lite, 4 KiB @ `0x4000_0000`)

**Identical** to the z020 sibling — same `ddr_validator_csr.vhd`. Key offsets
for the in-fabric XSDB readout:

| Off  | Name              | Acc | Bits / meaning |
|------|-------------------|-----|----------------|
| 0x00 | `CTRL`            | RW  | [0] start (W1P) · [1] soft_reset (W1P) · [8] continuous |
| 0x04 | `STATUS`          | R   | [0] running · [1] done · [2] pass · [3] fail · [4] write_idle · [5] read_idle |
| 0x0C | `N_WORDS`         | RW  | user-words to push this run |
| 0x10 | `ERROR_COUNT`     | R   | mismatches observed this run |
| 0x14 | `WORDS_WRITTEN`   | R   | S_AXIS beats accepted |
| 0x18 | `WORDS_READ`      | R   | M_AXIS beats captured |
| 0x1C | `FIRST_ERR_INDEX` | R   | index of first mismatch (else 0xFFFFFFFF) |
| 0x20 | `FIRST_ERR_EXP`   | R   | expected word at first mismatch |
| 0x24 | `FIRST_ERR_GOT`   | R   | observed word at first mismatch |
| 0x3C | `IDENT`           | R   | `0x0DD50200` — host sanity probe |

> The `FIRST_ERR_INDEX/EXP/GOT` triple is what makes the IDP-T2.5 scatter
> diagnosable in-fabric: on a Classic FAIL these say exactly which word first
> diverged and what the IP read back vs the `1..N` counter it wrote — the
> in-fabric analogue of the "64-bit-half-interleaved" PS-readback image.

## 5. Scenarios

Identical to the z020 sibling (`ddr_validator_bist.vhd` is shared verbatim):
0 `roundtrip`, 1 `boundary`, 2 `reset_recovery` (stub), 3 `pingpong` (stub,
DDR-P3.11). The adversarial pass floor (pass requires `WORDS_WRITTEN ==
WORDS_READ == N` AND `ERROR_COUNT == 0`) carries over.

## 6. LED encoding (autonomous, no host)

`led[0..3]` = **M14/M15/G14/D18** (LD0–LD3, bank 35) — the SAME pins as the
z020 (both boards share this LED rail). Materialized from
`routertl/boards/zybo/pinout.yml` by inferred pinout subscription; **no
hand-written pin XDC**.

| LED | meaning |
|-----|---------|
| LD0 | running |
| LD1 | done |
| LD2 | pass (latched until next start) |
| LD3 | fail (latched until next start) |

A clean run ends with **LD1 + LD2** lit, LD3 dark. A FAIL ends **LD1 + LD3**.

## 7. CDC constraint (RR--P3.8, inherited from the IP)

Same as the z020: the IP ships its scoped, cell-relative `set_max_delay
-datapath_only` CDC constraint via `ip.yml build.sources.xdc`, vendored here by
`rr pkg add --local` and applied `read_xdc -ref ddr_stream_axi` from `ip.lock`.
**This project carries ZERO timing XDC.**

## 8. Files

```
example/zybo_classic_ddr_validator/
  project.yml                          declarative: board=zybo, part=xc7z010, auto_sources, BD hook, mmap, lint
  src/
    top_zybo_classic_ddr_validator.vhd PL top (entity renamed; FCLK1 comment 100 MHz; otherwise identical wiring)
    ddr_validator_bist.vhd             shared verbatim with z020
    ddr_validator_csr.vhd              shared verbatim with z020
  tcl_hooks/
    gen_bd.tcl                         BD recipe — Classic deltas: ENABLE GP0+HP0 (preset ships them off), FCLK1=100
    ps7_unused_off.tcl                 turn off fabric IRQ / IRQ_F2P (preset ships them on)
  sw/linux/                            optional Linux path (mirrors z020); the PRIMARY verdict is in-fabric over JTAG
  docs/ DESIGN.md (this) + BRING_UP.md
```

> No `constraints/` or `xdc/` dir, no `sources.xdc`. CDC from the IP, LED pins
> from the board registry. **The project carries zero XDC.**

## 9. Build / bring-up flow (all `rr`, contained + staged)

```bash
rr pkg add --local ../.. --name ddr_stream_axi   # vendors IP + scoped CDC xdc
rr ws update                                      # subscribe sources; board_pins auto-materialized
rr synth run                                      # gen_project: enable GP0/HP0, FCLK1=100, read_xdc -ref
rr implementation run
rr bitstream run
# Then the in-fabric verdict over JTAG/XSDB — see docs/BRING_UP.md.
```

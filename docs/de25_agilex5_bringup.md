# DE25-Standard (Agilex 5) bring-up notes — toolchain, license, first footprint

Status as of 2026-06-03. Covers the `targets/de25_agilex5_smoke.yml` synth-smoke
target: what it proves, the toolchain/license it needs, the first Agilex 5
resource footprint, and the gotchas hit during bring-up.

The DE25-Standard is **our** validation board (TerASIC, Agilex 5 SoC
`A5ED013BB32AE4SR1`) — a second platform for exercising the rr_ddr_stream AVMM
datapath beyond the Xilinx/Arria-10 smoke targets. Board facts (DDR4 1 GB / 32-bit
/ no-ECC / DDR4-2400, 150 MHz refclk, HPS control plane) live in the tich-super
`boards/de25-standard/` scratch; this doc is the **toolchain + target** half.

## Toolchain (Quartus Prime **Pro** — Agilex 5 is Pro-only)

- **Quartus Prime Pro 25.3.1** installed at **`/opt/altera_pro/25.3`**
  (Agilex 5 + Quartus components only; the target's `tool_path` points here).
- **Patch 1.02 applied** (`quartus-25.3.1-1.02`) — version reads
  `25.3.1 Build 100 … Patches 1.02`. This patch is **mandatory** for our build
  date: unpatched Pro 23.3–25.3.1 crash after 2026-01-10 with
  `Error (22912) … ddm_assessor verify_token: Cannot identify the client`.
- Arria-10 work stays on the **Standard** install (`/opt/altera_std`, 23.1std);
  do not cross the two.

## License

- Agilex 5 is entitled by **`~/altera_licenses/LR-159839_License.dat`**
  (`~/.altera.quartus/quartus2.ini [General 25.3_pro] LICENSE_FILE` points here).
- The base-only `LR-291374` does **not** cover Agilex 5 → `Error (19286): No
  license for family Agilex 5`. `LR-159839` is the one that clears it.
- `get_family_list` is EMPTY for the Pro tool (base-tool/AUTH licensing), so the
  rr P3.663 license pre-flight correctly WARNS-and-proceeds (defers to Quartus)
  rather than false-aborting — validated live here.

## The target — sources from the resolver

`ddr_stream_avmm`'s rtl/ units declare `library rr; use rr.<unit>`, so they must
compile into VHDL library `rr`, not `work`. Quartus **Std** (`quartus_map`)
accepts them in `work`. Quartus **Pro** (`quartus_syn`, Agilex 5) does not: A&S
fails with `Error (13785): design library "rr" does not contain primary unit ...`
unless the `.qsf` carries `-library rr`.

rr places these files itself. The target declares only `paths.sources: [rtl, libs]`
and the four open-logic packages. It has no `sources.syn` list and no
`sources.libraries` block. The resolver walks from `ddr_stream_avmm` and selects:

| Files | Library | Count |
|---|---|---|
| rtl/ units, including the Intel `rr_fifo_*_xpm_body_stub` bodies | `rr` | 17 |
| open-logic primitives | `olo` | 12 |
| Xilinx `rr_fifo_*_xpm_body` (`library xpm`) | not selected | 0 |

Evidence (DDR-P3.46, 2026-10-20, queue job `a31ec75d0882`, Quartus Prime Pro
25.3.1 patch 1.02 on `A5ED013BB32AE4SCS`): the generated `.qsf` has 29
`VHDL_FILE` entries (17 `-library rr`, 12 `-library olo`, 0 `work`), and
synthesis is successful with 0 errors.

Before DDR-P3.46, the target hand-listed every file in `sources.syn` and repeated
each rtl/ file in `sources.libraries.rr`. RTL-P3.672 (closed) made that manual
pair unnecessary.

## First Agilex 5 footprint — `ddr_stream_avmm` (synth)

A&S succeeds on `A5ED013BB32AE4SR1` (Pro 25.3.1 patch 1.02), clean `.qsf`
(12 `-library olo` + 19 `-library rr`, 0 `work`):

| Resource | Used | Available | % |
|---|---|---|---|
| Dedicated registers | 5,028 | — | — |
| RAM blocks (M20K) | 27 | 358 | 8 % |
| Block memory bits | 524,832 | 7,331,840 | 7 % |
| DSP blocks | 0 | 188 | 0 % |

A **placed-ALM** fit needs the Phase-2 board-wrapped design: the bare AVMM-master
IP is unconstrained, so all bus ports become 1,443 top-level pins (> 414 on the
part) and the Fitter pin-fails (expected — this is a synth-only smoke, like
`quartus_smoke`).

## Gotchas / how to run

```sh
rr queue submit synth --target de25_agilex5_smoke
```

- **Nuke `out/de25_agilex5_smoke` before re-synth after changing the source /
  library layout.** `rr synth run` does NOT clear the generated `.qsf`, so
  `-library` re-assignments ACCUMULATE stale `-library work` entries across edits
  → `Error (16169): ambiguous entity binding`. (This stale accumulation was
  briefly mis-filed as a gen_project double-emit, RTL-P3.673 — withdrawn; a clean
  dir emits each source exactly once.)

## Ticket trail

- **RTL-P3.663** (license pre-flight empty-list branch) — VALIDATED on real
  Agilex 5 here (was unexercised on the per-family Arria-10 license).
- **RTL-P3.672** — auto-infer the VHDL `library` clause so the manual
  `sources.libraries` block is not needed on Pro. Closed. DDR-P3.46 removed the
  manual block from the target.
- **RTL-P3.673** — WITHDRAWN (misdiagnosis; stale `.qsf`, not a tool defect).

## Next (Phase 1 / 2)

Board-pinout registry entry for `de25-standard` + SDC + an HPS-wrapped top
(so the AVMM bus is internal, not top-level pins) → first **placed** Agilex 5
footprint and bitstream; then port the LFSR-write / replay-verify datapath through
the Agilex 5 EMIF.

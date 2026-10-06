# kv260_ddr_validator

Hardware validator for **`ddr_stream_axi`** on the **AMD Kria KV260** (Zynq
UltraScale+ MPSoC, `xck26`). The ZynqMP sibling of `../zybo_z7_20_ddr_validator`
and `../zybo_classic_ddr_validator` — proving the same DDR-streaming IP integrates
and round-trips on a MPSoC part, through the pure-`rr` flow.

A self-checking BIST pushes a counter pattern through the IP's write plane into
real PS **DDR4** (`S_AXI_HP0_FPD`), the IP reads it back through its read plane,
and the BIST compares word-for-word **in fabric** — the payload never touches the
A53s. The verdict is exposed over an AXI-Lite CSR at `0xA000_0000`.

KV260 specifics (see `docs/DESIGN.md §2`):
- **Port-less PL top** — DDR4/MIO are PS-internal on ZynqMP.
- **CSR-only verdict** — the KV260 SOM has no user LEDs on PL pins.
- **Auto-start** — the BIST self-runs on configuration so the verdict is
  readable over JTAG (`mrd 0xA0000004`) with no Linux needed.

Quick start:

```bash
rr ws update && rr lint && rr synth run && rr implementation run && rr bitstream run
rr lease acquire kv260 && rr program run     # auto-start BIST runs; mrd 0xA0000004 -> PASS
```

Full bring-up (build, JTAG verdict, Linux app): `docs/BRING_UP.md`.
Register map, scenarios, BD recipe rationale: `docs/DESIGN.md`.

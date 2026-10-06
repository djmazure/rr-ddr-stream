# zybo_classic_ddr_validator

In-fabric hardware validator for `ddr_stream_axi` on the **original Digilent
Zybo** (Classic, PB200-279 Rev B, `xc7z010clg400-1`). The `xc7z010` sibling of
[`../zybo_z7_20_ddr_validator`](../zybo_z7_20_ddr_validator) (`xc7z020`).

A PL BIST pushes a `1..N` counter pattern through the IP's **write** plane into
PS DDR3 via `S_AXI_HP0`, the IP reads it back through its **read** plane, and the
BIST compares **word-for-word in fabric** — the payload never touches the CPU.
The verdict is encoded on LEDs (LD0 running / LD1 done / LD2 pass / LD3 fail) and
in the AXI-Lite CSR at `0x4000_0000`, readable over JTAG/XSDB with no Linux.

## Why it exists (live diagnostic)

The ring/HP0 write path was **never validated on the `xc7z010`**. A sibling
effort (**IDP-T2.5**, repo `rr-idp`) found the IP's write plane **corrupts DDR on
the Classic** (scattered, 64-bit-half-interleaved image) while the same IP works
on the `z020`; the 16-beat AXI3 burst cap (`max_burst_len_g`, DDR-P2.5) did not
fix it. This validator compares **in fabric** and **avoids the PS-readback path**,
so its verdict is decisive:

- **FAIL in-fabric** → the bug is in the IP/HP/DDR/part interaction itself.
- **PASS in-fabric** → the corruption is specific to the PS-readback path.

## Build + verdict (pure `rr`, contained + staged)

See [`docs/BRING_UP.md`](docs/BRING_UP.md) for the full ladder including the
XSDB CSR-readout recipe. Short form:

```bash
source /tools/Xilinx/Vivado/2024.1/settings64.sh
export XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
cd example/zybo_classic_ddr_validator
rr pkg add --local ../.. --name ddr_stream_axi   # one-time
rr ws update && rr synth run && rr implementation run && rr bitstream run
rr program run --jtag-serial <serial>           # the board JTAG serial (from rr hardware doctor)
# then drive CTRL.start + read STATUS via XSDB — docs/BRING_UP.md Stage 3
RR_JTAG_SERIAL=<serial> xsdb scripts/run_verdict_xsdb.tcl   # serial required; refuses without it
```

## What differs from the z020 sibling

| | z020 | Classic (this) |
|---|---|---|
| part | `xc7z020clg400-1` | `xc7z010clg400-1` |
| board preset | `zybo-z7-20` | `zybo` |
| FCLK1 (HP0 DMA clock) | 125 MHz | **100 MHz** (z010 ring tops ~120 MHz) |
| board preset GP0/HP0 | shipped ON | shipped **OFF** → `gen_bd.tcl` enables them |
| ps7 cleanup hook | turns off S_AXI_HP2 | turns off fabric IRQ / IRQ_F2P |

`ddr_validator_bist.vhd`, `ddr_validator_csr.vhd`, the CSR map, the LED rail
(M14/M15/G14/D18), the record→flat HP0 shim, and the zero-XDC / IP-inherited-CDC
discipline are **shared verbatim** with the z020. The project carries **zero
timing XDC** (CDC from the IP's `RR--P3.8` scoped constraint; LED pins from the
board registry).

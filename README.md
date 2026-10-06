# rr-ddr-stream

A ring-buffered DDR-backed streaming-DMA primitive for FPGA designs.
Ships an AXI4 master backend (Xilinx targets) and — once DDR-P3.1 lands —
an Avalon-MM master backend (Intel targets), in a single IP library with
two TOP entities.

Carved out of [`rr-vfifo`](https://bitbucket.org/dasjimaz/rr-vfifo) on
2026-05-15. `rr-vfifo` now depends on this IP for its bus-master and
pointer-math substrate.

## Lineage

`rr-ddr-stream` is the bus-master + ring-buffer + burst-policy substrate
that `rr-vfifo` historically bundled. Looking at the v0.3 milestone of
`rr-vfifo`, an honest observation drove the split:

> Without the cache piece (mux_ep_ddr arbiter + cache_fifo), rr-vfifo is
> a DMA, not a FIFO. Every ingress beat goes through an uploader → AXI
> write to DDR; every egress beat goes through a downloader → AXI read
> from DDR.

That DMA-shape is independently useful — capture pipelines, sustained
logging, burst-store-and-forward — even without the FIFO surface on
top. Hence its own IP.

## Architecture

```
   data_valid_record_t   ┌──────────────────────────┐   axi4_h2c_bus_t /
   in/out (streaming)  → │      ddr_stream_axi      │ → axi4_c2h_bus_t
                         │  (or ddr_stream_avmm)    │   master port to DDR
                         └────────────┬─────────────┘
                                      │
              ┌───────────────────────┼────────────────────────┐
              │                       │                        │
       ┌──────▼──────┐         ┌──────▼──────┐         ┌───────▼──────┐
       │  uploader   │         │ downloader  │         │   ptr_mgr    │
       │   (write)   │         │    (read)   │         │  (FULL/EMPTY │
       │             │         │             │         │   /DIFF)     │
       └──────┬──────┘         └──────┬──────┘         └──────────────┘
              │                       │
       ┌──────▼──────────┐     ┌──────▼──────────┐
       │ burstw_setter   │     │ burstr_setter   │
       │ (write burst    │     │ (read burst     │
       │  length policy) │     │  length policy) │
       └──────┬──────────┘     └──────┬──────────┘
              │                       │
       ┌──────▼──────┐         ┌──────▼──────┐
       │   axi4w     │         │   axi4r     │
       │ (master FSM │         │ (master FSM │
       │  + skid buf)│         │  + skid buf)│
       └─────────────┘         └─────────────┘

  ddr_fifo_intf + sync_cdc / mux_cdc_er sit on the streaming-interface side
  (CDC + FIFO wrap between user clock and DDR clock).
```

The bus-agnostic entities (`ptr_mgr`, `burstw_setter`, `burstr_setter`,
`ddr_fifo_intf`, CDC primitives, `skid_buffer`) are shared between the
AXI and AVMM TOPs. Only the master FSMs (`axi4w` / `axi4r` and their
AVMM siblings) and the wire-ups (`uploader` / `downloader` and their
AVMM variants) are bus-specific.

## Status

| Backend | TOP entity | State |
|---|---|---|
| AXI4 | `ddr_stream_axi` | DDR-P2.1 — carve-out in progress |
| Avalon-MM | `ddr_stream_avmm` | DDR-P3.1 — blocked on routertl RTL-P3.495 (`sdk/cocotb/avmm_ram.py`) |

## CSR / register-bank contract

Status and telemetry are exposed on the `ddr_csr_o` (`fifo_csr_t`) port plus a
few flat signals; the IP has no register bank of its own. Consumers mapping
`DDR_CSR` into their AXI-Lite/Avalon register space should follow
[`docs/CSR_CONTRACT.md`](docs/CSR_CONTRACT.md) — field layout, semantics,
sticky-vs-live, the `write_error`/`read_error` bus-error bits (DDR-P3.25), and a
recommended register map.

## Plane elision (`ddr_stream_axi`)

`ddr_stream_axi` defaults to a full-duplex DMA, but write-only and read-only
consumers can elide the unused plane via two boolean generics. Synthesis
prunes the entire silicon of the disabled plane; the corresponding output
ports are tied to safe defaults (`s_axis_ready_o`/`m_axis_valid_o` low,
`*_idle_o` high, AW/W/B or AR/R record fields zero).

| Generic | Default | Effect when `false` |
|---|---|---|
| `enable_write_plane_g` | `true` | Elides `uploader`, `axi4w`, ingress skid buffer, `upBusBusy` CDC; ties off `s_axis_ready_o`, AW/W/B fields. |
| `enable_read_plane_g`  | `true` | Elides `downloader`, `axi4r`, egress pass-through, `downBusBusy` CDC; ties off `m_axis_*`, AR/R fields. |

At least one plane must be enabled — both-off triggers an elaboration-time
`severity failure` assertion. First consumer:
[Deepskopion DS-P1.9](https://github.com/dasjimaz/deepskopion) instantiates
the IP with `enable_read_plane_g => false` (the PS drains DDR via its own
HP master, not via this IP's read plane).

## Test surface

cocotb under `sim/cocotb/tests/`, driven by `rr sim run`. AXI memory
model: [`cocotbext-axi`](https://github.com/alexforencich/cocotbext-axi)'s
`AxiRam`. AVMM memory model: `routertl.sdk.cocotb.avmm_ram.AvmmRam`
(coming via RTL-P3.495).

| Test | Coverage |
|---|---|
| `test_ptr_mgr` | Pointer ring math (FULL/EMPTY/DIFF, wrap-after-overrun) |
| `test_ddr_stream_axi` | End-to-end AXI: mem_model_demo, 4k_boundary, reset_attack, cleaver_n20_axi_trace |
| `test_ddr_stream_avmm` | Same battery, AVMM bus protocol — once RTL-P3.495 + DDR-P3.1 land |

## License

Same as `rr-vfifo` (TBD — slated for the routertl public-registry pass).

<!--
SPDX-FileCopyrightText: 2026 Daniel J. Mazure
SPDX-License-Identifier: MIT
-->
# rr-ddr-stream — CSR / register-bank contract (for consumers)

`ddr_stream_axi` / `ddr_stream_avmm` expose all status and telemetry on the
**`ddr_csr_o : out fifo_csr_t`** port (record defined in
[`rtl/ddr_stream_pkg.vhd`](../rtl/ddr_stream_pkg.vhd)), plus a few flat status
signals. The IP itself has **no register bank** — a consumer (e.g. a
PS↔fabric regbank in deepskopion / rr-vfifo) maps these into its own
AXI-Lite/Avalon address space. This document is that mapping contract.

All of `ddr_csr_o` is **read-only telemetry in the `clk_sys` domain**, already
CDC-synchronised from `clk_ddr` inside the IP — the consumer needs **no extra
CDC** when sampling it. Reset value is `C_INIT_FIFO_CSR`.

## `fifo_csr_t`

| Field | Type | Meaning | Live / sticky |
|---|---|---|---|
| `fifoStatus.full` | std_logic | User-side write FIFO full | live |
| `fifoStatus.empty` | std_logic | Read pipe empty (nothing to drain) | live |
| `fifoStatus.almost_empty` | std_logic | Programmable almost-empty | live |
| `fifoStatus.almost_full` | std_logic | Programmable almost-full | live |
| `fifoStatus.prog_empty` | std_logic | Programmable-threshold empty | live |
| `fifoStatus.prog_full` | std_logic | Programmable-threshold full | live |
| `fifoStatus.chip_empty` | std_logic | DDR ring (chip side) empty | live |
| `fifoStatus.chip_full` | std_logic | DDR ring (chip side) full | live |
| `fifoStatus.overflow` | std_logic | Write attempted while full (guard breach — should never fire) | pulse |
| `fifoStatus.underflow` | std_logic | Read attempted while pipe empty (guard breach) | pulse |
| **`fifoStatus.write_error`** | std_logic | **DDR write got `BRESP /= OKAY`** (DDR-P3.25) | **sticky → reset** |
| **`fifoStatus.read_error`** | std_logic | **DDR read got `RRESP /= OKAY`** (DDR-P3.25) | **sticky → reset** |
| `fifoFreeCnt` | slv(31:0) | Free entries in the ring | live |
| `fifoCnt` | slv(31:0) | Occupied entries in the ring | live |
| `fifoFullCnt` | slv(31:0) | Times the FIFO went full (telemetry) | accumulating |
| `fifoEmptyCnt` | slv(31:0) | Times the FIFO went empty (telemetry) | accumulating |
| `blockStrobeCnt` | slv(31:0) | Block-commit strobe count | accumulating |

Counters are `C_FP_BUS_WIDTH = 32` bits.

## Bus-error bits — `write_error` / `read_error` (DDR-P3.25)

These promote the IP's internal DDR bus-error detection to software:

- **Set** when the AXI master sees a non-OKAY response (`axi4w` write
  response / `axi4r` read response). **Sticky** — they latch and hold until
  `rst_sys_i`. A consumer that needs clear-on-read/clear-on-write must add it
  in its own regbank wrapper (the IP has no clear input yet — future work).
- **`write_error` semantics:** on a write error the IP **deliberately
  withholds** the non-durable burst (it does not publish the commit pointer —
  `axi4w` `WAIT_BRESP`), so that burst's data never appears on `m_axis`. This
  is intended; `write_error` is how software learns it happened (the stream is
  otherwise silently short).
- **AVMM variant:** `write_error` / `read_error` are **always `0`** —
  Avalon-MM has no per-transfer error response (`avmm_w.vhd`/`avmm_r.vhd`).
  The bits are wired for record parity only.

## Flat status / control signals (also worth mapping)

| Signal | Dir | Map as |
|---|---|---|
| `version_o` (32b) | out | RO version register |
| `write_idle_o` / `read_idle_o` | out | RO status bits — host must see idle before loading a base/pointer; a one-cycle load/reset pulse while the corresponding signal is low is refused (dropped, not deferred), so software must retry after idle asserts |
| `wr_commit_addr_o` (addr) | out | RO last-committed write address (durable up to here) |
| `csr_wr_base_addr_i` + `csr_wr_base_load_i` | in | WO base-address load (pulse `load` only when `write_idle_o`) |
| `csr_rd_base_addr_i` + `csr_rd_base_load_i` | in | WO read-base load (gate on `read_idle_o`) |
| `csr_rd_ptr_addr_i` | in | WO software ring read pointer |

## Recommended consumer regbank map

A minimal mapping a consumer can mirror (word offsets are illustrative):

```
0x00  RO  VERSION          = version_o
0x04  RO  STATUS           = {.., read_error, write_error, underflow, overflow,
                              chip_full, chip_empty, .., full, empty}
0x08  RO  FIFO_CNT         = fifoCnt
0x0C  RO  FIFO_FREE        = fifoFreeCnt
0x10  RO  WR_COMMIT_ADDR   = wr_commit_addr_o
0x14  RW  WR_BASE_ADDR     -> csr_wr_base_addr_i (+ load strobe, gated on write_idle)
0x18  RW  RD_BASE_ADDR     -> csr_rd_base_addr_i (+ load strobe, gated on read_idle)
0x1C  RW  RD_PTR_ADDR      -> csr_rd_ptr_addr_i
```

Put `write_error` / `read_error` in the STATUS word so a software DMA driver
can detect a DDR ECC / decode / security fault that would otherwise be a
silent short stream.

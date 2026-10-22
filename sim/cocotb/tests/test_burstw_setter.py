"""Whitebox unit tests for burstw_setter — DDR-P3.34.

Locks the single/zero-word write-burst-length edge with HARD-CODED goldens
(ROUTERTL-002). DDR-P3.34 flagged the `fifoDataCnt-1` latch
(rtl/burstw_setter.vhd) as a possible underflow ("only one, or none?").

RTL analysis (why it is NOT a defect): the latch is gated in PROC_NSL/IDLE by
fifo_empty_i='0', and the egress FIFO cannot drain before cmd_valid_o is issued,
so fifoDataCnt >= 1 at the sample cycle. Therefore:
  * one word           -> cmd_len_o (=AWLEN=beats-1) = 0  (a legal 1-beat burst)
  * N words (< clamps) -> cmd_len_o = N-1
  * large count        -> BOUNDED by the 4 KiB-distance clamp, never underflow.

DDR-T3.6 adds the 4 KiB clamp at unaligned pointers, where it differs from the
design cap (4096/bytes_per_beat - 1). test_burstw_setter_w256.py runs the same
checks at 256 bits.

These tests prove exactly that. NOT @requires-tagged: burstw_setter is an
internal sub-block; the IP-boundary contract (DDR-REQ-2xx / DDR-REQ-203 4 KiB)
covers its externally-observable behaviour. This file is development
instrumentation that pins the leaf so we never re-flag the edge (the TODO the
ticket removed).

Harness pattern per test_ptr_mgr.py: flat-port entity, direct cocotb attach,
run_simulation(top_level="burstw_setter", custom_libraries=...). With the
512-bit default (64 B/beat), 4096 B to the next 4 KiB boundary = 64 beats, so
aligned pointers give axi4kBoundaryBurst = 63 (beats - 1).
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from pathlib import Path as _Path

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
    from sdk.cocotb_helpers import requires        # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None
    def requires(*_args, **_kwargs):
        def _wrap(fn):
            return fn
        return _wrap


_IP_ROOT = _Path(__file__).resolve().parents[3]   # rr-ddr-stream/
_RTL = _IP_ROOT / "rtl"


def main() -> None:
    """`rr sim run test_burstw_setter` entry point."""
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke via `rr sim run test_burstw_setter`."
        )
    run_simulation(
        top_level="burstw_setter",
        module="test_burstw_setter",
        custom_libraries={
            "rr": [
                str(_RTL / "support" / "functions_pkg.vhd"),
                str(_RTL / "ptr_mgr.vhd"),
                str(_RTL / "burstw_setter.vhd"),
            ],
        },
        generics={
            "ddr_data_width_g": 512,
            "ddr_addr_width_g": 32,
            "buffer_width_g": 16,
            "fifo_depth_g": 12,
            "max_burst_len_g": 256,
        },
        waves=True,
        simulator="nvc",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
async def _bring_up(dut, period_ns: float = 10.0):
    """Spin the clock, walk synchronous reset, park inputs idle."""
    cocotb.start_soon(Clock(dut.clk_ddr_i, period_ns, units="ns").start())
    dut.rst_ddr_i.value = 1
    dut.cs_i.value = 0
    dut.fifo_cnt_i.value = 0
    dut.fifo_empty_i.value = 1
    dut.ptr_write_i.value = 0
    dut.ptr_read_i.value = 0
    dut.bus_ready_i.value = 0
    for _ in range(5):
        await RisingEdge(dut.clk_ddr_i)
    dut.rst_ddr_i.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk_ddr_i)


async def _burst_len_for_count(dut, count: int, ptr: int = 0,
                               timeout: int = 80) -> int:
    """Drive `count` FIFO words with ptr_write_i = ptr_read_i = `ptr` and
    return the cmd_len_o of the emitted burst.

    Equal pointers keep the ring empty (ddrCnt = 0, so PtrFullBurst saturates
    and never clamps). Only the low 12 bits of `ptr` set the 4 KiB distance:
    ptr = 0 is aligned (the full 4096 B to the line)."""
    dut.ptr_write_i.value = ptr
    dut.ptr_read_i.value = ptr
    dut.fifo_cnt_i.value = count
    dut.fifo_empty_i.value = 0
    dut.bus_ready_i.value = 1
    dut.cs_i.value = 1
    for _ in range(timeout):
        await RisingEdge(dut.clk_ddr_i)
        if int(dut.cmd_valid_o.value) == 1:
            return int(dut.cmd_len_o.value)
    raise AssertionError(
        f"cmd_valid_o never asserted for count={count} ptr=0x{ptr:04X} "
        f"within {timeout} cycles"
    )


# ──────────────────────────────────────────────────────────────────────────────
# DDR-P3.34 — single/zero-word edge, hard-coded goldens
# ──────────────────────────────────────────────────────────────────────────────
@cocotb.test()
async def single_word_flush_len0(dut):
    """One FIFO word -> AWLEN=0 (a legal 1-beat burst), NOT an underflow.

    fifoDataCnt=1 < C_MIN_BURST(15) so the burst is emitted via the wait-timeout
    path (cnt_wait_burst_r reaching 15); s1 = 1-1 = 0 -> cmd_len_o = 0.
    """
    await _bring_up(dut)
    got = await _burst_len_for_count(dut, 1)
    assert got == 0, f"single word: cmd_len_o must be 0 (1 beat), got {got}"


@cocotb.test()
async def two_word_flush_len1(dut):
    """Two words -> AWLEN=1 (2 beats). s1 = 2-1 = 1."""
    await _bring_up(dut)
    got = await _burst_len_for_count(dut, 2)
    assert got == 1, f"two words: cmd_len_o must be 1 (2 beats), got {got}"


@cocotb.test()
async def sixteen_word_len15(dut):
    """16 words (> C_MIN_BURST=15) latches immediately; below the 4 KiB clamp
    (64) so cmd_len_o = 16-1 = 15."""
    await _bring_up(dut)
    got = await _burst_len_for_count(dut, 16)
    assert got == 15, f"16 words: cmd_len_o must be 15, got {got}"


@cocotb.test()
async def large_count_bounded_by_4k(dut):
    """A large count is BOUNDED by the 4 KiB clamp, never a `-1` underflow.

    DDR-T3.6: ptr = 0x0800 leaves 2048 B = 32 beats to the 4 KiB line, so the
    clamp gives AWLEN = 31. The design cap (4096/64 - 1 = 63) is different
    here. At an aligned pointer both are 63, so that case could not tell the
    clamp from the cap."""
    await _bring_up(dut)
    got = await _burst_len_for_count(dut, 100, ptr=0x0800)
    assert got == 31, (
        f"100 words at 0x0800: cmd_len_o must clamp to 31 (32 beats to the "
        f"4 KiB line), got {got}")


@cocotb.test()
async def large_count_aligned_design_cap(dut):
    """Aligned pointer: 4096 B to the line = 64 beats = the design cap, so
    AWLEN = 63 (the last beat exactly fills to the 4 KiB line)."""
    await _bring_up(dut)
    got = await _burst_len_for_count(dut, 100, ptr=0)
    assert got == 63, f"100 words aligned: cmd_len_o must be 63, got {got}"


# ──────────────────────────────────────────────────────────────────────────────
# DDR-T3.6 — the setter's own 4 KiB clamp at an unaligned pointer (512-bit)
# ──────────────────────────────────────────────────────────────────────────────
@cocotb.test()
async def near_line_4k_clamp_512(dut):
    """ptr = 0x0FC0 is 64 B = ONE 512-bit beat before the 4 KiB line, so 40
    words must give AWLEN = 0. Before DDR-T3.6 get_burst_from_distance took a
    null slice, numeric_std "/=" returned TRUE, and the burst was forced to the
    design cap: cmd_len_o = 39, a 40-beat burst across the line."""
    await _bring_up(dut)
    got = await _burst_len_for_count(dut, 40, ptr=0x0FC0)
    assert got == 0, (
        f"40 words at 0x0FC0 (512-bit): 4 KiB clamp must give cmd_len_o=0 "
        f"(1 beat to the line), got {got}")


@cocotb.test()
async def mid_page_4k_clamp_512(dut):
    """ptr = 0x0C00 leaves 1024 B = 16 beats: 40 words give AWLEN = 15."""
    await _bring_up(dut)
    got = await _burst_len_for_count(dut, 40, ptr=0x0C00)
    assert got == 15, (
        f"40 words at 0x0C00 (512-bit): 4 KiB clamp must give cmd_len_o=15 "
        f"(16 beats to the line), got {got}")


if __name__ == "__main__":
    main()

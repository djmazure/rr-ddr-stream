"""Whitebox unit tests for burstr_setter — DDR-P3.34 (read-side mirror).

Read-side twin of test_burstw_setter.py. burstr_setter sizes the DDR READ burst
from `fifo_room_i` (space in the downstream FIFO) instead of a word count, and
gates its latch on fifo_full_i='0' + DdrEmpty='0'. The single/zero-word edge is
the same `fifoDataRoom-1` latch (rtl/burstr_setter.vhd) DDR-P3.34 flagged.

RTL analysis: the latch is gated by fifo_full_i='0' AND DdrEmpty='0', and the
downstream FIFO cannot gain room-below-1 before cmd_valid_o issues, so
fifoDataRoom >= 1 at the sample; one word -> cmd_len_o = 0, N -> N-1, large ->
bounded by the 4 KiB clamp. Hard-coded goldens below (ROUTERTL-002).

DdrEmpty comes from the internal ptr_mgr, whose registered empty_r resets to '1'
and only updates on a diff==0 cycle. `_prime_not_empty` clears it: a diff!=0
cycle (not near empty/full) to latch almostEmpty_r<=0, then a diff==0 cycle to
drive empty_r<=0; afterwards a large diff holds empty_r at 0 while keeping
PtrEmptyBurst saturated so it never clamps the small room values under test.

DDR-T3.6 adds the 4 KiB clamp at unaligned pointers, where it differs from the
design cap (4096/bytes_per_beat - 1). test_burstr_setter_w256.py runs the same
checks at 256 bits.

NOT @requires-tagged: internal sub-block; DDR-REQ-2xx / DDR-REQ-203 cover it at
the IP boundary.
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
    """`rr sim run test_burstr_setter` entry point."""
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke via `rr sim run test_burstr_setter`."
        )
    run_simulation(
        top_level="burstr_setter",
        module="test_burstr_setter",
        custom_libraries={
            "rr": [
                str(_RTL / "support" / "functions_pkg.vhd"),
                str(_RTL / "ptr_mgr.vhd"),
                str(_RTL / "burstr_setter.vhd"),
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
    cocotb.start_soon(Clock(dut.clk_ddr_i, period_ns, units="ns").start())
    dut.rst_ddr_i.value = 1
    dut.cs_i.value = 0
    dut.fifo_room_i.value = 0
    dut.fifo_full_i.value = 1
    dut.ptr_write_i.value = 0
    dut.ptr_read_i.value = 0
    dut.bus_ready_i.value = 0
    for _ in range(5):
        await RisingEdge(dut.clk_ddr_i)
    dut.rst_ddr_i.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk_ddr_i)


async def _prime_not_empty(dut):
    """Clear ptr_mgr's registered empty_r (reset '1') so DdrEmpty='0'.

    (A) diff!=0, not near empty/full  -> latch almostEmpty_r<=0, almostFull_r<=0.
    (B) diff==0                        -> if/elsif else-branch drives empty_r<=0.
    Two edges each for cocotb 2.x inertial-write + registered settle.
    """
    # (A)
    dut.ptr_write_i.value = 0x0100
    dut.ptr_read_i.value = 0
    await RisingEdge(dut.clk_ddr_i)
    await RisingEdge(dut.clk_ddr_i)
    # (B)
    dut.ptr_write_i.value = 0
    dut.ptr_read_i.value = 0
    await RisingEdge(dut.clk_ddr_i)
    await RisingEdge(dut.clk_ddr_i)


async def _burst_len_for_room(dut, room: int, ptr: int = 0,
                              timeout: int = 80) -> int:
    """Hold the ring non-empty with a large diff (PtrEmptyBurst saturates to
    255) and ptr_read_i = `ptr`, drive `room` FIFO slots, return the cmd_len_o
    of the emitted read burst. Only the low 12 bits of `ptr` set the 4 KiB
    distance: ptr = 0 is aligned (the full 4096 B to the line)."""
    await _prime_not_empty(dut)
    dut.ptr_write_i.value = ptr + 0x8000   # diff = 0x8000 -> not empty, PtrEmptyBurst=255
    dut.ptr_read_i.value = ptr
    dut.fifo_room_i.value = room
    dut.fifo_full_i.value = 0
    dut.bus_ready_i.value = 1
    dut.cs_i.value = 1
    for _ in range(timeout):
        await RisingEdge(dut.clk_ddr_i)
        if int(dut.cmd_valid_o.value) == 1:
            return int(dut.cmd_len_o.value)
    raise AssertionError(
        f"cmd_valid_o never asserted for room={room} ptr=0x{ptr:04X} "
        f"within {timeout} cycles"
    )


# ──────────────────────────────────────────────────────────────────────────────
# DDR-P3.34 — single/zero-word edge, hard-coded goldens (read side)
# ──────────────────────────────────────────────────────────────────────────────
@cocotb.test()
async def single_word_room_len0(dut):
    """One FIFO slot -> ARLEN=0 (1-beat read burst), NOT an underflow."""
    await _bring_up(dut)
    got = await _burst_len_for_room(dut, 1)
    assert got == 0, f"single slot: cmd_len_o must be 0 (1 beat), got {got}"


@cocotb.test()
async def two_word_room_len1(dut):
    await _bring_up(dut)
    got = await _burst_len_for_room(dut, 2)
    assert got == 1, f"two slots: cmd_len_o must be 1 (2 beats), got {got}"


@cocotb.test()
async def sixteen_word_room_len15(dut):
    """16 slots (> C_MIN_BURST=15) latches immediately; below the 4 KiB clamp."""
    await _bring_up(dut)
    got = await _burst_len_for_room(dut, 16)
    assert got == 15, f"16 slots: cmd_len_o must be 15, got {got}"


@cocotb.test()
async def large_room_bounded_by_4k(dut):
    """Large room is BOUNDED by the 4 KiB clamp, never a `-1` underflow.

    DDR-T3.6: ptr_read = 0x0800 leaves 2048 B = 32 beats to the 4 KiB line, so
    the clamp gives ARLEN = 31. The design cap (4096/64 - 1 = 63) is different
    here. At an aligned pointer both are 63, so that case could not tell the
    clamp from the cap."""
    await _bring_up(dut)
    got = await _burst_len_for_room(dut, 100, ptr=0x0800)
    assert got == 31, (
        f"100 slots at 0x0800: cmd_len_o must clamp to 31 (32 beats to the "
        f"4 KiB line), got {got}")


@cocotb.test()
async def large_room_aligned_design_cap(dut):
    """Aligned pointer: 4096 B to the line = 64 beats = the design cap, so
    ARLEN = 63 (fills to the 4 KiB line, no crossing)."""
    await _bring_up(dut)
    got = await _burst_len_for_room(dut, 100, ptr=0)
    assert got == 63, f"100 slots aligned: cmd_len_o must be 63, got {got}"


# ──────────────────────────────────────────────────────────────────────────────
# DDR-T3.6 — the setter's own 4 KiB clamp at an unaligned pointer (512-bit)
# ──────────────────────────────────────────────────────────────────────────────
@cocotb.test()
async def near_line_4k_clamp_512(dut):
    """ptr_read = 0x0FC0 is 64 B = ONE 512-bit beat before the 4 KiB line, so
    40 slots must give ARLEN = 0. Before DDR-T3.6 the null-slice "/=" in
    get_burst_from_distance forced the design cap: cmd_len_o = 39."""
    await _bring_up(dut)
    got = await _burst_len_for_room(dut, 40, ptr=0x0FC0)
    assert got == 0, (
        f"40 slots at 0x0FC0 (512-bit): 4 KiB clamp must give cmd_len_o=0 "
        f"(1 beat to the line), got {got}")


@cocotb.test()
async def mid_page_4k_clamp_512(dut):
    """ptr_read = 0x0C00 leaves 1024 B = 16 beats: 40 slots give ARLEN = 15."""
    await _bring_up(dut)
    got = await _burst_len_for_room(dut, 40, ptr=0x0C00)
    assert got == 15, (
        f"40 slots at 0x0C00 (512-bit): 4 KiB clamp must give cmd_len_o=15 "
        f"(16 beats to the line), got {got}")


if __name__ == "__main__":
    main()

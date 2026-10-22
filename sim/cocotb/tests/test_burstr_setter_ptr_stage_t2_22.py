"""burstr_setter: the ring-pointer distance is registered before the burst clamp (DDR-T2.22).

Read-side mirror of test_burstw_setter_ptr_stage_t2_22.py. burstr_setter clamps
the read burst to the data available in the ring (PtrEmptyBurst, from ptr_mgr's
combinational wr-rd diff) with the same one-cycle chain the write side had; the
fix registers PtrEmptyBurst once. DDR-P3.12's DE25 path (avmm_w ddr_addr_r ->
downloader ptr_mgr compare) has this mechanism.

* depth pin: a pointer change after the edge that captures s1 no longer reaches
  that burst's clamp; one cycle earlier it still does. Not a protocol scenario.
* safety: inside the latch window the protocol only lets the write pointer
  advance (axi4r loads ddr_req_addra_r, its ddr_pointer_o and this setter's
  ptr_read_i, only in IDLE on a new command, axi4r.vhd line 268), and an
  advancing write pointer only adds data, so a burst never exceeds the data
  available when it issues.
* no-throughput-regression: stable pointers give the burst length the
  unregistered RTL gave (goldens captured on fad97ec).
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from pathlib import Path as _Path

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None


_IP_ROOT = _Path(__file__).resolve().parents[3]   # rr-ddr-stream/
_RTL = _IP_ROOT / "rtl"

# 16-bit ring of byte addresses, 64 B per beat. available = (wr - rd) mod 2^16.
LOOSE = (0x8000, 0)   # (ptr_write, ptr_read): 32 KiB available -> clamp 63
TIGHT = (384, 0)      # 384 B = 6 beats available -> PtrEmptyBurst 5
COUNT = 40            # FIFO room in words: s1 = 39


def main() -> None:
    """`rr sim run test_burstr_setter_ptr_stage_t2_22` entry point."""
    if run_simulation is None:
        raise RuntimeError("invoke via `rr sim run test_burstr_setter_ptr_stage_t2_22`.")
    run_simulation(
        top_level="burstr_setter",
        module="test_burstr_setter_ptr_stage_t2_22",
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
        waves=False,
        simulator="nvc",
    )


def _clock(dut):
    """Start the clock; once per test (cocotb ends it with the test)."""
    cocotb.start_soon(Clock(dut.clk_ddr_i, 10, units="ns").start())


async def _bring_up(dut, ptrs=LOOSE):
    """Synchronous reset with `ptrs` held, inputs idle."""
    dut.rst_ddr_i.value = 1
    dut.cs_i.value = 0
    dut.fifo_room_i.value = 0
    dut.fifo_full_i.value = 1
    dut.ptr_write_i.value, dut.ptr_read_i.value = ptrs
    dut.bus_ready_i.value = 0
    for _ in range(5):
        await RisingEdge(dut.clk_ddr_i)
    dut.rst_ddr_i.value = 0
    for _ in range(5):
        await RisingEdge(dut.clk_ddr_i)


async def _burst(dut, count, step_at=None, step_to=None, timeout=80):
    """Request a burst of `count` words. The request is applied after edge E-1,
    so the FSM decides at edge E0. If `step_at` is k, the pointers become
    `step_to` right after edge E(k). Returns cmd_len_o of the emitted burst."""
    dut.fifo_room_i.value = count
    dut.fifo_full_i.value = 0
    dut.bus_ready_i.value = 1
    dut.cs_i.value = 1
    for edge in range(timeout):
        await RisingEdge(dut.clk_ddr_i)
        if step_at is not None and edge == step_at:
            dut.ptr_write_i.value, dut.ptr_read_i.value = step_to
        if int(dut.cmd_valid_o.value) == 1:
            return int(dut.cmd_len_o.value)
    raise AssertionError(f"cmd_valid_o never asserted for count={count}")


@cocotb.test()
async def controls_loose_and_tight_clamps(dut):
    """Positive control for the two pointer settings the depth pin switches
    between: held from the start, LOOSE leaves s1 (39) alone and TIGHT clamps
    it to the data available (5)."""
    _clock(dut)
    await _bring_up(dut, LOOSE)
    assert await _burst(dut, COUNT) == 39
    await _bring_up(dut, TIGHT)
    assert await _burst(dut, COUNT) == 5


@cocotb.test()
async def pointer_change_after_s1_capture_misses_the_clamp(dut):
    """DDR-T2.22 depth pin. E0 decides, E1 captures s1, E2 clamps into s2. With
    PtrEmptyBurst registered, the clamp at E2 uses the pointers held before E1,
    so a change right after E1 is too late for this burst."""
    _clock(dut)
    await _bring_up(dut, LOOSE)
    got = await _burst(dut, COUNT, step_at=1, step_to=TIGHT)
    assert got == 39, (
        f"cmd_len_o={got}: a pointer change after the s1 capture edge reached the "
        "clamp, so the distance still feeds fifo_data_room_s2_r combinationally "
        "(the read-side mirror of the DDR-T2.22 setup path)")


@cocotb.test()
async def pointer_change_before_s1_capture_reaches_the_clamp(dut):
    """One register, not two: a change right after E0 is registered at E1 and
    still clamps this burst."""
    _clock(dut)
    await _bring_up(dut, LOOSE)
    got = await _burst(dut, COUNT, step_at=0, step_to=TIGHT)
    assert got == 5, f"cmd_len_o={got}: the distance is delayed by more than one register"


@cocotb.test()
async def write_pointer_advance_in_the_window_never_overreads(dut):
    """Safety. Inside the latch window only the write pointer moves, and only
    forward. Start with 6 beats available and advance the write pointer at
    each edge of the window: the burst never exceeds the data available at issue."""
    _clock(dut)
    for k in range(0, 4):
        await _bring_up(dut, TIGHT)
        got = await _burst(dut, COUNT, step_at=k, step_to=(384 + 640, 0))  # +10 beats
        avail_beats_at_issue = (384 + 640) // 64
        assert got + 1 <= avail_beats_at_issue, (k, got, avail_beats_at_issue)
        assert got in (5, 15), (k, got)   # the stale or the fresh clamp, nothing else


# (ptr_write, ptr_read, room) -> cmd_len_o, captured on the unregistered RTL (fad97ec).
# Read pointers are 4 KiB aligned, so no point depends on the setter's own
# 4 KiB clamp (dead at 64 B beats, filed separately).
STABLE_GOLDENS = [
    ((0x8000, 0), 1, 0),                    # one word of room -> 1 beat
    ((0x8000, 0), 16, 15),                  # room-bound
    ((0x8000, 0), 100, 63),                 # design cap (4096 B / 64 B - 1)
    ((384, 0), 40, 5),                      # 384 B available -> 6 beats
    ((384, 0), 3, 2),                       # room-bound with little data
    ((0x1000 + 1000, 0x1000), 30, 14),      # 1000 B available -> 15 beats
    ((0x9000, 0x8000), 200, 63),            # 4096 B available -> 64 beats
]


@cocotb.test()
async def stable_pointers_give_the_same_burst_length_as_before(dut):
    """No throughput regression: with pointers held steady, every read burst
    length equals what the unregistered RTL produced."""
    _clock(dut)
    wrong = []
    for ptrs, count, want in STABLE_GOLDENS:
        await _bring_up(dut, ptrs)
        got = await _burst(dut, count)
        if got != want:
            wrong.append(f"ptrs={ptrs} count={count}: cmd_len_o={got}, was {want}")
    assert not wrong, "burst length changed with stable pointers:\n" + "\n".join(wrong)


if __name__ == "__main__":
    main()

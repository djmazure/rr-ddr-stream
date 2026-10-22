"""burstr_setter at a 12-bit ring and 128-bit data — DDR-T2.23.

This is the read-ring configuration of test_ddr_swptr_fill,
test_ddr_ring_escape_read and test_ddr_reset_coverage: buffer_width_g = 12 and
16 B beats. The ring distance from ptr_mgr is then 12 bits wide, and before
DDR-T3.6 the overflow slice in get_burst_from_distance was null at this width
(distance(11 downto 12)). numeric_std "/=" returns TRUE for a null argument,
so the ring-distance clamp was dead: every read burst was sized as if the ring
held 256 beats, and the reader read past the write pointer.

DDR-T2.23 covers the distances below one beat. A software write pointer is a
byte offset, so the producer can publish part of a beat. The function computed
distance/16 - 1, which wraps to 255 when the distance is under 16 bytes.

Hard-coded goldens (ROUTERTL-002). NOT @requires-tagged: internal sub-block,
see test_burstr_setter.py.
"""

from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge
from pathlib import Path as _Path

from test_burstr_setter import _bring_up, _prime_not_empty  # noqa: E402

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None


_IP_ROOT = _Path(__file__).resolve().parents[3]   # rr-ddr-stream/
_RTL = _IP_ROOT / "rtl"


def main() -> None:
    """`rr sim run test_burstr_setter_b12` entry point."""
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke via `rr sim run test_burstr_setter_b12`."
        )
    run_simulation(
        top_level="burstr_setter",
        module="test_burstr_setter_b12",
        custom_libraries={
            "rr": [
                str(_RTL / "support" / "functions_pkg.vhd"),
                str(_RTL / "ptr_mgr.vhd"),
                str(_RTL / "burstr_setter.vhd"),
            ],
        },
        generics={
            "ddr_data_width_g": 128,
            "ddr_addr_width_g": 32,
            "buffer_width_g": 12,
            "fifo_depth_g": 12,
            "max_burst_len_g": 256,
        },
        waves=True,
        simulator="nvc",
    )


async def _drive(dut, ptr_write: int, ptr_read: int, room: int) -> None:
    dut.ptr_write_i.value = ptr_write
    dut.ptr_read_i.value = ptr_read
    dut.fifo_room_i.value = room
    dut.fifo_full_i.value = 0
    dut.bus_ready_i.value = 1
    dut.cs_i.value = 1


async def _next_burst(dut, cycles: int = 80) -> int | None:
    """cmd_len_o of the next read burst, or None if none issues in `cycles`."""
    for _ in range(cycles):
        await RisingEdge(dut.clk_ddr_i)
        if int(dut.cmd_valid_o.value) == 1:
            return int(dut.cmd_len_o.value)
    return None


@cocotb.test()
async def ring_distance_limits_burst(dut):
    """1 KiB produced (write 0x400, read 0x000) is 64 beats: 100 FIFO slots
    give ARLEN = 63. With the null slice the burst was 100 beats (cmd_len_o =
    99), 576 B past the write pointer."""
    await _bring_up(dut)
    await _prime_not_empty(dut)
    await _drive(dut, ptr_write=0x400, ptr_read=0x000, room=100)
    got = await _next_burst(dut)
    assert got == 63, (
        f"write 0x400 / read 0x000: ARLEN must be 63 (64 beats produced), "
        f"got {got}")


@cocotb.test()
async def partial_beat_produced_issues_no_read(dut):
    """8 B produced (write 0x008, read 0x000) is less than one 16 B beat, so no
    read burst may issue. Then 24 B produced holds one whole beat: ARLEN = 0.

    Before DDR-T2.23 the 8 B distance gave 0 - 1 = 255 and the setter issued a
    100-beat burst (cmd_len_o = 99) over data the producer never wrote."""
    await _bring_up(dut)
    await _prime_not_empty(dut)
    await _drive(dut, ptr_write=0x008, ptr_read=0x000, room=100)
    got = await _next_burst(dut)
    assert got is None, (
        f"write 0x008 / read 0x000: issued a read burst with less than one "
        f"beat produced (cmd_len_o = {got})")
    dut.ptr_write_i.value = 0x018
    got = await _next_burst(dut)
    assert got == 0, (
        f"write 0x018 / read 0x000: one whole beat produced, ARLEN must be 0, "
        f"got {got}")


@cocotb.test()
async def page_tail_partial_beat_4k_clamp_len0(dut):
    """Read pointer 0xFF8 is 8 B before the 4 KiB line, inside the page's last
    beat. 1032 B are produced (write 0x400 after the wrap), so only the 4 KiB
    clamp limits the burst: ARLEN = 0, the one partial beat up to the line.

    Before DDR-T2.23 the 8 B line distance gave 0 - 1 = 255, the clamp did
    nothing, and 100 slots gave cmd_len_o = 99 across the line."""
    await _bring_up(dut)
    await _prime_not_empty(dut)
    await _drive(dut, ptr_write=0x400, ptr_read=0xFF8, room=100)
    got = await _next_burst(dut)
    assert got == 0, (
        f"read 0xFF8 (8 B to the 4 KiB line): 4 KiB clamp must give ARLEN = 0, "
        f"got {got}")


if __name__ == "__main__":
    main()

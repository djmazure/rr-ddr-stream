"""burstr_setter at 256-bit data width — DDR-T3.6.

The 512-bit bench (test_burstr_setter.py) runs at one elaboration. This module
re-elaborates burstr_setter at ddr_data_width_g = 256 (32 B/beat, the DE25
validator width), where the design cap is 4096/32 - 1 = 127.

DDR-T3.6: at 256 and 512 bits get_burst_from_distance took a null slice for the
13-bit 4 KiB distance. numeric_std "/=" returns TRUE for a null argument, so
every 4 KiB burst was forced to the design cap and the setter's own 4 KiB clamp
never shortened a burst. Each test below uses a pointer where the 4 KiB clamp
and the design cap differ. Hard-coded goldens (ROUTERTL-002).

NOT @requires-tagged: internal sub-block, see test_burstr_setter.py.
"""

from __future__ import annotations

import cocotb
from pathlib import Path as _Path

from test_burstr_setter import _bring_up, _burst_len_for_room  # noqa: E402

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None


_IP_ROOT = _Path(__file__).resolve().parents[3]   # rr-ddr-stream/
_RTL = _IP_ROOT / "rtl"


def main() -> None:
    """`rr sim run test_burstr_setter_w256` entry point."""
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke via `rr sim run test_burstr_setter_w256`."
        )
    run_simulation(
        top_level="burstr_setter",
        module="test_burstr_setter_w256",
        custom_libraries={
            "rr": [
                str(_RTL / "support" / "functions_pkg.vhd"),
                str(_RTL / "ptr_mgr.vhd"),
                str(_RTL / "burstr_setter.vhd"),
            ],
        },
        generics={
            "ddr_data_width_g": 256,
            "ddr_addr_width_g": 32,
            "buffer_width_g": 16,
            "fifo_depth_g": 12,
            "max_burst_len_g": 256,
        },
        waves=True,
        simulator="nvc",
    )


@cocotb.test()
async def near_line_4k_clamp_256(dut):
    """ptr_read = 0x0FC0 is 64 B = TWO 256-bit beats before the 4 KiB line, so 40
    slots must give ARLEN = 1. With the null-slice defect cmd_len_o = 39."""
    await _bring_up(dut)
    got = await _burst_len_for_room(dut, 40, ptr=0x0FC0)
    assert got == 1, (
        f"40 slots at 0x0FC0 (256-bit): 4 KiB clamp must give cmd_len_o=1 "
        f"(2 beats to the line), got {got}")


@cocotb.test()
async def mid_page_4k_clamp_256(dut):
    """ptr_read = 0x0C00 leaves 1024 B = 32 beats: 40 slots give ARLEN = 31."""
    await _bring_up(dut)
    got = await _burst_len_for_room(dut, 40, ptr=0x0C00)
    assert got == 31, (
        f"40 slots at 0x0C00 (256-bit): 4 KiB clamp must give cmd_len_o=31 "
        f"(32 beats to the line), got {got}")


@cocotb.test()
async def large_room_bounded_by_4k_256(dut):
    """ptr_read = 0x0800 leaves 2048 B = 64 beats: 200 slots clamp to ARLEN = 63,
    not the design cap 127."""
    await _bring_up(dut)
    got = await _burst_len_for_room(dut, 200, ptr=0x0800)
    assert got == 63, (
        f"200 slots at 0x0800 (256-bit): cmd_len_o must clamp to 63 (64 beats "
        f"to the 4 KiB line), got {got}")


@cocotb.test()
async def large_room_aligned_design_cap_256(dut):
    """Aligned pointer: 4096 B = 128 beats = the design cap, so ARLEN = 127."""
    await _bring_up(dut)
    got = await _burst_len_for_room(dut, 200, ptr=0)
    assert got == 127, f"200 slots aligned (256-bit): cmd_len_o must be 127, got {got}"


if __name__ == "__main__":
    main()

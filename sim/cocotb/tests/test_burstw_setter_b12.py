"""burstw_setter at a 12-bit ring and 128-bit data — DDR-T2.23.

The write-side mirror of test_burstr_setter_b12.py. With buffer_width_g = 12
and 16 B beats the overflow slice in get_burst_from_distance was null before
DDR-T3.6, so the free-space clamp was dead and a write burst could run over
data the reader had not consumed.

DDR-T2.23 covers the free space below one beat. get_burst_from_distance
computed distance/16 - 1, which wraps to 255 below 16 bytes:

- At write + 16 = read one beat is free, the ring's last slot. ptr_mgr forces
  its difference to all ones there, so the free distance is 0, and the setter
  issued a full-size burst over unread data. It must write exactly that beat
  (the ring then reads full, DDR-REQ-501).
- A software read pointer is a byte offset, so the consumer can free part of a
  beat. No write burst may issue until a whole beat is free.

Hard-coded goldens (ROUTERTL-002). NOT @requires-tagged: internal sub-block,
see test_burstw_setter.py.
"""

from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge
from pathlib import Path as _Path

from test_burstw_setter import _bring_up  # noqa: E402

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None


_IP_ROOT = _Path(__file__).resolve().parents[3]   # rr-ddr-stream/
_RTL = _IP_ROOT / "rtl"


def main() -> None:
    """`rr sim run test_burstw_setter_b12` entry point."""
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke via `rr sim run test_burstw_setter_b12`."
        )
    run_simulation(
        top_level="burstw_setter",
        module="test_burstw_setter_b12",
        custom_libraries={
            "rr": [
                str(_RTL / "support" / "functions_pkg.vhd"),
                str(_RTL / "ptr_mgr.vhd"),
                str(_RTL / "burstw_setter.vhd"),
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


async def _drive(dut, ptr_write: int, ptr_read: int, count: int) -> None:
    dut.ptr_write_i.value = ptr_write
    dut.ptr_read_i.value = ptr_read
    dut.fifo_cnt_i.value = count
    dut.fifo_empty_i.value = 0
    dut.bus_ready_i.value = 1
    dut.cs_i.value = 1


async def _next_burst(dut, cycles: int = 80) -> int | None:
    """cmd_len_o of the next write burst, or None if none issues in `cycles`."""
    for _ in range(cycles):
        await RisingEdge(dut.clk_ddr_i)
        if int(dut.cmd_valid_o.value) == 1:
            return int(dut.cmd_len_o.value)
    return None


@cocotb.test()
async def free_space_limits_burst(dut):
    """Write 0x000, read 0x400: 3 KiB unread, 1 KiB free. The free distance is
    2^12 - 1 - 3072 = 1023 B (C_BUFFER_SIZE is all ones), 63 whole beats, so 100
    FIFO words give AWLEN = 62. The last slot goes in its own one-beat burst.
    With the null slice the burst was 100 beats (cmd_len_o = 99)."""
    await _bring_up(dut)
    await _drive(dut, ptr_write=0x000, ptr_read=0x400, count=100)
    got = await _next_burst(dut)
    assert got == 62, (
        f"write 0x000 / read 0x400: AWLEN must be 62 (1023 B free distance, "
        f"63 beats), got {got}")


@cocotb.test()
async def last_slot_writes_one_beat(dut):
    """Write 0x7F0, read 0x800: one beat free, the ring's last slot. 100 FIFO
    words give AWLEN = 0, the one beat that fills the ring.

    Before DDR-T2.23 the forced free distance 0 gave 0 - 1 = 255 and the setter
    issued a 100-beat burst (cmd_len_o = 99) from 0x7F0 over the unread data
    at 0x800."""
    await _bring_up(dut)
    await _drive(dut, ptr_write=0x7F0, ptr_read=0x800, count=100)
    got = await _next_burst(dut)
    assert got == 0, (
        f"write 0x7F0 / read 0x800 (last slot): AWLEN must be 0, one beat, "
        f"got {got}")


@cocotb.test()
async def partial_beat_free_issues_no_write(dut):
    """Write 0x7F8, read 0x800: 8 B free, less than one 16 B beat, so no write
    burst may issue. When the reader moves to 0x810, 24 B are free, one whole
    beat: AWLEN = 0.

    Before DDR-T2.23 the 8 B gave a free distance of 7, 0 - 1 = 255, and the
    setter issued a 100-beat burst (cmd_len_o = 99) over unread data."""
    await _bring_up(dut)
    await _drive(dut, ptr_write=0x7F8, ptr_read=0x800, count=100)
    got = await _next_burst(dut)
    assert got is None, (
        f"write 0x7F8 / read 0x800: issued a write burst with less than one "
        f"beat free, over unread data (cmd_len_o = {got})")
    dut.ptr_read_i.value = 0x810
    got = await _next_burst(dut)
    assert got == 0, (
        f"write 0x7F8 / read 0x810: one whole beat free, AWLEN must be 0, "
        f"got {got}")


if __name__ == "__main__":
    main()

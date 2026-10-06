# tests/test_demo_axi_write_backpressure_conformance.py
"""CONFORMANCE HUNT (EPIC RTL-P2.719 / ticket RTL-P3.851) — NEW vector:
AGGRESSIVE WRITE-CHANNEL BACKPRESSURE on the ddr_stream_axi AXI4 master.

The existing AXI4/M_AXIS conformance tests let the AxiRam responder accept
writes with zero backpressure, so the master's write path (uploader → axi4w →
AW/W/B) is never stressed by a slow DDR. This test pins aggressive pause
generators on the responder's AW/W/B channels (the real "DDR controller slow to
accept/ack writes" scenario) and asserts:

  * every seeded word survives the round-trip in order (scoreboard) — a master
    that drops/duplicates W beats or mis-counts AWLEN under B backpressure loses
    or corrupts data;
  * M_AXIS stays AXIS-conformant throughout (passive AxiStreamMonitor).

ROUTERTL-002: pause patterns are fixed, not runtime-derived.

Run:  rr sim run test_demo_axi_write_backpressure_conformance
"""

from __future__ import annotations

import itertools
import logging
from pathlib import Path as _Path

import cocotb
from cocotb.clock import Clock
from cocotb.handle import Immediate
from cocotb.triggers import RisingEdge, FallingEdge, Timer

from cocotbext.axi import AxiBus, AxiRam

from rr_cocotb_tb.drivers.axi_stream import AxiStreamBus
from rr_cocotb_tb.monitors.axi_stream_monitor import AxiStreamMonitor

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None

_LOG = logging.getLogger("demo_axi_wbp")

_IP_ROOT = _Path(__file__).resolve().parents[3]
_RTL_DIR = _IP_ROOT / "rtl"
_SUPPORT_DIR = _RTL_DIR / "support"
_TB_DIR = _IP_ROOT / "sim" / "tb"
_OLO_SYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_sync" / "src" / "base" / "vhdl"
_OLO_ASYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_async" / "src" / "base" / "vhdl"
_OLO_WCN_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_n2xn" / "src" / "base" / "vhdl"
_OLO_WCXN_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_xn2n" / "src" / "base" / "vhdl"

_USER_W = 32
_DDR_W = 128
_DDR_ADDR_W = 32
_RAM_SIZE = 1 << 20

DDR_PERIOD = 3.33
SYS_PERIOD = 10.0

_READY_PATTERN = (1, 0, 0, 1, 0, 0, 0, 1)


def _pauses(pattern):
    """Infinite bool pause-generator from a fixed pattern (True = stall)."""
    return itertools.cycle([bool(x) for x in pattern])


@cocotb.test()
async def axi_write_backpressure(dut):
    """Round-trip 256 words while the DDR responder aggressively backpressures
    AW/W/B; assert no loss/corruption and AXIS conformance on M_AXIS."""
    cocotb.start_soon(Clock(dut.CLK_DDR, DDR_PERIOD, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_SYS, SYS_PERIOD, units="ns").start())

    dut.RST_DDR.value = 1
    dut.RST_SYS.value = 1
    dut.S_AXIS_VALID.value = 0
    dut.M_AXIS_READY.value = 0
    dut.S_AXIS_DATA.value = 0

    bus = AxiStreamBus(
        tvalid=dut.M_AXIS_VALID, tready=dut.M_AXIS_READY, tdata=dut.M_AXIS_DATA,
    )
    mon = AxiStreamMonitor(
        bus, dut.CLK_SYS, dut.RST_SYS,
        log=_LOG, rst_active_low=False, channel="ddr_axi_wbp",
    )
    cocotb.start_soon(mon.run())

    axi = AxiBus.from_prefix(dut, "M_AXI")
    ram = AxiRam(axi, dut.CLK_DDR, dut.RST_DDR, size=_RAM_SIZE)

    # Aggressive write-path backpressure (real: DDR controller slow to accept).
    ram.write_if.aw_channel.set_pause_generator(_pauses([1, 1, 0]))
    ram.write_if.w_channel.set_pause_generator(_pauses([1, 1, 1, 0]))
    ram.write_if.b_channel.set_pause_generator(_pauses([1, 1, 1, 1, 0]))

    await RisingEdge(dut.CLK_DDR)
    for _ in range(5):
        await RisingEdge(dut.CLK_DDR)
    dut.RST_DDR.value = 0
    for _ in range(5):
        await RisingEdge(dut.CLK_DDR)
    await RisingEdge(dut.CLK_SYS)
    for _ in range(5):
        await RisingEdge(dut.CLK_SYS)
    dut.RST_SYS.value = 0
    for _ in range(5):
        await RisingEdge(dut.CLK_SYS)
    await Timer(3, units="us")
    await RisingEdge(dut.CLK_SYS)

    # ── Seed 256 words (S_AXIS h2c). The master must commit them to DDR while
    #    AW/W/B are throttled — backpressure on the write path, not the source.
    N = 256
    expected = list(range(1, N + 1))
    for i in range(N):
        dut.S_AXIS_DATA.value = Immediate(i + 1)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)
    await Timer(400, units="us")   # generous: writes are heavily throttled

    # ── Drain via M_AXIS (read path clean) with light output backpressure. ───
    drain_done = False

    async def ready_driver():
        idx = 0
        while not drain_done:
            dut.M_AXIS_READY.value = _READY_PATTERN[idx % len(_READY_PATTERN)]
            idx += 1
            await RisingEdge(dut.CLK_SYS)
    cocotb.start_soon(ready_driver())

    received: list[int] = []
    idle = 0
    for _ in range(N * 256):
        await FallingEdge(dut.CLK_SYS)
        if int(dut.M_AXIS_VALID.value) and int(dut.M_AXIS_READY.value):
            received.append(int(dut.M_AXIS_DATA.value))
            idle = 0
        else:
            idle += 1
        if len(received) >= N:
            break
        if idle > 12000:
            break
    drain_done = True
    await Timer(20, units="us")

    dut._log.info(
        f"RTL-P3.851 axi write-backpressure: received={len(received)}/{N}, "
        f"violations={mon.violation_count}"
    )
    assert mon.violation_count == 0, (
        f"M_AXIS AXIS violation under write backpressure:\n{mon.report()}"
    )
    assert len(received) == N, (
        f"WRITE-BACKPRESSURE LOSS: seeded {N}, only {len(received)} drained — "
        f"the master dropped/stalled words while AW/W/B were throttled. "
        f"tail={received[-4:]}"
    )
    diffs = [(i, hex(e), hex(g)) for i, (e, g) in enumerate(zip(expected, received)) if e != g]
    assert not diffs, f"stream corrupted/reordered under write backpressure: {diffs[:5]}"


def main() -> None:
    if run_simulation is None:
        raise RuntimeError(
            "invoke via `rr sim run test_demo_axi_write_backpressure_conformance`"
        )
def _custom_libraries():
    return {
        "olo": [
            str(_OLO_ASYNC_DIR / "olo_base_pkg_array.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_pkg_math.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_pkg_logic.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_pkg_string.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_pkg_attribute.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_cc_bits.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_cc_reset.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_ram_sdp.vhd"),
            str(_OLO_SYNC_DIR / "olo_base_fifo_sync.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_fifo_async.vhd"),
            str(_OLO_WCN_DIR / "olo_base_wconv_n2xn.vhd"),
            str(_OLO_WCXN_DIR / "olo_base_wconv_xn2n.vhd"),
        ],
        "rr": [
            str(_RTL_DIR / "ddr_stream_pkg.vhd"),
            str(_SUPPORT_DIR / "functions_pkg.vhd"),
            str(_SUPPORT_DIR / "ddr_sync_cdc.vhd"),
            str(_SUPPORT_DIR / "skid_buffer.vhd"),
            str(_SUPPORT_DIR / "xilinx" / "rr_fifo_sync_xpm_body.vhd"),
            str(_SUPPORT_DIR / "xilinx" / "rr_fifo_async_mixed_xpm_body.vhd"),
            str(_SUPPORT_DIR / "rr_fifo_sync.vhd"),
            str(_SUPPORT_DIR / "rr_fifo_async_mixed.vhd"),
            str(_RTL_DIR / "ddr_fifo_intf.vhd"),
            str(_RTL_DIR / "ptr_mgr.vhd"),
            str(_RTL_DIR / "burstw_setter.vhd"),
            str(_RTL_DIR / "burstr_setter.vhd"),
            str(_RTL_DIR / "axi4w.vhd"),
            str(_RTL_DIR / "axi4r.vhd"),
            str(_RTL_DIR / "downloader.vhd"),
            str(_RTL_DIR / "uploader.vhd"),
            str(_RTL_DIR / "ddr_stream_axi.vhd"),
            str(_RTL_DIR / "ddr_stream_fic.vhd"),
            str(_TB_DIR / "wrappers" / "wrapper_ddr_stream_axi.vhd"),
            str(_TB_DIR / "wrappers" / "wrapper_ddr_stream_fic.vhd"),
        ],
    }


def main() -> None:
    if run_simulation is None:
        raise RuntimeError(
            "invoke via `rr sim run test_demo_axi_write_backpressure_conformance`"
        )
    for _top, _ddr_w in (
        ("wrapper_ddr_stream_axi", 128),
        ("wrapper_ddr_stream_fic", 64),
    ):
        run_simulation(
            top_level=_top,
            module="test_demo_axi_write_backpressure_conformance",
            custom_libraries=_custom_libraries(),
            work_lib="rr",
            generics={
                "G_AXI_ID": 0,
                "G_FIFO_DEPTH": 9,
                "G_USER_DATA_WIDTH": _USER_W,
                "G_DDR_DATA_WIDTH": _ddr_w,
                "G_DDR_ADDR_WIDTH": _DDR_ADDR_W,
                "G_BUFFER_WIDTH": 20,
                "G_FIFO_IMPL": "olo",
            },
            waves=True,
            simulator="nvc",
        )


if __name__ == "__main__":
    main()

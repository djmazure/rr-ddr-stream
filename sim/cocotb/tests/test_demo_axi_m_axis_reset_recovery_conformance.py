# tests/test_demo_axi_m_axis_reset_recovery_conformance.py
"""CONFORMANCE HUNT (EPIC RTL-P2.719 / ticket RTL-P3.851) — MID-STREAM RESET on
the ddr_stream_AXI ``M_AXIS`` output (sibling of the AVMM reproducer for
DDR-T2.11).

ddr_stream_axi.vhd:478 drives ``m_axis_valid_o <= not fifoUserEmpty`` in the
normal (read-plane-present) config — the SAME unguarded combinational valid as
ddr_stream_avmm.vhd:216. (The ``m_axis_valid_o <= '0'`` at line 546 is the
read-plane-ELIDED config branch, not a reset guard.) So a mid-stream RST_SYS
should leave M_AXIS_VALID high → AXIS_RST violation (AMBA §2.7.1), identical to
DDR-T2.11. This test PROVES the sibling rather than inferring it.

Marked expect_fail per the bug-finding-is-success DoD (DDR-T2.11 covers both
IPs; remove the marker when the output valid is gated by reset). Do NOT weaken
the assertion. READY is left to the single driver coroutine (Haz.73).

Run:  rr sim run test_demo_axi_m_axis_reset_recovery_conformance
"""

from __future__ import annotations

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

_LOG = logging.getLogger("demo_axi_m_axis_reset")

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

_READY_PATTERN = (1, 0, 0, 0)


@cocotb.test()  # DDR-T2.11 FIXED (m_axis_valid_o reset-gated) — permanent guard
async def demo_axi_m_axis_reset_recovery(dut):
    """Drain M_AXIS under backpressure, assert RST_SYS mid-stream, require the
    AxiStreamMonitor to report ZERO AXIS violations across the reset (AXIS_RST:
    VALID must be 0 in reset). FAILS today = the WIN (DDR-T2.11 sibling)."""
    cocotb.start_soon(Clock(dut.CLK_DDR, DDR_PERIOD, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_SYS, SYS_PERIOD, units="ns").start())

    dut.RST_DDR.value = 1
    dut.RST_SYS.value = 1
    dut.S_AXIS_VALID.value = 0
    dut.M_AXIS_READY.value = 0
    dut.S_AXIS_DATA.value = 0

    bus = AxiStreamBus(
        tvalid=dut.M_AXIS_VALID,
        tready=dut.M_AXIS_READY,
        tdata=dut.M_AXIS_DATA,
    )
    mon = AxiStreamMonitor(
        bus, dut.CLK_SYS, dut.RST_SYS,
        log=_LOG, rst_active_low=False, channel="ddr_axi_m_axis_reset",
    )
    cocotb.start_soon(mon.run())

    axi = AxiBus.from_prefix(dut, "M_AXI")
    AxiRam(axi, dut.CLK_DDR, dut.RST_DDR, size=_RAM_SIZE)

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

    # ── Seed the ring. ───────────────────────────────────────────────────────
    N = 256
    for i in range(N):
        dut.S_AXIS_DATA.value = Immediate(i + 1)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)
    await Timer(150, units="us")

    # ── Backpressured drain, then strike RST_SYS mid-stream. ─────────────────
    drain_done = False

    async def ready_driver():
        idx = 0
        while not drain_done:
            dut.M_AXIS_READY.value = _READY_PATTERN[idx % len(_READY_PATTERN)]
            idx += 1
            await RisingEdge(dut.CLK_SYS)
    cocotb.start_soon(ready_driver())

    received = 0
    saw_valid_before_reset = False
    for _ in range(N * 8):
        await FallingEdge(dut.CLK_SYS)
        v = int(dut.M_AXIS_VALID.value)
        r = int(dut.M_AXIS_READY.value)
        if v:
            saw_valid_before_reset = True
        if v and r:
            received += 1
        if received >= 32:
            break

    assert saw_valid_before_reset, (
        "M_AXIS never asserted VALID before the reset strike — vacuous window. "
        "Stimulus/DUT enable path broken."
    )

    # ── STRIKE: RST_SYS mid-drain. AXIS_RST now requires VALID=0. READY left to
    # the single ready_driver (Haz.73 — no second driver / no Immediate-on-ready).
    await RisingEdge(dut.CLK_SYS)
    dut.RST_SYS.value = Immediate(1)
    for _ in range(8):
        await RisingEdge(dut.CLK_SYS)
    dut.RST_SYS.value = Immediate(0)
    for _ in range(8):
        await RisingEdge(dut.CLK_SYS)

    for _ in range(N * 4):
        await FallingEdge(dut.CLK_SYS)
        if int(dut.M_AXIS_VALID.value) and int(dut.M_AXIS_READY.value):
            received += 1
        if received >= N:
            break
    drain_done = True
    await Timer(20, units="us")

    _LOG.info(mon.report())
    dut._log.info(
        f"RTL-P3.851 axi M_AXIS mid-stream-reset: violations={mon.violation_count}"
    )
    assert mon.violation_count == 0, (
        "AXI M_AXIS committed an AXIS violation across a mid-stream reset "
        f"(AXIS_RST: VALID must be 0 in reset):\n{mon.report()}"
    )


def main() -> None:
    if run_simulation is None:
        raise RuntimeError(
            "invoke via `rr sim run test_demo_axi_m_axis_reset_recovery_conformance`"
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
            "invoke via `rr sim run test_demo_axi_m_axis_reset_recovery_conformance`"
        )
    for _top, _ddr_w in (
        ("wrapper_ddr_stream_axi", 128),
        ("wrapper_ddr_stream_fic", 64),
    ):
        run_simulation(
            top_level=_top,
            module="test_demo_axi_m_axis_reset_recovery_conformance",
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

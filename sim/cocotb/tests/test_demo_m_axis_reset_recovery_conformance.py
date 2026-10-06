# tests/test_demo_m_axis_reset_recovery_conformance.py
"""CONFORMANCE HUNT (EPIC RTL-P2.719 / ticket RTL-P3.851) — NEW attack vector:
MID-STREAM RESET on the ddr_stream_avmm ``M_AXIS`` output.

The existing test_demo_m_axis_bfm_conformance only walks RST_SYS at STARTUP, so
the AXIS_RST clause (VALID must be 0 in reset, AMBA §2.7.1) is checked once,
before any data is flowing. This test attacks the harder window: assert RST_SYS
*while M_AXIS is mid-drain with data still queued in the FWFT down-FIFO*, then
deassert and confirm the bus re-arms AXIS-clean.

A passive AxiStreamMonitor watches M_AXIS across the whole run. The win
conditions (a violation = a real DUT defect found):

  * AXIS_RST   — if M_AXIS_VALID stays HIGH during the mid-stream reset (the
                 output valid is not gated by RST_SYS — a very common miss when
                 a FWFT FIFO's dout_valid feeds the port directly), the monitor
                 fires here. THIS is the bug this vector hunts.
  * no X/Z     — VALID must never be metavalue during/after reset.
  * post-reset — VALID_STICKY / DATA_HOLD must hold once traffic resumes.

We deliberately do NOT assert data integrity across the reset: a reset may
legitimately drop in-flight ring data. We assert only AXIS PROTOCOL conformance
(the unambiguous, contract-defined behaviour) — never a harness-invented
expectation (bug-finding-is-success / no DUT workarounds).

Run:  rr sim run test_demo_m_axis_reset_recovery_conformance
"""

from __future__ import annotations

import logging
from pathlib import Path as _Path

import cocotb
from cocotb.clock import Clock
from cocotb.handle import Immediate
from cocotb.triggers import RisingEdge, FallingEdge, Timer

from rr_cocotb_tb.drivers.axi_stream import AxiStreamBus
from rr_cocotb_tb.monitors.axi_stream_monitor import AxiStreamMonitor
from sdk.cocotb_helpers.avmm_ram import AvmmBus, AvmmRam  # type: ignore

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None

_LOG = logging.getLogger("demo_m_axis_reset")

_IP_ROOT = _Path(__file__).resolve().parents[3]
_RTL_DIR = _IP_ROOT / "rtl"
_AVMM_DIR = _RTL_DIR / "avmm"
_SUPPORT_DIR = _RTL_DIR / "support"
_TB_DIR = _IP_ROOT / "sim" / "tb"
_OLO_SYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_sync" / "src" / "base" / "vhdl"
_OLO_ASYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_async" / "src" / "base" / "vhdl"
_OLO_WCN_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_n2xn" / "src" / "base" / "vhdl"
_OLO_WCXN_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_xn2n" / "src" / "base" / "vhdl"

_USER_W = 32
_DDR_W = 128
_DDR_ADDR_W = 32

DDR_PERIOD = 3.33
SYS_PERIOD = 10.0

# Sustained backpressure (1/4 ready) so M_AXIS still has an offered, un-accepted
# beat queued when we slam the reset mid-drain.
_READY_PATTERN = (1, 0, 0, 0)


@cocotb.test()  # DDR-T2.11 FIXED (m_axis_valid_o reset-gated) — permanent guard
async def demo_m_axis_reset_recovery(dut):
    """Drain M_AXIS under backpressure, assert RST_SYS mid-stream, and require
    the AxiStreamMonitor to report ZERO AXIS violations across the reset (chiefly
    AXIS_RST: VALID must drop to 0 in reset).

    DDR-T2.11 (PROVEN DEFECT, EPIC RTL-P2.719 / RTL-P3.851): this FAILS today and
    that is the WIN. ddr_stream_avmm.vhd:216 drives ``m_axis_valid_o <= not
    fifoUserEmpty`` with no sys-reset gating, so M_AXIS_VALID stays HIGH through a
    mid-stream RST_SYS → 8x AXIS_RST 'tvalid asserted during reset' (§2.7.1).
    Marked expect_fail per the bug-finding-is-success DoD: when the DUT gates the
    output valid with reset this test PASSES, cocotb 2.x reports the expect_fail
    as an unexpected pass, and the marker is removed → permanent conformance
    guard. Do NOT weaken the assertion to make it green."""
    DDR_ADDR_W = int(dut.G_DDR_ADDR_WIDTH.value)

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
        log=_LOG, rst_active_low=False, channel="ddr_m_axis_reset",
    )
    cocotb.start_soon(mon.run())

    avmm = AvmmBus.from_prefix(dut, "M_AVMM")
    AvmmRam(
        avmm, dut.CLK_DDR,
        reset=dut.RST_DDR, reset_active_level=True,
        size=1 << DDR_ADDR_W, read_latency=4,
    )

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

    # ── Drive backpressure; drain a few words; then reset MID-STREAM. ─────────
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
    # Drain ~32 words so the FWFT FIFO is mid-flight, then we strike.
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
        "M_AXIS never asserted VALID before the reset strike — the mid-stream "
        "reset window would be vacuous. Stimulus/DUT enable path broken."
    )

    # ── STRIKE: assert RST_SYS mid-drain. AXIS_RST now requires VALID=0. ──────
    # ready_driver keeps toggling M_AXIS_READY — we do NOT touch READY here
    # (the AXIS_RST clause is about VALID, independent of READY, and a second
    # driver on READY would race the monitor sample — Haz.73).
    await RisingEdge(dut.CLK_SYS)
    dut.RST_SYS.value = Immediate(1)
    # Hold reset several cycles — the monitor samples VALID each cycle and fires
    # AXIS_RST if the output valid is not gated by RST_SYS.
    for _ in range(8):
        await RisingEdge(dut.CLK_SYS)
    dut.RST_SYS.value = Immediate(0)
    for _ in range(8):
        await RisingEdge(dut.CLK_SYS)

    # ── Re-arm: resume backpressured drain so post-reset stickiness is checked.
    for _ in range(N * 4):
        await FallingEdge(dut.CLK_SYS)
        if int(dut.M_AXIS_VALID.value) and int(dut.M_AXIS_READY.value):
            received += 1
        if received >= N:
            break
    drain_done = True
    await Timer(20, units="us")

    # ── VERDICT — a violation here is a real DUT defect (the WIN). ────────────
    _LOG.info(mon.report())
    _LOG.info("observed corner-cases: %s", mon.observations())
    dut._log.info(
        f"RTL-P3.851 M_AXIS mid-stream-reset: violations={mon.violation_count}, "
        f"obs={mon.observations()}"
    )
    assert mon.violation_count == 0, (
        "M_AXIS committed an AXIS violation across a mid-stream reset "
        f"(chiefly AXIS_RST: VALID must be 0 in reset):\n{mon.report()}"
    )


def main() -> None:
    if run_simulation is None:
        raise RuntimeError(
            "invoke via `rr sim run test_demo_m_axis_reset_recovery_conformance`"
        )
    run_simulation(
        top_level="wrapper_ddr_stream_avmm",
        module="test_demo_m_axis_reset_recovery_conformance",
        custom_libraries={
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
                str(_AVMM_DIR / "avmm_w.vhd"),
                str(_AVMM_DIR / "avmm_r.vhd"),
                str(_AVMM_DIR / "uploader_avmm.vhd"),
                str(_AVMM_DIR / "downloader_avmm.vhd"),
                str(_RTL_DIR / "ddr_stream_avmm.vhd"),
                str(_TB_DIR / "wrappers" / "wrapper_ddr_stream_avmm.vhd"),
            ],
        },
        work_lib="rr",
        generics={
            "G_FIFO_DEPTH": 9,
            "G_USER_DATA_WIDTH": _USER_W,
            "G_DDR_DATA_WIDTH": _DDR_W,
            "G_DDR_ADDR_WIDTH": _DDR_ADDR_W,
            "G_BUFFER_WIDTH": 20,
            "G_FIFO_IMPL": "olo",
        },
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

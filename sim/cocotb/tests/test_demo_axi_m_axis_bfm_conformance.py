# tests/test_demo_axi_m_axis_bfm_conformance.py
"""CONFORMANCE HUNT (EPIC RTL-P2.719 / ticket RTL-P3.851) — confirm whether the
DDR-T2.9 defect that the AVMM backend (``ddr_stream_avmm``) exhibits ALSO affects
the AXI backend (``ddr_stream_axi``).

Sibling of ``test_demo_m_axis_bfm_conformance`` (the AVMM version, commit 5ac8b6d,
which found DDR-T2.9). Same recipe, different backend top + DDR responder:

    * top              : ``wrapper_ddr_stream_axi`` (AXI4 DMA master, not AVMM)
    * DDR responder    : ``cocotbext.axi.AxiRam`` on the flat ``M_AXI`` port
                         (the AVMM version used ``sdk.cocotb_helpers.avmm_ram``)
    * user output port : ``M_AXIS_DATA / M_AXIS_VALID / M_AXIS_READY`` on
                         ``CLK_SYS`` — IDENTICAL shape to the AVMM variant
                         (data/valid/ready, no tlast).

WHY THE AXI BACKEND IS EXPECTED TO BE AFFECTED (structural, not a guess):
the user-facing FWFT pass-through is the SAME three lines in both backends, fed by
the SHARED ``ddr_fifo_intf`` mixed-width down-FIFO:

    ddr_stream_avmm.vhd:215-217   m_axis_data_o  <= fifoUserDout;
                                  m_axis_valid_o <= not fifoUserEmpty;
                                  fifoUserRdEn   <= m_axis_valid_o and m_axis_ready_i;
    ddr_stream_axi.vhd :486-488   (identical)

``fifoUserDout`` is the FWFT-FIFO head and advances on its own ``rd_en`` /
empty-deassert independent of whether the consumer asserted READY — so under
READY-low backpressure the head word can be dropped with no handshake, the exact
AXIS_DATA_HOLD violation DDR-T2.9 names.

Live AXIS clauses on the M_AXIS port:
    AXIS_RST           VALID must be 0 in reset (AMBA §2.7.1)
    AXIS_VALID_STICKY  once VALID=1, hold it until READY=1 is seen (§2.2.1)
    AXIS_DATA_HOLD     DATA stable while VALID=1 && READY=0 (§2.2.1)

Round-trip: seed the ring by writing N words in via S_AXIS (no backpressure on
h2c), idle the producer so the uploader flushes to DDR over M_AXI (cocotbext
AxiRam responds), then drain via M_AXIS under a deterministic toggling
backpressure pattern (~3/8 ready, multi-cycle 0-runs — ROUTERTL-002: the
stimulus is hard-coded, never runtime-derived). Assert:
  * mon.violation_count == 0  (a violation here is the WIN — DDR-T2.9 on AXI)
  * backpressure genuinely coincided with VALID (non-vacuous)
  * the drained data matches the seeded data verbatim and in order

Run:  rr sim run test_demo_axi_m_axis_bfm_conformance
"""

from __future__ import annotations

import logging
from pathlib import Path as _Path

import cocotb
from cocotb.clock import Clock
from cocotb.handle import Immediate
from cocotb.triggers import RisingEdge, FallingEdge, Timer

from cocotbext.axi import AxiBus, AxiRam

# routertl verification moat — the validated passive AXIS protocol monitor.
from rr_cocotb_tb.drivers.axi_stream import AxiStreamBus
from rr_cocotb_tb.monitors.axi_stream_monitor import AxiStreamMonitor

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None

_LOG = logging.getLogger("demo_axi_m_axis_bfm")

_IP_ROOT = _Path(__file__).resolve().parents[3]      # rr-ddr-stream/
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
_RAM_SIZE = 1 << 20      # 1 MiB DDR model

DDR_PERIOD = 3.33   # ns, ~300 MHz
SYS_PERIOD = 10.0   # ns, 100 MHz

# Deterministic toggling backpressure on M_AXIS_READY (ROUTERTL-002: a fixed
# pattern, never runtime-derived). ~3/8 cycles ready, with multi-cycle 0-runs
# so the DUT must hold an offered beat (VALID high) across several READY-low
# cycles — the exact window AXIS_VALID_STICKY / AXIS_DATA_HOLD police.
_READY_PATTERN = (1, 0, 0, 1, 0, 0, 0, 1)


# DDR-T2.9 (EPIC RTL-P2.719 / RTL-P3.851): the AVMM backend FAILS this same
# reproducer (test_demo_m_axis_bfm_conformance). The AXI backend reuses the SAME
# FWFT pass-through (ddr_stream_axi.vhd:486-488, identical to the AVMM 215-217)
# over the SAME shared ddr_fifo_intf down-FIFO, so it is expected to FAIL too —
# DDR-T2.9 RESOLVED 2026-06-15 as a HARNESS ARTIFACT (Immediate-driven READY
# racing the monitor's post-edge sample), on BOTH backends — not a DUT defect.
# The AXI M_AXIS bus is AXIS-compliant; no skid was added. This test now drives
# READY with deferred writes and is a PERMANENT conformance guard for the AXI
# backend. The assertions were INTACT throughout (no weakening).
@cocotb.test()
async def demo_axi_m_axis_monitor_under_backpressure(dut):
    """Drive a full round-trip through the AXI backend then drain M_AXIS under
    toggling backpressure; the native AxiStreamMonitor watches M_AXIS and must
    report zero AXIS violations while the DATA_HOLD/VALID_STICKY windows are
    genuinely exercised.

    DDR-T2.9: this PASSES — the AXI M_AXIS bus is AXIS-compliant. The earlier
    "failure" was a harness artifact (Immediate-driven READY racing the
    monitor's post-edge sample), now fixed by deferred READY driving. Do NOT
    weaken the assertions below; if this ever goes RED it is a real AXIS break.
    """
    DDR_ADDR_W = int(dut.G_DDR_ADDR_WIDTH.value)

    # ── Attach the monitor BEFORE reset deasserts (so AXIS_RST is checked). ──
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
        log=_LOG, rst_active_low=False, channel="ddr_axi_m_axis",
    )
    cocotb.start_soon(mon.run())   # live across the reset window below

    # ── DDR responder: proven cocotbext AxiRam on the flat M_AXI master port. ──
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
    dut.RST_SYS.value = 0          # AXIS_RST window now closes
    for _ in range(5):
        await RisingEdge(dut.CLK_SYS)
    await Timer(3, units="us")
    await RisingEdge(dut.CLK_SYS)

    # ── Seed the ring: write N words in via S_AXIS (no backpressure on h2c). ──
    N = 256
    expected = list(range(1, N + 1))
    for i in range(N):
        dut.S_AXIS_DATA.value = Immediate(i + 1)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)

    # Let the h2c DMA land the words in DDR (and idle-flush the partial tail).
    await Timer(150, units="us")

    # ── Drain via M_AXIS under TOGGLING backpressure. ────────────────────────
    # HARNESS NOTE (DDR-T2.9, 2026-06-15): drive M_AXIS_READY with cocotb's
    # *deferred* write (``.value =`` in a synchronous coroutine), NOT the inline
    # ``Immediate(...)`` this test originally used — that Immediate write raced
    # the monitor's post-edge sample and manufactured a bogus AXIS_DATA_HOLD
    # "violation" on the (compliant) combinational-FWFT M_AXIS. DDR-T2.9 was a
    # harness artifact on BOTH backends, not a DUT defect. See the AVMM sibling
    # test + the routertl Immediate-READY hazard ticket.
    drain_done = False

    async def ready_driver():
        idx = 0
        while not drain_done:
            dut.M_AXIS_READY.value = _READY_PATTERN[idx % len(_READY_PATTERN)]  # deferred
            idx += 1
            await RisingEdge(dut.CLK_SYS)
    cocotb.start_soon(ready_driver())

    # Capture the STEADY mid-cycle bus state (FallingEdge) — phase-robust.
    received: list[int] = []
    backpressure_coincided = 0     # cycles with VALID=1 && READY=0 (real BP)
    max_cycles = N * 64
    for _ in range(max_cycles):
        await FallingEdge(dut.CLK_SYS)
        v = int(dut.M_AXIS_VALID.value)
        r = int(dut.M_AXIS_READY.value)
        if v and r:
            received.append(int(dut.M_AXIS_DATA.value))
        if v and not r:
            backpressure_coincided += 1
        if len(received) >= N:
            break
    drain_done = True
    await Timer(20, units="us")

    # ── VERDICT ─────────────────────────────────────────────────────────────
    _LOG.info(mon.report())
    _LOG.info("observed corner-cases: %s", mon.observations())
    dut._log.info(
        f"RTL-P3.851 AXI M_AXIS drain: received={len(received)}/{N}, "
        f"VALID&&!READY (backpressure-coincident) cycles={backpressure_coincided}"
    )

    # Backpressure must have genuinely coincided with an offered beat, else the
    # DATA_HOLD / VALID_STICKY rules were vacuously skipped (yellow flag).
    assert backpressure_coincided > 0, (
        "Backpressure never coincided with VALID — the DATA_HOLD/VALID_STICKY "
        "windows were not exercised; the conformance result would be vacuous. "
        "Stimulus or DUT enable path is broken; do NOT relax this floor."
    )

    # The conformance verdict — a violation here is a WIN (DDR-T2.9 on AXI).
    assert mon.violation_count == 0, (
        f"AXI M_AXIS output committed AXIS violation(s) under backpressure:\n"
        f"{mon.report()}"
    )

    obs = mon.observations()

    # Data integrity under backpressure: every drained word in order.
    assert len(received) == N, (
        f"AXI M_AXIS drained {len(received)} of {N} words under backpressure — "
        f"short drain. Stream stalled or dropped beats."
    )
    diffs = [
        (i, hex(exp), hex(got))
        for i, (exp, got) in enumerate(zip(expected, received))
        if exp != got
    ]
    assert not diffs, (
        f"AXI M_AXIS stream corrupted/reordered under backpressure: "
        f"{len(diffs)} mismatch(es). first 5: {diffs[:5]}"
    )

    dut._log.info(
        f"RTL-P3.851: AXI M_AXIS CLEAN under backpressure — 0 violations, "
        f"{len(received)} words verbatim, obs={obs}"
    )


# ──────────────────────────────────────────────────────────────
#  Engine entry point (ROUTERTL-001). Source list mirrors
#  test_demo_axi4_bfm_conformance (the AXI backend source set).
# ──────────────────────────────────────────────────────────────


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
        raise RuntimeError("invoke via `rr sim run test_demo_axi_m_axis_bfm_conformance`")
    for _top, _ddr_w in (
        ("wrapper_ddr_stream_axi", 128),
        ("wrapper_ddr_stream_fic", 64),
    ):
        run_simulation(
            top_level=_top,
            module="test_demo_axi_m_axis_bfm_conformance",
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

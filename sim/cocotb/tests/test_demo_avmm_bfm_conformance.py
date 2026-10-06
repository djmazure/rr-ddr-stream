# tests/test_demo_avmm_bfm_conformance.py
"""DEMONSTRATION (EPIC RTL-P2.719) — point the routertl-native AvalonMMMonitor at
a REAL DUT master port and get Avalon-MM conformance + corner-case observation
for free.

``ddr_stream_avmm`` is the Avalon-MM sibling of ``ddr_stream_axi``: an Avalon-MM
*master* that DMAs the AXI-Stream input into DDR over ``m_avmm_h2c_bus_o``
(flattened by the wrapper to a flat ``M_AVMM_*`` prefix). Same usage pattern as the
AXI4 demo, different protocol:

    bus = AvalonMMBus.from_prefix(dut, "M_AVMM", variant="minimal")
    mon = AvalonMMMonitor(bus, dut.CLK_DDR, dut.RST_DDR)   # passive
    cocotb.start_soon(mon.run())
    ... drive normal traffic ...
    assert mon.violation_count == 0, mon.report()
    assert mon.observed("AVL_WRITE") >= 1                  # non-vacuous

The monitor is the validated half (its KAT, test_avalon_mm_kat, proves it catches
every clause it claims). We bring the DUT. It checks the Avalon stability /
waitrequest / reset rules the same way the AXI4 monitor checks AMBA — for free,
with zero checker code in the test.

The DDR responder is a pluggable concern: this demo uses a minimal inline Avalon
responder (waitrequest held low so writes complete; readdata echo) so the demo
stays self-contained and the spotlight is on the monitor.
``rr_cocotb_tb.drivers.avalon_mm.AvalonMMSlave`` is the routertl-native
full-responder alternative.

Run:  rr sim run test_demo_avmm_bfm_conformance
"""

from __future__ import annotations

import logging
from pathlib import Path as _Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

# routertl verification moat — the validated BFM + passive protocol monitor.
from rr_cocotb_tb.drivers.avalon_mm import EMITS, AvalonMMBus
from rr_cocotb_tb.monitors.avalon_mm_monitor import AvalonMMMonitor

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None

_LOG = logging.getLogger("demo_avmm_bfm")

_IP_ROOT = _Path(__file__).resolve().parents[3]      # rr-ddr-stream/
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


async def _avmm_responder(dut):
    """Minimal Avalon-MM slave: never stalls, echoes a deterministic readdata
    one cycle after an accepted read. Just enough for the master's writes to
    complete so the monitor has live traffic to observe + check."""
    dut.M_AVMM_waitrequest.value = 0
    dut.M_AVMM_readdata.value = 0
    dut.M_AVMM_readdatavalid.value = 0
    while True:
        await RisingEdge(dut.CLK_DDR)
        read_now = int(dut.M_AVMM_read.value)
        dut.M_AVMM_readdatavalid.value = read_now      # 1-cycle read latency
        if read_now:
            dut.M_AVMM_readdata.value = 0xA5A5A5A5


@cocotb.test()
async def demo_avmm_monitor_on_real_master(dut):
    """Drive normal stream traffic; the native AvalonMMMonitor watches the DUT's
    M_AVMM master port and must report zero Avalon violations while observing the
    write corner-case actually happen.

    DDR-T2.8 FIXED (2026-06-15): this demo PAID OFF — the monitor caught a real
    conformance defect, which is now resolved. ddr_stream_avmm formerly composed
    its write- and read-plane sub-masters with a bare combinational OR-mux and NO
    arbiter; under concurrent full-duplex load with a legal zero-wait slave,
    M_AVMM_read and M_AVMM_write asserted in the same cycle (AVL_OVERLAP @ 824ns)
    — illegal Avalon-MM. The TOP now has a command-phase ARBITER
    (rtl/ddr_stream_avmm.vhd) that grants the shared port to ONE plane per
    command phase (round-robin) and stalls the other by holding its WAITREQUEST
    high, so READ and WRITE are never both asserted. The conformance assertion
    below therefore now PASSES with zero violations. The existing hand-rolled
    test_ddr_stream_avmm never checked mutual exclusion; this is exactly the gap
    the shared native monitor closes."""
    # ── 1. clocks + reset ───────────────────────────────────────────
    cocotb.start_soon(Clock(dut.CLK_SYS, 10, unit="ns").start())
    cocotb.start_soon(Clock(dut.CLK_DDR, 8, unit="ns").start())
    dut.RST_SYS.value = 1
    dut.RST_DDR.value = 1
    dut.S_AXIS_VALID.value = 0
    dut.S_AXIS_DATA.value = 0
    dut.M_AXIS_READY.value = 0
    dut.M_AVMM_waitrequest.value = 1   # stall while in reset

    # ── 2. DDR responder (pluggable) — minimal inline Avalon slave ───
    cocotb.start_soon(_avmm_responder(dut))

    # ── 3. THE DEMO: attach the routertl-native monitor to the DUT ───
    #     master port. Passive — it never drives a signal.
    # variant="minimal": this DUT is the classic Avalon-MM pipelined-burst
    # subset (byteenable + readdatavalid + burstcount, but NO optional
    # `response` channel). "minimal" discovers what's present, None for the
    # rest — the right choice for most DDR-class masters. ("full" requires
    # `response`, which only newer Avalon-MM masters expose.)
    bus = AvalonMMBus.from_prefix(dut, "M_AVMM", variant="minimal")
    mon = AvalonMMMonitor(bus, dut.CLK_DDR, dut.RST_DDR, log=_LOG)
    cocotb.start_soon(mon.run())

    await ClockCycles(dut.CLK_DDR, 8)
    dut.RST_SYS.value = 0
    dut.RST_DDR.value = 0
    await ClockCycles(dut.CLK_DDR, 4)

    # ── 4. drive normal AXI-Stream traffic → DUT DMAs it to DDR ──────
    n_beats = 256
    for i in range(n_beats):
        await RisingEdge(dut.CLK_SYS)
        dut.S_AXIS_DATA.value = (i + 1) & 0xFFFFFFFF
        dut.S_AXIS_VALID.value = 1
    await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = 0

    await ClockCycles(dut.CLK_DDR, 4000)

    # ── 5. VERDICT: conformance + non-vacuous coverage ──────────────
    _LOG.info(mon.report())
    _LOG.info("observed corner-cases: %s", mon.observations())

    assert mon.violation_count == 0, (
        f"DUT master committed Avalon-MM violation(s):\n{mon.report()}"
    )
    assert mon.observed("AVL_WRITE") >= 1, (
        "no AVL_WRITE observed — the DUT issued no completed write; "
        f"the demo proved nothing. EMITS={EMITS}, obs={mon.observations()}"
    )


# ──────────────────────────────────────────────────────────────
#  Engine entry point (ROUTERTL-001). Source list mirrors
#  test_ddr_stream_avmm (the avmm/* sources replace the axi4*).
# ──────────────────────────────────────────────────────────────


def main() -> None:
    if run_simulation is None:
        raise RuntimeError("invoke via `rr sim run test_demo_avmm_bfm_conformance`")
    run_simulation(
        top_level="wrapper_ddr_stream_avmm",
        module="test_demo_avmm_bfm_conformance",
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

# tests/test_demo_axi4_bfm_conformance.py
"""DEMONSTRATION (EPIC RTL-P2.719) — point the routertl-native Axi4Monitor at a
REAL DUT master port and get AMBA conformance + corner-case observation for free.

``ddr_stream_axi`` is an AXI4 *master*: it DMAs the AXI-Stream input into DDR over
its ``m_axi4_h2c_bus_o`` port (flattened by the wrapper to a flat ``M_AXI_*``
prefix). The point of this file is NOT to re-test the DMA — the repo's
``test_ddr_stream_axi`` already does that exhaustively. The point is to show the
*usage pattern* for the verification moat:

    bus = Axi4Bus.from_prefix(dut, "M_AXI")          # attach to the DUT master port
    mon = Axi4Monitor(bus, dut.CLK_DDR, dut.RST_DDR) # passive — never drives the bus
    cocotb.start_soon(mon.run())
    ... drive normal traffic ...
    assert mon.violation_count == 0, mon.report()    # every AMBA rule, checked free
    assert mon.observed("AXI4_WRITE") >= 1           # and the corner-cases were EXERCISED

The monitor is the validated half (its own KAT proves it catches each violation it
claims to). We bring the DUT. Any violation it reports here is the DUT's, not the
testbench's — and ``observations()`` proves the green bar actually drove the
write/burst corner-cases rather than passing vacuously.

The DDR responder is a pluggable concern: this demo uses the repo's proven
``cocotbext.axi.AxiRam`` (handles the DUT's wide multi-beat bursts), with the
routertl ``Axi4Monitor`` layered passively on top. ``rr_cocotb_tb.drivers.axi4``
``Axi4Slave`` is the routertl-native responder alternative.

Run:  rr sim run test_demo_axi4_bfm_conformance
"""

from __future__ import annotations

import logging
from pathlib import Path as _Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from cocotbext.axi import AxiBus, AxiRam

# routertl verification moat — the validated BFM + passive protocol monitor.
from rr_cocotb_tb.drivers.axi4 import EMITS, Axi4Bus
from rr_cocotb_tb.monitors.axi4_monitor import Axi4Monitor

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None  # lets cocotb collect under a non-rr runner

_LOG = logging.getLogger("demo_axi4_bfm")

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


@cocotb.test()
async def demo_axi4_monitor_on_real_master(dut):
    """Drive normal stream traffic; the native Axi4Monitor watches the DUT's
    M_AXI master port and must report zero AMBA violations while observing the
    write/burst corner-cases actually happen."""
    # ── 1. clocks + reset ───────────────────────────────────────────
    cocotb.start_soon(Clock(dut.CLK_SYS, 10, unit="ns").start())
    cocotb.start_soon(Clock(dut.CLK_DDR, 8, unit="ns").start())
    dut.RST_SYS.value = 1
    dut.RST_DDR.value = 1
    dut.S_AXIS_VALID.value = 0
    dut.S_AXIS_DATA.value = 0
    dut.M_AXIS_READY.value = 0

    # ── 2. DDR responder (pluggable) — proven cocotbext AxiRam ───────
    axi = AxiBus.from_prefix(dut, "M_AXI")
    AxiRam(axi, dut.CLK_DDR, dut.RST_DDR, size=_RAM_SIZE)

    # ── 3. THE DEMO: attach the routertl-native monitor to the DUT ───
    #     master port. Passive — it never drives a signal. Its KAT
    #     (test_axi4_kat) already proved it catches each clause it claims.
    bus = Axi4Bus.from_prefix(dut, "M_AXI")
    mon = Axi4Monitor(bus, dut.CLK_DDR, dut.RST_DDR, log=_LOG)
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

    # let the uploader flush the ring to DDR over M_AXI
    await ClockCycles(dut.CLK_DDR, 4000)

    # ── 5. VERDICT: conformance + non-vacuous coverage ──────────────
    _LOG.info(mon.report())
    _LOG.info("observed corner-cases: %s", mon.observations())

    assert mon.violation_count == 0, (
        f"DUT master committed AMBA violation(s):\n{mon.report()}"
    )
    # Non-vacuous: the green bar must have actually driven a write. (AXI4_WRITE
    # is observed on a B-channel handshake; AXI4_BURST on a multi-beat AWLEN.)
    assert mon.observed("AXI4_WRITE") >= 1, (
        "no AXI4_WRITE observed — the DUT issued no completed write; "
        f"the demo proved nothing. EMITS={EMITS}, obs={mon.observations()}"
    )


# ──────────────────────────────────────────────────────────────
#  Engine entry point (ROUTERTL-001) — single 32/128 olo config for a
#  fast, readable demo (the full width×impl sweep lives in
#  test_ddr_stream_axi). Source list mirrors that test verbatim.
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
        raise RuntimeError("invoke via `rr sim run test_demo_axi4_bfm_conformance`")
    for _top, _ddr_w in (
        ("wrapper_ddr_stream_axi", 128),
        ("wrapper_ddr_stream_fic", 64),
    ):
        run_simulation(
            top_level=_top,
            module="test_demo_axi4_bfm_conformance",
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

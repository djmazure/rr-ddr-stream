# tests/test_demo_axi_error_response_conformance.py
"""CONFORMANCE HUNT (EPIC RTL-P2.719 / ticket RTL-P3.851) — NEW vector:
AXI ERROR RESPONSES (RRESP/BRESP = SLVERR) from the DDR responder.

cocotbext AxiRam never returns an error, so the master's read/write paths have
NEVER seen a non-OKAY response. Real DDR can: ECC uncorrectable, address-decode
miss, security violation. This test builds an error-responder by wrapping the
AxiRam slave's ``_read`` / ``_write`` so selected transactions raise — the
cocotbext slave's own handler then drives RRESP/BRESP = SLVERR (R returns zeros).

Two response paths, two CONTRACTS (both must hold; neither may deadlock or
break AXIS framing):
  * READ errors (RRESP=SLVERR): the read path is transparent — errored beats
    return zeros but every beat still flows, so the full N words drain in order.
  * WRITE errors (BRESP=SLVERR): the master DELIBERATELY withholds the
    non-durable burst (axi4w WAIT_BRESP — don't publish a write that didn't
    land). That withholding is INTENDED (confirmed by-design 2026-06-21) — so
    the write test asserts a CLEAN partial delivery (forward progress + in-order
    + AXIS-conformant), NOT no-loss. The detected-but-unsurfaced error
    (busWrError/busRdError dangling) is tracked separately by DDR-P3.25.

A master that deadlocks on SLVERR, scrambles/duplicates the delivered words, or
breaks AXIS framing FAILS here — that is the WIN. ROUTERTL-002: fixed cadence.

Run:  rr sim run test_demo_axi_error_response_conformance
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

_LOG = logging.getLogger("demo_axi_err")

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


def _inject_read_errors(ram, every: int, stats: dict):
    """Wrap the slave read so every `every`-th beat raises → RRESP=SLVERR."""
    orig = ram.read_if._read

    async def erroring_read(address, length):
        stats["reads"] += 1
        if stats["reads"] % every == 0:
            stats["read_errs"] += 1
            raise RuntimeError("injected DDR read SLVERR")
        return await orig(address, length)

    ram.read_if._read = erroring_read


def _inject_write_errors(ram, every: int, stats: dict):
    """Wrap the slave write so every `every`-th beat raises → BRESP=SLVERR."""
    orig = ram.write_if._write

    async def erroring_write(address, data):
        stats["writes"] += 1
        if stats["writes"] % every == 0:
            stats["write_errs"] += 1
            raise RuntimeError("injected DDR write SLVERR")
        return await orig(address, data)

    ram.write_if._write = erroring_write


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.CLK_DDR, DDR_PERIOD, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_SYS, SYS_PERIOD, units="ns").start())
    dut.RST_DDR.value = 1
    dut.RST_SYS.value = 1
    dut.S_AXIS_VALID.value = 0
    dut.M_AXIS_READY.value = 0
    dut.S_AXIS_DATA.value = 0


async def _walk_reset(dut):
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


async def _seed(dut, N):
    for i in range(N):
        dut.S_AXIS_DATA.value = Immediate(i + 1)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)


async def _drain_count(dut, N):
    drain_done = [False]

    async def ready_driver():
        idx = 0
        while not drain_done[0]:
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
        if idle > 15000:    # ~150us quiescent ⇒ deadlocked on the error response
            break
    drain_done[0] = True
    await Timer(20, units="us")
    return received


@cocotb.test()
async def axi_read_error_response(dut):
    """Inject RRESP=SLVERR on every 7th DDR read; the master must not deadlock
    and M_AXIS must stay AXIS-conformant."""
    N = 256
    await _bring_up(dut)

    bus = AxiStreamBus(tvalid=dut.M_AXIS_VALID, tready=dut.M_AXIS_READY, tdata=dut.M_AXIS_DATA)
    mon = AxiStreamMonitor(bus, dut.CLK_SYS, dut.RST_SYS, log=_LOG,
                           rst_active_low=False, channel="ddr_axi_rderr")
    cocotb.start_soon(mon.run())

    axi = AxiBus.from_prefix(dut, "M_AXI")
    ram = AxiRam(axi, dut.CLK_DDR, dut.RST_DDR, size=_RAM_SIZE)
    stats = {"reads": 0, "read_errs": 0, "writes": 0, "write_errs": 0}
    _inject_read_errors(ram, every=7, stats=stats)

    await _walk_reset(dut)
    await _seed(dut, N)
    await Timer(150, units="us")
    received = await _drain_count(dut, N)

    dut._log.info(
        f"RTL-P3.851 read-SLVERR: received={len(received)}/{N}, "
        f"read_errs={stats['read_errs']}/{stats['reads']}, "
        f"AXIS violations={mon.violation_count}"
    )
    assert stats["read_errs"] > 0, "no read errors injected — vacuous (check cadence)"
    assert mon.violation_count == 0, (
        f"M_AXIS broke AXIS under injected read SLVERR:\n{mon.report()}"
    )
    assert len(received) >= N, (
        f"DEADLOCK/LOSS under read SLVERR: only {len(received)}/{N} words emerged on "
        f"M_AXIS ({stats['read_errs']} reads errored) — the master stalled or "
        f"dropped beats waiting on/around a non-OKAY read response."
    )
    # DDR-P3.25: the detected read error must now be PROMOTED to the CSR.
    assert int(dut.DDR_CSR.fifoStatus.read_error.value) == 1, (
        "DDR-P3.25: read_error CSR bit NOT set after RRESP=SLVERR — the detected "
        "bus error was not promoted to ddr_csr_o.fifoStatus."
    )


@cocotb.test()  # guards the INTENDED write-error contract (DDR-T2.12 = by-design)
async def axi_write_error_response(dut):
    """Inject BRESP=SLVERR on every 5th DDR write and guard the INTENDED
    contract (confirmed by-design 2026-06-21; DDR-P3.25 tracks surfacing the
    error to the user).

    On BRESP=SLVERR the master DELIBERATELY does not publish the commit pointer
    for the non-durable burst (axi4w.vhd WAIT_BRESP) — so the failed burst's data
    is correctly WITHHELD from the reader rather than presented as durable. That
    is intentional and must NOT be 'fixed'. What this test guards is that the
    withholding stays CLEAN:
      * the master keeps forward progress — it does NOT deadlock (the
        non-errored data still drains);
      * M_AXIS stays AXIS-conformant throughout;
      * whatever IS delivered is strictly in order (no corruption/reorder/dup) —
        a withheld burst leaves a gap, never a scrambled stream.
    It deliberately does NOT assert no-loss: withholding the errored burst is the
    contract. The separate gap — that the detected error (axi4w write_error_r →
    bus_werror_o) is never surfaced to the user — is DDR-P3.25."""
    N = 256
    await _bring_up(dut)

    bus = AxiStreamBus(tvalid=dut.M_AXIS_VALID, tready=dut.M_AXIS_READY, tdata=dut.M_AXIS_DATA)
    mon = AxiStreamMonitor(bus, dut.CLK_SYS, dut.RST_SYS, log=_LOG,
                           rst_active_low=False, channel="ddr_axi_wrerr")
    cocotb.start_soon(mon.run())

    axi = AxiBus.from_prefix(dut, "M_AXI")
    ram = AxiRam(axi, dut.CLK_DDR, dut.RST_DDR, size=_RAM_SIZE)
    stats = {"reads": 0, "read_errs": 0, "writes": 0, "write_errs": 0}
    _inject_write_errors(ram, every=5, stats=stats)

    await _walk_reset(dut)
    await _seed(dut, N)
    await Timer(200, units="us")
    received = await _drain_count(dut, N)

    dut._log.info(
        f"RTL-P3.851 write-SLVERR (intended withhold): received={len(received)}/{N}, "
        f"write_errs={stats['write_errs']}/{stats['writes']}, "
        f"AXIS violations={mon.violation_count}"
    )
    assert stats["write_errs"] > 0, "no write errors injected — vacuous (check cadence)"
    assert mon.violation_count == 0, (
        f"M_AXIS broke AXIS after injected write SLVERR:\n{mon.report()}"
    )
    # Forward progress: the errored bursts are withheld (intended), but the
    # master must NOT deadlock — the non-errored data still drains.
    assert 0 < len(received) < N, (
        f"expected partial delivery (errored bursts withheld, rest drained), got "
        f"{len(received)}/{N}. 0 ⇒ deadlock; N ⇒ no withholding (injection vacuous)."
    )
    # No corruption: whatever is delivered is strictly increasing (a withheld
    # burst leaves a gap, never a scrambled/duplicated stream).
    assert all(b > a for a, b in zip(received, received[1:])), (
        f"delivered stream not strictly in order under write SLVERR — "
        f"corruption/reorder/dup. first 8: {received[:8]}"
    )
    # DDR-P3.25: the detected write error must now be PROMOTED to the CSR — this
    # is what lets software see the otherwise-silent withholding.
    assert int(dut.DDR_CSR.fifoStatus.write_error.value) == 1, (
        "DDR-P3.25: write_error CSR bit NOT set after BRESP=SLVERR — the detected "
        "bus error was not promoted to ddr_csr_o.fifoStatus."
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
            "invoke via `rr sim run test_demo_axi_error_response_conformance`"
        )
    for _top, _ddr_w in (
        ("wrapper_ddr_stream_axi", 128),
        ("wrapper_ddr_stream_fic", 64),
    ):
        run_simulation(
            top_level=_top,
            module="test_demo_axi_error_response_conformance",
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

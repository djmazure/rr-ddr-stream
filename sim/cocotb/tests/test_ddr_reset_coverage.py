"""
DDR-T2.16 — reset coverage for the software-filled downloader ring.

The existing software-write-pointer tests intentionally wait out reset-busy
settling before applying stimulus. These tests watch that window directly:
reset must keep AXI/AXIS masters quiescent, reset recovery must work after a
mid-stream burst, and asymmetric reset release must not create stale empty state.

Run via:
    rr sim run test_ddr_reset_coverage
"""
from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles
from cocotbext.axi import AxiBus, AxiRam

from test_ddr_swptr_fill import (  # type: ignore
    CLK_DDR_NS,
    CLK_SYS_NS,
    LEAD,
    RING,
    _GENERICS,
    _custom_libraries,
)

try:
    from engine.simulation import run_simulation  # type: ignore
except ImportError:
    run_simulation = None

try:
    from sdk.cocotb_helpers import requires  # type: ignore
except ImportError:
    def requires(*_a, **_k):  # fallback when run outside the rr sim engine
        def _d(f):
            return f
        return _d


RESET_BUSY_DDR_CYCLES = 20
POST_RESET_SETTLE_DDR_CYCLES = 100


def _safe_int(signal) -> int | None:
    try:
        return int(signal.value)
    except ValueError:
        return None


async def _start_and_reset_inputs(dut) -> AxiRam:
    cocotb.start_soon(Clock(dut.CLK_SYS, CLK_SYS_NS, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_DDR, CLK_DDR_NS, units="ns").start())
    dut.RST_SYS.value = 1
    dut.RST_DDR.value = 1
    dut.S_AXIS_DATA.value = 0
    dut.S_AXIS_VALID.value = 0
    dut.M_AXIS_READY.value = 0
    dut.CSR_WR_BASE_ADDR.value = 0
    dut.CSR_WR_BASE_LOAD.value = 0
    dut.CSR_RD_BASE_ADDR.value = 0
    dut.CSR_RD_BASE_LOAD.value = 0
    dut.CSR_RD_PTR_ADDR.value = 0
    dut.CSR_RD_PTR_LOAD.value = 0
    dut.CSR_WR_PTR_ADDR.value = 0
    dut.CSR_WR_PTR_LOAD.value = 0
    ram = AxiRam(AxiBus.from_prefix(dut, "M_AXI"), dut.CLK_DDR, dut.RST_DDR, size=RING)
    await ClockCycles(dut.CLK_DDR, 2)
    return ram


async def _cold_reset_settle(dut) -> AxiRam:
    ram = await _start_and_reset_inputs(dut)
    await ClockCycles(dut.CLK_DDR, 50)
    dut.RST_SYS.value = 0
    dut.RST_DDR.value = 0
    await ClockCycles(dut.CLK_DDR, 80)
    return ram


async def _pulse_wr_ptr(dut, offset: int) -> None:
    dut.CSR_WR_PTR_ADDR.value = offset & (RING - 1)
    dut.CSR_WR_PTR_LOAD.value = 1
    await RisingEdge(dut.CLK_SYS)
    dut.CSR_WR_PTR_LOAD.value = 0


async def _sample_reset_window(dut, cycles: int = RESET_BUSY_DDR_CYCLES) -> dict[str, int]:
    counts = {"ar": 0, "aw": 0, "tvalid": 0}
    for _ in range(cycles):
        await RisingEdge(dut.CLK_DDR)
        if _safe_int(dut.M_AXI_ARVALID) == 1:
            counts["ar"] += 1
        if _safe_int(dut.M_AXI_AWVALID) == 1:
            counts["aw"] += 1
        if _safe_int(dut.M_AXIS_VALID) == 1:
            counts["tvalid"] += 1
    return counts


async def _watch_ar_handshakes(dut, ar: list[int]) -> None:
    while True:
        await RisingEdge(dut.CLK_DDR)
        arvalid = _safe_int(dut.M_AXI_ARVALID)
        arready = _safe_int(dut.M_AXI_ARREADY)
        if arvalid == 1 and arready == 1:
            ar.append(int(dut.M_AXI_ARADDR.value))


async def _watch_awvalid(dut, awvalid_seen: list[int]) -> None:
    while True:
        await RisingEdge(dut.CLK_DDR)
        if _safe_int(dut.M_AXI_AWVALID) == 1:
            awvalid_seen.append(int(dut.M_AXI_AWADDR.value))


async def _wait_for_ar_count(dut, ar: list[int], minimum: int, cycles: int) -> None:
    for _ in range(cycles):
        if len(ar) >= minimum:
            return
        await RisingEdge(dut.CLK_DDR)
    assert len(ar) >= minimum, (
        "observed %d ARADDR transaction(s), expected at least %d within %d CLK_DDR cycles"
        % (len(ar), minimum, cycles))


async def _assert_chip_empty_or_no_ar(dut, cycles: int) -> None:
    chip_empty = getattr(dut, "chip_empty", None)
    if chip_empty is not None:
        assert int(chip_empty.value) == 1, (
            "chip_empty must be 1 after reset on an unwritten ring; got %d"
            % int(chip_empty.value))
        return

    # The wrapper exposes chip_empty only inside the DDR_CSR record; if the
    # simulator does not flatten it, no-read behavior is the observable guard.
    arvalid = 0
    for _ in range(cycles):
        await RisingEdge(dut.CLK_DDR)
        if _safe_int(dut.M_AXI_ARVALID) == 1:
            arvalid += 1
    assert arvalid == 0, (
        "M_AXI_ARVALID asserted %d time(s) on an unwritten ring after reset; "
        "expected 0 read issues when chip_empty is not directly observable"
        % arvalid)


@cocotb.test()
@requires("DDR-REQ-RST-1")
async def reset_busy_no_axi_traffic(dut):
    await _start_and_reset_inputs(dut)
    await ClockCycles(dut.CLK_DDR, 50)
    reset_counts = await _sample_reset_window(dut)
    dut.RST_SYS.value = 0
    dut.RST_DDR.value = 0

    counts = await _sample_reset_window(dut)

    assert reset_counts["ar"] == 0, (
        "M_AXI_ARVALID asserted %d time(s) while RST_DDR was high; expected 0"
        % reset_counts["ar"])
    assert reset_counts["aw"] == 0, (
        "M_AXI_AWVALID asserted %d time(s) while RST_DDR was high; expected 0"
        % reset_counts["aw"])
    assert reset_counts["tvalid"] == 0, (
        "M_AXIS_VALID asserted %d time(s) while RST_DDR was high; expected 0"
        % reset_counts["tvalid"])
    assert counts["ar"] == 0, (
        "M_AXI_ARVALID asserted %d time(s) during the 20-cycle reset-busy window; expected 0"
        % counts["ar"])
    assert counts["aw"] == 0, (
        "M_AXI_AWVALID asserted %d time(s) during the 20-cycle reset-busy window; expected 0"
        % counts["aw"])
    assert counts["tvalid"] == 0, (
        "M_AXIS_VALID asserted %d time(s) during the 20-cycle reset-busy window; expected 0"
        % counts["tvalid"])

    await ClockCycles(dut.CLK_DDR, 80)
    await _assert_chip_empty_or_no_ar(dut, 200)


@cocotb.test()
@requires("DDR-REQ-RST-2")
async def empty_cdc_post_reset_chip_empty_is_one(dut):
    await _start_and_reset_inputs(dut)
    await ClockCycles(dut.CLK_DDR, 50)
    dut.RST_SYS.value = 0
    dut.RST_DDR.value = 0
    await ClockCycles(dut.CLK_DDR, POST_RESET_SETTLE_DDR_CYCLES)

    await _assert_chip_empty_or_no_ar(dut, 200)


@cocotb.test()
@requires("DDR-REQ-RST-3")
async def mid_stream_reset_recovery(dut):
    await _cold_reset_settle(dut)

    ar: list[int] = []
    awvalid_seen: list[int] = []
    cocotb.start_soon(_watch_ar_handshakes(dut, ar))
    cocotb.start_soon(_watch_awvalid(dut, awvalid_seen))

    dut.M_AXIS_READY.value = 1
    await _pulse_wr_ptr(dut, LEAD)

    async def sw_producer() -> None:
        while True:
            for _ in range(16):
                await RisingEdge(dut.CLK_SYS)
            if ar:
                await _pulse_wr_ptr(dut, (ar[-1] + LEAD) % RING)

    producer = cocotb.start_soon(sw_producer())
    await _wait_for_ar_count(dut, ar, 2, 5000)

    dut.RST_DDR.value = 1
    await ClockCycles(dut.CLK_DDR, 10)
    dut.RST_SYS.value = 1
    await ClockCycles(dut.CLK_SYS, 2)
    dut.RST_SYS.value = 0
    dut.RST_DDR.value = 0

    counts = await _sample_reset_window(dut)
    assert counts["aw"] == 0, (
        "M_AXI_AWVALID asserted %d time(s) during the mid-stream reset-busy window; expected 0"
        % counts["aw"])
    # DDR-T2.15 / DDR-P2.9: the original gap in this test — it only checked AW
    # (irrelevant here, the write plane is off) and never looked at AR/tvalid,
    # which are exactly the signals the DDR-T2.15 CS-gate protects on a plane
    # that WAS mid-traffic before the reset landed. A CS released on the fixed
    # timeout alone (the pre-fix behaviour) could re-arm ARVALID/M_AXIS_VALID
    # before the FIFO's reset-busy window actually cleared.
    assert counts["ar"] == 0, (
        "M_AXI_ARVALID asserted %d time(s) during the mid-stream reset-busy window; expected 0"
        % counts["ar"])
    assert counts["tvalid"] == 0, (
        "M_AXIS_VALID asserted %d time(s) during the mid-stream reset-busy window; expected 0"
        % counts["tvalid"])

    await ClockCycles(dut.CLK_DDR, POST_RESET_SETTLE_DDR_CYCLES)
    await _pulse_wr_ptr(dut, LEAD)

    recovered_at = len(ar) + 1
    await _wait_for_ar_count(dut, ar, recovered_at, 5000)
    assert len(ar) >= recovered_at, (
        "expected at least 1 new ARADDR transaction after mid-stream reset recovery; got 0")
    assert len(awvalid_seen) == 0, (
        "M_AXI_AWVALID asserted %d time(s) with the write plane disabled; expected 0"
        % len(awvalid_seen))
    producer.cancel()


@cocotb.test()
@requires("DDR-REQ-RST-5")
async def mid_stream_reset_clears_stale_ptr_no_corruption(dut):
    """DDR-P2.9 — a reset applied mid-stream, AFTER the software write pointer
    has been driven deep into the ring (non-zero, wrapped at least once), must
    fully clear the internal ptr state. If the reset only re-armed CS/AXI
    traffic (DDR-T2.15's fix) but left a stale wr_ptr_addr_ddr / downloader
    read-address register behind, the ring would report "not empty" against a
    corrupted/incoherent pointer the instant CS re-arms — issuing a read
    against a bogus address before any new CSR_WR_PTR pulse ever arrives.

    Guard: drive the ring hard enough that the write pointer wraps past the
    ring boundary at least once (so its value is provably non-zero and
    non-trivial), reset mid-stream, and require the untouched ring — no new
    CSR_WR_PTR pulse — to look exactly like a cold, never-written ring:
    chip_empty (or no AR) with zero traffic, precisely DDR-REQ-RST-2's
    post-reset invariant but now proven against PRIOR non-zero pointer state
    rather than only a ring that was never written to begin with.
    """
    await _cold_reset_settle(dut)

    ar: list[int] = []
    cocotb.start_soon(_watch_ar_handshakes(dut, ar))
    dut.M_AXIS_READY.value = 1

    # Drive the write pointer around the (small, 4 KiB) ring enough times that
    # it wraps at least once — proves the pointer state driven into the DUT is
    # non-trivial before the reset lands, not merely a near-zero offset a
    # buggy "reset-to-0" could pass by accident.
    offset = LEAD
    await _pulse_wr_ptr(dut, offset)
    wraps = 0
    last_ar = -1
    for _ in range(4000):
        await RisingEdge(dut.CLK_DDR)
        if ar and ar[-1] != last_ar:
            last_ar = ar[-1]
            offset = (offset + LEAD) % RING
            if offset < LEAD:
                wraps += 1
            await _pulse_wr_ptr(dut, offset)
        if wraps >= 2 and len(ar) >= 8:
            break
    assert wraps >= 2 and len(ar) >= 8, (
        "harness did not drive the ring hard enough to wrap the write pointer "
        "at least twice before reset (wraps=%d, ar=%d) — the stale-pointer "
        "regime this test targets was never entered" % (wraps, len(ar)))

    dut.RST_DDR.value = 1
    await ClockCycles(dut.CLK_DDR, 10)
    dut.RST_SYS.value = 1
    await ClockCycles(dut.CLK_SYS, 2)
    dut.RST_SYS.value = 0
    dut.RST_DDR.value = 0

    counts = await _sample_reset_window(dut)
    assert counts["ar"] == 0, (
        "M_AXI_ARVALID asserted %d time(s) during the reset-busy window that "
        "followed a non-trivial (twice-wrapped) write pointer; expected 0"
        % counts["ar"])
    assert counts["tvalid"] == 0, (
        "M_AXIS_VALID asserted %d time(s) during the reset-busy window that "
        "followed a non-trivial (twice-wrapped) write pointer; expected 0"
        % counts["tvalid"])

    # Deliberately do NOT re-pulse CSR_WR_PTR here. If the reset genuinely
    # cleared the internal wr_ptr_addr_ddr / read-address state to the ring
    # origin (both sides = 0, empty), the freshly re-armed downloader must
    # issue no reads at all — exactly the untouched-ring invariant. A stale,
    # un-reset pointer register would instead let the ring look non-empty
    # against the OLD (pre-reset) wrap position and issue a phantom read.
    await ClockCycles(dut.CLK_DDR, POST_RESET_SETTLE_DDR_CYCLES)
    await _assert_chip_empty_or_no_ar(dut, 300)


@cocotb.test()
@requires("DDR-REQ-RST-4")
async def asymmetric_reset_rsy_before_rddr(dut):
    await _start_and_reset_inputs(dut)
    await ClockCycles(dut.CLK_DDR, 50)
    reset_counts = await _sample_reset_window(dut)
    dut.RST_SYS.value = 0
    await ClockCycles(dut.CLK_SYS, 2)
    dut.RST_DDR.value = 0

    counts = await _sample_reset_window(dut)
    assert reset_counts["ar"] == 0, (
        "M_AXI_ARVALID asserted %d time(s) while RST_DDR was high; expected 0"
        % reset_counts["ar"])
    assert counts["ar"] == 0, (
        "M_AXI_ARVALID asserted %d time(s) during asymmetric reset-busy window; expected 0"
        % counts["ar"])

    await ClockCycles(dut.CLK_DDR, POST_RESET_SETTLE_DDR_CYCLES)
    await _assert_chip_empty_or_no_ar(dut, 200)


def main() -> None:
    if run_simulation is None:
        raise RuntimeError(
            'engine.simulation.run_simulation not available — '
            'invoke this test via rr sim run test_ddr_reset_coverage.')
    run_simulation(
        top_level='wrapper_ddr_stream_axi',
        module='test_ddr_reset_coverage',
        custom_libraries=_custom_libraries(),
        work_lib='rr',
        generics=_GENERICS,
        waves=True,
        simulator='nvc',
    )


if __name__ == '__main__':
    main()

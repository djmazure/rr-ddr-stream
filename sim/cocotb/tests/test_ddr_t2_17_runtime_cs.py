"""DDR-T2.17 — runtime RST_SYS (RST_DDR held low) must drop Chip Select.

Two independent guards, each red when only its RTL part is removed:

* ``cs_low_every_cycle_while_pipe_busy`` — the combinational gate. Samples
  ``uploader_cs`` / ``downloader_cs`` on every settled ``CLK_DDR`` edge from
  before the pulse until ``pipeBusyDdr`` falls, and fails if either select is
  high on any busy cycle. A sample that starts after the pulse misses the
  one-cycle window the registered re-arm leaves open.
* ``cs_rearms_init_timeout_after_runtime_busy`` — the registered re-arm.
  After ``pipeBusyDdr`` falls, both selects must stay low for the full
  ``C_RESET_INIT_TIMEOUT`` (64) ``CLK_DDR`` cycles before they release.

Run via ``rr sim run test_ddr_t2_17_runtime_cs``.
"""
from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge
from cocotbext.axi import AxiBus, AxiRam

from test_ddr_swptr_fill import (  # type: ignore
    CLK_DDR_NS,
    CLK_SYS_NS,
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


# rtl/ddr_stream_axi.vhd: C_RESET_INIT_TIMEOUT
C_RESET_INIT_TIMEOUT = 64


def _cs_nets(rtl):
    """Nets the burst setters observe.

    DDR-T2.17 names them ``uploader_cs`` / ``downloader_cs`` (the
    combinational gate). Before that fix the setters were wired straight to
    the registers, so the same observation falls back to those.
    """
    try:
        uploader_cs = rtl.uploader_cs
        downloader_cs = rtl.downloader_cs
    except AttributeError:
        uploader_cs = rtl.uploader_cs_r
        downloader_cs = rtl.downloader_cs_r
    return uploader_cs, downloader_cs, rtl.pipeBusyDdr


async def _cold_reset(dut) -> None:
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
    AxiRam(AxiBus.from_prefix(dut, "M_AXI"), dut.CLK_DDR, dut.RST_DDR, size=RING)
    await ClockCycles(dut.CLK_DDR, 50)
    dut.RST_SYS.value = 0
    dut.RST_DDR.value = 0


async def _trace_runtime_sys_reset(dut) -> list[tuple[int, int, int]]:
    """Pulse ``RST_SYS`` after init with ``RST_DDR`` low.

    Returns settled ``(pipeBusyDdr, uploader_cs, downloader_cs)`` samples
    from the first ``CLK_DDR`` edge of the pulse onward, through the busy
    window and ``C_RESET_INIT_TIMEOUT + 8`` clear cycles after it.
    """
    await _cold_reset(dut)
    rtl = dut.u_ddr_stream_axi
    uploader_cs, downloader_cs, pipeBusyDdr = _cs_nets(rtl)
    samples: list[tuple[int, int, int]] = []
    stop = [False]

    async def _sample() -> None:
        while not stop[0]:
            await RisingEdge(dut.CLK_DDR)
            await ReadOnly()
            samples.append((
                int(pipeBusyDdr.value),
                int(uploader_cs.value),
                int(downloader_cs.value),
            ))

    cocotb.start_soon(_sample())

    released = False
    for _ in range(400):
        await RisingEdge(dut.CLK_DDR)
        if samples and samples[-1] == (0, 1, 1):
            released = True
            break
    assert released, (
        "uploader_cs and downloader_cs must be 1 with pipeBusyDdr=0 "
        "after init, before the runtime RST_SYS pulse")
    assert int(dut.RST_DDR.value) == 0, "RST_DDR must stay low for this pulse"

    await RisingEdge(dut.CLK_DDR)
    mark = len(samples)
    dut.RST_SYS.value = 1
    await ClockCycles(dut.CLK_SYS, 5)
    dut.RST_SYS.value = 0

    saw_busy = False
    clear_run = 0
    for _ in range(500):
        await RisingEdge(dut.CLK_DDR)
        post = samples[mark:]
        if not post:
            continue
        if post[-1][0] == 1:
            saw_busy = True
            clear_run = 0
        elif saw_busy:
            clear_run += 1
            if clear_run >= C_RESET_INIT_TIMEOUT + 8:
                break
    stop[0] = True
    await RisingEdge(dut.CLK_DDR)
    post = samples[mark:]
    assert saw_busy, (
        "expected pipeBusyDdr=1 after pulsing RST_SYS with RST_DDR held low")
    assert any(row[0] == 0 for row in post[post.index(
        next(row for row in post if row[0] == 1)):]), (
        "pipeBusyDdr never fell after the runtime reset-busy window")
    return post


@cocotb.test()
@requires("DDR-REQ-RST-6")
async def cs_low_every_cycle_while_pipe_busy(dut):
    """Combinational CS gate: no busy cycle may show either select high."""
    post = await _trace_runtime_sys_reset(dut)
    busy = [row for row in post if row[0] == 1]
    assert busy, "expected at least one pipeBusyDdr=1 sample after RST_SYS"
    for pipeBusyDdr, uploader_cs, downloader_cs in busy:
        assert uploader_cs == 0 and downloader_cs == 0, (
            "Chip Select asserted during FIFO reset-busy "
            f"(uploader_cs={uploader_cs} downloader_cs={downloader_cs} "
            f"pipeBusyDdr={pipeBusyDdr})"
        )


@cocotb.test()
@requires("DDR-REQ-RST-6")
async def cs_rearms_init_timeout_after_runtime_busy(dut):
    """Registered re-arm: CS stays low for the full init timeout after busy."""
    post = await _trace_runtime_sys_reset(dut)
    last_busy = max(i for i, row in enumerate(post) if row[0] == 1)
    after = post[last_busy + 1:]
    assert after, "no CLK_DDR samples after pipeBusyDdr fell"
    low = 0
    for pipeBusyDdr, uploader_cs, downloader_cs in after:
        assert pipeBusyDdr == 0, (
            "pipeBusyDdr rose again before the init timeout elapsed")
        if uploader_cs == 0 and downloader_cs == 0:
            low += 1
            continue
        break
    assert low >= C_RESET_INIT_TIMEOUT, (
        "CS re-released before C_RESET_INIT_TIMEOUT "
        f"(uploader_cs/downloader_cs stayed 0 for {low} CLK_DDR cycles "
        f"after pipeBusyDdr fell; need {C_RESET_INIT_TIMEOUT})"
    )
    assert low < len(after), (
        "CS did not re-release after C_RESET_INIT_TIMEOUT "
        f"(still 0 for all {low} sampled clear cycles)")


def main() -> None:
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke this test via rr sim run test_ddr_t2_17_runtime_cs.")
    run_simulation(
        top_level="wrapper_ddr_stream_axi",
        module="test_ddr_t2_17_runtime_cs",
        custom_libraries=_custom_libraries(),
        work_lib="rr",
        generics=_GENERICS,
        waves=False,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

"""DDR-P1.2 — the DDR-T2.15 sim-only invariant still fires in simulation.

DDR-P1.2 hides the ``postponed assert`` in ``rtl/ddr_stream_axi.vhd`` from
synthesis with ``-- synthesis translate_off`` / ``translate_on``, because
Synplify does not accept it. The pragma must not turn the check off in
simulation as well. Nothing in the RTL can violate the invariant: the
combinational gate ``uploader_cs <= uploader_cs_r and not pipeBusyDdr``
makes it true by construction. So this testcase forces the violation:

* hold ``RST_DDR`` high, so ``pipeBusyDdr`` is 1 (its ``set_i`` is
  ``rst_ddr_i``);
* force ``uploader_cs`` to 1 for a few ``CLK_DDR`` cycles, then release it.

The VHDL assertion must print
``ddr_stream_axi: Chip Select asserted during FIFO reset-busy (DDR-T2.15)``.
cocotb cannot see a VHDL report, so the pytest guard
``tests/test_ddr_p1_2_synplify_compile.py::test_cs_invariant_still_fires_in_sim``
runs this module and looks for that line in the simulator output. The
simulator runs with ``--exit-severity=failure`` so the ``error``-severity
report is printed and the run continues to the end of this testcase.

Run via ``rr sim run test_ddr_p1_2_cs_invariant_fires``.
"""
from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.handle import Force, Release
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from test_ddr_swptr_fill import (  # type: ignore
    CLK_DDR_NS,
    CLK_SYS_NS,
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


@cocotb.test()
@requires("DDR-REQ-RST-6")
async def cs_invariant_assert_fires_when_forced(dut):
    """Force CS high while pipeBusyDdr=1: the VHDL invariant must report."""
    cocotb.start_soon(Clock(dut.CLK_SYS, CLK_SYS_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.CLK_DDR, CLK_DDR_NS, unit="ns").start())
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
    await ClockCycles(dut.CLK_DDR, 10)

    rtl = dut.u_ddr_stream_axi
    await ReadOnly()
    assert int(rtl.pipeBusyDdr.value) == 1, (
        "pipeBusyDdr must be 1 while RST_DDR is held high")
    assert int(rtl.uploader_cs.value) == 0, (
        "uploader_cs must be 0 while pipeBusyDdr=1 (DDR-T2.17 gate)")

    await RisingEdge(dut.CLK_DDR)
    rtl.uploader_cs.value = Force(1)
    await ClockCycles(dut.CLK_DDR, 4)
    await ReadOnly()
    # The force must have reached the net the assertion reads. Without this
    # a force that did nothing would look the same as a dead assertion.
    assert int(rtl.uploader_cs.value) == 1, "Force(1) on uploader_cs had no effect"
    assert int(rtl.pipeBusyDdr.value) == 1, "pipeBusyDdr fell during the force"
    await RisingEdge(dut.CLK_DDR)
    rtl.uploader_cs.value = Release()
    await ClockCycles(dut.CLK_DDR, 4)


def main() -> None:
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke this test via rr sim run test_ddr_p1_2_cs_invariant_fires.")
    run_simulation(
        top_level="wrapper_ddr_stream_axi",
        module="test_ddr_p1_2_cs_invariant_fires",
        custom_libraries=_custom_libraries(),
        work_lib="rr",
        generics=_GENERICS,
        waves=False,
        simulator="nvc",
        # An `error` report must not end the run: the pytest guard reads the
        # report text from the simulator output.
        test_args=["--exit-severity=failure"],
    )


if __name__ == "__main__":
    main()

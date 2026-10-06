"""AXI3-faithful burst-conformance test for ddr_stream_axi — VFIFO-T2.6 / IDP-T2.5.

The stock test_ddr_stream_axi drives cocotbext.axi.AxiRam — an *ideal AXI4*
memory that accepts up to 256-beat bursts. The Zynq-7 ``S_AXI_HP`` slave the IP
actually talks to on a Classic xc7z010 is **AXI3** (``AWLEN`` 4-bit ⇒ 16-beat
max), reached through an AXI4→AXI3 SmartConnect. AxiRam therefore MASKS the fact
that ``burstw_setter`` emits AXI3-illegal >16-beat bursts under DDR-cadence
back-pressure: sim is green, silicon corrupts.

This test replaces AxiRam with ``Axi3WriteSubordinate`` — a spec-faithful AXI3
write endpoint that ASSERTS the moment it sees ``AWLEN`` > 15 — and BINS the
observed AWLEN with the ``FunctionalCoverage`` harness, sweeping the new
``G_MAX_BURST_LEN`` generic 256 → 16:

  * G_MAX_BURST_LEN=256 (AXI4 default): the ">16 (AXI3-ILLEGAL)" bin LIGHTS UP
    and the BFM records AXI3 violations — this is the latent defect, now VISIBLE.
    (We use on_long_burst="truncate16" here so the run still completes; the
    violation list is the verdict, asserted non-empty.)
  * G_MAX_BURST_LEN=16 (the clamp / AXI3 target): the illegal bin stays EMPTY
    and the BFM records ZERO violations — proof the clamp works. Here the BFM
    runs in fail_on_violation=True (on_long_burst="assert"): if anyone reverts
    the clamp, the BFM aborts the test loudly. THAT assertion is the whole point
    (ROUTERTL-002 / bug-finding-is-success) and must never be weakened.

Run via:  rr sim run test_ddr_stream_axi_axi3
"""
from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly, Timer
from cocotb.handle import Immediate
from pathlib import Path as _Path

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
    from sdk.cocotb_helpers import requires       # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None

    def requires(*_args, **_kwargs):  # type: ignore[no-redef]
        def _wrap(fn):
            return fn
        return _wrap

from axi3_write_bfm import Axi3WriteSubordinate
from cov_harness import FunctionalCoverage

_IP_ROOT = _Path(__file__).resolve().parents[3]
_RTL_DIR = _IP_ROOT / "rtl"
_SUPPORT_DIR = _RTL_DIR / "support"
_TB_DIR = _IP_ROOT / "sim" / "tb"
_OLO_SYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_sync" / "src" / "base" / "vhdl"
_OLO_ASYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_async" / "src" / "base" / "vhdl"
_OLO_WCN_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_n2xn" / "src" / "base" / "vhdl"
_OLO_WCXN_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_xn2n" / "src" / "base" / "vhdl"

# 128-bit DDR bus mirrors the Zynq-7 S_AXI_HP datapath width.
HP0_DATA_W = 128
N_WRITES = 4096


def _libraries():
    """The canonical ddr_stream_axi compile set (mirrors test_ddr_stream_axi.main)."""
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
            # xpm_body entities (Xilinx-only, file-scope `library xpm`) must be
            # analysed BEFORE the rr_fifo_* wrappers that reference them via
            # `entity work.*_xpm_body` (mirrors test_ddr_stream_axi.main).
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
            str(_TB_DIR / "wrappers" / "wrapper_ddr_stream_axi.vhd"),
        ],
    }


def main() -> None:
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke via `rr sim run test_ddr_stream_axi_axi3`."
        )
    libs = _libraries()
    # Sweep the burst cap. Each value re-elaborates (a generic on the top
    # entity), so it is an across-build axis, driven here in main().
    for _cap in (256, 16):
        print(
            f"\n{'='*72}\n"
            f"=== test_ddr_stream_axi_axi3 — G_MAX_BURST_LEN={_cap}\n"
            f"{'='*72}\n"
        )
        run_simulation(
            top_level="wrapper_ddr_stream_axi",
            module="test_ddr_stream_axi_axi3",
            custom_libraries=libs,
            work_lib="rr",
            generics={
                "G_AXI_ID": 0,
                # Deep DDR-side FIFO so the egress accumulation can exceed 16
                # DDR words — that is the precondition for the IP to emit a
                # >16-beat burst at the AXI4 default (the masked defect). A
                # shallow FIFO (depth 9) self-limits to ~16 and would hide it.
                "G_FIFO_DEPTH": 12,
                "G_USER_DATA_WIDTH": 32,
                "G_DDR_DATA_WIDTH": HP0_DATA_W,
                "G_DDR_ADDR_WIDTH": 32,
                "G_BUFFER_WIDTH": 24,
                "G_FIFO_IMPL": "olo",
                "G_MAX_BURST_LEN": _cap,
            },
            waves=True,
            simulator="nvc",
        )


async def _awlen_monitor(dut, cov):
    """Sample every accepted AW burst's beat count into the AWLEN bin."""
    while True:
        await RisingEdge(dut.CLK_DDR)
        await ReadOnly()
        if int(dut.M_AXI_AWVALID.value) and int(dut.M_AXI_AWREADY.value):
            cov.sample("AWLEN_beats", int(dut.M_AXI_AWLEN.value) + 1)
            cov.sample("AWBURST", int(dut.M_AXI_AWBURST.value))


@cocotb.test()
@requires("DDR-REQ-204")
async def axi3_burst_cap_conformance(dut):
    """DDR-REQ-204 — with G_MAX_BURST_LEN=16 no `M_AXI_AWLEN` exceeds 15.

    The Axi3WriteSubordinate is the adversarial oracle: it ASSERTS on
    `M_AXI_AWLEN`>15 (the AXI3 max). At the AXI4 default (cap=256) we expect the
    illegal-burst bin to LIGHT UP and the BFM to record violations (defect made
    visible); at cap=16 we expect ZERO illegal bursts and run the BFM in
    fail-loudly mode so a reverted clamp aborts the test.
    """
    cap = int(dut.G_MAX_BURST_LEN.value)
    is_axi3 = cap <= 16

    cov = FunctionalCoverage()
    cov.declare_bin(
        "AWLEN_beats", edges=[4, 8, 16],
        labels=["1-4", "5-8", "9-16", ">16 (AXI3-ILLEGAL)"],
    )
    cov.declare_values("AWBURST")

    DDR_PERIOD = 3.33  # ns, ~300 MHz
    SYS_PERIOD = 10.0  # ns, 100 MHz
    cocotb.start_soon(Clock(dut.CLK_DDR, DDR_PERIOD, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_SYS, SYS_PERIOD, units="ns").start())

    # AXI3-faithful write subordinate REPLACING AxiRam. fail_on_violation only
    # at cap=16: there the IP MUST be legal, so any AWLEN>15 is a real defect we
    # want to abort on. At cap=256 we deliberately let the illegal bursts land
    # (truncate16) so the coverage bin can record them as the visible defect.
    bfm = Axi3WriteSubordinate(
        dut, dut.CLK_DDR, dut.RST_DDR,
        prefix="M_AXI", data_w=HP0_DATA_W, addr_w=32, mem_size=1 << 21,
        on_long_burst=("assert" if is_axi3 else "truncate16"),
        fail_on_violation=is_axi3,
        # Hold AWREADY low for an initial window (DDR init/cal busy) so the
        # egress FIFO fills PAST 16 DDR words before the first burst latches —
        # that is what makes the AXI4-default ring emit a >16-beat AXI3-illegal
        # burst (the masked defect). Then sustained AW->first-W latency keeps
        # provoking long bursts as ingress continues.
        aw_initial_stall=3000, aw_to_w_latency=120, w_stall_prob=0.1, b_latency=8,
    )

    dut.RST_DDR.value = 1
    dut.RST_SYS.value = 1
    dut.S_AXIS_VALID.value = 0
    dut.M_AXIS_READY.value = 0
    dut.S_AXIS_DATA.value = 0
    bfm.start()
    cocotb.start_soon(_awlen_monitor(dut, cov))

    for _ in range(8):
        await RisingEdge(dut.CLK_DDR)
    dut.RST_DDR.value = 0
    for _ in range(8):
        await RisingEdge(dut.CLK_SYS)
    dut.RST_SYS.value = 0
    await Timer(3, units="us")
    await RisingEdge(dut.CLK_SYS)

    # Drive a long sustained ingress so the IP batches into multi-beat bursts.
    for i in range(N_WRITES):
        dut.S_AXIS_DATA.value = Immediate((i + 1) & 0xFFFFFFFF)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)

    # Let the write pipeline drain into the BFM.
    await Timer(400, units="us")

    uncovered = cov.report(dut._log)
    illegal = cov.bins["AWLEN_beats"].hits[">16 (AXI3-ILLEGAL)"]
    dut._log.info(
        f"cap={cap}: AW bursts seen={bfm.aw_seen}, max AWLEN={bfm.max_awlen_seen}, "
        f">16-beat samples={illegal}, BFM violations={len(bfm.violations)}"
    )

    # Adversarial floor: a zero-traffic green bar is meaningless.
    assert bfm.aw_seen >= 4, (
        f"Insufficient AW traffic to validate DDR-REQ-204 — saw {bfm.aw_seen} "
        f"bursts, need >= 4. Stimulus/enable path broken; do NOT relax this floor."
    )

    if is_axi3:
        # THE FIX: at cap=16 the IP must be AXI3-legal. The BFM ran in
        # fail_on_violation mode, so reaching here already means no AWLEN>15
        # fired — but assert the coverage/violation verdict explicitly too.
        assert illegal == 0, (
            f"FIX REGRESSED: {illegal} >16-beat (AXI3-illegal) burst(s) sampled "
            f"at G_MAX_BURST_LEN={cap}; the burstw_setter clamp is not holding."
        )
        assert not bfm.violations, (
            f"AXI3 protocol violated at cap={cap}: {bfm.violations[0]}"
        )
        assert bfm.max_awlen_seen <= 15, (
            f"max AWLEN {bfm.max_awlen_seen} > 15 at cap={cap} — AXI3 slave "
            f"cannot accept this."
        )
    else:
        # THE DEFECT, MADE VISIBLE: the AXI4 default ring emits AXI3-illegal
        # bursts under DDR-cadence back-pressure. If this bin is empty the
        # coverage hole that shipped VFIFO-T2.6 has reopened (or the stimulus
        # stopped provoking accumulation) — fail, do not silently pass.
        assert illegal > 0, (
            "coverage HOLE undetected: expected the AXI4-default (cap=256) ring "
            "to emit >16-beat AXI3-illegal bursts under DDR-cadence back-pressure, "
            "but the illegal AWLEN bin stayed empty."
        )
        assert bfm.violations, (
            "expected the AXI3 BFM to record AWLEN>15 protocol violations at "
            "cap=256 (the masked defect), but it saw none."
        )

    # Sanity: real multi-beat traffic was exercised, not just degenerate bursts.
    assert cov.bins["AWLEN_beats"].hits["9-16"] > 0, (
        "saw no 9-16 beat bursts — the IP never batched; stimulus too sparse."
    )


if __name__ == "__main__":
    main()

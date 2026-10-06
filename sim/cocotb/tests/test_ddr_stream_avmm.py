"""Cocotb test suite for ddr_stream_avmm — AVMM peer of test_ddr_stream_axi.

Same four scenarios (mem_model_demo / 4k_boundary / reset_attack /
cleaver_n20_avmm_trace) with the same assertions; the only mechanical
difference is the bus model — sdk.cocotb_helpers.avmm_ram.AvmmRam
replaces cocotbext.axi.AxiRam, and the wrapper exposes M_AVMM_*
flat ports instead of M_AXI_*.

DDR-P3.1 — companion to DDR-P3.5 (public registry publish) and the
shared bus-agnostic policy core (ptr_mgr / burst*_setter /
ddr_fifo_intf / skid_buffer) that both backends reuse unchanged.
"""
from __future__ import annotations

import cocotb
import logging
import random
from collections import deque, Counter
from pathlib import Path as _Path

from cocotb.triggers import Timer, RisingEdge, ReadOnly, NextTimeStep
from cocotb.clock import Clock
from cocotb.handle import Immediate

# File-level log so post-mortem dives don't depend on the simulator's stdout.
log_file_path = _Path(__file__).parent / "ddr_stream_avmm_test.log"
logging.basicConfig(
    filename=log_file_path,
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# AVMM slave memory model — RTL-P3.495 / DDR-P3.1 unblocker. API parity
# with cocotbext-axi.AxiRam so the test bodies below stay structurally
# identical to test_ddr_stream_axi.py.
from sdk.cocotb_helpers.avmm_ram import AvmmBus, AvmmRam  # type: ignore

# RouteRTL simulation engine.
try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
    from sdk.cocotb_helpers import requires       # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None
    def requires(*_args, **_kwargs):  # type: ignore[no-redef]
        def _wrap(fn):
            return fn
        return _wrap

_IP_ROOT     = _Path(__file__).resolve().parents[3]
_RTL_DIR     = _IP_ROOT / "rtl"
_AVMM_DIR    = _RTL_DIR / "avmm"
_SUPPORT_DIR = _RTL_DIR / "support"
_TB_DIR      = _IP_ROOT / "sim" / "tb"
# olo (open-logic) sources for the rr_fifo_sync / rr_fifo_async_mixed
# wrappers (DDR-P3.6 / RR--P3.1).
_OLO_SYNC_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_sync" / "src" / "base" / "vhdl"
_OLO_ASYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_async" / "src" / "base" / "vhdl"
_OLO_WCN_DIR   = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_n2xn" / "src" / "base" / "vhdl"
_OLO_WCXN_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_xn2n" / "src" / "base" / "vhdl"

GENERICS = {
    "G_FIFO_DEPTH":      9,
    # RR--P3.6: defaults match the historical 32-in / 128-out shape;
    # the width sweep below overrides per-iteration.
    "G_USER_DATA_WIDTH": 32,
    "G_DDR_DATA_WIDTH":  128,
    "G_DDR_ADDR_WIDTH":  32,
    "G_BUFFER_WIDTH":    20,
}


def main() -> None:
    """Run via `rr sim run test_ddr_stream_avmm`. Drives the wrapper at
    sim/tb/wrappers/wrapper_ddr_stream_avmm.vhd which flattens the IP's
    record-typed AVMM ports so AvmmBus.from_prefix can attach the slave
    memory model.
    """
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke this test via `rr sim run test_ddr_stream_avmm`."
        )

    # RR--P3.2 + RR--P3.6: sweep FIFO backend × user/DDR width combos.
    _width_sweep = (
        (32, 128),   # historical default — ratio 4
        (32, 512),   # Intel-EMIF native — ratio 16
    )
    for _user_w, _ddr_w in _width_sweep:
      for _impl in ("olo", "xpm"):
        print(
            f"\n{'='*72}\n"
            f"=== test_ddr_stream_avmm — fifo_impl_g={_impl!r} "
            f"user={_user_w}b ddr={_ddr_w}b\n"
            f"{'='*72}\n"
        )
        run_simulation(
            top_level="wrapper_ddr_stream_avmm",
            module="test_ddr_stream_avmm",
            custom_libraries={
            # RR--T2.1: open-logic into its own `olo` library so the
            # consumer-side `entity olo.olo_base_*` refs in rr_fifo_*.vhd
            # resolve cross-library. See test_ddr_stream_axi.py for
            # the full note.
            "olo": [
                str(_OLO_ASYNC_DIR / "olo_base_pkg_array.vhd"),
                str(_OLO_ASYNC_DIR / "olo_base_pkg_math.vhd"),
                str(_OLO_ASYNC_DIR / "olo_base_pkg_logic.vhd"),
                str(_OLO_ASYNC_DIR / "olo_base_pkg_string.vhd"),
                str(_OLO_ASYNC_DIR / "olo_base_pkg_attribute.vhd"),
                str(_OLO_ASYNC_DIR / "olo_base_cc_bits.vhd"),
                str(_OLO_ASYNC_DIR / "olo_base_cc_reset.vhd"),
                str(_OLO_ASYNC_DIR / "olo_base_ram_sdp.vhd"),
                str(_OLO_SYNC_DIR  / "olo_base_fifo_sync.vhd"),
                str(_OLO_ASYNC_DIR / "olo_base_fifo_async.vhd"),
                str(_OLO_WCN_DIR   / "olo_base_wconv_n2xn.vhd"),
                str(_OLO_WCXN_DIR  / "olo_base_wconv_xn2n.vhd"),
            ],
            "rr": [
                str(_RTL_DIR / "ddr_stream_pkg.vhd"),
                str(_SUPPORT_DIR / "functions_pkg.vhd"),
                str(_SUPPORT_DIR / "ddr_sync_cdc.vhd"),
                str(_SUPPORT_DIR / "skid_buffer.vhd"),
                # RR--P3.3: xpm_body entities (Xilinx-only — file-scope
                # `library xpm`) — analysed before the wrappers that
                # reference them via `entity work.*_xpm_body`. Same gating
                # as the AXI test; the synth flow on Quartus never sees
                # these because ip.yml splits them under sources_per_vendor.
                str(_SUPPORT_DIR / "xilinx" / "rr_fifo_sync_xpm_body.vhd"),
                str(_SUPPORT_DIR / "xilinx" / "rr_fifo_async_mixed_xpm_body.vhd"),
                # rr_fifo_* wrappers (DDR-P3.6 / RR--P3.1).
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
            # RR--T2.1: explicit work_lib so elaborate hits `rr` (the lib
            # the wrapper lives in), not `olo` (the first dict key).
            work_lib="rr",
            generics={
                **GENERICS,
                "G_USER_DATA_WIDTH": _user_w,
                "G_DDR_DATA_WIDTH":  _ddr_w,
                "G_FIFO_IMPL":       _impl,
            },
            waves=True,
            simulator="nvc",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Shared bring-up + back-pressure injection helpers.
# ──────────────────────────────────────────────────────────────────────────────

DDR_PERIOD = 3.33  # ns, 300 MHz
SYS_PERIOD = 10.0  # ns, 100 MHz


def _waitrequest_jitter(p: float, seed: int):
    """Pause-generator factory: returns a zero-arg callable the AvmmRam BFM
    polls each cycle (`bool(pause_generator())`). Asserts WAITREQUEST with
    probability `p` to mirror the AXI test's ar/aw stall flavour.
    """
    rng = random.Random(seed)
    def _tick() -> bool:
        return rng.random() < p
    return _tick


async def _bring_up_clocks_and_reset(dut, *, ddr_size: int, stall_p: float = 0.03):
    """Boilerplate matching test_ddr_stream_axi._bring_up_clocks_and_reset:
    launch both clocks, walk both resets through the right edges, return the
    AvmmRam instance ready to take traffic.

    ``stall_p`` controls the WAITREQUEST jitter probability — drives
    realistic EMIF-flavour back-pressure into the BFM. The earlier
    stall-toggle race (RTL-T2.1) was fixed in routertl df4b8210 (2026-05-15)
    by gating the BFM commit on the master's observed waitrequest, so
    every test now runs with stalls active from day one — including
    mem_model_demo's 8192-message round-trip.
    """
    cocotb.start_soon(Clock(dut.CLK_DDR, DDR_PERIOD, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_SYS, SYS_PERIOD, units="ns").start())

    dut.RST_DDR.value = 1
    dut.RST_SYS.value = 1
    dut.S_AXIS_VALID.value   = 0
    dut.M_AXIS_READY.value   = 0
    dut.S_AXIS_DATA.value     = 0

    avmm = AvmmBus.from_prefix(dut, "M_AVMM")
    ram  = AvmmRam(
        avmm,
        dut.CLK_DDR,
        reset=dut.RST_DDR,
        reset_active_level=True,
        size=ddr_size,
        read_latency=4,                              # rough EMIF-flavour
        pause_generator=(
            _waitrequest_jitter(p=stall_p, seed=42) if stall_p > 0 else None
        ),
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
    return ram


# ──────────────────────────────────────────────────────────────────────────────
# Output collector — same shape as the AXI test's `data_grabber`.
# ──────────────────────────────────────────────────────────────────────────────

fifo: list[int] = []


async def data_grabber(dut):
    """AXI-Stream data grabber: capture only on (VALID AND READY) handshake,
    sampled in the post-edge ReadOnly phase (RTL-T3.11) so the handshake read
    is the one the DUT registers at that edge — phase-correct regardless of
    whether the consumer drives READY via deferred (RTL-P3.854) or legacy
    Immediate writes. Reading the handshake in the racy post-resume region (the
    old shape) double-captured the held head word once READY was driven with a
    deferred write — the Immediate-on-ready footgun in reverse (Haz.73).
    DDR-P3.10: M_AXIS_VALID is sticky now; capturing on the settled handshake
    (not a bare VALID count) avoids over-counting words held in the output skid
    while the consumer isn't ready.
    """
    while True:
        await RisingEdge(dut.CLK_SYS)
        await ReadOnly()
        if dut.M_AXIS_VALID.value == 1 and dut.M_AXIS_READY.value == 1:
            fifo.append(int(dut.M_AXIS_DATA.value))


def fifo_checker(received, expected):
    n_recv = len(received)
    n_exp  = len(expected)
    print(
        f"\n[fifo_checker] received={n_recv} expected={n_exp} "
        f"delta={n_recv - n_exp}"
    )
    print(f"[fifo_checker] first 16 received: {[hex(v) for v in received[:16]]}")
    print(f"[fifo_checker] first 16 expected: {[hex(v) for v in expected[:16]]}")

    dq = deque(received)
    received.clear()
    for i, exp in enumerate(expected):
        got = dq.popleft()
        assert got == exp, (
            f"Word {i+1} mismatch! Expected 0x{exp:08x}, got 0x{got:08x}"
        )
    assert n_recv == n_exp, f"FIFO length mismatch: got {n_recv}, expected {n_exp}"
    print("FIFO check completed successfully!")


# ──────────────────────────────────────────────────────────────────────────────
# Test 1 — mem_model_demo (smoke / round-trip).
# ──────────────────────────────────────────────────────────────────────────────

@cocotb.test()
@requires("DDR-REQ-001")
async def mem_model_demo(dut):
    """Round-trip N_WRITES words through the DDR ring via the AVMM master and
    verify the read-back stream matches verbatim. Mirrors the AXI test of
    the same name; only the slave model differs.
    """
    DDR_WIDTH = int(dut.G_DDR_ADDR_WIDTH.value)
    ram = await _bring_up_clocks_and_reset(dut, ddr_size=1 << DDR_WIDTH)

    cocotb.start_soon(data_grabber(dut))

    # Bus-side counters for hostile-reviewer evidence — a green-bar
    # round-trip is only meaningful if real bus traffic happened.
    counts = {"read_cmt": 0, "write_cmt": 0, "rdv": 0}

    async def watch_avmm():
        while True:
            await RisingEdge(dut.CLK_DDR)
            wr = int(dut.M_AVMM_waitrequest.value)
            if int(dut.M_AVMM_read.value) and not wr:
                counts["read_cmt"] += 1
            if int(dut.M_AVMM_write.value) and not wr:
                counts["write_cmt"] += 1
            if int(dut.M_AVMM_readdatavalid.value):
                counts["rdv"] += 1

    cocotb.start_soon(watch_avmm())

    # Seed an unrelated location so the round-trip never accidentally agrees
    # with stale memory contents.
    ram.write(0x0000_1000, b"\xDE\xAD\xBE\xEF")

    N_WRITES = 2048 * 4
    expected: list[int] = []
    for i in range(N_WRITES):
        dut.S_AXIS_DATA.value     = Immediate(i + 1)
        dut.S_AXIS_VALID.value   = Immediate(1)
        expected.append(i + 1)
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value   = Immediate(0)

    await Timer(200, units="us")

    # Cleaver dump — same hostile-reviewer split as the AXI test: prove the
    # write path landed correctly in slave memory before the read kicks off.
    dut._log.info("DDR-P3.1 cleaver: AvmmRam contents before read phase")
    dump = ram.read(0, 256)
    for ddr_word_idx in range(16):
        word_bytes = dump[ddr_word_idx * 16 : (ddr_word_idx + 1) * 16]
        word = int.from_bytes(word_bytes, byteorder="little")
        lanes = [(word >> (lane * 32)) & 0xFFFFFFFF for lane in range(4)]
        dut._log.info(
            f"  addr 0x{ddr_word_idx * 16:04x}: "
            f"[{lanes[0]:#x}, {lanes[1]:#x}, {lanes[2]:#x}, {lanes[3]:#x}]"
        )

    for _ in range(N_WRITES + 1):
        dut.M_AXIS_READY.value = 1
        await RisingEdge(dut.CLK_SYS)
    dut.M_AXIS_READY.value = 0
    await Timer(100, units="us")

    dut._log.info(
        f"AVMM bus counts: write_commits={counts['write_cmt']}, "
        f"read_commits={counts['read_cmt']}, rdv_pulses={counts['rdv']}"
    )
    # Adversarial floor: zero-traffic green-bar is a hostile-reviewer red
    # flag (see feedback_test_success_is_bugs_found).
    assert counts["write_cmt"] > 100, (
        f"Insufficient AVMM write traffic — saw {counts['write_cmt']} commits."
    )
    assert counts["rdv"] > 100, (
        f"Insufficient AVMM RDV pulses — saw {counts['rdv']} pulses."
    )

    fifo_checker(fifo, expected)


# ──────────────────────────────────────────────────────────────────────────────
# Test 2 — burst_safety (4K boundary monitor analogue).
# ──────────────────────────────────────────────────────────────────────────────
# AMBA AXI4 forbids 4 KiB boundary crossings; Avalon-MM allows them in
# principle but the burstw_setter / burstr_setter policy entities (shared
# with the AXI backend) deliberately avoid them. Same monitor invariant.

class BurstStats:
    __slots__ = ("write_bursts", "read_bursts", "violations")
    def __init__(self) -> None:
        self.write_bursts = 0
        self.read_bursts  = 0
        self.violations: list[str] = []


async def avmm_4k_boundary_monitor(dut, clk, stats: BurstStats) -> None:
    """Watch every committed AVMM transfer (write|read with !waitrequest)
    and flag any burst whose start-address + total bytes crosses a 4 KiB
    boundary. burstcount is the per-cycle field; for our single-beat
    writes total = beat_bytes, for burst reads total = burstcount * beat_bytes.
    """
    beat_bytes = int(dut.G_DDR_DATA_WIDTH.value) // 8
    while True:
        await RisingEdge(clk)
        wait_req = int(dut.M_AVMM_waitrequest.value)
        if wait_req:
            continue
        write_req = int(dut.M_AVMM_write.value)
        read_req  = int(dut.M_AVMM_read.value)
        if not (write_req or read_req):
            continue

        addr  = int(dut.M_AVMM_address.value)
        burst = max(1, int(dut.M_AVMM_burstcount.value))
        total = burst * beat_bytes

        if write_req:
            stats.write_bursts += 1
        else:
            stats.read_bursts += 1

        if (addr & 0xFFF) + total > 0x1000:
            kind = "WRITE" if write_req else "READ"
            msg = (
                f"AVMM {kind} boundary cross: addr=0x{addr:08x} "
                f"burstcount={burst} ({total}B total) straddles 4 KiB at "
                f"0x{(addr & ~0xFFF) + 0x1000:08x}"
            )
            stats.violations.append(msg)
            dut._log.error(msg)


@cocotb.test()
@requires("DDR-REQ-203")
async def avmm_4k_boundary_test(dut):
    """No write or read burst crosses a 4 KiB address boundary. Adversarial:
    requires non-trivial traffic in both directions before declaring victory
    (zero-traffic green-bar is a hostile-reviewer red flag — see
    feedback_test_success_is_bugs_found).
    """
    DDR_WIDTH = int(dut.G_DDR_ADDR_WIDTH.value)
    ram = await _bring_up_clocks_and_reset(dut, ddr_size=1 << DDR_WIDTH)

    stats = BurstStats()
    cocotb.start_soon(avmm_4k_boundary_monitor(dut, dut.CLK_DDR, stats))

    N_WRITES = 4096
    for i in range(N_WRITES):
        dut.S_AXIS_DATA.value     = (i + 1) & 0xFFFFFFFF
        dut.S_AXIS_VALID.value   = 1
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value   = 0

    await Timer(200, units="us")

    for _ in range(N_WRITES + 1):
        dut.M_AXIS_READY.value = 1
        await RisingEdge(dut.CLK_SYS)
    dut.M_AXIS_READY.value = 0
    await Timer(100, units="us")

    dut._log.info(
        f"AVMM boundary monitor: writes={stats.write_bursts}, "
        f"reads={stats.read_bursts}, violations={len(stats.violations)}"
    )

    # Single-beat writes mean each writes counter increments per beat (not per
    # logical "burst"); the floor is generous to keep the no-op-test guard
    # honest without overspecifying the exact policy.
    assert stats.write_bursts >= 16, (
        f"Insufficient WRITE traffic to validate the boundary invariant — "
        f"saw {stats.write_bursts} commits, need >= 16. Stimulus or DUT "
        f"enable path is broken; do NOT relax this floor."
    )
    assert stats.read_bursts >= 4, (
        f"Insufficient READ traffic — saw {stats.read_bursts} bursts, need >= 4."
    )
    assert not stats.violations, (
        f"4 KiB boundary violated — {len(stats.violations)} burst(s) crossed. "
        f"First: {stats.violations[0]}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Test 3 — reset_attack — same shape as the AXI version.
# ──────────────────────────────────────────────────────────────────────────────

@cocotb.test()
@requires("DDR-REQ-002")
async def reset_attack_test(dut):
    """RST_SYS asserted mid-flight drains the IP. Adversarial: post-deassert
    we expect (a) DOUT_VLD stays low across reset+settle; (b) BUSY clears
    within a bounded settling window; (c) no spurious AVMM master traffic
    in the 200 CLK_DDR cycles after deassert (the IP must not flush
    pre-reset bursts).
    """
    DDR_WIDTH = int(dut.G_DDR_ADDR_WIDTH.value)
    await _bring_up_clocks_and_reset(dut, ddr_size=1 << DDR_WIDTH)

    for i in range(512):
        dut.S_AXIS_DATA.value     = (i + 1) & 0xFFFFFFFF
        dut.S_AXIS_VALID.value   = 1
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value   = 0

    await Timer(2, units="us")

    dout_vld_observed = 0
    async def watch_dout_vld():
        """DDR-P3.10: count handshakes (VALID AND READY), not sticky-valid."""
        nonlocal dout_vld_observed
        while True:
            await RisingEdge(dut.CLK_SYS)
            if int(dut.M_AXIS_VALID.value) and int(dut.M_AXIS_READY.value):
                dout_vld_observed += 1

    watcher = cocotb.start_soon(watch_dout_vld())

    dut.RST_SYS.value = 1
    for _ in range(8):
        await RisingEdge(dut.CLK_SYS)
    dut.RST_SYS.value = 0

    # DDR-P3.10 (2026-05-18): BUSY-poll retired (busy_o port gone).
    # See test_ddr_stream_axi.py reset_attack_test for the rationale.
    BUSY_SETTLE_MAX = 500
    busy_cleared_at = 0
    busy_after = 0
    for _ in range(BUSY_SETTLE_MAX):
        await RisingEdge(dut.CLK_SYS)

    post_write = 0
    post_read  = 0
    for _ in range(200):
        await RisingEdge(dut.CLK_DDR)
        if int(dut.M_AVMM_write.value) and not int(dut.M_AVMM_waitrequest.value):
            post_write += 1
        if int(dut.M_AVMM_read.value) and not int(dut.M_AVMM_waitrequest.value):
            post_read += 1

    watcher.kill()

    dut._log.info(
        f"VFIFO-REQ-004 attack: DOUT_VLD pulses during reset+settle="
        f"{dout_vld_observed}, BUSY cleared at cycle {busy_cleared_at} "
        f"(of {BUSY_SETTLE_MAX} max), post-deassert write={post_write} "
        f"read={post_read}"
    )

    assert dout_vld_observed == 0, (
        f"VFIFO-REQ-004(a) violated — DOUT_VLD asserted "
        f"{dout_vld_observed} time(s) during reset window."
    )
    # DDR-P3.10: VFIFO-REQ-004(b) BUSY check retired (busy_o port gone).
    assert post_write == 0 and post_read == 0, (
        f"VFIFO-REQ-004(c) violated — observed {post_write} writes + "
        f"{post_read} reads in the 200 CLK_DDR cycles after RST_SYS deassert; "
        f"IP is flushing stale pre-reset traffic."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Test 4 — cleaver_n20_avmm_trace — small-N reproduction with full
# WRITE-side trace + AvmmRam dump. Mirrors cleaver_n20_axi_trace; written
# as a clean-path validator after the write path is wired up.
# ──────────────────────────────────────────────────────────────────────────────

@cocotb.test()
async def cleaver_n20_avmm_trace(dut):
    """Small-N (20) reproduction with full AVMM write-side trace.
    Per-beat WDATA, address and the mem-model dump are recorded, and the
    test asserts every committed beat's lane-0 word matches the expected
    monotonic value. A regression on avmm_w's skid_buffer would fire here
    with a list of mismatched beats (mirrors the VFIFO-T1 cleaver).

    RR--P3.6: ratio-4 only — see test_ddr_stream_axi for the rationale.
    """
    _ratio = int(dut.G_DDR_DATA_WIDTH.value) // int(dut.G_USER_DATA_WIDTH.value)
    if _ratio != 4:
        dut._log.info(
            f"cleaver_n20_avmm: skipping — ratio={_ratio} (test is ratio-4 only)"
        )
        return
    DDR_WIDTH = int(dut.G_DDR_ADDR_WIDTH.value)
    ram = await _bring_up_clocks_and_reset(dut, ddr_size=1 << DDR_WIDTH)

    write_trace: list[tuple[float, int, int]] = []  # (time_ns, addr, writedata)

    async def watch_writes():
        beat_idx = 0
        while True:
            await RisingEdge(dut.CLK_DDR)
            if int(dut.M_AVMM_write.value) and not int(dut.M_AVMM_waitrequest.value):
                t_ns = cocotb.utils.get_sim_time(units="ns")
                addr = int(dut.M_AVMM_address.value)
                data = int(dut.M_AVMM_writedata.value)
                write_trace.append((t_ns, addr, data))
                beat_idx += 1

    cocotb.start_soon(watch_writes())

    N_WRITES = 20
    expected: list[int] = []
    for i in range(N_WRITES):
        dut.S_AXIS_DATA.value     = Immediate(i + 1)
        dut.S_AXIS_VALID.value   = Immediate(1)
        expected.append(i + 1)
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value   = Immediate(0)

    await Timer(50, units="us")

    dut._log.info(f"=== CLEAVER N20 — AVMM WRITE TRACE ({len(write_trace)} beats) ===")
    for i, (t, addr, data) in enumerate(write_trace[:16]):
        lanes = [(data >> (lane * 32)) & 0xFFFFFFFF for lane in range(4)]
        dut._log.info(
            f"  W[{i:2d}] @ {t:8.1f}ns: addr=0x{addr:08x} "
            f"data=[{lanes[0]:#x}, {lanes[1]:#x}, {lanes[2]:#x}, {lanes[3]:#x}]"
        )

    dut._log.info("=== CLEAVER N20 — AvmmRam dump @ base ===")
    dump = ram.read(0, 128)
    for i in range(8):
        word = int.from_bytes(dump[i*16:(i+1)*16], byteorder="little")
        lanes = [(word >> (lane * 32)) & 0xFFFFFFFF for lane in range(4)]
        dut._log.info(
            f"  RAM[0x{i*16:04x}]: [{lanes[0]:#x}, {lanes[1]:#x}, "
            f"{lanes[2]:#x}, {lanes[3]:#x}]"
        )

    # Lane-0 of each DDR word is the first packed message in that word.
    # With G_DDR_DATA_WIDTH=128 / C_USER_MSG_WIDTH=32, each beat packs
    # 4 messages → expected lane0 of beat k = (k * 4 + 1).
    diffs = []
    for i, (_, _, data) in enumerate(write_trace):
        lane0    = data & 0xFFFFFFFF
        expected_lane0 = (i * 4 + 1) & 0xFFFFFFFF
        if lane0 != expected_lane0:
            diffs.append((i, hex(expected_lane0), hex(lane0)))
    assert not diffs, (
        f"DDR-P3.1 regression: {len(diffs)} of {len(write_trace)} W beats "
        f"wrong on lane 0.\n  first 5 (idx, expected, got): {diffs[:5]}"
    )

    # Also validate AvmmRam contents at base..base+5*16.
    ram_diffs = []
    for ddr_word_idx in range(min(5, N_WRITES // 4)):
        word_bytes = ram.read(ddr_word_idx * 16, 16)
        word = int.from_bytes(word_bytes, byteorder="little")
        for lane in range(4):
            actual   = (word >> (lane * 32)) & 0xFFFFFFFF
            expected = ddr_word_idx * 4 + lane + 1
            if actual != expected:
                ram_diffs.append((ddr_word_idx, lane, hex(expected), hex(actual)))
    assert not ram_diffs, (
        f"DDR-P3.1 regression: {len(ram_diffs)} AvmmRam slots wrong.\n"
        f"  first 5 (ddr_word, lane, expected, got): {ram_diffs[:5]}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# NIK-P2.47 / VFIFO-T2.4 — ratio-16 partial-word pad-flush on the AVMM write path.
# ──────────────────────────────────────────────────────────────────────────────
# Downstream DDR4 DMA (ddr_stream_avmm → Merlin → io96b EMIF, 512-bit = ratio 16)
# corrupted bursts >128B: burst-length-dependent, STALE trailing-beat values,
# SIM-CLEAN under the drain-race repro (which used FULL 16-beat bursts → never
# exercised a partial wide word, so the DDR-T2 pad-flush never fired). The shared
# ddr_fifo_intf pad-flush was hardcoded ratio-4 (2-bit pad counters) → at ratio 16
# it mis-padded the partial final wide word, stranding/corrupting the tail =
# exactly the HW signature. This pushes a NON-ratio-aligned count + idles the
# producer to FIRE the flush, then asserts full in-order recovery. Runs across the
# 128b/512b sweep. Pre-fix (ratio-4 hardcode) FAILS at 512b; post-fix PASSES.
@cocotb.test()
@requires("DDR-REQ-001")
async def t24_pad_flush_partial_tail_roundtrip(dut):
    """Non-ratio-aligned AVMM roundtrip — must recover every word (NIK-P2.47)."""
    DDR_WIDTH = int(dut.G_DDR_ADDR_WIDTH.value)
    ram = await _bring_up_clocks_and_reset(dut, ddr_size=1 << DDR_WIDTH, stall_p=0.30)
    fifo.clear()
    cocotb.start_soon(data_grabber(dut))

    ratio = int(dut.G_DDR_DATA_WIDTH.value) // int(dut.G_USER_DATA_WIDTH.value)
    # 70 is non-aligned to ratio 4 (rem 2) AND ratio 16 (rem 6) → partial wide word.
    N = 70
    expected = list(range(1, N + 1))
    for i in range(N):
        dut.S_AXIS_DATA.value  = Immediate(i + 1)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)

    # Producer idle → DDR-T2 pad-flush (C_IDLE_FLUSH_THRES) completes the partial
    # wide word; then drain via M_AXIS.
    await Timer(120, units="us")
    for _ in range(N * 64):
        dut.M_AXIS_READY.value = 1
        await RisingEdge(dut.CLK_SYS)
        if len(fifo) >= N:
            break
    dut.M_AXIS_READY.value = 0
    await Timer(20, units="us")

    dut._log.info(f"NIK-P2.47/T2.4-AVMM: ratio={ratio} N={N} (rem {N % ratio}) "
                  f"recovered {len(fifo)}/{N}")
    fifo_checker(fifo, expected)


# ──────────────────────────────────────────────────────────────────────────────
# DDR-P3.9 (AVMM mirror): runtime base-address loading via CSR ports.
#
# The AXI backend (test_ddr_stream_axi.py::runtime_base_addr_test) has covered
# this since DDR-P3.9 landed; the AVMM backend never got the equivalent test.
# Same shape: default base (== ddr_base_addr_g generic = 0), pulse
# csr_wr_base_load + csr_rd_base_load with a NEW base pre-traffic, push a
# batch, and assert it lands at the new AvmmRam region (not the old one).
# ──────────────────────────────────────────────────────────────────────────────
@cocotb.test()
async def runtime_base_addr_test(dut):
    """DDR-P3.9 (AVMM) — load a new write/read base pre-traffic and verify
    the batch lands at the new region, with nothing at the old one.
    """
    dut.CSR_WR_BASE_LOAD.value = 0
    dut.CSR_RD_BASE_LOAD.value = 0
    dut.CSR_WR_BASE_ADDR.value = 0
    dut.CSR_RD_BASE_ADDR.value = 0

    DDR_WIDTH = int(dut.G_DDR_ADDR_WIDTH.value)
    ram = await _bring_up_clocks_and_reset(dut, ddr_size=1 << DDR_WIDTH)

    NEW_BASE = 0x1000
    dut._log.info(f"DDR-P3.9 (AVMM): load base=0x{NEW_BASE:x}")
    assert int(dut.WRITE_IDLE.value) == 1, "WRITE_IDLE should be high post-reset"
    assert int(dut.READ_IDLE.value) == 1, "READ_IDLE should be high post-reset"
    dut.CSR_WR_BASE_ADDR.value = Immediate(NEW_BASE)
    dut.CSR_RD_BASE_ADDR.value = Immediate(NEW_BASE)
    dut.CSR_WR_BASE_LOAD.value = Immediate(1)
    dut.CSR_RD_BASE_LOAD.value = Immediate(1)
    await RisingEdge(dut.CLK_SYS)
    dut.CSR_WR_BASE_LOAD.value = Immediate(0)
    dut.CSR_RD_BASE_LOAD.value = Immediate(0)
    for _ in range(32):
        await RisingEdge(dut.CLK_DDR)

    N = 64
    expected = list(range(1, 1 + N))
    for v in expected:
        dut.S_AXIS_DATA.value = Immediate(v & 0xFFFFFFFF)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)
    await Timer(30, units="us")

    n_ddr_words = (N + 3) // 4
    old_region = ram.read(0x000, n_ddr_words * 16)
    assert all(b == 0 for b in old_region), (
        f"DDR-P3.9 (AVMM): old base 0x000 has non-zero bytes — runtime base "
        f"didn't take effect. first 16={list(old_region[:16])}"
    )
    new_region_data = ram.read(NEW_BASE, n_ddr_words * 16)
    lanes: list[int] = []
    for w in range(n_ddr_words):
        word_bytes = new_region_data[w * 16:(w + 1) * 16]
        word = int.from_bytes(word_bytes, byteorder="little")
        for lane in range(4):
            lanes.append((word >> (lane * 32)) & 0xFFFFFFFF)
    assert lanes == expected, (
        f"DDR-P3.9 (AVMM): new base 0x{NEW_BASE:x} should hold {expected[:8]}.., "
        f"got {lanes[:8]}.."
    )

    dut.M_AXIS_READY.value = 1
    received: list[int] = []
    WATCHDOG_CYCLES = 2048
    cycles = 0
    while len(received) < N and cycles < WATCHDOG_CYCLES:
        await RisingEdge(dut.CLK_SYS)
        await ReadOnly()
        if int(dut.M_AXIS_VALID.value) and int(dut.M_AXIS_READY.value):
            received.append(int(dut.M_AXIS_DATA.value))
        cycles += 1
    await RisingEdge(dut.CLK_SYS)
    dut.M_AXIS_READY.value = 0
    assert received == expected, (
        f"DDR-P3.9 (AVMM) read-back: expected {expected}, got {received}"
    )
    dut._log.info(f"DDR-P3.9 (AVMM): all {N} words landed + read back at the new base ✓")


# ──────────────────────────────────────────────────────────────────────────────
# DDR-P3.11 (AVMM mirror): pointer-reset CSR — ping-pong buffer switching.
# See test_ddr_stream_axi.py's DDR-P3.11 block for the full design rationale.
# ──────────────────────────────────────────────────────────────────────────────
async def _push_and_drain_avmm(dut, ram, base_addr, expected, *, tag):
    N = len(expected)
    for v in expected:
        dut.S_AXIS_DATA.value = Immediate(v & 0xFFFFFFFF)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)
    await Timer(30, units="us")

    dut.M_AXIS_READY.value = 1
    received: list[int] = []
    WATCHDOG_CYCLES = 4096
    cycles = 0
    while len(received) < N and cycles < WATCHDOG_CYCLES:
        await RisingEdge(dut.CLK_SYS)
        await ReadOnly()
        if int(dut.M_AXIS_VALID.value) and int(dut.M_AXIS_READY.value):
            received.append(int(dut.M_AXIS_DATA.value))
        cycles += 1
    await RisingEdge(dut.CLK_SYS)
    dut.M_AXIS_READY.value = 0

    assert received == expected, (
        f"DDR-P3.11 (AVMM) [{tag}] read-back mismatch: expected {expected[:8]}.., "
        f"got {received[:8]}.."
    )

    n_ddr_words = (N + 3) // 4
    region = ram.read(base_addr, n_ddr_words * 16)
    lanes: list[int] = []
    for w in range(n_ddr_words):
        word_bytes = region[w * 16:(w + 1) * 16]
        word = int.from_bytes(word_bytes, byteorder="little")
        for lane in range(4):
            lanes.append((word >> (lane * 32)) & 0xFFFFFFFF)
    assert lanes[:N] == expected, (
        f"DDR-P3.11 (AVMM) [{tag}] AvmmRam@0x{base_addr:x} should hold "
        f"{expected[:8]}.., got {lanes[:8]}.. — offset did NOT start at 0 of this buffer"
    )


@cocotb.test()
async def ping_pong_basic_test(dut):
    """DDR-P3.11 (AVMM) — load base A, push N words, drain; load base B +
    pulse ptr_reset, push N more words, drain. Buffer B's data must land at
    OFFSET 0 of base B, and buffer A's region must survive untouched.
    """
    dut.CSR_WR_BASE_LOAD.value = 0
    dut.CSR_RD_BASE_LOAD.value = 0
    dut.CSR_WR_BASE_ADDR.value = 0
    dut.CSR_RD_BASE_ADDR.value = 0
    dut.CSR_WR_PTR_RESET.value = 0
    dut.CSR_RD_PTR_RESET.value = 0

    DDR_WIDTH = int(dut.G_DDR_ADDR_WIDTH.value)
    ram = await _bring_up_clocks_and_reset(dut, ddr_size=1 << DDR_WIDTH)

    BASE_A = 0x0
    BASE_B = 0x200000
    N = 64

    assert int(dut.WRITE_IDLE.value) == 1
    assert int(dut.READ_IDLE.value) == 1
    dut._log.info("DDR-P3.11 (AVMM): buffer A (base=0x0) — batch 1")
    expected_a = list(range(1, 1 + N))
    await _push_and_drain_avmm(dut, ram, BASE_A, expected_a, tag="buffer-A")

    assert int(dut.WRITE_IDLE.value) == 1
    assert int(dut.READ_IDLE.value) == 1
    dut._log.info(f"DDR-P3.11 (AVMM): swap to buffer B (base=0x{BASE_B:x}) — load+ptr_reset")
    dut.CSR_WR_BASE_ADDR.value = Immediate(BASE_B)
    dut.CSR_RD_BASE_ADDR.value = Immediate(BASE_B)
    dut.CSR_WR_BASE_LOAD.value = Immediate(1)
    dut.CSR_RD_BASE_LOAD.value = Immediate(1)
    dut.CSR_WR_PTR_RESET.value = Immediate(1)
    dut.CSR_RD_PTR_RESET.value = Immediate(1)
    await RisingEdge(dut.CLK_SYS)
    dut.CSR_WR_BASE_LOAD.value = Immediate(0)
    dut.CSR_RD_BASE_LOAD.value = Immediate(0)
    dut.CSR_WR_PTR_RESET.value = Immediate(0)
    dut.CSR_RD_PTR_RESET.value = Immediate(0)
    for _ in range(32):
        await RisingEdge(dut.CLK_DDR)

    dut._log.info(f"DDR-P3.11 (AVMM): buffer B (base=0x{BASE_B:x}) — batch 2, must start at OFFSET 0")
    expected_b = list(range(1000, 1000 + N))
    await _push_and_drain_avmm(dut, ram, BASE_B, expected_b, tag="buffer-B")

    n_ddr_words = (N + 3) // 4
    region_a_after = ram.read(BASE_A, n_ddr_words * 16)
    lanes_a: list[int] = []
    for w in range(n_ddr_words):
        word_bytes = region_a_after[w * 16:(w + 1) * 16]
        word = int.from_bytes(word_bytes, byteorder="little")
        for lane in range(4):
            lanes_a.append((word >> (lane * 32)) & 0xFFFFFFFF)
    assert lanes_a[:N] == expected_a, (
        "DDR-P3.11 (AVMM): buffer A's data must survive the swap to buffer B untouched"
    )
    dut._log.info("DDR-P3.11 (AVMM): ping-pong swap — buffer A intact, buffer B at offset 0 ✓")


@cocotb.test()
async def ping_pong_concurrent_test(dut):
    """DDR-P4.4 AVMM mirror: a two-plane base+ptr swap pulse while the
    shared AVMM bus is actively writing and draining A is refused.  Once the
    drain completes, a full two-plane retry clears both offset trackers so B
    begins at offset zero."""
    dut.CSR_WR_BASE_LOAD.value = 0
    dut.CSR_RD_BASE_LOAD.value = 0
    dut.CSR_WR_BASE_ADDR.value = 0
    dut.CSR_RD_BASE_ADDR.value = 0
    dut.CSR_WR_PTR_RESET.value = 0
    dut.CSR_RD_PTR_RESET.value = 0

    DDR_WIDTH = int(dut.G_DDR_ADDR_WIDTH.value)
    # Hostile WAITREQUEST runs make the read-busy interval span several
    # clk_sys samples, so the mid-drain attack window is deterministic.
    ram = await _bring_up_clocks_and_reset(
        dut, ddr_size=1 << DDR_WIDTH, stall_p=0.35
    )

    BASE_B = 0x200000
    # Long enough that the 512-bit DDR / XPM sweep still has simultaneous
    # write and read activity; 128 words can drain before that overlap opens.
    N = 512
    WATCHDOG_CYCLES = 8192
    expected_a = list(range(1, 1 + N))

    fifo.clear()
    grabber = cocotb.start_soon(data_grabber(dut))
    dut.M_AXIS_READY.value = 1

    async def push_a():
        for v in expected_a:
            dut.S_AXIS_DATA.value = Immediate(v & 0xFFFFFFFF)
            dut.S_AXIS_VALID.value = Immediate(1)
            await RisingEdge(dut.CLK_SYS)
            while not int(dut.S_AXIS_READY.value):
                await RisingEdge(dut.CLK_SYS)
        dut.S_AXIS_VALID.value = Immediate(0)

    producer_a = cocotb.start_soon(push_a())
    # AVMM has one shared command bus, so wide configurations can alternate
    # write and read busy slices without ever reporting both low together.
    # Attack each plane in its own busy slice of the same active drain.
    cycles = 0
    while cycles < WATCHDOG_CYCLES:
        await RisingEdge(dut.CLK_SYS)
        await ReadOnly()
        if (0 < len(fifo) < N
                and int(dut.WRITE_IDLE.value) == 0):
            break
        cycles += 1
    assert 0 < len(fifo) < N, (
        "DDR-P4.4 (AVMM) positive control failed: no observably active "
        f"mid-drain window ({len(fifo)}/{N} A words had drained)"
    )
    assert int(dut.WRITE_IDLE.value) == 0, (
        "DDR-P4.4 (AVMM) precondition failed: write base+pointer reset must "
        "be attempted during a write-busy slice of the active drain"
    )

    await NextTimeStep()
    dut.CSR_WR_BASE_ADDR.value = Immediate(BASE_B)
    dut.CSR_RD_BASE_ADDR.value = Immediate(BASE_B)
    dut.CSR_WR_BASE_LOAD.value = Immediate(1)
    dut.CSR_WR_PTR_RESET.value = Immediate(1)
    await RisingEdge(dut.CLK_SYS)
    assert int(dut.WRITE_IDLE.value) == 0, (
        "DDR-P4.4 (AVMM) sampling-edge precondition failed: write_idle_o "
        "was not low when the DUT sampled the write-side swap pulse"
    )
    await NextTimeStep()
    dut.CSR_WR_BASE_LOAD.value = Immediate(0)
    dut.CSR_WR_PTR_RESET.value = Immediate(0)

    cycles = 0
    while cycles < WATCHDOG_CYCLES:
        await RisingEdge(dut.CLK_SYS)
        await ReadOnly()
        if 0 < len(fifo) < N and int(dut.READ_IDLE.value) == 0:
            break
        cycles += 1
    assert 0 < len(fifo) < N, (
        "DDR-P4.4 (AVMM) positive control failed: A finished draining "
        "before the read-side attack"
    )
    assert int(dut.READ_IDLE.value) == 0, (
        "DDR-P4.4 (AVMM) precondition failed: read base+pointer reset must "
        "be attempted during a read-busy slice of the active drain"
    )

    await NextTimeStep()
    dut.CSR_RD_BASE_LOAD.value = Immediate(1)
    dut.CSR_RD_PTR_RESET.value = Immediate(1)
    await RisingEdge(dut.CLK_SYS)
    assert int(dut.READ_IDLE.value) == 0, (
        "DDR-P4.4 (AVMM) sampling-edge precondition failed: read_idle_o "
        "was not low when the DUT sampled the read-side swap pulse"
    )
    await NextTimeStep()
    dut.CSR_RD_BASE_LOAD.value = Immediate(0)
    dut.CSR_RD_PTR_RESET.value = Immediate(0)

    await producer_a
    cycles = 0
    while len(fifo) < N and cycles < WATCHDOG_CYCLES:
        await RisingEdge(dut.CLK_SYS)
        cycles += 1
    assert len(fifo) == N, (
        f"DDR-P4.4 (AVMM) refusal: buffer A never fully drained ({len(fifo)}/{N})"
    )
    assert fifo == expected_a, (
        "DDR-P4.4 (AVMM) refusal: the mid-drain request changed a "
        "base/pointer or otherwise corrupted buffer A"
    )

    # Independent write-plane oracle: the rejected write-side base/reset must
    # not split A across the old and requested bases while the producer runs.
    n_ddr_words = (N + 3) // 4
    region_a = ram.read(0, n_ddr_words * 16)
    lanes_a: list[int] = []
    for w in range(n_ddr_words):
        word = int.from_bytes(region_a[w * 16:(w + 1) * 16], byteorder="little")
        for lane in range(4):
            lanes_a.append((word >> (lane * 32)) & 0xFFFFFFFF)
    assert lanes_a[:N] == expected_a, (
        "DDR-P4.4 (AVMM) write-plane refusal: buffer A was split or "
        f"corrupted by the busy swap — got {lanes_a[:8]}.."
    )

    cycles = 0
    while cycles < WATCHDOG_CYCLES:
        await RisingEdge(dut.CLK_SYS)
        await ReadOnly()
        if int(dut.WRITE_IDLE.value) == 1 and int(dut.READ_IDLE.value) == 1:
            break
        cycles += 1
    assert int(dut.WRITE_IDLE.value) == 1 and int(dut.READ_IDLE.value) == 1, (
        "DDR-P4.4 (AVMM) retry window never opened after buffer A drained"
    )

    fifo.clear()
    await NextTimeStep()
    dut.CSR_WR_BASE_LOAD.value = Immediate(1)
    dut.CSR_RD_BASE_LOAD.value = Immediate(1)
    dut.CSR_WR_PTR_RESET.value = Immediate(1)
    dut.CSR_RD_PTR_RESET.value = Immediate(1)
    await RisingEdge(dut.CLK_SYS)
    await NextTimeStep()
    dut.CSR_WR_BASE_LOAD.value = Immediate(0)
    dut.CSR_RD_BASE_LOAD.value = Immediate(0)
    dut.CSR_WR_PTR_RESET.value = Immediate(0)
    dut.CSR_RD_PTR_RESET.value = Immediate(0)
    for _ in range(32):
        await RisingEdge(dut.CLK_DDR)

    expected_b = list(range(2000, 2000 + N))
    for v in expected_b:
        dut.S_AXIS_DATA.value = Immediate(v & 0xFFFFFFFF)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
        while not int(dut.S_AXIS_READY.value):
            await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)

    cycles = 0
    while len(fifo) < N and cycles < WATCHDOG_CYCLES:
        await RisingEdge(dut.CLK_SYS)
        cycles += 1
    dut.M_AXIS_READY.value = 0
    grabber.cancel()

    assert len(fifo) == N, (
        f"DDR-P4.4 (AVMM) retry: buffer B short — got {len(fifo)}/{N}"
    )
    assert fifo == expected_b, (
        "DDR-P4.4 (AVMM) retry: buffer B read-back mismatch — "
        f"expected {expected_b[:8]}.., got {fifo[:8]}.."
    )

    region_b = ram.read(BASE_B, n_ddr_words * 16)
    lanes_b: list[int] = []
    for w in range(n_ddr_words):
        word = int.from_bytes(region_b[w * 16:(w + 1) * 16], byteorder="little")
        for lane in range(4):
            lanes_b.append((word >> (lane * 32)) & 0xFFFFFFFF)
    assert lanes_b[:N] == expected_b, (
        f"DDR-P4.4 (AVMM) retry: AvmmRam@0x{BASE_B:x} should hold "
        f"{expected_b[:8]}.., got {lanes_b[:8]}.. — offset did NOT reset"
    )
    dut._log.info("DDR-P4.4 (AVMM): mid-drain swap refused; idle retry reset offsets ✓")


if __name__ == "__main__":
    main()

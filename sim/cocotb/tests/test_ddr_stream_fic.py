# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""Microchip PolarFire SoC MSS FIC backend test suite for rr_ddr_stream (DDR-P2.15).

Tests the Microchip PolarFire SoC MSS FIC AXI4 backend:
  1. fic_mem_model_demo: 64-bit wide MSS LPDDR4 roundtrip data integrity.
  2. fic_4k_boundary_test: 4 KiB boundary adherence across sequential bursts.
  3. fic_reset_attack_test: mid-stream reset recovery, zero spurious egress.
  4. fic_core_contract_test: write-plane arming (chip_empty deassertion),
     commit pointer tracking (WR_COMMIT_ADDR), write_idle_o, and
     dbg_reset_status_o telemetry as instrumented in DS-T1.13.
  5. fic_burst_cap_conformance: burst cap constant C_PFSOC_FIC_MAX_BURST (64 beats)
     enforcement under DDR backpressure.

Run via: rr sim run test_ddr_stream_fic
"""
from __future__ import annotations

import cocotb
import logging
from collections import deque
from pathlib import Path as _Path

from cocotb.clock import Clock
from cocotb.handle import Immediate
from cocotb.triggers import ReadOnly, RisingEdge, Timer


def _ip_yml_version_word() -> int:
    """ip.yml ``version: "X.Y.Z"`` as the VERSION register word 0xXXYYZZ00.

    DDR-P2.24: the expected VERSION used to be a hard-coded literal, so the
    0.3.3 release bump left this test red and blocked ``rr pkg publish``.
    """
    import re
    ip_yml = _Path(__file__).resolve().parents[3] / "ip.yml"
    m = re.search(r'^version:\s*["\']?(\d+)\.(\d+)\.(\d+)', ip_yml.read_text(),
                  re.MULTILINE)
    assert m, f"no X.Y.Z version in {ip_yml}"
    major, minor, patch = (int(g) for g in m.groups())
    return (major << 24) | (minor << 16) | (patch << 8)

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
    from sdk.cocotb_helpers import requires  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None

    def requires(*_args, **_kwargs):  # type: ignore[no-redef]
        def _wrap(fn):
            return fn

        return _wrap


from fic_target_bfm import FicTargetSubordinate

_IP_ROOT = _Path(__file__).resolve().parents[3]
_RTL_DIR = _IP_ROOT / "rtl"
_SUPPORT_DIR = _RTL_DIR / "support"
_TB_DIR = _IP_ROOT / "sim" / "tb"
_OLO_SYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_sync" / "src" / "base" / "vhdl"
_OLO_ASYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_async" / "src" / "base" / "vhdl"
_OLO_WCN_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_n2xn" / "src" / "base" / "vhdl"
_OLO_WCXN_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_xn2n" / "src" / "base" / "vhdl"

# 64-bit DDR bus matches the PolarFire SoC MSS FIC0/FIC1 target interface width.
FIC_DATA_W = 64
FIC_BURST_CAP = 64

DDR_PERIOD = 3.33  # ns, ~300 MHz
SYS_PERIOD = 10.0  # ns, 100 MHz


def _libraries():
    """Analysis set for ddr_stream_fic."""
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
            str(_TB_DIR / "wrappers" / "wrapper_ddr_stream_fic.vhd"),
        ],
    }


def main() -> None:
    if run_simulation is None:
        raise RuntimeError("Invoke via `rr sim run test_ddr_stream_fic`.")
    libs = _libraries()
    run_simulation(
        top_level="wrapper_ddr_stream_fic",
        module="test_ddr_stream_fic",
        custom_libraries=libs,
        work_lib="rr",
        generics={
            "G_AXI_ID": 0,
            "G_FIFO_DEPTH": 12,
            "G_USER_DATA_WIDTH": 32,
            "G_DDR_DATA_WIDTH": FIC_DATA_W,
            "G_DDR_ADDR_WIDTH": 32,
            "G_BUFFER_WIDTH": 20,
            "G_FIFO_IMPL": "olo",
        },
        waves=True,
        simulator="nvc",
    )


async def _bring_up_clocks_and_reset(dut):
    """Start clocks and cycle resets into clean initial state."""
    cocotb.start_soon(Clock(dut.CLK_DDR, DDR_PERIOD, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_SYS, SYS_PERIOD, units="ns").start())

    dut.RST_DDR.value = 1
    dut.RST_SYS.value = 1
    dut.S_AXIS_VALID.value = 0
    dut.M_AXIS_READY.value = 0
    dut.S_AXIS_DATA.value = 0

    dut.CSR_WR_BASE_ADDR.value = 0
    dut.CSR_WR_BASE_LOAD.value = 0
    dut.CSR_RD_BASE_ADDR.value = 0
    dut.CSR_RD_BASE_LOAD.value = 0
    dut.CSR_WR_PTR_RESET.value = 0
    dut.CSR_RD_PTR_RESET.value = 0
    dut.CSR_RD_PTR_ADDR.value = 0
    dut.CSR_RD_PTR_LOAD.value = 0
    dut.CSR_WR_PTR_ADDR.value = 0
    dut.CSR_WR_PTR_LOAD.value = 0

    for _ in range(8):
        await RisingEdge(dut.CLK_DDR)
    dut.RST_DDR.value = 0
    for _ in range(8):
        await RisingEdge(dut.CLK_SYS)
    dut.RST_SYS.value = 0

    await Timer(3, units="us")
    await RisingEdge(dut.CLK_SYS)


# ──────────────────────────────────────────────────────────────────────────────
# Test 1: Full roundtrip data integrity through PolarFire SoC FIC target BFM
# ──────────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("DDR-REQ-001")
async def fic_mem_model_demo(dut):
    """PolarFire SoC FIC: Write 1024 words through s_axis into MSS LPDDR4, read back."""
    await _bring_up_clocks_and_reset(dut)

    bfm = FicTargetSubordinate(
        dut,
        dut.CLK_DDR,
        dut.RST_DDR,
        prefix="M_AXI",
        data_w=FIC_DATA_W,
        addr_w=32,
        mem_size=1 << 21,
        fail_on_violation=True,
        b_latency=4,
        ar_to_r_latency=4,
    )
    bfm.start()

    fifo_recv: list[int] = []

    async def grabber():
        while True:
            await RisingEdge(dut.CLK_SYS)
            await ReadOnly()
            if int(dut.M_AXIS_VALID.value) and int(dut.M_AXIS_READY.value):
                fifo_recv.append(int(dut.M_AXIS_DATA.value))

    cocotb.start_soon(grabber())

    N_WRITES = 1024
    expected: list[int] = []
    for i in range(N_WRITES):
        val = (i + 1) & 0xFFFFFFFF
        dut.S_AXIS_DATA.value = Immediate(val)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
        while not int(dut.S_AXIS_READY.value):
            await RisingEdge(dut.CLK_SYS)
        expected.append(val)
    dut.S_AXIS_VALID.value = Immediate(0)

    # Allow write pipeline to drain to FIC subordinate
    await Timer(50, units="us")

    # Read phase: drive M_AXIS_READY high until all items received
    dut.M_AXIS_READY.value = 1
    for _ in range(N_WRITES + 50):
        await RisingEdge(dut.CLK_SYS)
        if len(fifo_recv) >= N_WRITES:
            break
    dut.M_AXIS_READY.value = 0
    await Timer(10, units="us")

    dut._log.info(
        f"FIC mem demo: received={len(fifo_recv)}, expected={len(expected)}, "
        f"AW bursts={bfm.aw_seen}, AR bursts={bfm.ar_seen}, violations={len(bfm.violations)}"
    )

    assert bfm.aw_seen >= 2, f"Insufficient AW bursts seen: {bfm.aw_seen}"
    assert bfm.ar_seen >= 2, f"Insufficient AR bursts seen: {bfm.ar_seen}"
    assert not bfm.violations, f"FIC violations: {bfm.violations}"
    assert len(fifo_recv) == len(expected), (
        f"Length mismatch: received {len(fifo_recv)} words, expected {len(expected)}"
    )
    for idx, (exp, act) in enumerate(zip(expected, fifo_recv)):
        assert exp == act, f"Word {idx} mismatch: expected 0x{exp:08x}, got 0x{act:08x}"


# ──────────────────────────────────────────────────────────────────────────────
# Test 2: 4 KiB boundary conformance across FIC target port
# ──────────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("DDR-REQ-203")
async def fic_4k_boundary_test(dut):
    """PolarFire SoC FIC: No AXI4 burst into MSS LPDDR4 crosses a 4 KiB boundary."""
    await _bring_up_clocks_and_reset(dut)

    bfm = FicTargetSubordinate(
        dut,
        dut.CLK_DDR,
        dut.RST_DDR,
        prefix="M_AXI",
        data_w=FIC_DATA_W,
        addr_w=32,
        mem_size=1 << 21,
        fail_on_violation=True,
    )
    bfm.start()

    N_WRITES = 4096
    for i in range(N_WRITES):
        dut.S_AXIS_DATA.value = Immediate((i + 1) & 0xFFFFFFFF)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
        while not int(dut.S_AXIS_READY.value):
            await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)

    # Let write pipeline drain
    await Timer(150, units="us")

    # Read phase so AR bursts get tested
    dut.M_AXIS_READY.value = 1
    for _ in range(N_WRITES + 100):
        await RisingEdge(dut.CLK_SYS)
    dut.M_AXIS_READY.value = 0
    await Timer(50, units="us")

    dut._log.info(
        f"FIC 4K test: AW bursts={bfm.aw_seen}, AR bursts={bfm.ar_seen}, "
        f"violations={len(bfm.violations)}"
    )

    assert bfm.aw_seen >= 4, f"Insufficient AW bursts: {bfm.aw_seen}"
    assert bfm.ar_seen >= 4, f"Insufficient AR bursts: {bfm.ar_seen}"
    assert not bfm.violations, f"4KB boundary violations detected: {bfm.violations}"


# ──────────────────────────────────────────────────────────────────────────────
# Test 3: Reset recovery with traffic in-flight
# ──────────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("DDR-REQ-RST-1")
async def fic_reset_recovery_test(dut):
    """PolarFire SoC FIC: Mid-stream reset while traffic is actively in-flight."""
    await _bring_up_clocks_and_reset(dut)

    bfm = FicTargetSubordinate(
        dut,
        dut.CLK_DDR,
        dut.RST_DDR,
        prefix="M_AXI",
        data_w=FIC_DATA_W,
        addr_w=32,
        mem_size=1 << 21,
        fail_on_violation=True,
    )
    bfm.start()

    stop_stream = False

    async def tx_stream():
        i = 0
        while not stop_stream:
            dut.S_AXIS_DATA.value = Immediate((i + 1) & 0xFFFFFFFF)
            dut.S_AXIS_VALID.value = Immediate(1)
            await RisingEdge(dut.CLK_SYS)
            i += 1
        dut.S_AXIS_VALID.value = Immediate(0)

    tx_task = cocotb.start_soon(tx_stream())

    # Wait until write traffic is actively handshaking on M_AXI
    for _ in range(200):
        await RisingEdge(dut.CLK_DDR)
        if int(dut.M_AXI_AWVALID.value) and int(dut.M_AXI_AWREADY.value):
            break

    # Slam reset while bursts are in-flight
    dut.RST_SYS.value = 1
    dut.RST_DDR.value = 1
    stop_stream = True
    await tx_task

    for _ in range(16):
        await RisingEdge(dut.CLK_DDR)
    dut.RST_SYS.value = 0
    dut.RST_DDR.value = 0

    # Observe post-reset settling window: no spurious master traffic
    post_aw = 0
    post_ar = 0
    for _ in range(200):
        await RisingEdge(dut.CLK_DDR)
        if int(dut.M_AXI_AWVALID.value) and int(dut.M_AXI_AWREADY.value):
            post_aw += 1
        if int(dut.M_AXI_ARVALID.value) and int(dut.M_AXI_ARREADY.value):
            post_ar += 1

    dut._log.info(f"FIC reset recovery: post_aw={post_aw}, post_ar={post_ar}")
    assert post_aw == 0 and post_ar == 0, (
        f"Observed {post_aw} AW and {post_ar} AR handshakes after reset; pipeline failed to flush."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Test 4: Core-side contract validation (DS-T1.13 instrumented ports)
# ──────────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("DDR-REQ-207")
async def fic_core_contract_test(dut):
    """PolarFire SoC FIC: write-plane arming, commit tracking, write_idle, and telemetry."""
    await _bring_up_clocks_and_reset(dut)

    bfm = FicTargetSubordinate(
        dut,
        dut.CLK_DDR,
        dut.RST_DDR,
        prefix="M_AXI",
        data_w=FIC_DATA_W,
        addr_w=32,
        mem_size=1 << 21,
        fail_on_violation=True,
        ar_to_r_latency=4000,
    )
    bfm.start()

    # Track write-plane arming across the transfer window
    chip_empty_deasserted = False

    async def monitor_chip_empty():
        nonlocal chip_empty_deasserted
        while True:
            await RisingEdge(dut.CLK_SYS)
            if int(dut.DDR_CSR.fifoStatus.chip_empty.value) == 0:
                chip_empty_deasserted = True

    cocotb.start_soon(monitor_chip_empty())

    # 1. Version stamp check: C_DDR_STREAM_VERSION must equal ip.yml's
    # version (DDR-P2.24 — a release bump stranded a hard-coded literal).
    expected_ver = _ip_yml_version_word()
    ver = int(dut.VERSION.value)
    dut._log.info(f"Core contract: VERSION = 0x{ver:08x}")
    assert ver == expected_ver, (
        f"Expected version 0x{expected_ver:08x} (ip.yml), got 0x{ver:08x}")

    # 2. dbg_reset_status_o check: CS active post-reset
    dbg_status = int(dut.DBG_RESET_STATUS.value)
    dut._log.info(f"Core contract: DBG_RESET_STATUS = 0x{dbg_status:x}")
    assert (dbg_status & 0x3) == 0x3, f"CS not active post-reset: 0x{dbg_status:x}"

    # 3. Initial idle status & DS-T1.13 backpressure counters
    assert int(dut.WRITE_IDLE.value) == 1, "WRITE_IDLE not high initially"
    assert int(dut.READ_IDLE.value) == 1, "READ_IDLE not high initially"
    assert int(dut.DDR_CSR.fifoStatus.chip_empty.value) == 1, "Initial chip_empty is not 1"
    assert int(dut.DDR_CSR.fifoStatus.empty.value) == 1, "Initial empty is not 1"

    fifo_cnt_init = int(dut.DDR_CSR.fifoCnt.value)
    fifo_full_cnt_init = int(dut.DDR_CSR.fifoFullCnt.value)
    fifo_empty_cnt_init = int(dut.DDR_CSR.fifoEmptyCnt.value)
    fifo_free_cnt_init = int(dut.DDR_CSR.fifoFreeCnt.value)
    dut._log.info(
        f"Initial DS-T1.13 counters: fifoCnt={fifo_cnt_init}, fifoFullCnt={fifo_full_cnt_init}, "
        f"fifoEmptyCnt={fifo_empty_cnt_init}, fifoFreeCnt={fifo_free_cnt_init}"
    )

    # 4. Write-plane ingress: push data into ingress FIFO
    for i in range(64):
        dut.S_AXIS_DATA.value = Immediate((i + 1) & 0xFFFFFFFF)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
        while not int(dut.S_AXIS_READY.value):
            await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)

    # 5. Wait for transfer to arm and commit into DDR
    await Timer(5, units="us")
    commit_addr = int(dut.WR_COMMIT_ADDR.value)
    dut._log.info(f"Core contract: WR_COMMIT_ADDR = 0x{commit_addr:08x}")
    assert commit_addr > 0, "WR_COMMIT_ADDR failed to advance after write burst"

    # Write-plane arming: chip_empty must deassert (chip_empty=0 when ring has data)
    chip_empty_val = int(dut.DDR_CSR.fifoStatus.chip_empty.value)
    dut._log.info(f"Write-plane armed: chip_empty={chip_empty_val}, deasserted_seen={chip_empty_deasserted}")
    assert chip_empty_deasserted or chip_empty_val == 0, (
        "Write-plane arming failed: chip_empty did not deassert after ingress"
    )

    # 6. Wait for write pipeline to complete and check idle & counters
    await Timer(40, units="us")
    assert int(dut.WRITE_IDLE.value) == 1, "WRITE_IDLE did not return to 1 after pipeline drain"

    fifo_cnt_post = int(dut.DDR_CSR.fifoCnt.value)
    fifo_full_cnt_post = int(dut.DDR_CSR.fifoFullCnt.value)
    dut._log.info(
        f"Post-transfer DS-T1.13 counters: fifoCnt={fifo_cnt_post}, fifoFullCnt={fifo_full_cnt_post}"
    )
    assert fifo_full_cnt_post == 0, f"Unexpected fifoFullCnt={fifo_full_cnt_post} without ring overflow"


# ──────────────────────────────────────────────────────────────────────────────
# Test 5: PolarFire SoC FIC burst cap constant enforcement (64-beat limit)
# ──────────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("DDR-REQ-206")
async def fic_burst_cap_conformance(dut):
    """PolarFire SoC FIC: C_PFSOC_FIC_MAX_BURST (64 beats) is honoured under backpressure."""
    cap = int(dut.G_MAX_BURST_LEN.value)
    assert cap <= 64, f"Generic G_MAX_BURST_LEN={cap} exceeds PolarFire SoC FIC burst cap (64)"

    await _bring_up_clocks_and_reset(dut)

    bfm = FicTargetSubordinate(
        dut,
        dut.CLK_DDR,
        dut.RST_DDR,
        prefix="M_AXI",
        data_w=FIC_DATA_W,
        addr_w=32,
        mem_size=1 << 21,
        fail_on_violation=True,
        on_long_burst="assert",
        # Provoke deep burst accumulation with initial stall and per-burst latency
        aw_initial_stall=2000,
        aw_to_w_latency=80,
        w_stall_prob=0.1,
        b_latency=4,
    )
    bfm.start()

    N_WRITES = 4096
    for i in range(N_WRITES):
        dut.S_AXIS_DATA.value = Immediate((i + 1) & 0xFFFFFFFF)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
        while not int(dut.S_AXIS_READY.value):
            await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)

    # Let pipeline drain into BFM
    await Timer(200, units="us")

    dut._log.info(
        f"FIC burst cap: cap={cap}, AW bursts={bfm.aw_seen}, "
        f"max AWLEN={bfm.max_awlen_seen} (beats={bfm.max_awlen_seen + 1}), "
        f"violations={len(bfm.violations)}"
    )

    # Adversarial floor: must have observed burst traffic
    assert bfm.aw_seen >= 4, f"Insufficient AW bursts seen: {bfm.aw_seen}"
    # Max AWLEN must not exceed 63 (burst length <= 64 beats)
    assert bfm.max_awlen_seen <= 63, (
        f"Burst cap VIOLATED: max AWLEN seen was {bfm.max_awlen_seen} "
        f"({bfm.max_awlen_seen + 1} beats), exceeding FIC limit of 64 beats"
    )
    assert not bfm.violations, f"FIC protocol violations: {bfm.violations}"
    # Adversarial floor: verify traffic actually batched into multi-beat bursts
    assert bfm.max_awlen_seen >= 16, (
        f"Max AWLEN seen was only {bfm.max_awlen_seen}; stimulus failed to provoke long bursts"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Test 6: Positive control — verify FicTargetSubordinate catches bursts > 64 beats
# ──────────────────────────────────────────────────────────────────────────────
class _MockSignal:
    def __init__(self, val: int = 0):
        self.value = val


class _MockDut:
    def __init__(self):
        self.M_AXI_AWADDR = _MockSignal(0)
        self.M_AXI_AWLEN = _MockSignal(0)
        self.M_AXI_AWSIZE = _MockSignal(3)  # 8 bytes/beat
        self.M_AXI_AWBURST = _MockSignal(1)  # INCR
        self.M_AXI_AWID = _MockSignal(0)
        self._log = logging.getLogger("mock_fic")


@cocotb.test()
async def fic_bfm_positive_control(dut):
    """Positive control: verify FicTargetSubordinate flags bursts exceeding 64 beats."""
    mock_dut = _MockDut()
    bfm = FicTargetSubordinate(
        mock_dut,
        dut.CLK_DDR,
        dut.RST_DDR,
        prefix="M_AXI",
        data_w=FIC_DATA_W,
        addr_w=32,
        mem_size=1 << 21,
        fail_on_violation=False,
    )

    # 1. Legal burst of 64 beats (AWLEN = 63): should NOT flag a violation
    mock_dut.M_AXI_AWLEN.value = 63
    bfm._capture_aw()
    assert len(bfm.violations) == 0, f"Unexpected violation on legal 64-beat burst: {bfm.violations}"

    # 2. Illegal burst of 65 beats (AWLEN = 64): MUST flag a violation
    mock_dut.M_AXI_AWLEN.value = 64
    bfm._capture_aw()
    assert len(bfm.violations) == 1, "BFM failed to flag violation on 65-beat burst (positive control failed)"
    assert "FIC max 64 beats" in bfm.violations[0], f"Unexpected violation message: {bfm.violations[0]}"

    # 3. Illegal burst of 256 beats (AWLEN = 255, default AXI4 burst): MUST flag a violation
    mock_dut.M_AXI_AWLEN.value = 255
    bfm._capture_aw()
    assert len(bfm.violations) == 2, "BFM failed to flag violation on 256-beat burst (positive control failed)"
    assert "FIC max 64 beats" in bfm.violations[1], f"Unexpected violation message: {bfm.violations[1]}"

    dut._log.info("fic_bfm_positive_control: successfully proved BFM flags bursts > 64 beats")


if __name__ == "__main__":
    main()

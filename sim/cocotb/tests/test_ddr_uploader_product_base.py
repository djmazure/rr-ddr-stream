# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DDR-P2.14 / DS-T1.12 package regression: full write path at product geometry.

Exercises the full uploader write path using deepskopion DE25 product generics:
  - G_ENABLE_READ_PLANE = False (read plane elided; write-only path)
  - G_ENABLE_WRITE_PLANE = True
  - G_DDR_DATA_WIDTH = 256 (256-bit f2sdram width)
  - G_DDR_ADDR_WIDTH = 32
  - G_DDR_BASE_ADDR = 0xA0000000 (upper half of 32-bit address space, >= 0x8000_0000)
  - G_BUFFER_WIDTH = 28 (deepskopion G_DDR_WIN_WIDTH = 28, 256 MiB ring)
  - G_MAX_BURST_LEN = 16 (deepskopion G_AXI_MAX_BURST = 16, AXI3 cap)
  - G_FIFO_DEPTH = 9
  - G_USER_DATA_WIDTH = 32
  - G_AXI_ID = 0
  - G_FIFO_IMPL = "olo"

INGRESS CADENCE:
Drives the realistic ingress cadence produced by deepskopion's sniffer:
  - 1-word frames followed by idle bubbles (testing the 1-word FIFO edge and
    burstw_setter single-word burst boundary)
  - 0-word FIFO edges (allowing FIFO to empty completely between bursts)
  - Short multi-word frames (2-word, 5-word, 6-word zs_msg packet lengths)
  - Sustained bursts accumulating multiple 256-bit beats and AXI transactions.

WHAT THIS TEST CAN CATCH:
  - Functional write path regressions at product geometry:
    * Elaboration or arithmetic truncation of DDR base address >= 0x8000_0000
    * Address computation escaping the 256 MiB ring [0xA0000000, 0xB0000000)
    * Width-conversion faults in 32-bit user to 256-bit DDR path (ratio 8)
    * Burst length violations exceeding G_MAX_BURST_LEN=16 (AWLEN > 15)
    * Burstw_setter underflow / sizing defects at 1-word and 0-word FIFO edges
    * Uploader wedging IDLE (WRITE_IDLE stuck at 1) under ingress
    * Commit pointer (WR_COMMIT_ADDR / RING_COMMIT_PTR) failing to advance off 0
      or failing to track completed write bytes proportionally.

WHAT THIS TEST CANNOT CATCH:
  - As established in DS-T1.14 / DDR-P2.14 re-scoping (2026-09-04), the original
    0.3.1->0.3.2 DE25 silicon wedge was a physical placement/timing-dependent
    reset-deassertion metastability fault on the hardware target, invisible to
    RTL simulation. Simulation cannot observe clock-domain crossing metastability
    or asynchronous reset recovery violations. The red-first clause against
    d24b0cd was therefore waived, and this test serves as a permanent functional
    guard at exact product geometry.

Run via: ``rr sim run test_ddr_uploader_product_base``.
"""
from __future__ import annotations

import struct
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.handle import Immediate
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

from cocotbext.axi import AxiBus, AxiRam

try:
    from engine.simulation import run_simulation  # type: ignore
    from sdk.cocotb_helpers import requires        # type: ignore
except ImportError:
    run_simulation = None

    def requires(*_args, **_kwargs):  # type: ignore[no-redef]
        def _wrap(fn):
            return fn
        return _wrap


_IP_ROOT     = Path(__file__).resolve().parents[3]
_RTL_DIR     = _IP_ROOT / "rtl"
_SUPPORT_DIR = _RTL_DIR / "support"
_TB_DIR      = _IP_ROOT / "sim" / "tb"
_OLO_SYNC_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_sync"  / "src" / "base" / "vhdl"
_OLO_ASYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_async" / "src" / "base" / "vhdl"
_OLO_WCN_DIR   = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_n2xn" / "src" / "base" / "vhdl"
_OLO_WCXN_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_xn2n" / "src" / "base" / "vhdl"

# DE25 Agilex-5 product geometry (deepskopion_de25_top.vhd / deepskopion_core.vhd)
PRODUCT_BASE = 0xA000_0000
PRODUCT_BASE_BITS = f"{PRODUCT_BASE:032b}"
DDR_DATA_WIDTH = 256
DDR_ADDR_WIDTH = 32
USER_DATA_WIDTH = 32
BUFFER_WIDTH = 28                 # 2^28 = 256 MiB ring window
RING_SIZE = 1 << BUFFER_WIDTH
MAX_BURST_LEN = 16                # Zynq-7 / AXI3 cap (AWLEN <= 15)
FIFO_DEPTH = 9

CLK_SYS_PERIOD_NS = 10.0          # 100 MHz fabric clock
CLK_DDR_PERIOD_NS = 5.0           # 200 MHz DDR clock

GENERICS = {
    "G_AXI_ID":             0,
    "G_FIFO_DEPTH":         FIFO_DEPTH,
    "G_USER_DATA_WIDTH":    USER_DATA_WIDTH,
    "G_DDR_DATA_WIDTH":     DDR_DATA_WIDTH,
    "G_DDR_ADDR_WIDTH":     DDR_ADDR_WIDTH,
    "G_DDR_BASE_ADDR":      PRODUCT_BASE_BITS,
    "G_BUFFER_WIDTH":       BUFFER_WIDTH,
    "G_FIFO_IMPL":          "olo",
    "G_ENABLE_WRITE_PLANE": True,
    "G_ENABLE_READ_PLANE":  False,
    "G_MAX_BURST_LEN":      MAX_BURST_LEN,
}


def _custom_libraries() -> dict[str, list[str]]:
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


async def _bootstrap(dut) -> AxiRam:
    """Start clocks, apply reset, attach AxiRam model over 32-bit address space."""
    cocotb.start_soon(Clock(dut.CLK_SYS, CLK_SYS_PERIOD_NS, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_DDR, CLK_DDR_PERIOD_NS, units="ns").start())

    dut.RST_SYS.value          = Immediate(1)
    dut.RST_DDR.value          = Immediate(1)
    dut.S_AXIS_DATA.value      = Immediate(0)
    dut.S_AXIS_VALID.value     = Immediate(0)
    dut.M_AXIS_READY.value     = Immediate(0)
    dut.CSR_WR_BASE_ADDR.value = Immediate(0)
    dut.CSR_WR_BASE_LOAD.value = Immediate(0)
    dut.CSR_RD_BASE_ADDR.value = Immediate(0)
    dut.CSR_RD_BASE_LOAD.value = Immediate(0)
    dut.CSR_WR_PTR_RESET.value = Immediate(0)
    dut.CSR_RD_PTR_RESET.value = Immediate(0)
    dut.CSR_RD_PTR_ADDR.value  = Immediate(0)
    dut.CSR_RD_PTR_LOAD.value  = Immediate(0)
    dut.CSR_WR_PTR_ADDR.value  = Immediate(0)
    dut.CSR_WR_PTR_LOAD.value  = Immediate(0)

    axi = AxiBus.from_prefix(dut, "M_AXI")
    ram = AxiRam(axi, dut.CLK_DDR, dut.RST_DDR, size=1 << DDR_ADDR_WIDTH)

    # Hold reset for 32 DDR cycles, then release and wait out C_RESET_INIT_TIMEOUT
    await ClockCycles(dut.CLK_DDR, 32)
    dut.RST_SYS.value = Immediate(0)
    dut.RST_DDR.value = Immediate(0)
    await ClockCycles(dut.CLK_DDR, 100)
    await RisingEdge(dut.CLK_SYS)
    return ram


async def _send_burst(dut, words: list[int]) -> int:
    """Drive a burst of 32-bit words into S_AXIS, handshaking on S_AXIS_READY."""
    accepted = 0
    idx = 0
    dut.S_AXIS_VALID.value = Immediate(1)
    while idx < len(words):
        dut.S_AXIS_DATA.value = Immediate(words[idx] & 0xFFFF_FFFF)
        await ReadOnly()
        ready = int(dut.S_AXIS_READY.value)
        await RisingEdge(dut.CLK_SYS)
        if ready:
            accepted += 1
            idx += 1
    dut.S_AXIS_VALID.value = Immediate(0)
    return accepted


async def _idle_gap(dut, cycles: int) -> None:
    """Hold S_AXIS_VALID low for a specified number of sys clock cycles."""
    dut.S_AXIS_VALID.value = Immediate(0)
    await ClockCycles(dut.CLK_SYS, cycles)


@cocotb.test()
@requires("DDR-REQ-208")
async def test_product_base_uploader_issues_aw_and_commit_advances(dut):
    """Assert uploader write path operates at DE25 product geometry.

    Drives deepskopion sniffer cadence (short bursts, 1-word and 0-word FIFO
    edges included) and verifies:
      1. S_AXIS_VALID/READY accepts all ingress words.
      2. WRITE_IDLE deasserts (drops to '0') during write operations.
      3. Uploader issues write bursts: M_AXI_AWVALID asserts.
      4. Every M_AXI_AWADDR carries the product base 0xA0000000 (top nibble 0xA)
         and stays bounded in [0xA0000000, 0xB0000000).
      5. Every M_AXI_AWLEN <= 15 (honouring G_MAX_BURST_LEN = 16).
      6. Commit pointer WR_COMMIT_ADDR (RING_COMMIT_PTR) advances off 0 and tracks
         committed bytes.
      7. Written data verified byte-exact in AxiRam memory model.
    """
    ram = await _bootstrap(dut)

    seen_aw: list[tuple[int, int]] = []
    awvalid_ever = [False]
    write_idle_dropped = [False]

    async def _monitor_aw():
        while True:
            await RisingEdge(dut.CLK_DDR)
            try:
                awv = int(dut.M_AXI_AWVALID.value)
                awr = int(dut.M_AXI_AWREADY.value)
                wid = int(dut.WRITE_IDLE.value)
            except ValueError:
                continue
            if wid == 0:
                write_idle_dropped[0] = True
            if awv == 1:
                awvalid_ever[0] = True
                if awr == 1:
                    addr = int(dut.M_AXI_AWADDR.value)
                    length = int(dut.M_AXI_AWLEN.value)
                    seen_aw.append((addr, length))

    mon_aw = cocotb.start_soon(_monitor_aw())

    all_written_words: list[int] = []
    total_accepted = 0
    word_counter = 0x1000

    def next_words(count: int) -> list[int]:
        nonlocal word_counter
        chunk = [(word_counter + i) & 0xFFFF_FFFF for i in range(count)]
        word_counter += count
        all_written_words.extend(chunk)
        return chunk

    # ── Sniffer cadence phase 1: single-word burst (1-word FIFO edge) ──
    # Tests burstw_setter behavior when only 1 word is available, followed
    # by an idle gap where the FIFO drains / sits at 0 (0-word FIFO edge).
    acc = await _send_burst(dut, next_words(1))
    total_accepted += acc
    await _idle_gap(dut, 30)

    # ── Sniffer cadence phase 2: 7 words to complete the first 256-bit DDR beat ──
    # (1 + 7 = 8 user words = 32 bytes = 1 beat of 256-bit DDR bus)
    acc = await _send_burst(dut, next_words(7))
    total_accepted += acc
    await _idle_gap(dut, 30)

    # ── Sniffer cadence phase 3: short sniffer frames (6 words = zs_msg frame) ──
    for _ in range(4):
        acc = await _send_burst(dut, next_words(6))
        total_accepted += acc
        await _idle_gap(dut, 20)

    # ── Sniffer cadence phase 4: mixed short bursts (1, 2, 5, 8, 16 words) ──
    for burst_len in [1, 2, 5, 8, 12, 16]:
        acc = await _send_burst(dut, next_words(burst_len))
        total_accepted += acc
        await _idle_gap(dut, 15)

    # ── Sniffer cadence phase 5: sustained ingress run (256 words = 32 DDR beats) ──
    acc = await _send_burst(dut, next_words(256))
    total_accepted += acc

    # ── Pipeline drain: wait for all bursts to commit and WRITE_IDLE to return 1 ──
    await ClockCycles(dut.CLK_SYS, 50)
    for _ in range(200):
        await Timer(2, unit="us")
        if int(dut.WRITE_IDLE.value) == 1:
            break
    await ClockCycles(dut.CLK_SYS, 50)

    mon_aw.cancel()

    commit = int(dut.WR_COMMIT_ADDR.value)
    dut._log.info(
        f"DDR-P2.14 product geometry: base=0x{PRODUCT_BASE:08X} "
        f"accepted={total_accepted} AW handshakes={len(seen_aw)} "
        f"WR_COMMIT_ADDR=0x{commit:08X} idle_dropped={write_idle_dropped[0]}"
    )

    # (1) All driven words must have been accepted by S_AXIS_READY
    assert total_accepted == len(all_written_words), (
        f"Ingress under-accepted: {total_accepted}/{len(all_written_words)} words accepted."
    )

    # (2) WRITE_IDLE must have deasserted during traffic
    assert write_idle_dropped[0], (
        "WRITE_IDLE stayed high for entire run — uploader never left IDLE (DE25 silicon symptom)."
    )

    # (3) AWVALID must have asserted and AW handshakes must have occurred
    assert awvalid_ever[0], (
        f"Uploader never asserted M_AXI_AWVALID across {total_accepted} accepted words."
    )
    assert len(seen_aw) > 0, (
        "M_AXI_AWVALID asserted but no AW handshake completed on the AXI bus."
    )

    # (4) Every AWADDR must carry base 0xA0000000 and stay in [base, base + 2^28)
    for addr, length in seen_aw:
        assert (addr >> 28) == 0xA, (
            f"AWADDR 0x{addr:08X} does not carry product base 0xA0000000 (top nibble != 0xA) — "
            f"base was truncated or corrupted during elaboration."
        )
        assert PRODUCT_BASE <= addr < (PRODUCT_BASE + RING_SIZE), (
            f"AWADDR 0x{addr:08X} escaped ring window [0x{PRODUCT_BASE:08X}, 0x{PRODUCT_BASE + RING_SIZE:08X})."
        )
        # (5) Every burst length must obey the AXI3 cap G_MAX_BURST_LEN = 16
        assert length <= (MAX_BURST_LEN - 1), (
            f"AWLEN {length} exceeds maximum burst length {MAX_BURST_LEN - 1} beats."
        )

    # (6) Commit pointer WR_COMMIT_ADDR (RING_COMMIT_PTR) must advance off 0
    assert commit > 0, (
        f"WR_COMMIT_ADDR stayed stuck at 0 despite {len(seen_aw)} AW burst(s) — "
        f"the DS-T1.12 commit pointer wedge."
    )

    # Proportional floor: on 256-bit DDR bus, completed writes commit multiples of 32 bytes
    min_expected_commit = (total_accepted // 8) * 32
    assert commit >= min_expected_commit, (
        f"WR_COMMIT_ADDR=0x{commit:08X} did not track completed writes (expected >= 0x{min_expected_commit:08X})."
    )

    # (7) Verify data integrity in memory: all written ingress words must match DDR RAM
    committed_bytes = bytes(ram.read(PRODUCT_BASE, commit))
    num_written = len(all_written_words)
    read_words = list(struct.unpack(f"<{num_written}I", committed_bytes[:num_written * 4]))
    for idx, (r, e) in enumerate(zip(read_words, all_written_words)):
        assert r == e, (
            f"Data mismatch in DDR RAM at word index {idx}: expected 0x{e:08X}, got 0x{r:08X}"
        )

    dut._log.info(
        f"DDR-P2.14 verified: {total_accepted} words accepted, {len(seen_aw)} AW bursts, "
        f"WR_COMMIT_ADDR=0x{commit:08X}, all {num_written} words verified in DDR RAM."
    )


def main() -> None:
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation unavailable — run via `rr sim run`."
        )
    run_simulation(
        top_level="wrapper_ddr_stream_axi",
        module=Path(__file__).stem,
        custom_libraries=_custom_libraries(),
        work_lib="rr",
        generics=GENERICS,
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

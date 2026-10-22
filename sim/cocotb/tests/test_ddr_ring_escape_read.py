"""
DS-T2.23 read-side regression guard — ARADDR must stay inside the ring window.

This pins ddr_addr_width_g (16) STRICTLY GREATER THAN buffer_width_g (12) and
asserts every accepted M_AXI_ARADDR stays in [0, 2^buffer_width_g). The pre-
DS-T2.23 axi4r wrapped OFFSET at 2^ddr_addr_width_g, so ARADDR climbed past the
4 KiB ring toward 64 KiB and this test turns RED.

Run via:  rr sim run test_ddr_ring_escape_read
"""
from __future__ import annotations

from pathlib import Path as _Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer
from cocotbext.axi import AxiBus, AxiRam

from test_ddr_swptr_fill import _ReadLog  # type: ignore

try:
    from engine.simulation import run_simulation  # type: ignore
except ImportError:
    run_simulation = None

try:
    from sdk.cocotb_helpers import requires  # type: ignore
except ImportError:
    def requires(*_a, **_k):
        def _d(f):
            return f
        return _d


_IP_ROOT     = _Path(__file__).resolve().parents[3]
_RTL_DIR     = _IP_ROOT / "rtl"
_SUPPORT_DIR = _RTL_DIR / "support"
_TB_DIR      = _IP_ROOT / "sim" / "tb"
_OLO_SYNC_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_sync"  / "src" / "base" / "vhdl"
_OLO_ASYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_async" / "src" / "base" / "vhdl"
_OLO_WCN_DIR   = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_n2xn" / "src" / "base" / "vhdl"
_OLO_WCXN_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_xn2n" / "src" / "base" / "vhdl"

CLK_SYS_NS = 8
CLK_DDR_NS = 4

# The bug-exposing configuration: address space WIDER than the ring.
ADDR_W   = 16            # 64 KiB address space
BUF_W    = 12            # 4 KiB ring window  (STRICTLY < ADDR_W)
RING_WIN = 1 << BUF_W    # 4096 — ARADDR must never leave [0, RING_WIN)
ADDR_SP  = 1 << ADDR_W   # 65536 — AxiRam size, big enough to absorb an escape so
                         # the test can OBSERVE the escaped address (not crash on it)

_GENERICS = {
    "G_AXI_ID":             0,
    "G_FIFO_DEPTH":         9,
    "G_USER_DATA_WIDTH":    32,
    "G_DDR_DATA_WIDTH":     128,
    "G_DDR_ADDR_WIDTH":     ADDR_W,
    "G_BUFFER_WIDTH":       BUF_W,
    "G_FIFO_IMPL":          "olo",
    "G_ENABLE_WRITE_PLANE": False,
    "G_ENABLE_READ_PLANE":  True,
}

LEAD = RING_WIN // 4


async def _pulse_wr_ptr(dut, offset: int, log: _ReadLog | None = None):
    if log is not None:
        log.published = offset & (RING_WIN - 1)
    dut.CSR_WR_PTR_ADDR.value = offset & (RING_WIN - 1)
    dut.CSR_WR_PTR_LOAD.value = 1
    await RisingEdge(dut.CLK_SYS)
    dut.CSR_WR_PTR_LOAD.value = 0


@cocotb.test()
@requires("DDR-REQ-502")
async def araddr_stays_in_ring_window(dut):
    """ARADDR must stay in [0, 2^buffer_width_g) across many read wraps."""
    cocotb.start_soon(Clock(dut.CLK_SYS, CLK_SYS_NS, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_DDR, CLK_DDR_NS, units="ns").start())
    dut.RST_SYS.value = 1; dut.RST_DDR.value = 1
    dut.S_AXIS_DATA.value = 0; dut.S_AXIS_VALID.value = 0; dut.M_AXIS_READY.value = 0
    dut.CSR_WR_BASE_ADDR.value = 0; dut.CSR_WR_BASE_LOAD.value = 0
    dut.CSR_RD_BASE_ADDR.value = 0; dut.CSR_RD_BASE_LOAD.value = 0
    dut.CSR_RD_PTR_ADDR.value = 0;  dut.CSR_RD_PTR_LOAD.value = 0
    dut.CSR_WR_PTR_ADDR.value = 0;  dut.CSR_WR_PTR_LOAD.value = 0

    ram = AxiRam(AxiBus.from_prefix(dut, "M_AXI"), dut.CLK_DDR, dut.RST_DDR, size=ADDR_SP)  # noqa: F841

    log = _ReadLog(RING_WIN)
    ar = log.ar
    escaped: list[int] = []

    async def watch_ar():
        while True:
            await RisingEdge(dut.CLK_DDR)
            try:
                if int(dut.M_AXI_ARVALID.value) == 1 and int(dut.M_AXI_ARREADY.value) == 1:
                    a = int(dut.M_AXI_ARADDR.value)
                    log.accept(a, int(dut.M_AXI_ARLEN.value))
                    if not (0 <= a < RING_WIN):     # escaped the ring window
                        escaped.append(a)
            except ValueError:
                pass
    cocotb.start_soon(watch_ar())

    await ClockCycles(dut.CLK_DDR, 50)
    dut.RST_SYS.value = 0
    await ClockCycles(dut.CLK_DDR, 80)
    dut.RST_DDR.value = 0
    await ClockCycles(dut.CLK_DDR, 80)

    dut.M_AXIS_READY.value = 1
    dut.CSR_WR_PTR_LOAD.value = 0
    await _pulse_wr_ptr(dut, LEAD, log)

    # Lead the read pointer the last burst leaves behind, not its ARADDR
    # (DDR-T2.23, see _read_end in test_ddr_swptr_fill).
    async def sw_producer():
        while True:
            for _ in range(16):
                await RisingEdge(dut.CLK_SYS)
            if ar:
                await _pulse_wr_ptr(dut, (log.read_end() + LEAD) % RING_WIN, log)
    cocotb.start_soon(sw_producer())

    await Timer(160, units="us")
    dut.M_AXIS_READY.value = 0
    await Timer(20, units="us")

    wraps = sum(1 for i in range(len(ar) - 1) if ar[i + 1] < ar[i])
    dut._log.info(
        "ADDR_W=%d BUF_W=%d: AR=%d range=[0x%x..0x%x] window=[0,0x%x) wraps=%d escapes=%d"
        % (ADDR_W, BUF_W, len(ar), min(ar) if ar else -1, max(ar) if ar else -1,
           RING_WIN, wraps, len(escaped)))

    assert ar, "no AR handshakes — downloader never read"
    assert not log.overreads, (
        "%d read burst(s) read PAST the software write pointer — data the "
        "producer never wrote (DDR-REQ-502 is lossless SPSC). First: %s"
        % (len(log.overreads), log.overreads[:4]))
    # Primary guard (the precise DS-T2.23 signature): the read address must
    # never leave the ring window. On the buggy RTL it climbs toward the address
    # space (2^ADDR_W) and this trips immediately.
    assert not escaped, (
        "M_AXI_ARADDR ESCAPED the ring window [0,0x%x) — wrapped at the address "
        "space (2^%d) instead of the ring (2^%d). max=0x%x, first escapes: %s "
        "(DS-T2.23)." % (RING_WIN, ADDR_W, BUF_W, max(ar),
                         [hex(x) for x in escaped[:8]]))
    # Sanity: the stimulus actually drove the boundary many times.
    assert wraps >= 4, "ring did not wrap enough to exercise the boundary (wraps=%d)" % wraps
    dut._log.info("PASS: ARADDR stayed in the ring window across %d wraps." % wraps)


def _custom_libraries() -> dict:
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


def main() -> None:
    if run_simulation is None:
        raise RuntimeError("invoke via `rr sim run test_ddr_ring_escape_read`.")
    run_simulation(
        top_level="wrapper_ddr_stream_axi",
        module=_Path(__file__).stem,
        custom_libraries=_custom_libraries(),
        work_lib="rr",
        generics=_GENERICS,
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

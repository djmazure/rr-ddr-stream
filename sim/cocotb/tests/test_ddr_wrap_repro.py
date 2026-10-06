"""DDR-P2.3 wrap reproduction — IN SIMULATION, current RTL, no IP change.

Shrink the ring to a few KB via a small G_DDR_ADDR_WIDTH / G_BUFFER_WIDTH so the
DMA write pointer wraps in a handful of bursts, and drive a CONCURRENT DRAIN
(M_AXIS_READY held high) so the write side can keep going past one ring and
actually wrap (without a consumer it just fills and stops — which is what we saw
on HW). Observe every M_AXI AWADDR and assert:

  * every write address stays inside the ring window [base, base+ring_size)
    — a burst must never write past the ring end into the wrap;
  * the write pointer actually WRAPS (address returns to low values after
    reaching the top) — i.e. it behaves as a ring, not a fill-then-wedge buffer.

If the burst-sizer/axi4w wrap is robust this passes; if a burst overshoots the
exact wrap point (axi4w '== size - C_AW_SIZE') and escapes, AWADDR leaves the
window or the pointer wedges at the top — reproducing DDR-P2.3.
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer
from cocotb.handle import Immediate
from cocotbext.axi import AxiBus, AxiRam

import routertl_core as _rrc  # noqa
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
try:
    from engine.simulation import run_simulation
    from sdk.cocotb_helpers import requires
except ImportError:
    run_simulation = None
    def requires(*_a, **_k):
        def _d(f): return f
        return _d

# Reuse the proven source list + library buckets from the main axi test.
from test_ddr_stream_axi import (  # noqa: E402
    _OLO_ASYNC_DIR, _OLO_SYNC_DIR, _OLO_WCN_DIR, _OLO_WCXN_DIR,
    _RTL_DIR, _SUPPORT_DIR, _TB_DIR,
)

# Small ring so the pointer wraps fast (2**12 = 4 KiB DDR window).
SMALL_ADDR_W = 12
GENERICS = {
    "G_AXI_ID": 0, "G_FIFO_DEPTH": 9,
    "G_USER_DATA_WIDTH": 32, "G_DDR_DATA_WIDTH": 128,
    "G_DDR_ADDR_WIDTH": SMALL_ADDR_W, "G_BUFFER_WIDTH": SMALL_ADDR_W,
}

@cocotb.test()
@requires("DDR-REQ-200")
async def write_pointer_wraps_small_ring(dut):
    ring = 1 << SMALL_ADDR_W
    cocotb.start_soon(Clock(dut.CLK_DDR, 3.33, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_SYS, 10.0, units="ns").start())

    dut.RST_DDR.value = 1; dut.RST_SYS.value = 1
    dut.S_AXIS_VALID.value = 0; dut.M_AXIS_READY.value = 0
    ram = AxiRam(AxiBus.from_prefix(dut, "M_AXI"), dut.CLK_DDR, size=ring)

    # Capture every accepted AW (the DMA write address) + flag escapes live.
    aw = []
    escaped = []
    async def watch_aw():
        while True:
            await RisingEdge(dut.CLK_DDR)
            try:
                if dut.M_AXI_AWVALID.value == 1 and dut.M_AXI_AWREADY.value == 1:
                    a = int(dut.M_AXI_AWADDR.value)
                    aw.append(a)
                    if not (0 <= a < ring):
                        escaped.append(a)
            except ValueError:
                pass  # X during reset
    cocotb.start_soon(watch_aw())

    for _ in range(6): await RisingEdge(dut.CLK_DDR)
    dut.RST_DDR.value = 0
    for _ in range(6): await RisingEdge(dut.CLK_SYS)
    dut.RST_SYS.value = 0
    for _ in range(6): await RisingEdge(dut.CLK_SYS)

    # Drain continuously so the write side can wrap (not just fill+stop).
    dut.M_AXIS_READY.value = 1

    # Drive ~4 rings worth of user words -> forces several wraps.
    n_words = (ring // 4) * 4 * 4   # ring bytes/4B-per-word, x4 rings
    for i in range(n_words):
        dut.S_AXIS_DATA.value = Immediate((i + 1) & 0xFFFFFFFF)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)
    await Timer(50, units="us")

    dut._log.info("ring=%dB  AW accepted=%d  addr range=[0x%x..0x%x]"
                  % (ring, len(aw), min(aw) if aw else -1, max(aw) if aw else -1))
    assert aw, "no AW handshakes observed — DMA never wrote"
    assert not escaped, (
        "AWADDR ESCAPED the ring window [0,0x%x): %s — a burst overshot the wrap "
        "(DDR-P2.3)." % (ring, [hex(x) for x in escaped[:8]]))
    wrapped = any(aw[i + 1] < aw[i] for i in range(len(aw) - 1))
    assert wrapped, (
        "write pointer never WRAPPED (addr only grew to 0x%x in a 0x%x ring) — "
        "it filled and wedged instead of behaving as a ring (DDR-P2.3)." % (max(aw), ring))
    dut._log.info("PASS: write pointer wrapped and stayed in-window across ~4 rings.")


def main():
    if run_simulation is None:
        raise RuntimeError("run via `rr sim run test_ddr_wrap_repro`")
    run_simulation(
        top_level="wrapper_ddr_stream_axi",
        module="test_ddr_wrap_repro",
        work_lib="rr",
        custom_libraries={
            "olo": [
                str(_OLO_ASYNC_DIR / f) for f in (
                    "olo_base_pkg_array.vhd","olo_base_pkg_math.vhd","olo_base_pkg_logic.vhd",
                    "olo_base_pkg_string.vhd","olo_base_pkg_attribute.vhd","olo_base_cc_bits.vhd",
                    "olo_base_cc_reset.vhd","olo_base_ram_sdp.vhd")
            ] + [str(_OLO_SYNC_DIR / "olo_base_fifo_sync.vhd"),
                 str(_OLO_ASYNC_DIR / "olo_base_fifo_async.vhd"),
                 str(_OLO_WCN_DIR / "olo_base_wconv_n2xn.vhd"),
                 str(_OLO_WCXN_DIR / "olo_base_wconv_xn2n.vhd")],
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
        },
        generics=GENERICS,
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

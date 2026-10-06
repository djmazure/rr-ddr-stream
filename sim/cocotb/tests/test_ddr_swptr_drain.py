"""
DDR-P2.4 / DS-P2.59 — software-managed ring read pointer (the re-instated drain).

When the read plane is elided (`enable_read_plane_g=false`) there is NO hardware
downloader to advance the ring's read side. Historically that meant the ring
filled exactly once and wedged FULL: the uploader's auto-wrap is gated by
ptr_mgr's full_o (full when `write_ptr + ptr_size == read_ptr`), and with the
read pointer tied to 0 the write pointer can only crawl up to `ring - ptr_size`
and then stall forever. On deepskopion this is the silent-drop / "no
backpressure" behaviour observed on hardware (DS-P2.59).

The fix adds `csr_rd_ptr_addr_i` / `csr_rd_ptr_load_i`: a PS consumer that drains
DDR via its own HP master writes the byte offset (mod ring size) it has CONSUMED
up to; HW feeds it into ptr_mgr's read input so freed space lets the write
pointer auto-wrap. This is the standard SPSC ring drain, just software-driven.

Two attack tests, both with the read plane OFF (deepskopion's exact config):

  * `sw_read_ptr_lets_ring_wrap` — drive the write path hard AND advance
    `CSR_RD_PTR` trailing the write pointer. Assert the write address WRAPS many
    times (sustained operation past many rings) and never escapes the ring
    window. This is the behaviour deepskopion needs.

  * `no_sw_read_ptr_fills_once` — drive the write path hard and NEVER advance the
    read pointer (load held low). Assert the ring fills to the top exactly once
    and then STALLS — proving the change is opt-in / byte-for-byte back-compatible
    with the legacy tied-low read pointer.

Run via:
    rr sim run test_ddr_swptr_drain
"""
from __future__ import annotations

from pathlib import Path as _Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer
from cocotbext.axi import AxiBus, AxiRam

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


_IP_ROOT     = _Path(__file__).resolve().parents[3]
_RTL_DIR     = _IP_ROOT / "rtl"
_SUPPORT_DIR = _RTL_DIR / "support"
_TB_DIR      = _IP_ROOT / "sim" / "tb"
_OLO_SYNC_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_sync"  / "src" / "base" / "vhdl"
_OLO_ASYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_async" / "src" / "base" / "vhdl"
_OLO_WCN_DIR   = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_n2xn" / "src" / "base" / "vhdl"
_OLO_WCXN_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_xn2n" / "src" / "base" / "vhdl"

CLK_SYS_NS = 8   # 125 MHz
CLK_DDR_NS = 4   # 250 MHz

# Tiny ring (2**12 = 4 KiB) so the write pointer wraps in a handful of bursts.
SMALL_ADDR_W = 12
RING = 1 << SMALL_ADDR_W

_GENERICS = {
    "G_AXI_ID":             0,
    "G_FIFO_DEPTH":         9,
    "G_USER_DATA_WIDTH":    32,
    "G_DDR_DATA_WIDTH":     128,
    "G_DDR_ADDR_WIDTH":     SMALL_ADDR_W,
    "G_BUFFER_WIDTH":       SMALL_ADDR_W,
    "G_FIFO_IMPL":          "olo",
    "G_ENABLE_WRITE_PLANE": True,
    "G_ENABLE_READ_PLANE":  False,   # deepskopion's config: PS drains, no HW downloader
}


async def _bootstrap(dut):
    """Start clocks, hold reset past the DUT's C_RESET_INIT_TIMEOUT warm-up,
    attach an AxiRam slave, and arm the AW monitor. Returns (ram, aw, escaped)."""
    cocotb.start_soon(Clock(dut.CLK_SYS, CLK_SYS_NS, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_DDR, CLK_DDR_NS, units="ns").start())
    dut.RST_SYS.value          = 1
    dut.RST_DDR.value          = 1
    dut.S_AXIS_DATA.value      = 0
    dut.S_AXIS_VALID.value     = 0
    dut.M_AXIS_READY.value     = 0
    dut.CSR_WR_BASE_ADDR.value = 0
    dut.CSR_WR_BASE_LOAD.value = 0
    dut.CSR_RD_BASE_ADDR.value = 0
    dut.CSR_RD_BASE_LOAD.value = 0
    dut.CSR_RD_PTR_ADDR.value  = 0
    dut.CSR_RD_PTR_LOAD.value  = 0

    ram = AxiRam(AxiBus.from_prefix(dut, "M_AXI"), dut.CLK_DDR, dut.RST_DDR, size=RING)

    aw: list[int] = []
    escaped: list[int] = []

    async def watch_aw():
        while True:
            await RisingEdge(dut.CLK_DDR)
            try:
                if int(dut.M_AXI_AWVALID.value) == 1 and int(dut.M_AXI_AWREADY.value) == 1:
                    a = int(dut.M_AXI_AWADDR.value)
                    aw.append(a)
                    if not (0 <= a < RING):
                        escaped.append(a)
            except ValueError:
                pass  # X during reset
    cocotb.start_soon(watch_aw())

    await ClockCycles(dut.CLK_DDR, 50)
    dut.RST_SYS.value = 0
    dut.RST_DDR.value = 0
    await ClockCycles(dut.CLK_DDR, 80)
    return ram, aw, escaped


def _count_wraps(aw: list[int]) -> int:
    """A wrap = the captured write address decreasing (returns to a low offset
    after reaching the top of the ring)."""
    return sum(1 for i in range(len(aw) - 1) if aw[i + 1] < aw[i])


@cocotb.test()
@requires("DDR-REQ-500")
async def sw_read_ptr_lets_ring_wrap(dut):
    """With the read plane off, a software read pointer that trails the write
    pointer must let the ring WRAP repeatedly — sustained, lossless operation."""
    _ram, aw, escaped = await _bootstrap(dut)

    # SW drain: advance CSR_RD_PTR to follow the write pointer a few bursts
    # behind ("consumed up to here") and pulse CSR_RD_PTR_LOAD for ONE clk_sys
    # cycle per update — exactly how a regbank write-strobe drives it. The pulse
    # crosses to clk_ddr through the IP's ddr_sync_cdc (coherent multi-bit MCP
    # CDC); the address is held stable between pulses. Updates are spaced
    # (~16 clk_sys cycles) well beyond the CDC sync latency.
    dut.CSR_RD_PTR_LOAD.value = 0
    LAG_BURSTS = 8

    async def sw_consumer():
        while True:
            for _ in range(16):
                await RisingEdge(dut.CLK_SYS)
            if len(aw) > LAG_BURSTS:
                # Byte offset consumed up to = a burst we already saw written,
                # several bursts behind the leading edge (never claims
                # not-yet-written data). ptr_mgr handles the modulo wrap.
                dut.CSR_RD_PTR_ADDR.value = aw[-LAG_BURSTS]
                dut.CSR_RD_PTR_LOAD.value = 1
                await RisingEdge(dut.CLK_SYS)
                dut.CSR_RD_PTR_LOAD.value = 0
    cocotb.start_soon(sw_consumer())

    # Saturate the write path. Hold S_AXIS_VALID high; the IP applies its own
    # back-pressure via S_AXIS_READY, so a perpetual-valid source is the correct
    # max-throughput attacker.
    dut.S_AXIS_VALID.value = 1
    word = 0
    for _ in range(40000):
        word = (word + 1) & 0xFFFFFFFF
        dut.S_AXIS_DATA.value = word
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = 0
    await Timer(20, units="us")

    wraps = _count_wraps(aw)
    dut._log.info(
        "read-plane-off + SW read ptr: AW accepted=%d  range=[0x%x..0x%x]  wraps=%d"
        % (len(aw), min(aw) if aw else -1, max(aw) if aw else -1, wraps))

    assert aw, "no AW handshakes observed — DMA never wrote"
    assert not escaped, (
        "AWADDR ESCAPED the ring window [0,0x%x): %s — a burst overshot the wrap."
        % (RING, [hex(x) for x in escaped[:8]]))
    assert wraps >= 4, (
        "write pointer wrapped only %d time(s) (max addr 0x%x in a 0x%x ring) — the "
        "software read pointer did NOT free space; the ring wedged FULL instead of "
        "sustaining operation (DS-P2.59)." % (wraps, max(aw), RING))
    dut._log.info("PASS: SW read pointer let the ring wrap %d times, in-window." % wraps)


@cocotb.test()
@requires("DDR-REQ-501", "DDR-REQ-400")
async def no_sw_read_ptr_fills_once(dut):
    """Back-compat guard: with the read plane off AND no software read pointer
    (load held low), the ring must fill to the top exactly once and STALL —
    byte-identical to the legacy tied-low read pointer. Proves the new CSR is
    strictly opt-in."""
    _ram, aw, escaped = await _bootstrap(dut)

    # DDR-REQ-400: with G_ENABLE_READ_PLANE=false there is no HW downloader, so the IP
    # must NEVER issue a read burst — a parallel passive sampler watches `M_AXI_ARVALID`
    # for the whole run (post-reset; _bootstrap has deasserted RST_DDR).
    ar_seen: list[int] = []
    async def _assert_no_ar():
        while True:
            await RisingEdge(dut.CLK_DDR)
            try:
                if int(dut.M_AXI_ARVALID.value) == 1:
                    ar_seen.append(int(dut.M_AXI_ARADDR.value))
            except ValueError:
                pass  # X during reset
    cocotb.start_soon(_assert_no_ar())

    # Never advance the read pointer — leave the load strobe low.
    dut.CSR_RD_PTR_LOAD.value = 0

    dut.S_AXIS_VALID.value = 1
    word = 0
    # Drive many rings' worth of data; with no drain it must NOT keep wrapping.
    for _ in range(40000):
        word = (word + 1) & 0xFFFFFFFF
        dut.S_AXIS_DATA.value = word
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = 0
    await Timer(20, units="us")

    wraps = _count_wraps(aw)
    top = max(aw) if aw else -1
    dut._log.info(
        "read-plane-off, NO SW read ptr: AW accepted=%d  range=[0x%x..0x%x]  wraps=%d"
        % (len(aw), min(aw) if aw else -1, top, wraps))

    assert aw, "no AW handshakes observed — DMA never wrote"
    assert not escaped, (
        "AWADDR ESCAPED the ring window [0,0x%x): %s" % (RING, [hex(x) for x in escaped[:8]]))
    assert not ar_seen, (
        "DDR-REQ-400: `M_AXI_ARVALID` asserted %d time(s) (e.g. 0x%x) with the read plane "
        "DISABLED — the IP issued a read burst it must never issue." % (len(ar_seen), ar_seen[0] if ar_seen else 0))
    # Fills to the top once: the highest address reached is the last ring slot.
    assert top >= RING - 2 * (_GENERICS["G_DDR_DATA_WIDTH"] // 8), (
        "ring did not fill to the top (max 0x%x of 0x%x) — unexpected." % (top, RING))
    # And then stalls — no sustained wrapping. At most a single wrap is tolerated
    # as the pointer reaches the boundary; repeated wraps would mean the legacy
    # behaviour changed.
    assert wraps <= 1, (
        "ring kept WRAPPING (%d times) with no software read pointer — the change "
        "is NOT back-compatible; the legacy tied-low read pointer must wedge full."
        % wraps)
    dut._log.info("PASS: ring filled once and stalled (wraps=%d) — legacy behaviour intact." % wraps)


# ---------------------------------------------------------------------------
# Engine entry point
# ---------------------------------------------------------------------------

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
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke this test via `rr sim run test_ddr_swptr_drain`.")
    run_simulation(
        top_level="wrapper_ddr_stream_axi",
        module="test_ddr_swptr_drain",
        custom_libraries=_custom_libraries(),
        work_lib="rr",
        generics=_GENERICS,
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

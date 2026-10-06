"""
DDR-P3.16 — software-managed ring WRITE pointer (the symmetric source).

The exact mirror of DDR-P2.4 / DS-P2.59 (test_ddr_swptr_drain), on the other
plane. When the WRITE plane is elided (`enable_write_plane_g=false`) there is NO
hardware uploader to advance the ring's write side. Historically that meant the
ring reported EMPTY forever: the downloader's read FSM is gated by ptr_mgr's
empty_o (empty when `read_ptr == write_ptr`), and with the write pointer tied to
`uploader_addr` = 0 the read pointer can never move — the HW downloader has
nothing to drain.

The fix adds `csr_wr_ptr_addr_i` / `csr_wr_ptr_load_i`: a PS PRODUCER that fills
DDR via its own HP master writes the byte offset (mod ring size) it has PRODUCED
up to; HW feeds it into the read side's ptr_mgr write input so the downloader
sees available data and drains it, wrapping continuously. Standard SPSC ring
source, software-driven.

Two attack tests, both with the WRITE plane OFF (the read-only integration's
exact config — HW downloader present, no HW uploader):

  * `sw_write_ptr_lets_ring_wrap` — drive the read path hard (M_AXIS consumer
    perpetually ready) AND advance `CSR_WR_PTR` leading the read pointer. Assert
    the read address (ARADDR) WRAPS many times (sustained operation past many
    rings) and never escapes the ring window. This is the read-only behaviour the
    symmetric mechanism enables.

  * `no_sw_write_ptr_stays_empty` — enable the read path but NEVER advance the
    write pointer (load held low). Assert the downloader issues NO reads (the ring
    is empty) — byte-identical to the legacy tied-to-uploader_addr (=0) write
    pointer. Proves the new CSR is strictly opt-in / back-compatible.

Run via:
    rr sim run test_ddr_swptr_fill
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

# Tiny ring (2**12 = 4 KiB) so the read pointer wraps in a handful of bursts.
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
    "G_ENABLE_WRITE_PLANE": False,   # read-only config: PS produces, no HW uploader
    "G_ENABLE_READ_PLANE":  True,
}

BURST = _GENERICS["G_DDR_DATA_WIDTH"] // 8   # 16 bytes / beat
# Keep the write pointer a steady quarter-ring ahead of the read pointer: always
# data available to drain, never lapping (would need 3/4-ring of drift).
LEAD = RING // 4


async def _bootstrap(dut):
    """Start clocks, hold reset past the DUT's warm-up, pre-load an AxiRam read
    slave, and arm the AR monitor. Returns (ram, ar, escaped)."""
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
    dut.CSR_WR_PTR_ADDR.value  = 0
    dut.CSR_WR_PTR_LOAD.value  = 0

    ram = AxiRam(AxiBus.from_prefix(dut, "M_AXI"), dut.CLK_DDR, dut.RST_DDR, size=RING)

    ar: list[int] = []
    escaped: list[int] = []

    async def watch_ar():
        while True:
            await RisingEdge(dut.CLK_DDR)
            try:
                if int(dut.M_AXI_ARVALID.value) == 1 and int(dut.M_AXI_ARREADY.value) == 1:
                    a = int(dut.M_AXI_ARADDR.value)
                    ar.append(a)
                    if not (0 <= a < RING):
                        escaped.append(a)
            except ValueError:
                pass  # X during reset
    cocotb.start_soon(watch_ar())

    await ClockCycles(dut.CLK_DDR, 50)
    dut.RST_SYS.value = 0
    dut.RST_DDR.value = 0
    await ClockCycles(dut.CLK_DDR, 80)
    return ram, ar, escaped


def _count_wraps(ptr: list[int]) -> int:
    """A wrap = the captured address decreasing (returns to a low offset after
    reaching the top of the ring)."""
    return sum(1 for i in range(len(ptr) - 1) if ptr[i + 1] < ptr[i])


async def _pulse_wr_ptr(dut, offset: int):
    """Drive CSR_WR_PTR_ADDR + a ONE-cycle clk_sys load strobe — exactly how a
    regbank write-strobe drives the multi-bit MCP CDC. The address is held stable
    on the port between pulses (a real host register does the same)."""
    dut.CSR_WR_PTR_ADDR.value = offset & (RING - 1)
    dut.CSR_WR_PTR_LOAD.value = 1
    await RisingEdge(dut.CLK_SYS)
    dut.CSR_WR_PTR_LOAD.value = 0


@cocotb.test()
@requires("DDR-REQ-502")
async def sw_write_ptr_lets_ring_wrap(dut):
    """With the write plane off, a software write pointer that leads the read
    pointer must let the ring WRAP repeatedly — sustained, lossless reads."""
    _ram, ar, escaped = await _bootstrap(dut)

    # Perpetual consumer: hold M_AXIS_READY high so the downloader keeps draining
    # its FIFO and re-fetching from DDR (max-throughput reader).
    dut.M_AXIS_READY.value = 1

    # Seed: give the reader an initial quarter-ring of "produced" data so it can
    # start (chicken-and-egg — no AR is issued until write_ptr > read_ptr).
    await _pulse_wr_ptr(dut, LEAD)

    # SW producer: keep the write pointer a steady LEAD ahead of the observed read
    # pointer (ARADDR ≈ downloader read pointer, base=0). ptr_mgr does the modulo
    # wrap, so (read + LEAD) mod RING is always exactly LEAD of available data.
    async def sw_producer():
        while True:
            for _ in range(16):
                await RisingEdge(dut.CLK_SYS)
            if ar:
                await _pulse_wr_ptr(dut, (ar[-1] + LEAD) % RING)
    cocotb.start_soon(sw_producer())

    # Let the read side run for many rings' worth of bursts.
    await Timer(160, units="us")
    dut.M_AXIS_READY.value = 0

    wraps = _count_wraps(ar)
    dut._log.info(
        "write-plane-off + SW write ptr: AR accepted=%d  range=[0x%x..0x%x]  wraps=%d"
        % (len(ar), min(ar) if ar else -1, max(ar) if ar else -1, wraps))

    assert ar, "no AR handshakes observed — the downloader never read (write ptr not seen?)"
    assert not escaped, (
        "ARADDR ESCAPED the ring window [0,0x%x): %s — a burst overshot the wrap "
        "(symmetric axi4r wrap bug?)." % (RING, [hex(x) for x in escaped[:8]]))
    assert wraps >= 4, (
        "read pointer wrapped only %d time(s) (max addr 0x%x in a 0x%x ring) — the "
        "software write pointer did NOT present available data; the ring stayed "
        "EMPTY instead of sustaining reads (DDR-P3.16)." % (wraps, max(ar), RING))
    dut._log.info("PASS: SW write pointer let the ring wrap %d times, in-window." % wraps)


@cocotb.test()
@requires("DDR-REQ-503", "DDR-REQ-401")
async def no_sw_write_ptr_stays_empty(dut):
    """Back-compat guard: with the write plane off AND no software write pointer
    (load held low), the ring must stay EMPTY — the downloader issues NO reads,
    byte-identical to the legacy tied-to-uploader_addr (=0) write pointer. Proves
    the new CSR is strictly opt-in."""
    _ram, ar, escaped = await _bootstrap(dut)

    # DDR-REQ-401: with G_ENABLE_WRITE_PLANE=false there is no HW uploader, so the IP
    # must NEVER issue a write burst — a parallel passive sampler watches `M_AXI_AWVALID`
    # for the whole run (post-reset; _bootstrap has deasserted RST_DDR).
    aw_seen: list[int] = []
    async def _assert_no_aw():
        while True:
            await RisingEdge(dut.CLK_DDR)
            try:
                if int(dut.M_AXI_AWVALID.value) == 1:
                    aw_seen.append(int(dut.M_AXI_AWADDR.value))
            except ValueError:
                pass  # X during reset
    cocotb.start_soon(_assert_no_aw())

    # Consumer ready, but never advance the write pointer — leave load low.
    dut.M_AXIS_READY.value    = 1
    dut.CSR_WR_PTR_LOAD.value = 0

    # Give the DUT ample time to (incorrectly) start reading, if it were going to.
    await Timer(160, units="us")
    dut.M_AXIS_READY.value = 0

    dut._log.info(
        "write-plane-off, NO SW write ptr: AR accepted=%d  escaped=%d"
        % (len(ar), len(escaped)))

    # The write pointer never moves off 0, so the ring is permanently empty and
    # the read FSM must never fire an AR burst.
    assert not ar, (
        "the downloader issued %d read burst(s) with an empty ring (write ptr "
        "never advanced) — the change is NOT back-compatible; a read-only ring "
        "with no producer must stay empty. First few: %s"
        % (len(ar), [hex(x) for x in ar[:8]]))
    assert not aw_seen, (
        "DDR-REQ-401: `M_AXI_AWVALID` asserted %d time(s) (e.g. 0x%x) with the write plane "
        "DISABLED — the IP issued a write burst it must never issue." % (len(aw_seen), aw_seen[0] if aw_seen else 0))
    dut._log.info("PASS: empty ring, downloader idle (0 reads) — legacy behaviour intact.")


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
            "invoke this test via `rr sim run test_ddr_swptr_fill`.")
    run_simulation(
        top_level="wrapper_ddr_stream_axi",
        module="test_ddr_swptr_fill",
        custom_libraries=_custom_libraries(),
        work_lib="rr",
        generics=_GENERICS,
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

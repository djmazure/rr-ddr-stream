"""
KV260-T2.1 sim repro — does the downloader's ARADDR escape the ring window at the
EXACT KV260 config (buffer_width=25, base=0x79000000, write-plane-elided)?

WHY: on silicon (kv260_linux), once KV2-T2.1 was fixed the PL datapath went live
and a kernel panic appeared in the PS GEM-RX DMA path — strong evidence a now-active
PL master is DMA-ing OUTSIDE its intended window into kernel-managed DDR (boot uses
mem=512M: the kernel owns 0–512 MB; fb=0x78000000 / ring=0x79000000 sit above it, so
`mem=` does NOT fence the PL). The rr-ddr-stream downloader reads the ring over HP1;
if its ARADDR ever dips BELOW base (0x79000000) it points into kernel DDR (0–512 MB).
A read can't itself corrupt memory, but an escaping read feeds the decoder garbage
that can cascade into the h264_fb_dma writer escaping — so a bounded downloader is
the cheap thing to rule out FIRST, before any silicon ILA.

The existing test_ddr_swptr_fill proves "ARADDR never escapes" but only at a tiny
4 KiB ring (buffer_width=12) and base 0. This test reruns that property at the
silicon config — a 32 MiB ring at a high non-zero base — where width/overflow/mask
bugs that the small config can't reach would show up.

We can't back a 0x79000000+32 MiB region with AxiRam, so we use a lightweight AR/R
responder (accept every AR, return dummy R beats) and an ARADDR monitor. We drive
CSR_WR_PTR like rtp_bridge: a running byte offset that grows PAST the ring size and
wraps, plus adversarial edge values around the 2**25 wrap boundary.

Hard-coded expected bounds (ROUTERTL-002). The test FAILS if any ARADDR (or any
burst's end address) leaves [base, base+ring) — that would be the KV260-T2.1 root
cause caught in sim.

Run via:  rr sim run test_ddr_swptr_drain_kv260_bounds
"""
from __future__ import annotations

from pathlib import Path as _Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer

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

# ── EXACT KV260 config (top_kv260_h264.vhd:348-357) ─────────────────────────────
CLK_NS       = 8                       # single fabric clock (KV260: clk_sys == clk_ddr)
BUFFER_WIDTH = 25                      # 2**25 = 32 MiB ring
RING         = 1 << BUFFER_WIDTH       # 0x0200_0000
BASE         = 0x7900_0000             # ddr_base_addr_g
TOP          = BASE + RING             # 0x7B00_0000 — first address past the ring
BYTES_BEAT   = 128 // 8                # 16 bytes / 128-bit beat

_GENERICS = {
    "G_AXI_ID":             0,
    "G_FIFO_DEPTH":         9,
    "G_USER_DATA_WIDTH":    8,         # KV260: byte stream into ring_framer
    "G_DDR_DATA_WIDTH":     128,
    "G_DDR_ADDR_WIDTH":     32,
    # NVC parses STD_LOGIC_VECTOR generic overrides as bit strings.
    "G_DDR_BASE_ADDR":      f"{BASE:032b}",
    "G_BUFFER_WIDTH":       BUFFER_WIDTH,
    "G_FIFO_IMPL":          "olo",
    "G_ENABLE_WRITE_PLANE": False,     # PS produces (rtp_bridge), no HW uploader
    "G_ENABLE_READ_PLANE":  True,      # HW downloader drains over HP1
}


async def _bootstrap(dut):
    """Single fabric clock (CLK_SYS == CLK_DDR rate), reset past warm-up, arm the
    AR bounds monitor + a lightweight AR/R responder. Returns (ar, escaped)."""
    cocotb.start_soon(Clock(dut.CLK_SYS, CLK_NS, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_DDR, CLK_NS, units="ns").start())
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
    # Read-data slave: always ready to accept AR; R driven by the responder.
    dut.M_AXI_ARREADY.value = 1
    dut.M_AXI_RVALID.value  = 0
    dut.M_AXI_RLAST.value   = 0
    dut.M_AXI_RDATA.value   = 0
    dut.M_AXI_RRESP.value   = 0
    dut.M_AXI_RID.value     = 0

    ar: list[tuple[int, int]] = []     # (araddr, arlen)
    escaped: list[tuple[int, int]] = []

    async def watch_ar():
        while True:
            await RisingEdge(dut.CLK_DDR)
            try:
                if int(dut.M_AXI_ARVALID.value) == 1 and int(dut.M_AXI_ARREADY.value) == 1:
                    a = int(dut.M_AXI_ARADDR.value)
                    n = int(dut.M_AXI_ARLEN.value)
                    ar.append((a, n))
                    end = a + (n + 1) * BYTES_BEAT          # first byte past the burst
                    if a < BASE or a >= TOP or end > TOP:
                        escaped.append((a, n))
            except ValueError:
                pass  # X during reset
    cocotb.start_soon(watch_ar())

    # Pending-AR queue → R beats. Decoupled accept/drive so back-to-back ARs work.
    pending: list[tuple[int, int]] = []

    async def ar_queue():
        while True:
            await RisingEdge(dut.CLK_DDR)
            try:
                if int(dut.M_AXI_ARVALID.value) == 1 and int(dut.M_AXI_ARREADY.value) == 1:
                    pending.append((int(dut.M_AXI_ARID.value), int(dut.M_AXI_ARLEN.value)))
            except ValueError:
                pass

    async def r_driver():
        while True:
            await RisingEdge(dut.CLK_DDR)
            if not pending:
                continue
            rid, rlen = pending.pop(0)
            for beat in range(rlen + 1):
                dut.M_AXI_RID.value    = rid
                dut.M_AXI_RDATA.value  = 0
                dut.M_AXI_RRESP.value  = 0
                dut.M_AXI_RLAST.value  = 1 if beat == rlen else 0
                dut.M_AXI_RVALID.value = 1
                while True:                       # hold until RREADY handshakes the beat
                    await RisingEdge(dut.CLK_DDR)
                    if int(dut.M_AXI_RREADY.value) == 1:
                        break
                dut.M_AXI_RVALID.value = 0
                dut.M_AXI_RLAST.value  = 0
    cocotb.start_soon(ar_queue())
    cocotb.start_soon(r_driver())

    await ClockCycles(dut.CLK_DDR, 50)
    dut.RST_SYS.value = 0
    dut.RST_DDR.value = 0
    await ClockCycles(dut.CLK_DDR, 80)
    return ar, escaped


async def _pulse_wr_ptr(dut, byte_off: int):
    """Drive CSR_WR_PTR_ADDR + a one-cycle load, the way rtp_bridge's devmem CSR
    write does. We pass the FULL running byte offset (masked to 32 bits like the
    host register) — the IP is responsible for masking to the ring internally."""
    dut.CSR_WR_PTR_ADDR.value = byte_off & 0xFFFF_FFFF
    dut.CSR_WR_PTR_LOAD.value = 1
    await RisingEdge(dut.CLK_SYS)
    dut.CSR_WR_PTR_LOAD.value = 0


def _fail_msg(escaped, ar):
    lo = min(a for a, _ in ar) if ar else -1
    hi = max(a for a, _ in ar) if ar else -1
    return (
        "downloader ARADDR ESCAPED the ring window [0x%08x, 0x%08x) at the KV260 "
        "config (buffer_width=%d, base=0x%08x). First escapes (addr,len): %s. "
        "Observed AR range [0x%08x..0x%08x]. An ARADDR < base points into PS-DDR the "
        "kernel manages (0–512 MB under mem=512M) — the KV260-T2.1 corruption vector."
        % (BASE, TOP, BUFFER_WIDTH, BASE,
           [(hex(a), n) for a, n in escaped[:8]], lo, hi)
    )


@cocotb.test()
@requires("DDR-REQ-502")
async def kv260_drain_araddr_stays_in_ring(dut):
    """Drive the read path hard while a software write pointer leads it by a quarter
    ring, using rtp_bridge-style RUNNING byte offsets that grow past 32 MiB and wrap.
    Every ARADDR (and every burst end) must stay inside [BASE, BASE+RING)."""
    ar, escaped = await _bootstrap(dut)
    dut.M_AXIS_READY.value = 1                    # perpetual consumer → max re-fetch

    LEAD = RING // 4
    produced = LEAD                               # running byte counter (only grows)
    await _pulse_wr_ptr(dut, produced)            # seed so write_ptr > read_ptr

    async def sw_producer():
        nonlocal produced
        while True:
            for _ in range(12):
                await RisingEdge(dut.CLK_SYS)
            produced += LEAD // 2                 # keep leading; grows unbounded, wraps in HW
            await _pulse_wr_ptr(dut, produced)
    cocotb.start_soon(sw_producer())

    await Timer(240, units="us")
    dut.M_AXIS_READY.value = 0

    dut._log.info(
        "KV260 drain: AR accepted=%d  range=[0x%08x..0x%08x]  escapes=%d"
        % (len(ar), min(a for a, _ in ar) if ar else -1,
           max(a for a, _ in ar) if ar else -1, len(escaped)))
    assert ar, "no AR handshakes — downloader never read (wr ptr not seen?)"
    assert not escaped, _fail_msg(escaped, ar)
    dut._log.info("PASS: %d bursts, all ARADDR within the 32 MiB ring at base 0x%08x."
                  % (len(ar), BASE))


@cocotb.test()
@requires("DDR-REQ-502")
async def kv260_wrap_boundary_byte_offsets(dut):
    """Adversarial wr-pointer values straddling the 2**25 wrap boundary and beyond
    (the regime a long-running rtp_bridge byte counter reaches). ARADDR must remain
    bounded — a mask/overflow bug at the high base would surface here."""
    ar, escaped = await _bootstrap(dut)
    dut.M_AXIS_READY.value = 1

    # Walk the write pointer across the wrap: just under ring, exactly ring, just
    # over, and a few multiples — exactly the values an unmasked running counter hits.
    for off in (RING - BYTES_BEAT, RING, RING + 0x4000,
                2 * RING + 0x10, 5 * RING + 0x1234, 0xFFFF_FFF0):
        await _pulse_wr_ptr(dut, off)
        await ClockCycles(dut.CLK_DDR, 400)

    dut.M_AXIS_READY.value = 0
    dut._log.info(
        "KV260 wrap-edge: AR accepted=%d  range=[0x%08x..0x%08x]  escapes=%d"
        % (len(ar), min(a for a, _ in ar) if ar else -1,
           max(a for a, _ in ar) if ar else -1, len(escaped)))
    assert not escaped, _fail_msg(escaped, ar)
    dut._log.info("PASS: wrap-boundary byte offsets kept ARADDR in-window.")


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
            "invoke this test via `rr sim run test_ddr_swptr_drain_kv260_bounds`.")
    run_simulation(
        top_level="wrapper_ddr_stream_axi",
        module="test_ddr_swptr_drain_kv260_bounds",
        custom_libraries=_custom_libraries(),
        work_lib="rr",
        generics=_GENERICS,
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

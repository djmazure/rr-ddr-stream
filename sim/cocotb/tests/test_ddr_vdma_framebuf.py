"""
DDR-P2.12 — frame-aware VDMA frame-buffer contract on the ddr-stream ring.

A VDMA frame buffer is a *disciplined use* of the linear byte ring: the ring
window is carved into N equal FRAME_BYTES slots, and the consumer advances its
read pointer only on whole-frame boundaries. The IP itself is frame-AGNOSTIC —
no SOF port, no frame-length notion — so these tests pin down what the ring
guarantees a frame-buffer consumer (rotation, tear-free handoff) AND prove the
input-domain constraints (exact cadence, whole-buffer geometry) are load-bearing.

Geometry: ring = 4 KiB (`G_BUFFER_WIDTH`=12), N=4 buffers, FRAME_BYTES=1024,
write plane on / read plane off (PS reader drains via `CSR_RD_PTR_ADDR`) — the
linux_zybo stereo-capture topology (2x VmodCAM).

Run:  rr sim run test_ddr_vdma_framebuf --wait
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

CLK_SYS_NS = 8   # 125 MHz
CLK_DDR_NS = 4   # 250 MHz

# Frame-buffer geometry: ring = N_BUFFERS * FRAME_BYTES, all powers of two so a
# frame divides the ring cleanly (DDR-REQ-603 input-domain constraint).
SMALL_ADDR_W = 12                     # ring = 4 KiB
RING         = 1 << SMALL_ADDR_W
N_BUFFERS    = 4
FRAME_BYTES  = RING // N_BUFFERS       # 1024 bytes / buffer
WORD_B       = 4                       # G_USER_DATA_WIDTH = 32 bits
FRAME_WORDS  = FRAME_BYTES // WORD_B    # 256 words / frame
RING_WORDS   = RING // WORD_B           # 1024 words = one ring
MARKER_HI    = 0xF0F00000              # first word of each frame carries this tag

_GENERICS = {
    "G_AXI_ID":             0,
    "G_FIFO_DEPTH":         9,
    "G_USER_DATA_WIDTH":    32,
    "G_DDR_DATA_WIDTH":     128,
    "G_DDR_ADDR_WIDTH":     SMALL_ADDR_W,
    "G_BUFFER_WIDTH":       SMALL_ADDR_W,
    "G_FIFO_IMPL":          "olo",
    "G_ENABLE_WRITE_PLANE": True,
    "G_ENABLE_READ_PLANE":  False,   # PS/HW reader drains via CSR_RD_PTR
    # linux_zybo consumer target = Zynq-7 S_AXI_HP (AXI3, 16-beat cap). This
    # also bounds a single burst to 16*16 = 256 B < FRAME_BYTES(1024), the
    # precondition for frame-granular back-pressure (DDR-REQ-601): with the
    # AXI4 default of 256 the uploader can emit one 4096-B burst that swallows
    # the whole ring, making per-frame back-pressure impossible.
    "G_MAX_BURST_LEN":      16,
}


async def _bootstrap(dut):
    """Start clocks, hold reset past the warm-up, attach an AxiRam slave, and
    arm an AW monitor capturing (addr, len, size). Returns (ram, aw)."""
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

    aw: list[tuple[int, int, int]] = []   # (addr, len, size)

    async def watch_aw():
        while True:
            await RisingEdge(dut.CLK_DDR)
            try:
                if int(dut.M_AXI_AWVALID.value) == 1 and int(dut.M_AXI_AWREADY.value) == 1:
                    aw.append((int(dut.M_AXI_AWADDR.value),
                               int(dut.M_AXI_AWLEN.value),
                               int(dut.M_AXI_AWSIZE.value)))
            except ValueError:
                pass  # X during reset
    cocotb.start_soon(watch_aw())

    await ClockCycles(dut.CLK_DDR, 50)
    dut.RST_SYS.value = 0
    dut.RST_DDR.value = 0
    await ClockCycles(dut.CLK_DDR, 80)
    return ram, aw


async def _drive_words(dut, words, bubble_every=0):
    """Push a list of 32-bit words on S_AXIS. If bubble_every>0, deassert
    `S_AXIS_VALID` for one cycle every that-many words (source back-pressure)."""
    n = 0
    for w in words:
        if bubble_every and (n % bubble_every == 0) and n > 0:
            dut.S_AXIS_VALID.value = 0
            dut.S_AXIS_DATA.value  = 0
            await RisingEdge(dut.CLK_SYS)
        dut.S_AXIS_VALID.value = 1
        dut.S_AXIS_DATA.value  = w
        await RisingEdge(dut.CLK_SYS)
        n += 1
    dut.S_AXIS_VALID.value = 0


def _span(ln: int, sz: int) -> int:
    return (ln + 1) * (1 << sz)


def _scan_markers(ram) -> dict:
    """Return {frame_idx: byte_offset} for every MARKER_HI|idx word found in the
    ring image. Little-endian 32-bit."""
    data = bytes(ram.read(0, RING))
    found = {}
    for off in range(0, RING, WORD_B):
        w = int.from_bytes(data[off:off + WORD_B], "little")
        if (w & 0xFFFF0000) == (MARKER_HI & 0xFFFF0000):
            found[w & 0xFFFF] = off
    return found


# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("DDR-REQ-600")
async def rotation_visits_all_n_buffers(dut):
    """DDR-REQ-600: the write head `M_AXI_AWADDR` sweeps all N frame-buffer
    slots in strict ascending round-robin, starting SOF-aligned at slot 0, and
    keeps rotating across many ring passes — even with `S_AXIS_VALID` bubbles at
    frame starts."""
    _ram, aw = await _bootstrap(dut)

    # Software reader trails the write head so the ring keeps wrapping.
    async def sw_consumer():
        while True:
            for _ in range(16):
                await RisingEdge(dut.CLK_SYS)
            if len(aw) > 8:
                dut.CSR_RD_PTR_ADDR.value = aw[-8][0]
                dut.CSR_RD_PTR_LOAD.value = 1
                await RisingEdge(dut.CLK_SYS)
                dut.CSR_RD_PTR_LOAD.value = 0
    cocotb.start_soon(sw_consumer())

    # Saturate the write path with periodic source bubbles at frame cadence.
    word = 0
    for _ in range(24):                                    # 24 frames of traffic
        frame_words = [MARKER_HI] + [((word + i) & 0x0FFFFFFF) for i in range(1, FRAME_WORDS)]
        word = (word + FRAME_WORDS) & 0x0FFFFFFF
        await _drive_words(dut, frame_words, bubble_every=FRAME_WORDS)  # bubble at each SOF
    await Timer(20, units="us")

    assert aw, "no AW handshakes — DMA never wrote"
    first = aw[0][0]
    assert first % FRAME_BYTES == 0, (
        "first `M_AXI_AWADDR` 0x%x is not SOF-aligned to a frame base (FRAME_BYTES=%d)"
        % (first, FRAME_BYTES))

    # Compress the AWADDR stream into the sequence of slot indices actually
    # touched, then check every transition is +1 (mod N) — no slot skipped.
    slots = [a // FRAME_BYTES for (a, _l, _s) in aw]
    seq = [slots[0]] + [s for i, s in enumerate(slots[1:], 1) if s != slots[i - 1]]
    bad = [(seq[i - 1], seq[i]) for i in range(1, len(seq))
           if seq[i] != (seq[i - 1] + 1) % N_BUFFERS]
    wraps = sum(1 for i in range(1, len(seq)) if seq[i] == 0 and seq[i - 1] == N_BUFFERS - 1)
    dut._log.info("REQ-600 rotation: slot-visit seq len=%d, distinct=%s, wraps=%d, bad-transitions=%s"
                  % (len(seq), sorted(set(slots)), wraps, bad[:8]))

    assert set(slots) == set(range(N_BUFFERS)), (
        "rotation did not visit all %d frame slots — visited %s" % (N_BUFFERS, sorted(set(slots))))
    assert not bad, (
        "N-buffer rotation SKIPPED a slot — non-consecutive transitions %s (a frame "
        "buffer was jumped over; VDMA rotation is broken)." % bad[:8])
    assert wraps >= 3, (
        "write head wrapped the N-buffer pool only %d time(s) — rotation did not sustain "
        "across many passes (read side is not freeing frame slots)." % wraps)
    dut._log.info("PASS REQ-600: round-robin over %d slots, %d wraps, SOF-aligned start." % (N_BUFFERS, wraps))


# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("DDR-REQ-601")
async def framealigned_drain_sustains_rotation(dut):
    """DDR-REQ-601: a reader that advances `CSR_RD_PTR_ADDR` ONLY in whole
    -FRAME_BYTES increments (never mid-frame) sustains lossless rotation — the
    write head `M_AXI_AWADDR` keeps wrapping the N-buffer pool and never wedges
    and never escapes. Frame-granular back-pressure is as lossless as byte
    -granular: proving whole-frame release is a valid tear-free drain."""
    _ram, aw = await _bootstrap(dut)

    # SW reader consumes up to a FRAME-ALIGNED offset trailing the write head by
    # a couple of frames, pulsing CSR_RD_PTR_LOAD once per update — the reader
    # only ever releases whole frames (rd is floored to a FRAME_BYTES boundary).
    loaded_rd: list[int] = []

    async def frame_consumer():
        while True:
            for _ in range(16):
                await RisingEdge(dut.CLK_SYS)
            if len(aw) > 8:
                trail = aw[-8][0]
                rd = (trail // FRAME_BYTES) * FRAME_BYTES   # whole-frame release
                dut.CSR_RD_PTR_ADDR.value = rd
                dut.CSR_RD_PTR_LOAD.value = 1
                loaded_rd.append(rd)
                await RisingEdge(dut.CLK_SYS)
                dut.CSR_RD_PTR_LOAD.value = 0
    cocotb.start_soon(frame_consumer())

    dut.S_AXIS_VALID.value = 1
    word = 0
    escaped = []
    for _ in range(40000):
        word = (word + 1) & 0x0FFFFFFF
        dut.S_AXIS_DATA.value = word
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = 0
    await Timer(20, units="us")

    addrs = [a for (a, _l, _s) in aw]
    escaped = [a for a in addrs if not (0 <= a < RING)]
    wraps = sum(1 for i in range(1, len(addrs)) if addrs[i] < addrs[i - 1])
    unaligned_rd = [r for r in loaded_rd if r % FRAME_BYTES != 0]

    dut._log.info("REQ-601 frame-drain: AW=%d, wraps=%d, frame-aligned rd loads=%d, unaligned=%d"
                  % (len(aw), wraps, len(loaded_rd), len(unaligned_rd)))
    assert aw, "no AW handshakes — DMA never wrote"
    assert not unaligned_rd, (
        "reader released a NON-frame-aligned `CSR_RD_PTR_ADDR` (%s) — the test failed to keep "
        "the drain whole-frame; it does not prove frame-granular drain." % unaligned_rd[:6])
    assert not escaped, (
        "`M_AXI_AWADDR` ESCAPED the N-buffer pool [0,0x%x): %s" % (RING, [hex(x) for x in escaped[:6]]))
    assert wraps >= 4, (
        "write head wrapped only %d time(s) under a FRAME-ALIGNED drain — whole-frame release "
        "did NOT free space; the ring wedged instead of sustaining rotation. Frame-granular "
        "back-pressure is broken." % wraps)
    dut._log.info("PASS REQ-601: frame-aligned drain sustained %d wraps, in-pool, no wedge." % wraps)


# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("DDR-REQ-602")
async def runt_frame_desyncs_rotation(dut):
    """DDR-REQ-602: the IP is frame-agnostic — it commits `S_AXIS_DATA` words in
    byte order with no frame-length notion. A RUNT frame is outside the input
    domain: it is NOT re-aligned, so every subsequent frame marker lands at a
    non-FRAME_BYTES-aligned DDR offset. Proves exact frame cadence is required."""
    ram, aw = await _bootstrap(dut)

    # No read pointer advance: the ring fills exactly once and stalls, leaving a
    # stable image to inspect (legacy tied-low behaviour). Write < one ring.
    RUNT_DEFICIT = 8   # words; the runt frame (idx 1) is short by 8 words = 32 B

    dut.S_AXIS_VALID.value = 1
    frames = []
    frames.append([MARKER_HI | 0] + [(0x1000 + i) & 0x0FFFFFFF for i in range(1, FRAME_WORDS)])
    frames.append([MARKER_HI | 1] + [(0x2000 + i) & 0x0FFFFFFF for i in range(1, FRAME_WORDS - RUNT_DEFICIT)])  # RUNT
    frames.append([MARKER_HI | 2] + [(0x3000 + i) & 0x0FFFFFFF for i in range(1, FRAME_WORDS)])
    for fr in frames:
        await _drive_words(dut, fr)
    dut.S_AXIS_VALID.value = 0
    await Timer(20, units="us")

    markers = _scan_markers(ram)
    dut._log.info("REQ-602 runt: markers found (frame->offset) = %s"
                  % {k: hex(v) for k, v in sorted(markers.items())})

    assert 0 in markers and 2 in markers, (
        "expected frame-0 and frame-2 markers in the ring image; found %s" % markers)
    # Frame 0 is SOF-aligned at the pool base.
    assert markers[0] % FRAME_BYTES == 0, (
        "frame-0 marker at 0x%x is not frame-aligned" % markers[0])
    # Frame 2 follows a runt frame -> its marker is shifted off the frame grid.
    assert markers[2] % FRAME_BYTES != 0, (
        "frame-2 marker at 0x%x IS frame-aligned after a RUNT frame — the IP appears to "
        "have re-aligned a short frame, which it does not do; runt-cadence guard is vacuous."
        % markers[2])
    assert markers[2] == (2 * FRAME_BYTES - RUNT_DEFICIT * WORD_B), (
        "frame-2 marker at 0x%x is not shifted by exactly the runt deficit (%d B) — "
        "unexpected layout." % (markers[2], RUNT_DEFICIT * WORD_B))
    dut._log.info("PASS REQ-602: runt frame shifted frame-2 base by %d B (0x%x) — exact cadence is load-bearing."
                  % (RUNT_DEFICIT * WORD_B, markers[2]))


# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("DDR-REQ-603")
async def geometry_divides_and_bursts_may_straddle(dut):
    """DDR-REQ-603: with a dividing geometry (FRAME_BYTES | ring), the write head
    `M_AXI_AWADDR` crosses each FRAME_BYTES boundary at an aligned address; AND
    the linear ring gives NO per-burst atomicity — at least one write burst
    straddles a FRAME_BYTES slot boundary. Consumers must design around both."""
    _ram, aw = await _bootstrap(dut)

    async def sw_consumer():
        while True:
            for _ in range(16):
                await RisingEdge(dut.CLK_SYS)
            if len(aw) > 8:
                dut.CSR_RD_PTR_ADDR.value = aw[-8][0]
                dut.CSR_RD_PTR_LOAD.value = 1
                await RisingEdge(dut.CLK_SYS)
                dut.CSR_RD_PTR_LOAD.value = 0
    cocotb.start_soon(sw_consumer())

    dut.S_AXIS_VALID.value = 1
    word = 0
    for _ in range(8000):
        word = (word + 1) & 0x0FFFFFFF
        dut.S_AXIS_DATA.value = word
        await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = 0
    await Timer(20, units="us")

    assert RING % FRAME_BYTES == 0, "test geometry itself must divide the ring"

    # Every burst START address is a whole multiple of the DDR beat size, and
    # the set of frame-boundary addresses seen are all FRAME_BYTES-aligned.
    beat_b = _GENERICS["G_DDR_DATA_WIDTH"] // 8
    misaligned_starts = [a for (a, _l, _s) in aw if a % beat_b != 0]

    straddlers = []
    for (a, ln, sz) in aw:
        sp = _span(ln, sz)
        if a // FRAME_BYTES != (a + sp - 1) // FRAME_BYTES:
            straddlers.append((a, sp))

    dut._log.info("REQ-603 geometry: bursts=%d, misaligned-starts=%d, straddlers=%d (e.g. %s)"
                  % (len(aw), len(misaligned_starts), len(straddlers),
                     [(hex(a), s) for a, s in straddlers[:4]]))

    assert not misaligned_starts, (
        "`M_AXI_AWADDR` not beat-aligned: %s" % [hex(a) for a in misaligned_starts[:6]])
    assert straddlers, (
        "expected the linear ring to emit at least one burst straddling a FRAME_BYTES "
        "boundary (no per-burst atomicity); saw none — the no-atomicity clause is unproven.")
    dut._log.info("PASS REQ-603: dividing geometry keeps starts aligned; %d burst(s) straddle a slot boundary (no per-burst atomicity)." % len(straddlers))


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
            "invoke via `rr sim run test_ddr_vdma_framebuf`.")
    run_simulation(
        top_level="wrapper_ddr_stream_axi",
        module="test_ddr_vdma_framebuf",
        custom_libraries=_custom_libraries(),
        work_lib="rr",
        generics=_GENERICS,
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

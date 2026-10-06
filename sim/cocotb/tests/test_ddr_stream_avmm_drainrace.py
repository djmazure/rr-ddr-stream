"""NIK-P3.27 drain-race repro: ddr_stream_avmm 32->512 write path corrupts data
on real Arria-10 EMIF for bursts >128 B (>2 beats), non-deterministically (HW:
52 mismatches one run, 2 the next; got=stale values, beat-aligned). Hypothesis:
avmm_w drains 512-bit beats far faster than the 32-bit producer fills, and the
mixed-width FIFO's read-side empty/count crossing lets a partially-filled (stale)
beat be read.

This test attacks that path the way HW does: harsh drain>>fill clock ratio
(266/50 MHz like the board, plus an even harsher 500/50 to amplify), a
max-throughput sink (NO waitrequest stalls — the BFM-attacker stance; stalls
would pace avmm_w and hide the race), a >2-beat burst, and a clk-phase sweep
(sim is phase-fixed, so a functional phase-race only shows if we sweep it).

It verifies the SLAVE-memory contents directly (mirrors the HW `dma verify`) and
counts committed AVMM beats, so a failure is unambiguous: beats-missing => write
dropped; beats-present + data-wrong => stale-beat read (the drain race).

Read ROUTERTL-002: assertions are hard-coded (expected word i == i+1), not
re-derived from the DUT. If this PASSES across all ratios/phases, the functional
logic is sound and the HW corruption is a CDC *metastability* path (sim-invisible)
on the read-side count/empty crossing — which is then an STA/CDC-hardening fix,
not an RTL-logic fix.
"""
from __future__ import annotations

import cocotb
from pathlib import Path as _Path
from cocotb.triggers import Timer, RisingEdge
from cocotb.clock import Clock
from cocotb.handle import Immediate

from sdk.cocotb_helpers.avmm_ram import AvmmBus, AvmmRam  # type: ignore

try:
    from engine.simulation import run_simulation  # type: ignore
    from sdk.cocotb_helpers import requires        # type: ignore
except ImportError:
    run_simulation = None
    def requires(*_a, **_k):
        def _w(fn): return fn
        return _w

_IP_ROOT     = _Path(__file__).resolve().parents[3]
_RTL_DIR     = _IP_ROOT / "rtl"
_AVMM_DIR    = _RTL_DIR / "avmm"
_SUPPORT_DIR = _RTL_DIR / "support"
_TB_DIR      = _IP_ROOT / "sim" / "tb"
_OLO_SYNC_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_sync"  / "src" / "base" / "vhdl"
_OLO_ASYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_async" / "src" / "base" / "vhdl"
_OLO_WCN_DIR   = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_n2xn"  / "src" / "base" / "vhdl"
_OLO_WCXN_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_xn2n"  / "src" / "base" / "vhdl"

# HW config: 32-in / 512-out (Intel-EMIF native, ratio 16), olo FIFO.
GENERICS = {
    "G_FIFO_DEPTH":      9,
    "G_USER_DATA_WIDTH": 32,
    "G_DDR_DATA_WIDTH":  512,
    "G_DDR_ADDR_WIDTH":  32,
    "G_BUFFER_WIDTH":    20,
    "G_FIFO_IMPL":       "olo",
}

WORDS_PER_BEAT = GENERICS["G_DDR_DATA_WIDTH"] // GENERICS["G_USER_DATA_WIDTH"]   # 16
BEAT_BYTES     = GENERICS["G_DDR_DATA_WIDTH"] // 8                                # 64
N_WORDS        = 256          # 16 beats = 1 KiB — solidly past the 2-beat envelope


async def _bring_up(dut, *, ddr_ns: float, sys_ns: float, ddr_phase_ns: float):
    """Launch clocks with an explicit DDR phase offset (so we can sweep the
    fill/drain phase relationship sim otherwise pins at 0), walk both resets,
    and attach a MAX-THROUGHPUT AvmmRam (pause_generator=None => waitrequest
    never asserted by the sink; the natural attacker stance)."""
    cocotb.start_soon(Clock(dut.CLK_SYS, sys_ns, units="ns").start())
    if ddr_phase_ns > 0:
        await Timer(ddr_phase_ns, units="ns")
    cocotb.start_soon(Clock(dut.CLK_DDR, ddr_ns, units="ns").start())

    dut.RST_DDR.value = 1
    dut.RST_SYS.value = 1
    dut.S_AXIS_VALID.value = 0
    dut.M_AXIS_READY.value = 0
    dut.S_AXIS_DATA.value  = 0

    avmm = AvmmBus.from_prefix(dut, "M_AVMM")
    ram  = AvmmRam(avmm, dut.CLK_DDR, reset=dut.RST_DDR, reset_active_level=True,
                   size=1 << int(dut.G_DDR_ADDR_WIDTH.value),
                   read_latency=4, pause_generator=None)   # <- no stalls

    await RisingEdge(dut.CLK_DDR)
    for _ in range(5): await RisingEdge(dut.CLK_DDR)
    dut.RST_DDR.value = 0
    for _ in range(5): await RisingEdge(dut.CLK_DDR)
    await RisingEdge(dut.CLK_SYS)
    for _ in range(5): await RisingEdge(dut.CLK_SYS)
    dut.RST_SYS.value = 0
    for _ in range(5): await RisingEdge(dut.CLK_SYS)
    await Timer(3, units="us")
    await RisingEdge(dut.CLK_SYS)
    return ram


async def _run_one(dut, *, ddr_ns, sys_ns, phase_ns):
    """One fill+verify at a given clock ratio/phase. Returns (n_beats_committed,
    mismatches:list[(idx,exp,got)])."""
    ram = await _bring_up(dut, ddr_ns=ddr_ns, sys_ns=sys_ns, ddr_phase_ns=phase_ns)

    beats = {"w": 0}
    async def watch():
        while True:
            await RisingEdge(dut.CLK_DDR)
            if int(dut.M_AVMM_write.value) and not int(dut.M_AVMM_waitrequest.value):
                beats["w"] += 1
    watcher = cocotb.start_soon(watch())

    # Poison the target region so a *dropped* beat reads as a recognizable
    # sentinel (not accidentally-correct stale data).
    for b in range(N_WORDS // WORDS_PER_BEAT):
        ram.write(b * BEAT_BYTES, b"\xA5" * BEAT_BYTES)

    # Fill: 1 word/CLK_SYS, value = idx+1 (hard-coded expectation, ROUTERTL-002).
    # Drain >> fill keeps the FIFO near-empty so S_AXIS_READY stays high
    # (matches mem_model_demo); honour it anyway to be safe.
    for i in range(N_WORDS):
        dut.S_AXIS_DATA.value  = Immediate(i + 1)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
        # if not ready, hold the same beat until accepted
        while int(dut.S_AXIS_READY.value) == 0:
            await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)

    await Timer(60, units="us")   # let every beat drain
    watcher.kill()

    raw = ram.read(0, N_WORDS * 4)
    mism = []
    for i in range(N_WORDS):
        got = int.from_bytes(raw[i*4:(i+1)*4], "little")
        if got != i + 1:
            mism.append((i, i + 1, got))
    return beats["w"], mism


@cocotb.test()
@requires("DDR-REQ-001")
async def drain_race_nik327(dut):
    expected_beats = N_WORDS // WORDS_PER_BEAT
    # (ddr_ns, sys_ns, label): 266/50 = HW; 500/50 = amplified; phase sweep each.
    ratios = [(3.76, 20.0, "266/50_HW"), (2.0, 20.0, "500/50_amplified")]
    phases = [0.0, 1.0, 1.88, 2.5]      # sub-DDR-cycle offsets to expose phase races

    any_fail = False
    for ddr_ns, sys_ns, rlabel in ratios:
        for ph in phases:
            n_beats, mism = await _run_one(dut, ddr_ns=ddr_ns, sys_ns=sys_ns, phase_ns=ph)
            tag = f"[{rlabel} phase={ph}ns]"
            dut._log.info(f"{tag} committed_beats={n_beats}/{expected_beats} "
                          f"mismatches={len(mism)}/{N_WORDS}")
            if mism:
                any_fail = True
                for (idx, exp, got) in mism[:12]:
                    beat = idx // WORDS_PER_BEAT
                    lane = idx %  WORDS_PER_BEAT
                    sentinel = (got == 0xA5A5A5A5)
                    dut._log.error(
                        f"{tag} idx={idx} (beat {beat} lane {lane}) exp=0x{exp:08x} "
                        f"got=0x{got:08x} xor=0x{exp ^ got:08x}"
                        f"{'  <DROPPED(sentinel)>' if sentinel else ''}")
            # Beat-accounting: every expected 512-bit beat must commit on the bus.
            assert n_beats >= expected_beats, (
                f"{tag} avmm_w committed only {n_beats}/{expected_beats} beats — "
                f"writes DROPPED at the master (not a data-stale race).")

    assert not any_fail, (
        "NIK-P3.27 REPRODUCED in sim — see idx/exp/got above. Beats committed but "
        "data wrong => stale-beat read (mixed-FIFO read-side race). This is now an "
        "RTL-logic bug, fix it here.")
    dut._log.info(
        "NIK-P3.27 did NOT reproduce across HW + amplified ratios and the phase "
        "sweep: the 32->512 write path is functionally sound in zero-delay sim. "
        "=> the HW corruption is a CDC metastability/timing path on the mixed-FIFO "
        "read-side count/empty crossing (sim cannot model metastability). Fix is "
        "STA/CDC hardening on that crossing, per the FPGA-debug rule.")


def main() -> None:
    if run_simulation is None:
        raise RuntimeError("run via `rr sim run test_ddr_stream_avmm_drainrace`")
    run_simulation(
        top_level="wrapper_ddr_stream_avmm",
        module="test_ddr_stream_avmm_drainrace",
        custom_libraries={
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
                str(_AVMM_DIR / "avmm_w.vhd"),
                str(_AVMM_DIR / "avmm_r.vhd"),
                str(_AVMM_DIR / "uploader_avmm.vhd"),
                str(_AVMM_DIR / "downloader_avmm.vhd"),
                str(_RTL_DIR / "ddr_stream_avmm.vhd"),
                str(_TB_DIR / "wrappers" / "wrapper_ddr_stream_avmm.vhd"),
            ],
        },
        work_lib="rr",
        generics=GENERICS,
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

# SPDX-License-Identifier: MIT
"""DDR-T2.24: a producer write on the idle-flush trigger cycle desyncs ddr_fifo_intf's pad count.

ddr_fifo_intf completes a partial DDR word with zero messages after C_IDLE_FLUSH_THRES (128) idle cycles
(GEN_UPLOADER_BLOCK, pad_flush_proc). On the trigger cycle, the one where idle_cnt_r = 128 with partial_cnt_r = p /= 0,
pad_active_r is still 0, so fifo_in_full_o = full or pad_active_r admits a producer write. That write moves
partial_cnt_r to p+1 while the trigger still sets pad_active_r, and the pad-event sideband is written with the old p
(pad count ratio - p). Measured at 9abb606, ratio 4 (128-bit) and 8 (256-bit), every p:

  * p < ratio-1: ratio-(p+1) pads go in, the read-side strip removes ratio-p, so one real message is lost on M_AXIS;
    the ring bytes and the commit pointer are correct.
  * p = ratio-1: the partial wraps to 0 and pad mode injects a whole word of zeros: WR_COMMIT_ADDR advances one
    extra DDR word, and ratio-1 zero messages reach M_AXIS.

Tests (both elaborated at G_DDR_DATA_WIDTH 128 and 256):

  * residue_and_neighbour_cycles (must pass): N = 2*ratio + r for every r, then idle: commit = ceil(N/ratio) words,
    ring = the messages then zeros only, M_AXIS = exactly the messages. Then one extra message one cycle before the
    trigger (it lands while idle_cnt_r = 127: one flush for N+1) and one cycle after it (pad mode holds it off: two
    flushes), each with the expected commit, ring and M_AXIS.
  * trigger_cycle_write: the extra message lands ON the trigger cycle, for every p. Required: commit =
    ceil((N+1)/ratio) words, ring = the N+1 messages then zeros only, M_AXIS = exactly the N+1 messages. If the
    monitor does not see the write land on the trigger cycle the test raises RuntimeError ("not exercised").
    Red on 9abb606 (it carried expect_fail in 916410af); green with the fix, which counts a write that lands on the
    trigger cycle in the trigger, the pad count and the pad-event sideband.
"""
from __future__ import annotations

from pathlib import Path as _Path

import cocotb
from cocotb.clock import Clock
from cocotb.handle import Immediate
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer
from cocotbext.axi import AxiBus, AxiRam

try:
    from engine.simulation import run_simulation  # type: ignore
    from sdk.cocotb_helpers import requires       # type: ignore
except ImportError:
    run_simulation = None

    def requires(*_args, **_kwargs):  # type: ignore[no-redef]
        def _wrap(fn):
            return fn
        return _wrap

import test_demo_axi_partial_tail_conformance as _pt   # its library list and clock periods

THRES = 128                      # ddr_fifo_intf.vhd C_IDLE_FLUSH_THRES
RAM_SIZE = 1 << 20
FIRST = 0x1000                   # message values: FIRST + i, never zero
EXTRA = 0xBEEF


def _ratio(dut) -> int:
    return len(dut.M_AXI_WDATA) // 32


def _pad(dut):
    return dut.u_ddr_stream_axi.i_ddr_fifo_intf.GEN_UPLOADER_BLOCK


async def _start(dut):
    cocotb.start_soon(Clock(dut.CLK_DDR, _pt.DDR_PERIOD, unit="ns").start())
    cocotb.start_soon(Clock(dut.CLK_SYS, _pt.SYS_PERIOD, unit="ns").start())
    return AxiRam(AxiBus.from_prefix(dut, "M_AXI"), dut.CLK_DDR, dut.RST_DDR, size=RAM_SIZE)


async def _reset(dut, ram):
    dut.RST_DDR.value = 1
    dut.RST_SYS.value = 1
    dut.S_AXIS_VALID.value = 0
    dut.S_AXIS_DATA.value = 0
    dut.M_AXIS_READY.value = 1
    await ClockCycles(dut.CLK_DDR, 6)
    ram.write(0, bytes(RAM_SIZE // 16))
    dut.RST_DDR.value = 0
    await ClockCycles(dut.CLK_DDR, 5)
    await ClockCycles(dut.CLK_SYS, 6)
    dut.RST_SYS.value = 0
    await ClockCycles(dut.CLK_SYS, 5)
    await Timer(3, unit="us")
    await RisingEdge(dut.CLK_SYS)


async def _write(dut, words):
    for w in words:
        dut.S_AXIS_DATA.value = Immediate(w)
        dut.S_AXIS_VALID.value = Immediate(1)
        await RisingEdge(dut.CLK_SYS)
        while not int(dut.S_AXIS_READY.value):
            await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = Immediate(0)


async def _monitor(dut, rec, stop):
    """Per landed producer write (fifo_in_wren_i = 1, full = 0): the cycle's idle_cnt_r, partial_cnt_r and
    pad_active_r. A trigger-cycle write is idle_cnt_r = THRES, partial_cnt_r /= 0, pad_active_r = 0."""
    blk = _pad(dut)
    intf = dut.u_ddr_stream_axi.i_ddr_fifo_intf
    while not stop["v"]:
        await RisingEdge(dut.CLK_SYS)
        if int(intf.fifo_in_wren_i.value) == 1 and int(blk.full.value) == 0:
            rec.append((int(blk.idle_cnt_r.value), int(blk.partial_cnt_r.value), int(blk.pad_active_r.value)))


async def _collect(dut, out, stop):
    while not stop["v"]:
        await FallingEdge(dut.CLK_SYS)
        if int(dut.M_AXIS_VALID.value) and int(dut.M_AXIS_READY.value):
            out.append(int(dut.M_AXIS_DATA.value))


async def _run(dut, ram, n, inject_at=None):
    """n messages, then (optionally) one EXTRA message once idle_cnt_r reads inject_at at a falling edge."""
    await _reset(dut, ram)
    stop, got, landed = {"v": False}, [], []
    cocotb.start_soon(_collect(dut, got, stop))
    cocotb.start_soon(_monitor(dut, landed, stop))
    sent = [FIRST + i for i in range(n)]
    await _write(dut, sent)
    if inject_at is not None:
        blk = _pad(dut)
        for _ in range(4 * THRES):
            await FallingEdge(dut.CLK_SYS)
            if int(blk.idle_cnt_r.value) == inject_at:
                break
        else:
            raise RuntimeError(f"idle_cnt_r never read {inject_at}")
        await _write(dut, [EXTRA])
        sent.append(EXTRA)
    await ClockCycles(dut.CLK_SYS, 4000)
    stop["v"] = True
    await ClockCycles(dut.CLK_SYS, 2)
    commit = int(dut.WR_COMMIT_ADDR.value)
    raw = ram.read(0, commit)
    ring = [int.from_bytes(raw[i:i + 4], "little") for i in range(0, len(raw), 4)]
    trigger_hit = any(i == THRES and p != 0 and a == 0 for i, p, a in landed)
    return sent, ring, commit, got, trigger_hit


def _expect_one_flush(ratio, sent):
    words = -(-len(sent) // ratio) * ratio
    return words * 4, sent + [0] * (words - len(sent))


def _expect_two_flushes(ratio, first, extra):
    w1 = -(-len(first) // ratio) * ratio
    ring = first + [0] * (w1 - len(first)) + [extra] + [0] * (ratio - 1)
    return len(ring) * 4, ring


@cocotb.test()
@requires("DDR-REQ-FLUSH-1", "DDR-REQ-FLUSH-2", "DDR-REQ-FLUSH-3", "DDR-REQ-FLUSH-4")
async def residue_and_neighbour_cycles(dut):
    ram = await _start(dut)
    ratio = _ratio(dut)
    for r in range(ratio):
        n = 2 * ratio + r
        sent, ring, commit, got, _ = await _run(dut, ram, n)
        want_commit, want_ring = _expect_one_flush(ratio, sent)
        assert (commit, ring, got) == (want_commit, want_ring, sent), (
            f"ratio {ratio} residue {r}: commit {commit}/{want_commit}, ring {[hex(w) for w in ring[-ratio:]]}, "
            f"M_AXIS {len(got)}/{len(sent)}")
    for r in range(1, ratio):
        n = 2 * ratio + r
        # one cycle before the trigger: lands while idle_cnt_r = THRES-1, one flush for n+1
        sent, ring, commit, got, hit = await _run(dut, ram, n, inject_at=THRES - 2)
        want_commit, want_ring = _expect_one_flush(ratio, sent)
        assert not hit, f"ratio {ratio} p={r}: the before-trigger injection landed on the trigger cycle"
        assert (commit, ring, got) == (want_commit, want_ring, sent), (
            f"ratio {ratio} p={r} before trigger: commit {commit}/{want_commit}, M_AXIS {len(got)}/{len(sent)}")
        # one cycle after the trigger: pad mode holds it off, the word is flushed first, then the extra one
        sent, ring, commit, got, hit = await _run(dut, ram, n, inject_at=THRES)
        want_commit, want_ring = _expect_two_flushes(ratio, sent[:-1], EXTRA)
        assert not hit, f"ratio {ratio} p={r}: the after-trigger injection landed on the trigger cycle"
        assert (commit, ring, got) == (want_commit, want_ring, sent), (
            f"ratio {ratio} p={r} after trigger: commit {commit}/{want_commit}, M_AXIS {len(got)}/{len(sent)}")


@cocotb.test()
@requires("DDR-REQ-FLUSH-2", "DDR-REQ-FLUSH-3", "DDR-REQ-FLUSH-4")
async def trigger_cycle_write(dut):
    ram = await _start(dut)
    ratio = _ratio(dut)
    failures = []
    for r in range(1, ratio):
        n = 2 * ratio + r
        sent, ring, commit, got, hit = await _run(dut, ram, n, inject_at=THRES - 1)
        if not hit:
            raise RuntimeError(f"not exercised: ratio {ratio} p={r}: no producer write landed on the trigger cycle")
        want_commit, want_ring = _expect_one_flush(ratio, sent)
        if (commit, ring, got) != (want_commit, want_ring, sent):
            failures.append(f"p={r}: commit {commit}/{want_commit}, ring_ok {ring == want_ring}, "
                            f"M_AXIS {len(got)}/{len(sent)} tail {[hex(w) for w in got[-2:]]}")
    assert not failures, f"DDR-T2.24 ratio {ratio}: " + "; ".join(failures)


def main() -> None:
    if run_simulation is None:
        raise RuntimeError("invoke via `rr sim run test_ddr_t2_24_flush_trigger_race`")
    for ddr_w in (128, 256):
        run_simulation(
            top_level="wrapper_ddr_stream_axi",
            module="test_ddr_t2_24_flush_trigger_race",
            custom_libraries=_pt._custom_libraries(),
            work_lib="rr",
            generics={"G_AXI_ID": 0, "G_FIFO_DEPTH": 9, "G_USER_DATA_WIDTH": 32, "G_DDR_DATA_WIDTH": ddr_w,
                      "G_DDR_ADDR_WIDTH": 32, "G_BUFFER_WIDTH": 20, "G_FIFO_IMPL": "olo"},
            waves=False,
            simulator="nvc",
        )


if __name__ == "__main__":
    main()

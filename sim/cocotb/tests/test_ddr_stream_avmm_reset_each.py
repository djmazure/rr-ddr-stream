"""Reset-each-fill reproduction for ddr_stream_avmm.

WHY THIS EXISTS
---------------
On HW the AVMM DMA path wedges on the *second* back-to-back "fill" when a
soft-reset is issued between fills: the write master stops draining to DDR
and the upload FIFO pins full (NIK-P3.29 open item). The existing
``reset_attack_test`` in test_ddr_stream_avmm.py asserts RST_SYS exactly
once and only checks the IP goes *quiet* — it never drives a *follow-on
fill* after the reset, so the cross-domain reset-recovery path that wedges
on HW is never exercised in sim. This testbench closes that gap.

HW soft-reset semantics (mirrored here)
---------------------------------------
The memory note for this saga states the HW soft-reset "clears both the
FIFO and the ring pointer". In the RTL those live in different reset
domains:
  * ring pointer (ptr_mgr) + both AVMM masters + reset-init guard
    (rstTimeout_r / cs_r) + downloader FIFO     ← rst_ddr_i
  * uploader FIFO (BOTH clk_sys & clk_ddr sides) + input skid + pad FSMs
                                                  ← rst_sys_i
So a faithful soft-reset pulses BOTH rst_sys_i and rst_ddr_i. They recover
on their own clocks (staggered), and the TOP additionally holds the
uploader/downloader chip-selects low for C_RESET_INIT_TIMEOUT=64 clk_ddr
cycles after rst_ddr_i deasserts. That staggered recovery across the async
upload FIFO — whose clk_ddr read side is reset by the clk_sys-domain
rst_sys_i — is the prime wedge suspect.

WHAT A WEDGE LOOKS LIKE HERE
----------------------------
Per fill we wait (watchdog-bounded) for the expected number of committed
AVMM write beats. If they never arrive (master stuck, FIFO full) the
watchdog fires and we fail at that fill with bus-state diagnostics — the
sim analogue of the HW "LFSR_DONE never asserts, fifo_cnt pinned full".
"""
from __future__ import annotations

import cocotb
import logging
import random
from pathlib import Path as _Path

from cocotb.triggers import Timer, RisingEdge
from cocotb.clock import Clock
from cocotb.utils import get_sim_time

log_file_path = _Path(__file__).parent / "ddr_stream_avmm_reset_each.log"
logging.basicConfig(
    filename=log_file_path,
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

from sdk.cocotb_helpers.avmm_ram import AvmmBus, AvmmRam  # type: ignore

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
    from sdk.cocotb_helpers import requires       # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None
    def requires(*_args, **_kwargs):  # type: ignore[no-redef]
        def _wrap(fn):
            return fn
        return _wrap

_IP_ROOT     = _Path(__file__).resolve().parents[3]
_RTL_DIR     = _IP_ROOT / "rtl"
_AVMM_DIR    = _RTL_DIR / "avmm"
_SUPPORT_DIR = _RTL_DIR / "support"
_TB_DIR      = _IP_ROOT / "sim" / "tb"
_OLO_SYNC_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_sync" / "src" / "base" / "vhdl"
_OLO_ASYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_async" / "src" / "base" / "vhdl"
_OLO_WCN_DIR   = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_n2xn" / "src" / "base" / "vhdl"
_OLO_WCXN_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_xn2n" / "src" / "base" / "vhdl"

GENERICS = {
    "G_FIFO_DEPTH":      9,
    "G_USER_DATA_WIDTH": 32,
    "G_DDR_DATA_WIDTH":  128,
    "G_DDR_ADDR_WIDTH":  32,
    "G_BUFFER_WIDTH":    20,
}

# Scenario knobs.
N_WORDS      = 32     # 32 × 32-bit = 128 B = the reliable ≤128 B HW chunk.
K_FILLS      = 6      # HW wedges on fill #2; a few more confirms it's not a fluke.
FILL_TIMEOUT_US = 40  # generous: 8 beats need ~tens of clk_ddr cycles even
                      # with stalls; only a true wedge takes >40 us.


def main() -> None:
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke this test via `rr sim run test_ddr_stream_avmm_reset_each`."
        )

    # Start narrow + fast (the historical 32/128 olo shape) — the reset
    # logic under test is width-independent. Add (32, 512) to mirror the
    # Intel-EMIF-native shape once a result is in hand.
    _configs = (
        (32, 128, "olo"),
        (32, 512, "olo"),   # Intel-EMIF-native shape — what the board runs.
    )
    for _user_w, _ddr_w, _impl in _configs:
        print(
            f"\n{'='*72}\n"
            f"=== reset_each_fill — fifo_impl_g={_impl!r} "
            f"user={_user_w}b ddr={_ddr_w}b\n"
            f"{'='*72}\n"
        )
        run_simulation(
            top_level="wrapper_ddr_stream_avmm",
            module="test_ddr_stream_avmm_reset_each",
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
            generics={
                **GENERICS,
                "G_USER_DATA_WIDTH": _user_w,
                "G_DDR_DATA_WIDTH":  _ddr_w,
                "G_FIFO_IMPL":       _impl,
            },
            waves=True,
            simulator="nvc",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Harness.
# ──────────────────────────────────────────────────────────────────────────────

DDR_PERIOD = 3.33  # ns, 300 MHz
SYS_PERIOD = 10.0  # ns, 100 MHz


def _waitrequest_jitter(p: float, seed: int):
    rng = random.Random(seed)
    def _tick() -> bool:
        return rng.random() < p
    return _tick


class BusWatch:
    """Background AVMM-bus accountant. Counts committed write/read beats
    (req & !waitrequest) and the most recent address, so a wedge can be
    diagnosed from the deltas instead of a bare hang."""
    def __init__(self) -> None:
        self.write_beats = 0
        self.read_beats  = 0
        self.last_write_addr = -1

    async def run(self, dut):
        while True:
            await RisingEdge(dut.CLK_DDR)
            wr = int(dut.M_AVMM_waitrequest.value)
            if wr:
                continue
            if int(dut.M_AVMM_write.value):
                self.write_beats += 1
                self.last_write_addr = int(dut.M_AVMM_address.value)
            if int(dut.M_AVMM_read.value):
                self.read_beats += 1


async def _bring_up(dut, *, ddr_size: int, stall_p: float = 0.03):
    cocotb.start_soon(Clock(dut.CLK_DDR, DDR_PERIOD, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_SYS, SYS_PERIOD, units="ns").start())

    dut.RST_DDR.value = 1
    dut.RST_SYS.value = 1
    dut.S_AXIS_VALID.value = 0
    dut.S_AXIS_DATA.value  = 0
    # Drain the read side continuously so the downloader's prefetch FIFO
    # never back-pressures — isolates the WRITE path (the wedge surface).
    dut.M_AXIS_READY.value = 1

    avmm = AvmmBus.from_prefix(dut, "M_AVMM")
    ram  = AvmmRam(
        avmm,
        dut.CLK_DDR,
        reset=dut.RST_DDR,
        reset_active_level=True,
        size=ddr_size,
        read_latency=4,
        pause_generator=_waitrequest_jitter(p=stall_p, seed=42) if stall_p > 0 else None,
    )

    for _ in range(6):
        await RisingEdge(dut.CLK_DDR)
    dut.RST_DDR.value = 0
    for _ in range(6):
        await RisingEdge(dut.CLK_SYS)
    dut.RST_SYS.value = 0
    await Timer(3, units="us")
    await RisingEdge(dut.CLK_SYS)
    return ram


async def soft_reset(dut, *, recovery_us: float = 3.0, ddr_lead_cyc: int = 0):
    """Mirror the HW DS_REG_CTRL soft-reset: clear the FIFO (rst_sys) AND
    the ring pointer (rst_ddr). Assert together; deassert and let each
    domain recover on its own clock.

    ``ddr_lead_cyc`` deasserts rst_ddr that many clk_ddr cycles BEFORE
    rst_sys (a stagger, modelling independent per-domain reset
    synchronisers — real HW never deasserts the two perfectly together).
    ``recovery_us`` is the settle wait before returning; 0 means the
    caller races the next fill against the TOP's 64-cycle rstTimeout_r
    re-arm + the async-FIFO rd/wr_rst_busy (the HW 'immediate go' case)."""
    dut.S_AXIS_VALID.value = 0
    dut.RST_SYS.value = 1
    dut.RST_DDR.value = 1
    for _ in range(8):
        await RisingEdge(dut.CLK_DDR)
    if ddr_lead_cyc > 0:
        dut.RST_DDR.value = 0
        for _ in range(ddr_lead_cyc):
            await RisingEdge(dut.CLK_DDR)
        dut.RST_SYS.value = 0
    else:
        dut.RST_DDR.value = 0
        dut.RST_SYS.value = 0
    if recovery_us > 0:
        await Timer(recovery_us, units="us")
    await RisingEdge(dut.CLK_SYS)


async def feed(dut, values):
    """Push a list of user words on S_AXIS, holding each stable until the
    handshake (VALID & READY) is observed. N is small vs the FIFO so READY
    is essentially always high on a healthy IP — a stall here is itself a
    wedge symptom."""
    for v in values:
        dut.S_AXIS_DATA.value  = v & 0xFFFFFFFF
        dut.S_AXIS_VALID.value = 1
        await RisingEdge(dut.CLK_SYS)
        while int(dut.S_AXIS_READY.value) != 1:
            await RisingEdge(dut.CLK_SYS)
    dut.S_AXIS_VALID.value = 0


async def wait_for_beats(dut, watch: BusWatch, target: int, timeout_us: float) -> bool:
    """Wait until write_beats reaches *target*. Returns False on timeout
    (the wedge)."""
    t0 = get_sim_time(units="ns")
    while watch.write_beats < target:
        await RisingEdge(dut.CLK_DDR)
        if get_sim_time(units="ns") - t0 > timeout_us * 1000.0:
            return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# The test.
# ──────────────────────────────────────────────────────────────────────────────

async def _run_reset_each(dut, *, label: str, soft_reset_kwargs: dict):
    ddr_addr_w   = int(dut.G_DDR_ADDR_WIDTH.value)
    user_w       = int(dut.G_USER_DATA_WIDTH.value)
    ddr_w        = int(dut.G_DDR_DATA_WIDTH.value)
    msgs_per_beat = ddr_w // user_w
    beats_per_fill = (N_WORDS + msgs_per_beat - 1) // msgs_per_beat

    ram = await _bring_up(dut, ddr_size=1 << ddr_addr_w)

    watch = BusWatch()
    cocotb.start_soon(watch.run(dut))

    dut._log.info(
        f"[{label}] N={N_WORDS} words/fill ({N_WORDS*user_w//8} B), "
        f"{beats_per_fill} DDR beats/fill, K={K_FILLS} fills, "
        f"msgs/beat={msgs_per_beat}. soft_reset={soft_reset_kwargs}"
    )

    for k in range(K_FILLS):
        base_val = (k + 1) << 16
        values = [base_val + (i + 1) for i in range(N_WORDS)]
        beats_before = watch.write_beats

        await feed(dut, values)

        ok = await wait_for_beats(
            dut, watch, beats_before + beats_per_fill, FILL_TIMEOUT_US
        )
        got_beats = watch.write_beats - beats_before

        if not ok:
            assert False, (
                f"WEDGE at fill #{k+1}/{K_FILLS}: only {got_beats}/"
                f"{beats_per_fill} AVMM write beats committed within "
                f"{FILL_TIMEOUT_US} us after feeding {N_WORDS} words.\n"
                f"  S_AXIS_READY={int(dut.S_AXIS_READY.value)} "
                f"WRITE_IDLE={int(dut.WRITE_IDLE.value)} "
                f"M_AVMM_write={int(dut.M_AVMM_write.value)} "
                f"M_AVMM_waitrequest={int(dut.M_AVMM_waitrequest.value)}\n"
                f"  last_write_addr=0x{watch.last_write_addr:x} "
                f"total write_beats={watch.write_beats} read_beats={watch.read_beats}\n"
                f"  This is the HW reset-each wedge reproduced in sim — the "
                f"write master stopped draining after the soft-reset."
            )

        # Let the writes settle in the model, then verify this fill's data
        # landed at the ring base (soft-reset reseeds the ring to base each
        # fill, so every fill overwrites [base, base+chunk)).
        await Timer(1, units="us")
        dump = ram.read(0, beats_per_fill * (ddr_w // 8))
        mismatches = []
        for m in range(N_WORDS):
            beat = m // msgs_per_beat
            lane = m % msgs_per_beat
            byte_addr = beat * (ddr_w // 8) + lane * (user_w // 8)
            got = int.from_bytes(dump[byte_addr:byte_addr + user_w // 8], "little")
            if got != values[m]:
                mismatches.append((m, hex(values[m]), hex(got)))

        assert not mismatches, (
            f"fill #{k+1}/{K_FILLS}: {len(mismatches)}/{N_WORDS} words wrong "
            f"in DDR at base after a clean-looking write "
            f"({got_beats} beats committed).\n"
            f"  first 5 (msg_idx, expected, got): {mismatches[:5]}"
        )

        dut._log.info(
            f"  [{label}] fill #{k+1}/{K_FILLS} OK: {got_beats} write beats, "
            f"{N_WORDS} words verified at base."
        )

        await soft_reset(dut, **soft_reset_kwargs)

    dut._log.info(
        f"[{label}] PASS: {K_FILLS} fills each drained + verified "
        f"across soft-resets — no wedge in sim under the AvmmRam BFM."
    )


@cocotb.test()
@requires("DDR-REQ-002")
async def reset_each_fill_test(dut):
    """Relaxed: full 3 us recovery wait between soft-reset and the next
    fill (the IP is fully settled before GO)."""
    await _run_reset_each(
        dut, label="relaxed",
        soft_reset_kwargs=dict(recovery_us=3.0, ddr_lead_cyc=0),
    )


@cocotb.test()
@requires("DDR-REQ-002")
async def reset_each_fill_tight_test(dut):
    """Race the recovery: deassert rst_ddr 4 clk_ddr cycles before rst_sys
    (cross-domain stagger) and feed the next fill IMMEDIATELY — no settle
    wait. This is the HW 'streamtest' shape (back-to-back go after reset,
    no inter-op idle) that the memory fingers as the wedge trigger, vs the
    'diag' shape (CSR reads between fills) that runs clean."""
    await _run_reset_each(
        dut, label="tight",
        soft_reset_kwargs=dict(recovery_us=0.0, ddr_lead_cyc=4),
    )


if __name__ == "__main__":
    main()

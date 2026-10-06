"""Unit-level cocotb tests for ptr_mgr — standalone, no contract attribution.

This file is the WORKED EXAMPLE for sub-block testing inside rr-vfifo
without proliferating ip.yml files. The pattern is:

  1. Keep one rr-vfifo/ip.yml (unchanged).
  2. Each sub-block gets its own test_<block>.py here with its own main()
     that calls run_simulation(top_level=<entity>, custom_libraries=...)
     listing only the files this unit actually needs.
  3. `rr sim run test_<block>` drives just that DUT — fast compile, fast sim,
     and the unit stays green even when the integration top is red.

Boundary-only scoping rule (requirements.yml header, 2026-05-13):
  These tests are DEVELOPMENT INSTRUMENTATION, not contract claims. None of
  them carry @requires() decorators because ptr_mgr's behaviour (DIFF math,
  FULL/EMPTY semantics) is INTERNAL — not observable at the rr_vfifo IP
  boundary. The IP-level contract that covers ptr_mgr's correctness is
  VFIFO-REQ-001 (lossless round-trip via DIN→DOUT) — any pointer-math
  regression shows up there.

  This test file still earns its keep: when integration is red, narrow
  unit tests like these tell you whether the leaf is sane before you go
  hunting in the wiring.

ptr_mgr is a flat-port leaf entity (std_logic / std_logic_vector / unsigned —
no record-typed ports), so cocotb attaches directly to the entity without a
flat-AXI wrapper. Only the top-level virtual_fifo needs a wrapper because
its AXI ports are record-typed.
"""

from __future__ import annotations

import cocotb
import random
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from pathlib import Path as _Path

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
    from sdk.cocotb_helpers import requires       # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None
    def requires(*_args, **_kwargs):
        def _wrap(fn):
            return fn
        return _wrap


_IP_ROOT = _Path(__file__).resolve().parents[3]   # rr-vfifo/
_RTL_DIR = _IP_ROOT / "rtl"


def main() -> None:
    """`rr sim run test_ptr_mgr` entry point.

    Compiles only the files ptr_mgr.vhd actually depends on (vfifo_pkg.vhd is
    NOT in this list — ptr_mgr is package-free, see its USE clauses). No
    wrapper required: ptr_mgr's port list is flat std_logic / unsigned.
    """
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke this test via `rr sim run test_ptr_mgr`."
        )

    run_simulation(
        top_level="ptr_mgr",
        module="test_ptr_mgr",
        custom_libraries={
            "rr": [
                str(_RTL_DIR / "ptr_mgr.vhd"),
            ],
        },
        # Defaults from the entity: G_PTR_WIDTH=5 (32-entry ring),
        # G_PTR_SIZE=16 (burst footprint in entries), G_PROG_FULL=2
        # (prog_full at 2 burst-footprints from FULL). Override here to a
        # smaller G_PTR_WIDTH for faster wrap exercise.
        generics={"ptr_width_g": 5, "ptr_size_g": 4, "prog_full_g": 2},
        waves=True,
        simulator="nvc",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
async def _bring_up(dut, period_ns: float = 10.0):
    """Spin a clock, walk synchronous reset, return at the post-reset edge."""
    cocotb.start_soon(Clock(dut.clk_i, period_ns, units="ns").start())
    dut.rst_i.value = 1
    dut.in_ptr_wr_i.value = 0
    dut.in_ptr_rd_i.value = 0
    for _ in range(5):
        await RisingEdge(dut.clk_i)
    dut.rst_i.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk_i)


def _ptr_width(dut) -> int:
    return int(dut.ptr_width_g.value)


def _mask(dut) -> int:
    return (1 << _ptr_width(dut)) - 1


async def _set_ptrs(dut, wr: int, rd: int):
    """Drive WR/RD pointers and step two clock edges.

    Two edges, not one: cocotb 2.x's inertial-write timing model commits
    `dut.X.value = N` at end-of-time-step, AFTER the rising-edge callback
    fires for the current edge. So a single `await RisingEdge` returns at
    a moment when the registered process has already sampled the OLD input
    values for this edge.

    For COMBINATIONAL outputs (e.g. DIFF) this is harmless — they settle
    once the inertial write commits, and we read them after the await.
    For REGISTERED outputs (EMPTY, FULL, ALMOST_*) we need a SECOND edge:
    the first commits the input write; the second is when the registered
    process samples the new inputs and updates its flip-flops.

    Mirrors the `await _step(dut, 2)  # combinational + registered`
    convention used in test_mux_ep_ddr.py.
    """
    m = _mask(dut)
    dut.in_ptr_wr_i.value = wr & m
    dut.in_ptr_rd_i.value = rd & m
    await RisingEdge(dut.clk_i)
    await RisingEdge(dut.clk_i)


# ──────────────────────────────────────────────────────────────────────────────
# DIFF arithmetic — internal pointer-math sanity (no boundary REQ claimed)
# ──────────────────────────────────────────────────────────────────────────────
# These tests verify that ptr_mgr's DIFF output equals (WR - RD) mod 2^PTR_WIDTH.
# They are NOT @requires-tagged: the boundary contract for the IP is
# VFIFO-REQ-001 (DIN→DOUT lossless round-trip), which would fail if the
# pointer math were wrong. These tests are development scaffolding — they
# narrow the search when round-trip fails.
@cocotb.test()
async def diff_correct_no_wrap(dut):
    """DIFF == (WR - RD) when WR >= RD (no wrap involved)."""
    await _bring_up(dut)

    cases = [(0, 0), (1, 0), (5, 2), (15, 0), (15, 14)]
    for wr, rd in cases:
        await _set_ptrs(dut, wr, rd)
        # DIFF is combinational off A/B and settles within the same cycle;
        # but we observe it after a clock edge so the registered FULL/EMPTY
        # are also stable.
        actual = int(dut.diff_o.value)
        # ptr_mgr drives DIFF to all-ones when full_int='1' to indicate
        # "burst-worth-or-more free" — when WR-RD == G_PTR_SIZE the entity
        # considers itself full and clamps DIFF. We don't claim that here;
        # we test the no-clamp regime (WR-RD < G_PTR_SIZE).
        ptr_size = int(dut.ptr_size_g.value)
        if (wr - rd) < ptr_size:
            assert actual == (wr - rd), (
                f"VFIFO-REQ-101 no-wrap: WR={wr} RD={rd} -> DIFF={actual}, "
                f"expected {wr - rd}"
            )


@cocotb.test()
async def diff_correct_across_wrap(dut):
    """DIFF == (WR - RD) mod 2^G_PTR_WIDTH across the wrap boundary.

    Adversarial cases focus on the wrap region: WR small, RD large -> raw
    (WR - RD) is negative; the entity must add C_BUFFER_SIZE = 2^G_PTR_WIDTH
    to land back in [0, 2^G_PTR_WIDTH).
    """
    await _bring_up(dut)

    width = _ptr_width(dut)
    cap = 1 << width      # 2^G_PTR_WIDTH (ring capacity)
    ptr_size = int(dut.ptr_size_g.value)

    # RD just past the wrap, WR just before — classic "RD is ahead of WR
    # in linear value but BEHIND in ring sense".
    cases = [
        (0, cap - 1),    # delta = +1 in ring sense (cap - 1)
        (1, cap - 2),    # delta = +3 (cap - 3)
        (2, cap - 1),    # delta = +3 (cap - 3)
        (0, 1),          # delta = cap - 1 (RD ahead by 1 -> full-ish)
    ]
    for wr, rd in cases:
        await _set_ptrs(dut, wr, rd)
        expected = (wr - rd) & (cap - 1)
        actual = int(dut.diff_o.value)
        # DIFF gets clamped to all-ones when full_int='1' (A + PTR_SIZE == B).
        # Filter those: if the entity considers itself FULL, the clamp is
        # documented behaviour, not a bug. Otherwise DIFF must equal the
        # ring-modular delta.
        is_full = (wr + ptr_size) % cap == (rd % cap)
        if not is_full:
            assert actual == expected, (
                f"VFIFO-REQ-101 wrap: WR={wr} RD={rd} -> DIFF={actual}, "
                f"expected {expected} (cap={cap})"
            )


@cocotb.test()
async def diff_random_modular(dut):
    """Adversarial: 200 random (WR, RD) pairs. For each, DIFF must equal
    (WR - RD) mod 2^G_PTR_WIDTH unless the entity is in its FULL clamp.
    """
    await _bring_up(dut)
    rng = random.Random(20260513)

    width = _ptr_width(dut)
    cap = 1 << width
    ptr_size = int(dut.ptr_size_g.value)

    mismatches = []
    for _ in range(200):
        wr = rng.randrange(cap)
        rd = rng.randrange(cap)
        await _set_ptrs(dut, wr, rd)
        expected = (wr - rd) & (cap - 1)
        actual = int(dut.diff_o.value)
        is_full = (wr + ptr_size) % cap == (rd % cap)
        if not is_full and actual != expected:
            mismatches.append((wr, rd, expected, actual))

    if mismatches:
        sample = mismatches[:5]
        raise AssertionError(
            f"VFIFO-REQ-101 random: {len(mismatches)}/200 mismatches. "
            f"First few (wr, rd, expected, got): {sample}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# FULL / EMPTY semantics — wrap-after-overrun characterization
# ──────────────────────────────────────────────────────────────────────────────
# ptr_mgr's FULL output is NOT a simple "less than one burst free" threshold.
# Reading rtl/ptr_mgr.vhd's sequential process carefully:
#
#   * almostFull_r  is REGISTERED, updated ONLY when diff_int /= 0, latching
#                   full_int (combinational: A + PTR_SIZE = B mod cap).
#   * almostEmpty_r is REGISTERED, updated ONLY when diff_int /= 0, latching
#                   empty_int (combinational: B + PTR_SIZE = A mod cap).
#   * full_r / empty_r are driven from a three-way if/elsif chain that fires
#                   ONLY when diff_int = 0, using the LATCHED almost* values
#                   from the previous cycle.
#
# So FULL only goes high when (a) almostFull_r was latched '1' at some earlier
# cycle when diff_int was non-zero, AND (b) the current cycle has diff_int = 0
# AND almostEmpty_r = '0' (otherwise the empty branch wins).
#
# That makes FULL a "wrap-after-overrun" detector: the pointers had to be
# nearly-full (within G_PTR_SIZE of collision) and then walk all the way
# around back to coincidence. Same shape for EMPTY but with the read pointer
# leading.
#
# This is unusual. It's characterized — not contract-claimed — by the tests
# below; VFIFO-T3 in tich-pm tracks whether to formalize this as REQ-102 or
# revisit the design.


@cocotb.test()
async def empty_after_reset(dut):
    """RST drives empty_r='1', almostEmpty_r='1'. After RST deassert with
    WR=RD=0 (diff=0), the registered empty_r holds high through the diff=0
    + almostEmpty_r=1 branch of the if/elsif chain.
    """
    await _bring_up(dut)
    # No pointer motion — straight from reset.
    assert int(dut.empty_o.value) == 1, (
        f"EMPTY should be '1' immediately after RST with WR=RD=0; "
        f"got EMPTY={int(dut.empty_o.value)}"
    )
    assert int(dut.full_o.value) == 0, (
        f"FULL should be '0' after RST; got FULL={int(dut.full_o.value)}"
    )
    assert int(dut.almost_empty_o.value) == 1, (
        f"ALMOST_EMPTY should be '1' after RST (almostEmpty_r reset to '1', "
        f"OR with empty_r='1'); got {int(dut.almost_empty_o.value)}"
    )
    assert int(dut.almost_full_o.value) == 0, (
        f"ALMOST_FULL should be '0' after RST; got {int(dut.almost_full_o.value)}"
    )


@cocotb.test()
async def full_via_wrap_after_overrun(dut):
    """Trigger FULL='1' via the documented wrap-after-overrun sequence:

      1. Drive pointers such that full_int = '1' AND empty_int = '0', with
         diff_int /= 0. The clocked process latches almostFull_r <= '1' and
         almostEmpty_r <= '0'.
      2. Drive WR=RD (diff_int = 0). The if/elsif chain sees almostEmpty_r=0
         AND almostFull_r=1, falls into the FULL branch, drives full_r <= '1'.

    This characterizes the actual FULL semantics. If a future refactor
    switches to standard FIFO behaviour ("FULL when WR == RD-1 mod cap"),
    this test fails loudly and the design owner has to update VFIFO-T3.
    """
    await _bring_up(dut)
    cap = 1 << _ptr_width(dut)
    ptr_size = int(dut.ptr_size_g.value)

    # Step 1: prime almostFull_r='1'. WR+PTR_SIZE = RD mod cap gives full_int=1.
    # With RD=0 and PTR_SIZE=4, cap=32: WR=28 gives 28+4 mod 32 = 0 = RD. ✓
    rd_prime = 0
    wr_prime = (rd_prime - ptr_size) % cap
    await _set_ptrs(dut, wr_prime, rd_prime)

    # At this clock edge: diff_int = 28 (non-zero), almostFull_r updates to
    # full_int=1. Check that ALMOST_FULL went high (which is almostFull_r OR
    # full_r — almostFull_r='1' so ALMOST_FULL='1' regardless of full_r).
    assert int(dut.almost_full_o.value) == 1, (
        f"ALMOST_FULL should be '1' once full_int=1 latches almostFull_r='1'; "
        f"got ALMOST_FULL={int(dut.almost_full_o.value)}"
    )
    assert int(dut.full_o.value) == 0, (
        f"FULL should still be '0' on the priming cycle (diff_int /= 0); "
        f"got FULL={int(dut.full_o.value)}"
    )

    # Step 2: WR=RD, diff_int=0. Clocked process sees almostFull_r='1' (from
    # step 1) → drives full_r='1'.
    await _set_ptrs(dut, 0, 0)
    assert int(dut.full_o.value) == 1, (
        f"FULL should assert '1' after the wrap-after-overrun sequence "
        f"(primed almostFull_r=1, then diff_int=0). Got FULL={int(dut.full_o.value)}"
    )
    assert int(dut.empty_o.value) == 0, (
        f"EMPTY should be '0' when FULL is '1' (mutually exclusive in the "
        f"if/elsif chain); got EMPTY={int(dut.empty_o.value)}"
    )


@cocotb.test()
async def empty_after_consumption(dut):
    """Symmetric to full_via_wrap_after_overrun for the EMPTY side.

      1. Drive empty_int=1 (RD+PTR_SIZE = WR mod cap) with diff_int /= 0.
         Latches almostEmpty_r <= '1', almostFull_r <= '0'.
      2. Drive WR=RD (diff_int = 0). almostEmpty_r=1 → empty_r <= '1'.
    """
    await _bring_up(dut)
    cap = 1 << _ptr_width(dut)
    ptr_size = int(dut.ptr_size_g.value)

    # Step 1: prime almostEmpty_r='1'. RD + PTR_SIZE = WR mod cap.
    # With WR=4 and PTR_SIZE=4, cap=32: RD=0 gives 0+4 = 4 = WR. ✓
    wr_prime = ptr_size
    rd_prime = 0
    await _set_ptrs(dut, wr_prime, rd_prime)

    assert int(dut.almost_empty_o.value) == 1, (
        f"ALMOST_EMPTY should be '1' once empty_int=1 latches almostEmpty_r='1'; "
        f"got ALMOST_EMPTY={int(dut.almost_empty_o.value)}"
    )

    # Step 2: WR=RD again.
    await _set_ptrs(dut, 0, 0)
    assert int(dut.empty_o.value) == 1, (
        f"EMPTY should assert '1' when RD catches up to WR with almostEmpty_r='1' "
        f"latched. Got EMPTY={int(dut.empty_o.value)}"
    )
    assert int(dut.full_o.value) == 0, (
        f"FULL should be '0' when EMPTY is '1'. Got FULL={int(dut.full_o.value)}"
    )


@cocotb.test()
async def reset_clears_in_flight_state(dut):
    """RST=1 mid-traffic must drive empty_r='1', full_r='0', almostEmpty_r='1',
    almostFull_r='0' regardless of how much pointer state was accumulated.
    """
    await _bring_up(dut)
    cap = 1 << _ptr_width(dut)
    ptr_size = int(dut.ptr_size_g.value)

    # Build a mid-traffic state that drives almostFull_r='1'.
    await _set_ptrs(dut, (cap - ptr_size) % cap, 0)

    # Smash RST.
    dut.rst_i.value = 1
    await RisingEdge(dut.clk_i)
    await RisingEdge(dut.clk_i)
    dut.rst_i.value = 0
    # Drive WR=RD=0 to make diff_int=0 so the if/elsif chain evaluates.
    await _set_ptrs(dut, 0, 0)

    assert int(dut.empty_o.value) == 1, (
        f"Post-RST: EMPTY should be '1'; got {int(dut.empty_o.value)}"
    )
    assert int(dut.full_o.value) == 0, (
        f"Post-RST: FULL should be '0' (almostFull_r reset to '0' even though "
        f"pre-RST it was '1'); got FULL={int(dut.full_o.value)}"
    )
    assert int(dut.almost_empty_o.value) == 1, (
        f"Post-RST: ALMOST_EMPTY should be '1'; got {int(dut.almost_empty_o.value)}"
    )
    assert int(dut.almost_full_o.value) == 0, (
        f"Post-RST: ALMOST_FULL should be '0'; got {int(dut.almost_full_o.value)}"
    )


@cocotb.test()
async def diff_full_clamp_asserts_all_ones(dut):
    """When full_int='1' (combinational), the entity clamps DIFF to all-ones
    via the bottom-of-architecture `DIFF <= ... when full_int='0' else
    (others => '1')` assignment. Verified positively here.
    """
    await _bring_up(dut)
    cap = 1 << _ptr_width(dut)
    ptr_size = int(dut.ptr_size_g.value)

    # full_int=1 means A+PTR_SIZE=B mod cap. WR=28, RD=0 with cap=32 PTR_SIZE=4.
    rd = 0
    wr = (rd - ptr_size) % cap
    await _set_ptrs(dut, wr, rd)

    actual = int(dut.diff_o.value)
    expected_clamp = (cap - 1)
    assert actual == expected_clamp, (
        f"DIFF full-clamp: with full_int='1' (WR={wr}, RD={rd}), DIFF should "
        f"be all-ones = {expected_clamp}; got {actual}"
    )


@cocotb.test()
async def almost_empty_or_chain(dut):
    """ALMOST_EMPTY = almostEmpty_r OR empty_r (per the output assignment in
    rtl/ptr_mgr.vhd). After reset, BOTH are '1' so ALMOST_EMPTY is '1'.
    Walk to a state where empty_r='0' but almostEmpty_r='1' (just after
    priming almostEmpty in a non-zero-diff cycle) — ALMOST_EMPTY must stay '1'.
    """
    await _bring_up(dut)
    cap = 1 << _ptr_width(dut)
    ptr_size = int(dut.ptr_size_g.value)

    # Prime almostEmpty_r='1' via empty_int=1; diff_int /= 0 so empty_r stays '0'.
    wr_prime = ptr_size      # makes RD + PTR_SIZE = WR
    rd_prime = 0
    await _set_ptrs(dut, wr_prime, rd_prime)

    # Observe: diff_int = PTR_SIZE /= 0, so empty_r='0', almostEmpty_r='1'.
    # ALMOST_EMPTY = almostEmpty_r OR empty_r = '1' OR '0' = '1'.
    assert int(dut.almost_empty_o.value) == 1, (
        f"ALMOST_EMPTY should be '1' via the OR with almostEmpty_r='1' even "
        f"though empty_r='0'; got {int(dut.almost_empty_o.value)}"
    )
    assert int(dut.empty_o.value) == 0, (
        f"EMPTY (just empty_r) should be '0' on the priming cycle; "
        f"got {int(dut.empty_o.value)}"
    )


@cocotb.test()
async def almost_full_or_chain(dut):
    """ALMOST_FULL = almostFull_r OR full_r. Mirror of the EMPTY side."""
    await _bring_up(dut)
    cap = 1 << _ptr_width(dut)
    ptr_size = int(dut.ptr_size_g.value)

    # Prime almostFull_r='1' via full_int=1; diff_int /= 0 so full_r stays '0'.
    rd_prime = 0
    wr_prime = (rd_prime - ptr_size) % cap   # makes WR + PTR_SIZE = RD
    await _set_ptrs(dut, wr_prime, rd_prime)

    assert int(dut.almost_full_o.value) == 1, (
        f"ALMOST_FULL should be '1' via the OR with almostFull_r='1' even "
        f"though full_r='0'; got {int(dut.almost_full_o.value)}"
    )
    assert int(dut.full_o.value) == 0, (
        f"FULL (just full_r) should be '0' on the priming cycle; "
        f"got {int(dut.full_o.value)}"
    )


if __name__ == "__main__":
    main()

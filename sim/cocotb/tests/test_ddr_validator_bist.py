"""Unit-level cocotb tests for ddr_validator_bist — standalone, no contract
attribution (example/ scope, same spirit as test_ddr_validator_csr.py).

DDR-P2.9: scenario 2 (reset_recovery) used to be a defined-safe STUB that
silently behaved as scenario 0 (plain roundtrip) — the docstring said so, but
nothing ever checked it. A host that armed scenario 2 on real silicon,
expecting it to exercise the DDR-T2.15 CS/reset-busy path, got an ordinary
roundtrip and a green PASS that proved nothing about reset recovery.

This module drives ddr_validator_bist standalone (it depends only on `ieee`,
same as the CSR) against a trivial Python-side DDR loopback model — no
ddr_stream_axi, no AXI, just the S_AXIS/M_AXIS handshakes the BIST itself
drives. That is enough to prove:

  * scenario 0 completes a clean roundtrip (baseline sanity for the harness
    itself — if this doesn't pass nothing else here means anything).
  * scenario 2 actually PULSES ip_rst_o once, roughly mid-run, not merely
    behaving as scenario 0 (the exact defect this test exists to catch).
  * scenario 2 still completes with pass_o=1 / error_count_o=0 afterwards —
    the write pass cleanly restarts from word 1 post-pulse.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from pathlib import Path as _Path

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None


_IP_ROOT = _Path(__file__).resolve().parents[3]   # rr-ddr-stream/
_BIST_VHD = _IP_ROOT / "example" / "zybo_z7_20_ddr_validator" / "src" / "ddr_validator_bist.vhd"

SCENARIO_ROUNDTRIP = 0x0
SCENARIO_RESET_RECOVERY = 0x2


def main() -> None:
    """`rr sim run test_ddr_validator_bist` entry point.

    Compiles only ddr_validator_bist.vhd — it is package-free (ieee only).
    """
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke this test via `rr sim run test_ddr_validator_bist`."
        )

    run_simulation(
        top_level="ddr_validator_bist",
        module="test_ddr_validator_bist",
        custom_libraries={
            "rr": [
                str(_BIST_VHD),
            ],
        },
        generics={"user_data_width_g": 32},
        waves=True,
        simulator="nvc",
    )


async def _bring_up(dut, period_ns: float = 10.0):
    cocotb.start_soon(Clock(dut.clk_i, period_ns, unit="ns").start())
    dut.rst_i.value          = 1
    dut.start_i.value        = 0
    dut.soft_reset_i.value   = 0
    dut.scenario_i.value     = 0
    dut.n_words_i.value      = 0
    dut.s_axis_ready_i.value = 0
    dut.m_axis_data_i.value  = 0
    dut.m_axis_valid_i.value = 0
    dut.write_idle_i.value   = 1
    dut.read_idle_i.value    = 1
    for _ in range(5):
        await RisingEdge(dut.clk_i)
    dut.rst_i.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk_i)


async def _start(dut, scenario: int, n_words: int) -> None:
    dut.scenario_i.value = scenario
    dut.n_words_i.value  = n_words
    dut.start_i.value    = 1
    await RisingEdge(dut.clk_i)
    dut.start_i.value = 0


async def _drive_loopback_ddr_and_wait_done(dut, n_words: int, max_cycles: int = 200000):
    """Trivial Python-side DDR: services S_AXIS writes into an ordered list,
    resetting that list whenever ip_rst_o pulses (real DDR content at a given
    ring index is simply OVERWRITTEN when the write pass restarts from word 1
    — the physical result is identical to clearing and replaying), then plays
    the same list back on M_AXIS once the BIST reaches its read phase.
    Always-ready on both sides (perpetual backpressure-free model — the BIST's
    own state machine gates s_valid/m_ready, not this loopback).
    Returns (reset_pulses_seen, reset_pulse_positions) — the write-count value
    (from words_written_o) sampled on each cycle ip_rst_o is observed high.
    """
    written: list[int] = []
    read_back: list[int] = []
    reset_positions: list[int] = []
    dut.s_axis_ready_i.value = 1

    for _ in range(max_cycles):
        await RisingEdge(dut.clk_i)

        if int(dut.ip_rst_o.value) == 1:
            reset_positions.append(int(dut.words_written_o.value))
            written.clear()

        if int(dut.s_axis_valid_o.value) == 1 and int(dut.s_axis_ready_i.value) == 1:
            written.append(int(dut.s_axis_data_o.value))

        # Serve M_AXIS strictly in written order, one beat behind the BIST's
        # own m_axis_ready_o gating (data_i must be valid the same cycle
        # valid_i and ready_o are both high — present the NEXT unread word
        # whenever the BIST is asking, i.e. m_axis_ready_o = '1').
        if int(dut.m_axis_ready_o.value) == 1 and len(read_back) < len(written):
            dut.m_axis_data_i.value  = written[len(read_back)]
            dut.m_axis_valid_i.value = 1
            read_back.append(written[len(read_back)])
        else:
            dut.m_axis_valid_i.value = 0

        if int(dut.done_o.value) == 1:
            break
    else:
        assert False, f"BIST did not assert done_o within {max_cycles} cycles"

    # pass_o/fail_o are latched by the S_DONE case arm the cycle AFTER
    # state_r first reads as S_DONE (done_o is combinational on state_r, one
    # cycle ahead of the registered verdict) — settle one more edge before a
    # caller reads the verdict.
    await RisingEdge(dut.clk_i)
    dut.m_axis_valid_i.value = 0
    dut.s_axis_ready_i.value = 0
    return reset_positions


@cocotb.test()
async def scenario0_roundtrip_passes(dut):
    """Baseline sanity: scenario 0 completes with pass_o=1, no errors, and
    (adversarial floor) both write/read counts actually reached n_words."""
    await _bring_up(dut)
    N = 64
    await _start(dut, SCENARIO_ROUNDTRIP, N)
    reset_positions = await _drive_loopback_ddr_and_wait_done(dut, N)

    assert reset_positions == [], (
        f"scenario 0 must never pulse ip_rst_o; saw pulses at word-counts {reset_positions}")
    assert int(dut.pass_o.value) == 1, "scenario 0 roundtrip did not report pass_o=1"
    assert int(dut.fail_o.value) == 0
    assert int(dut.error_count_o.value) == 0
    assert int(dut.words_written_o.value) == N
    assert int(dut.words_read_o.value) == N


@cocotb.test()
async def scenario2_pulses_ip_reset_mid_run(dut):
    """DDR-P2.9 regression — scenario 2 (reset_recovery) must actually pulse
    ip_rst_o once, roughly halfway through the write pass, not merely behave
    as an ordinary roundtrip (the pre-fix STUB behaviour). Guards the exact
    defect: a host arming scenario 2 to exercise DDR-T2.15 CS/reset-busy
    recovery on silicon must be able to trust that a reset actually happens."""
    await _bring_up(dut)
    N = 64
    await _start(dut, SCENARIO_RESET_RECOVERY, N)
    reset_positions = await _drive_loopback_ddr_and_wait_done(dut, N)

    assert len(reset_positions) >= 1, (
        "scenario 2 (reset_recovery) never pulsed ip_rst_o — it behaved as a "
        "plain roundtrip (the pre-fix STUB), which proves nothing about "
        "DDR-T2.15 reset/CS recovery on real silicon.")
    # The pulse must land roughly mid-write (not at the very start, where it
    # would race the first beat, nor after the write pass already finished,
    # where there is no in-flight traffic to interrupt — DDR-T2.15 only
    # manifests against genuine mid-stream state).
    first_pulse_at = reset_positions[0]
    assert 0 < first_pulse_at < N, (
        f"scenario 2 pulsed ip_rst_o at words_written={first_pulse_at}, "
        f"expected somewhere strictly inside the write pass (0, {N})")

    assert int(dut.pass_o.value) == 1, (
        "scenario 2 did not recover cleanly after its own reset pulse — "
        f"pass_o=0 fail_o={int(dut.fail_o.value)} "
        f"error_count={int(dut.error_count_o.value)}")
    assert int(dut.fail_o.value) == 0
    assert int(dut.error_count_o.value) == 0
    assert int(dut.words_written_o.value) == N
    assert int(dut.words_read_o.value) == N


@cocotb.test()
async def scenario2_survives_its_own_reset_pulse(dut):
    """The BIST's own state machine must NOT reset when ip_rst_o pulses —
    rst_i (this module's reset) is a DIFFERENT signal in real designs
    (mirrors the bist_ip_rst / ip_rst_sys top-level split: ip_rst_o resets
    ONLY the IP, never the BIST/CSR). Proven here by observing running_o stay
    asserted (mod cycle granularity) across the pulse — a self-reset would
    drop back to S_IDLE and running_o would deassert independent of state."""
    await _bring_up(dut)
    N = 64
    await _start(dut, SCENARIO_RESET_RECOVERY, N)

    saw_pulse = False
    running_dropped_during_pulse = False
    written: list[int] = []
    read_back: list[int] = []
    dut.s_axis_ready_i.value = 1
    for _ in range(200000):
        await RisingEdge(dut.clk_i)
        if int(dut.ip_rst_o.value) == 1:
            saw_pulse = True
            written.clear()
            if int(dut.running_o.value) != 1:
                running_dropped_during_pulse = True
        if int(dut.s_axis_valid_o.value) == 1 and int(dut.s_axis_ready_i.value) == 1:
            written.append(int(dut.s_axis_data_o.value))
        if int(dut.m_axis_ready_o.value) == 1 and len(read_back) < len(written):
            dut.m_axis_data_i.value  = written[len(read_back)]
            dut.m_axis_valid_i.value = 1
            read_back.append(written[len(read_back)])
        else:
            dut.m_axis_valid_i.value = 0
        if int(dut.done_o.value) == 1:
            break
    dut.m_axis_valid_i.value = 0
    dut.s_axis_ready_i.value = 0

    assert saw_pulse, "scenario 2 never pulsed ip_rst_o — harness precondition not met"
    assert not running_dropped_during_pulse, (
        "running_o deasserted while ip_rst_o was high — the BIST's own state "
        "machine appears to be wired to (or otherwise reacting to) its own "
        "ip_rst_o pulse, which would make it unable to observe the recovery "
        "it exists to exercise")


if __name__ == "__main__":
    main()

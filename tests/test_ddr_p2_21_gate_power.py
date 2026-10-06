"""DDR-P2.21 — power guards for the two DDR-P2.18 vacuous gates.

DDR-P2.18's land was refused (tich-pm-data 0c7511759d) because two gates
CERTIFIED NOTHING while reporting green — the "green-suite-is-not-evidence"
shape. This file is the regression guard that would have caught both, and it
asserts each defect at the SURFACE a real consumer reaches, not at a helper's
return value:

  1. VACUOUS SILICON ROUND-TRIP GATE.  `test_committed_silicon_roundtrip_
     capture_satisfies_the_contract` in tests/test_pfsoc_ddr_hw_witness.py was
     marked ``xfail(strict=True)``.  A missing silicon capture then made the
     node an *expected fail* — i.e. it PASSED the suite (pytest exit 0) with no
     write->commit->read ever observed on the board.  The honest gate must go
     RED *by evaluating the committed silicon artifact* (today the idle no-DDR
     capture: wr_commit_addr==0, m_axis_valid never 1, read_data unchanged) and
     turn GREEN only on a real, distinct-data round-trip.  We prove the EFFECT by
     running that exact node in a child pytest and asserting it was COLLECTED and
     FAILED (pytest exit 1) with the ``SILICON_ROUNDTRIP_UNMET`` signal.  A bare
     ``exit != 0`` is itself vacuous — a collection/import error is non-zero for
     an unrelated reason (why the earlier form passed on origin/main, where the
     witness node did not exist).  A subprocess is required because ``xfail``
     changes only the COLLECTED OUTCOME, never the function's direct return.

  2. EMPTY / UNBOUND CDC SDC.  constraints/ddr_stream_cdc_scoped_microchip.sdc
     carried set_max_delay / set_false_path crossing exceptions but defined NO
     ``create_clock``.  On a Synopsys-style analyzer (Libero SmartTime) an
     inter-clock exception with no defined clocks constrains ZERO paths, so
     VERIFYTIMING reports clean REGARDLESS of the design.  The honest SDC must
     define the crossing clocks ON REAL PORTS so those exceptions bind.  We can't
     run Libero here (RTL-P2.1273 — no SmartTime on this bench), so the guard
     asserts the computable precondition Libero's non-zero constrained-path count
     DEPENDS ON: every ``create_clock`` binds a real INPUT PORT of the consuming
     entity (ddr_stream_fic: clk_sys_i / clk_ddr_i).  A create_clock on a
     non-existent port is an empty ``get_ports`` selection (SDC error) that scores
     nothing — so a dummy/unbound clock leaves the count 0 and the guard RED,
     exactly as VERIFYTIMING would report nothing.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WITNESS_TEST = REPO_ROOT / "tests" / "test_pfsoc_ddr_hw_witness.py"
ROUNDTRIP_NODE = (
    "tests/test_pfsoc_ddr_hw_witness.py::"
    "test_committed_silicon_roundtrip_capture_satisfies_the_contract"
)
REAL_ROUNDTRIP = (
    REPO_ROOT / "docs" / "internal" / "ddr_p2_18_witness" / "rea_capture.csv"
)
MICROCHIP_SDC = (
    REPO_ROOT / "constraints" / "ddr_stream_cdc_scoped_microchip.sdc"
)
# The design this scoped CDC SDC constrains. A create_clock in the SDC can only
# bind a path if its get_ports target is a REAL port of this entity — an
# unbound clock is an empty selection (SDC error) that scores nothing.
DDR_STREAM_FIC = REPO_ROOT / "rtl" / "ddr_stream_fic.vhd"


# --------------------------------------------------------------------------- #
# Defect 1 — the silicon round-trip gate must be RED, not a vacuous xfail-green.
# --------------------------------------------------------------------------- #
def _run_node_exit_code() -> tuple[int, str]:
    """Run the committed-silicon round-trip node in a child pytest and return
    (exit_code, combined_output). The child run is deliberately minimal (no
    conftest, no cache, no addopts) so it reproduces the plain gate outcome a
    CI/land run reaches."""
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "-p", "no:cacheprovider",
            "--noconftest",
            "-o", "addopts=",
            "-q", "--no-header",
            ROUNDTRIP_NODE,
        ],
        cwd=str(REPO_ROOT),
        text=True,
        errors="replace",
        capture_output=True,
        timeout=90,
    )
    return proc.returncode, f"{proc.stdout}\n{proc.stderr}"


def test_committed_roundtrip_gate_is_red_while_no_silicon_capture():
    """EFFECT-level guard: while no real silicon round-trip is committed, the
    committed-round-trip node must FAIL *by evaluating the idle silicon artifact
    and finding no write->commit->read* — not merely by any non-zero exit.

    The prior vacuity was an xfail(strict) marker that turned the missing-capture
    failure into an *expected fail* (pytest exit 0). But `exit_code != 0` alone is
    ALSO vacuous: a collection error / missing test file / import error is
    non-zero for a reason unrelated to silicon (it is exactly why this guard
    passed on origin/main, where the witness node did not yet exist). So this
    asserts the child ran and failed HONESTLY:
      * pytest exit == 1 (tests were COLLECTED and one FAILED) — not 4 (usage /
        collection error), not 5 (no tests collected), not 0 (passed);
      * the failure message carries the SILICON_ROUNDTRIP_UNMET tag the witness
        node emits when the committed capture does not satisfy the round-trip
        contract. A collection/import failure cannot emit that tag.
    On origin/main (witness node absent) the child cannot be collected -> exit 4,
    no tag -> this guard is RED, as a regression guard must be before the fix.
    """
    witness_dir = REAL_ROUNDTRIP.parent
    if not witness_dir.is_dir():
        # The public mirror omits docs/internal/ (DDR-P2.26/P2.27): the witness
        # node then SKIPS, so there is nothing for this guard to judge.
        if os.environ.get("RR_REQUIRE_PRIVATE_WITNESS") == "1":
            pytest.fail(
                f"RR_REQUIRE_PRIVATE_WITNESS=1 but witness evidence is missing: {witness_dir}"
            )
        pytest.skip(f"DDR-P2.27: witness evidence is absent under {witness_dir}")
    assert not REAL_ROUNDTRIP.exists(), (
        "precondition changed: a real rea_capture.csv is now committed, so this "
        "guard (which asserts the gate is RED *because* only the idle capture "
        "exists) must be re-derived to assert the gate is GREEN on the real "
        f"distinct-data round-trip instead. Capture found at {REAL_ROUNDTRIP}."
    )
    exit_code, output = _run_node_exit_code()
    assert exit_code == 1, (
        "VACUOUS_ROUNDTRIP_GATE: the committed-silicon round-trip node did not go "
        f"RED by EVALUATING the idle capture (pytest exit {exit_code}, wanted 1 = "
        "collected-and-failed). Exit 0 = xfail-masked / passing without silicon; "
        "exit 4/5 = collection or import error (non-zero for a reason unrelated "
        "to the round-trip — the origin/main false-green this guard must reject). "
        f"\n--- child pytest output ---\n{output}"
    )
    assert "SILICON_ROUNDTRIP_UNMET" in output, (
        "VACUOUS_ROUNDTRIP_GATE: the round-trip node failed but NOT with the "
        "SILICON_ROUNDTRIP_UNMET signal — it did not fail by evaluating the idle "
        "silicon capture against the write->commit->read contract. A red for any "
        "other reason (missing file, collection error) certifies nothing about "
        f"silicon.\n--- child pytest output ---\n{output}"
    )


# --------------------------------------------------------------------------- #
# Defect 2 — the Microchip CDC SDC must define clocks so its exceptions bind.
# --------------------------------------------------------------------------- #
# A create_clock naming its source ports: `create_clock ... [get_ports {a b}]`.
_CREATE_CLOCK_PORTS = re.compile(
    r"^\s*create_clock\b[^\n]*?\bget_ports\s*\{([^}]*)\}",
    re.MULTILINE,
)
# A bare create_clock with NO get_ports target (a virtual/unbound clock).
_CREATE_CLOCK_ANY = re.compile(r"^\s*create_clock\b", re.MULTILINE)
# A crossing exception is a set_max_delay / set_false_path carrying a `-to`
# destination selector (the constrained endpoints).
_CROSSING_EXCEPTION = re.compile(
    r"^\s*(set_max_delay|set_false_path)\b[^\n]*?-to\b",
    re.MULTILINE | re.DOTALL,
)


def _sdc_command_text(path: Path) -> str:
    """SDC with comment (#) lines stripped, command continuations joined."""
    keep = [
        ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not ln.lstrip().startswith("#")
    ]
    return re.sub(r"\\\s*\n\s*", " ", "\n".join(keep))


def _entity_input_ports(path: Path) -> set[str]:
    """The input ports the consuming IP actually exposes — the only ports a
    create_clock in this SDC can legally bind. Parsed from the VHDL entity port
    list (``name : in <type>``). If the RTL is absent the set is empty, which
    makes every create_clock read as unbound (RED) rather than silently green."""
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        m.group(1)
        for m in re.finditer(r"^\s*(\w+)\s*:\s*in\b", text, re.MULTILINE | re.IGNORECASE)
    }


def _entity_clock_ports(path: Path) -> set[str]:
    """The CLOCK input ports of the consuming entity (``clk*`` inputs) — the
    crossing-domain boundaries an inter-clock exception is bounded by. For
    ddr_stream_fic this is {clk_sys_i, clk_ddr_i}: its two async domains. A
    reset or data input is a legal ``create_clock`` binding target for the
    port-existence check, but a clock defined only on such a port gives
    SmartTime no crossing domain and scores nothing."""
    return {p for p in _entity_input_ports(path) if p.lower().startswith("clk")}


def _smarttime_constrained_path_count(path: Path) -> int:
    """The number of crossing exceptions SmartTime could actually SCORE.

    A set_max_delay/set_false_path is an INTER-CLOCK exception; SmartTime can
    only apply it against a path if BOTH clocks bounding that path are DEFINED
    *and those clocks bind real clock ports* (the SDC's own section 0 states
    exactly this). So the count is 0 unless ALL of:
      1. at least one create_clock exists;
      2. EVERY create_clock present binds only real input ports of
         ddr_stream_fic (a dummy/non-existent port is an empty get_ports
         selection: Libero errors SDC0023 and scores nothing);
      3. EVERY clock port of the consuming entity (clk_sys_i AND clk_ddr_i)
         carries a defined clock — one bound clock alone (or a clock defined
         on a non-clock input such as rst_sys_i) leaves the crossing with an
         undefined bounding domain, and an inter-clock exception with only one
         defined clock is unscorable (the reviewer's second mutant).
    Only then does SmartTime have both live crossing domains and the exception
    count is scorable."""
    text = _sdc_command_text(path)
    if not _CREATE_CLOCK_ANY.search(text):
        return 0
    ports = _entity_input_ports(DDR_STREAM_FIC)
    clock_ports = _entity_clock_ports(DDR_STREAM_FIC)
    if not clock_ports:
        # No parseable clock ports on the entity: nothing can be proven bound.
        return 0
    bound_targets: set[str] = set()
    for m in re.finditer(r"^\s*create_clock\b[^\n]*", text, re.MULTILINE):
        line = m.group(0)
        pm = _CREATE_CLOCK_PORTS.search(line)
        targets = pm.group(1).split() if pm else []
        # Unbound if: no get_ports target at all, or any named port is not a real
        # port of the consuming entity (empty selection -> SDC error -> 0 paths).
        if not targets or not all(t in ports for t in targets):
            return 0
        bound_targets.update(targets)
    # BOTH crossing-domain clocks must be defined, not merely "some clock on
    # some input": a reset-pin-only or single-clock SDC scores zero crossings.
    if not clock_ports.issubset(bound_targets):
        return 0
    return len(_CROSSING_EXCEPTION.findall(text))


def test_microchip_cdc_sdc_constrains_nonzero_paths():
    """The Microchip SmartTime CDC SDC must define its crossing clocks ON REAL
    PORTS so its exceptions bind real paths. With no create_clock — OR a
    create_clock whose get_ports target does not exist in the consuming entity
    (an empty selection Libero errors on) — VERIFYTIMING scores ZERO constrained
    paths and reports clean regardless of the design (the vacuous CDC gate).
    RTL-P2.1273 means Libero itself can't run on this bench, so this asserts the
    bound-create_clock precondition that a non-zero SmartTime count DEPENDS ON."""
    assert MICROCHIP_SDC.is_file(), f"missing constraint file: {MICROCHIP_SDC}"
    assert DDR_STREAM_FIC.is_file(), (
        f"cannot verify clock binding: consuming entity {DDR_STREAM_FIC} is "
        "missing, so no create_clock target can be proven to bind a real port."
    )
    count = _smarttime_constrained_path_count(MICROCHIP_SDC)
    assert count > 0, (
        "EMPTY_CDC_SDC: constraints/ddr_stream_cdc_scoped_microchip.sdc has no "
        "create_clock BOUND TO A REAL ddr_stream_fic port (clk_sys_i / clk_ddr_i), "
        "so Libero SmartTime has no live clocks to bind its set_max_delay / "
        "set_false_path crossing exceptions against — VERIFYTIMING would score 0 "
        "constrained paths and report clean for ANY design (a vacuous CDC gate). "
        "A create_clock on a non-existent port is an empty get_ports selection "
        "(SDC error) that scores nothing, not a fix; a clock defined only on a "
        "non-clock input (rst_sys_i) or on just ONE of the two crossing clocks "
        "leaves the inter-clock exceptions with an undefined bounding domain "
        "and scores nothing either. BOTH clk_sys_i AND clk_ddr_i must carry a "
        "create_clock so the scoped exceptions bind."
    )

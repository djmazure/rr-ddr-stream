"""DDR-P3.37 guard: plane-conditional CDC selectors must not emit TMC-20026.

WHY THIS TEST EXISTS
--------------------
`ddr_stream_cdc_scoped.sdc` ships to EVERY Quartus consumer of rr_ddr_stream,
whatever plane mix that consumer enables. Two of its crossings target registers
that can be LEGITIMATELY ABSENT on a valid single-plane build:

  * section C  rd/wr_base_addr_sys -> rd/wr_base_addr_meta  (base-address MCP):
    a write-plane-only consumer has no READ base-address flops (and vice-versa),
    and a fixed base can be constant-folded away.
  * section E  rd/wr_ptr_addr_sys  -> rd/wr_ptr_addr_ddr    (SW-pointer MCP):
    the AVMM top has no SW-pointer plane at all; validators fold the pointers to
    constants.

Quartus's `get_registers {a b}` emits a **TMC-20026 zero-match warning (332174)
for EACH empty SUB-pattern of the brace-list, even when the overall collection
is non-empty**. So on the deepskopion de25_datalogger post-fit netlist
(write-plane-on, read base-address folded away) the section-C selector matched
its WRITE half (32 regs) yet still emitted two
``*rd_base_addr_{sys,meta}[*] could not be matched`` warnings — a direct
violation of DDR-T1.3 DONE-WHEN (b) "zero TMC-20026 empty collections".

The cure, proven by A/B STA on that exact fitted netlist (DDR-P3.37 witness
docs/internal/DDR_P3_37_BENCH_STA_WITNESS.md): fetch every plane-conditional
selector with ``get_registers -nowarn`` (as section E already did), so an absent
plane's sub-pattern is silenced at the raw-warning level while a controlled
``get_collection_size`` guard still makes a fully-absent selector VISIBLE via
``post_message`` (owner clause 3 — a 0-match is never SILENTLY swallowed).

The INVERSE must also hold. Sections A (olo_base_cc_bits RegIn/Reg0), B
(olo_base_cc_reset RstLatch/RstRqst) and D (ddr_sync_cdc stages_r) target
primitives that are ALWAYS instantiated on any streaming top. A 0-match THERE is
a genuine drift/rename defect (the crossing would silently go unconstrained), so
those selectors must NOT be ``-nowarn`` — the raw Quartus warning is the wanted
alarm, and ``tests/test_cdc_xdc_drift.py`` is its desk-side twin.

This guard runs at the desk (no Quartus): it asserts the SDC SOURCE property that
DETERMINES the netlist-time surface behaviour verified in the witness. The
warning is emitted at read_sdc / update_timing_netlist time, so the source
property is a complete predictor of the fit/STA outcome.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SDC = REPO_ROOT / "constraints" / "ddr_stream_cdc_scoped.sdc"

# get_registers [-nowarn] {pat1 pat2 ...} — capture the -nowarn presence and the
# brace-list body, exactly the shape tests/test_cdc_xdc_drift.py parses.
_GET_REGISTERS = re.compile(r"get_registers\s+(-nowarn\s+)?\{([^}]+)\}")

# Selectors that may LEGITIMATELY resolve to 0 registers on a valid consumer
# build (plane-conditional / AVMM-absent). These MUST be -nowarn guarded.
_PLANE_CONDITIONAL_TOKENS = ("base_addr", "ptr_addr")

# Selectors that target ALWAYS-present primitives. A 0-match here is a real
# defect, so they must NOT be -nowarn (the raw warning is the wanted alarm).
_ALWAYS_PRESENT_TOKENS = (
    "i_ddr_fifo_intf",  # A: olo_base_cc_bits RegIn/Reg0
    "i_rst_cc",         # B: olo_base_cc_reset RstLatch/RstRqst
    "i_sync_cdc",       # D: ddr_sync_cdc stages_r
)


def _sdc_noncomment_text() -> str:
    assert SDC.is_file(), f"missing constraint file: {SDC}"
    out = []
    for line in SDC.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def _get_registers_calls():
    """(-nowarn: bool, body: str) for every get_registers in live SDC code."""
    calls = []
    for m in _GET_REGISTERS.finditer(_sdc_noncomment_text()):
        calls.append((m.group(1) is not None, m.group(2).strip()))
    assert calls, "no get_registers calls parsed from the SDC — parser drift?"
    return calls


def _bodies_matching(tokens):
    return [
        (nowarn, body)
        for nowarn, body in _get_registers_calls()
        if any(tok in body for tok in tokens)
    ]


def test_parser_sees_the_expected_selector_families():
    """Sanity: the SDC still contains the plane-conditional and always-present
    selectors this guard reasons about (so a rewrite can't make it vacuous)."""
    assert _bodies_matching(_PLANE_CONDITIONAL_TOKENS), (
        "no base_addr/ptr_addr get_registers found — section C or E was removed "
        "or renamed; re-derive this guard against the new SDC."
    )
    assert _bodies_matching(_ALWAYS_PRESENT_TOKENS), (
        "no i_ddr_fifo_intf/i_rst_cc/i_sync_cdc get_registers found — sections "
        "A/B/D were removed or renamed; re-derive this guard."
    )


@pytest.mark.parametrize(
    "body",
    [b for nowarn, b in _bodies_matching(_PLANE_CONDITIONAL_TOKENS)],
)
def test_plane_conditional_selectors_are_nowarn(body):
    """Every base_addr / ptr_addr get_registers must be -nowarn, so an absent
    plane emits no Quartus TMC-20026 (332174). This is the DDR-P3.37 fix: the
    write-plane-only de25_datalogger emitted two 332174 on the section-C read
    half until this held."""
    calls = {b: nowarn for nowarn, b in _get_registers_calls()}
    assert calls[body], (
        f"plane-conditional selector {{{body}}} uses a BARE get_registers. On a "
        f"single-plane / AVMM consumer its absent sub-pattern emits a TMC-20026 "
        f"zero-match warning (332174), violating DDR-T1.3 DONE-WHEN (b). Add "
        f"-nowarn (and keep the get_collection_size else-guard) — see section E "
        f"and DDR-P3.37."
    )


@pytest.mark.parametrize(
    "body",
    [b for nowarn, b in _bodies_matching(_ALWAYS_PRESENT_TOKENS)],
)
def test_always_present_selectors_are_not_nowarn(body):
    """Sections A/B/D target primitives that are ALWAYS instantiated. They must
    NOT be -nowarn: a 0-match there is a genuine drift/rename defect that must
    stay loud (the raw Quartus warning is the wanted alarm)."""
    calls = {b: nowarn for nowarn, b in _get_registers_calls()}
    assert not calls[body], (
        f"always-present selector {{{body}}} is -nowarn. That silences the only "
        f"alarm for a rename/refactor that leaves this metastability crossing "
        f"UNCONSTRAINED. Remove -nowarn here — only plane-conditional "
        f"(base_addr/ptr_addr) selectors may carry it (DDR-P3.37)."
    )


def test_plane_conditional_selectors_carry_a_visible_else_guard():
    """A -nowarn on a plane-conditional selector must be paired with a
    get_collection_size guard whose else emits post_message, so a FULLY-absent
    selector is still VISIBLE (owner clause 3), not silently dropped."""
    text = _sdc_noncomment_text()
    # One guard per plane-conditional crossing (section C base_addr, section E
    # ptr_addr). Each must appear as an `if {[get_collection_size ...] > 0}` with
    # an `else` that calls post_message.
    n_guards = len(re.findall(r"get_collection_size[^\n]*?>\s*0", text))
    n_postmsg = len(re.findall(r"post_message\s+-type\s+warning", text))
    assert n_guards >= 2, (
        f"expected >=2 get_collection_size>0 guards (section C base_addr + "
        f"section E ptr_addr), found {n_guards}. A -nowarn without the size "
        f"guard would silently swallow a fully-absent selector (DDR-P3.37)."
    )
    assert n_postmsg >= 2, (
        f"expected >=2 post_message -type warning else-branches (one per "
        f"plane-conditional guard), found {n_postmsg}. Owner clause 3: a "
        f"0-match must be VISIBLE, not silently swallowed."
    )

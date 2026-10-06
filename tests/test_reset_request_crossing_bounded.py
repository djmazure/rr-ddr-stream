"""DDR-T1.3 guard: the olo_base_cc_reset REQUEST crossing must carry a bound.

WHY THIS TEST EXISTS
--------------------
`ddr_stream_cdc_scoped.sdc` is the Quartus/Altera twin of the silicon-proven
`ddr_stream_cdc_scoped.xdc`.  The deepskopion DE25 Agilex-5 consumer builds
through Quartus, so it reads the **.sdc** — never the .xdc.

The reset request crossing is the async-assert / sync-deassert path
`RstALatch -> RstRqstA2B` (and the B2A mirror) inside every `olo_base_cc_reset`
(instance `i_rst_cc`) under each `olo_base_fifo_async`.  `RstRqstA2B` /
`RstRqstB2A` are 3-bit vectors driven all-at-once by the OTHER domain's latch
through an asynchronous preset.

Until DDR-T1.3 the .sdc constrained that crossing with a **bare**
``set_false_path`` and nothing else.  A bare false path does two things:

  1. it (correctly) suppresses the recovery/removal report on the async preset,
  2. it (incorrectly) leaves the crossing's physical route delay AND its
     inter-bit skew **unbounded** — the async assertion can arrive at the 3
     RstRqst flops spread across an arbitrary window, so the reset RELEASE
     becomes route-dependent instead of deterministic.

That is a defect by the owner's constraints-audit **class table**:

    "a reset request crossing => must carry a bound; a bare false path is a
     defect."

That single ground is checkable at the desk without Quartus, and it is the ONLY
ground this guard rests on.

RETRACTED (2026-09-04): a prior version of this test also asserted an
"XDC twin bounds this exact crossing" parity story. That was **false**. The
`.xdc` section-A ``set_max_delay -datapath_only`` lists ``RstRqst*_reg`` as a
`-to` *cell*, which bounds those flops' INTRA-domain ``'0'``-fill D-shift (same
capture clock) — NOT the cross-domain async preset. The genuine crossing (the
async PRE/CLR pin) is ``set_false_path``-ed by XDC section B with NO datapath
bound, so the original bare ``set_false_path`` in the .sdc was a FAITHFUL
translation of XDC section B. The section-B bound is a NEW, stricter constraint
standing on the class table alone. ``test_xdc_reset_request_crossing_is_false_pathed``
below now asserts that TRUE relationship instead of the retracted parity claim.

So this guard asserts: for every reset-request crossing the .sdc false-paths, it
ALSO carries a ``set_net_delay -max`` (latency bound) and a ``set_max_skew``
(inter-bit coherence bound) over the same endpoints.  It is pure text analysis —
runs anywhere, no simulator, no Quartus.

WHAT THIS GUARD DOES NOT PROVE (owner done-when d, bench work — see
docs/internal/DDR_T1_3_SDC_CDC_AUDIT.md §5): it never opens a netlist, so it
cannot show a selector matches >=1 register on the deepskopion build (owner
clause (d), a netlist match-count), and it cannot show Quartus honours
``set_net_delay`` on an async-preset endpoint. Those are STA/bench checks. What
this suite CAN give at the desk is SOURCE-TEXT evidence that the retention
attributes are present in BOTH the Vivado spelling (``keep : string is "true"``)
AND the Quartus-correct spelling (``preserve ... true``) on both RTL tops — so
the first-landing defect (a Vivado-only KEEP that Quartus ignores) reds here.
That is necessary-but-not-sufficient for the selector to bind; the netlist count
still closes it (audit §5.2).

BASELINE (pre-fix, bare false paths): RED — no matching set_net_delay/set_max_skew.
MUTATION (delete the bound again): RED.  The green bar means the bound is present.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SDC = REPO_ROOT / "constraints" / "ddr_stream_cdc_scoped.sdc"
XDC = REPO_ROOT / "constraints" / "ddr_stream_cdc_scoped.xdc"

# The reset-request DESTINATION token that identifies this crossing class.
_RSTRQST = "RstRqst"


def _sdc_noncomment_lines() -> list[str]:
    assert SDC.is_file(), f"missing constraint file: {SDC}"
    out = []
    for line in SDC.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
    return out


def _sdc_logical_commands() -> list[str]:
    """Join backslash-continued lines, resolve `set _var [get_registers {..}]`
    aliases to their literal pattern, and return one string per Tcl command."""
    # Join line continuations first.
    raw = "\n".join(_sdc_noncomment_lines())
    raw = re.sub(r"\\\n\s*", " ", raw)

    # Collect variable -> get_registers-pattern aliases.
    var_pat: dict[str, str] = {}
    for m in re.finditer(
        r"set\s+(\S+)\s+\[get_registers\s+(?:-nowarn\s+)?\{([^}]*)\}\s*\]", raw
    ):
        var_pat[m.group(1)] = m.group(2).strip()

    commands = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("set ") or line in ("}", "{"):
            continue
        # Substitute $var references with the literal get_registers pattern so a
        # -to $var resolves to its RstRqst target for classification.
        def _sub(mo):
            name = mo.group(1)
            return f"{{{var_pat.get(name, name)}}}"

        commands.append(re.sub(r"\$(\w+)", _sub, line))
    return commands


def _endpoints(cmd: str) -> tuple[str | None, str | None]:
    """Extract the -from / -to selector text (brace body or bracket expr)."""

    def grab(flag: str) -> str | None:
        m = re.search(
            rf"-{flag}\s+(?:\[get_registers\s+(?:-nowarn\s+)?)?\{{([^}}]*)\}}", cmd
        )
        return m.group(1).strip() if m else None

    return grab("from"), grab("to")


def _reset_request_crossings():
    """Every distinct (from, to) endpoint pair whose -to targets RstRqst."""
    pairs = set()
    for cmd in _sdc_logical_commands():
        frm, to = _endpoints(cmd)
        if to and _RSTRQST in to:
            pairs.add((frm, to))
    return sorted(pairs)


def test_sdc_has_a_reset_request_crossing_at_all():
    """Sanity: the parser must find the reset-request crossing, else the guard
    below is vacuously green (the exact trap this suite is built to avoid)."""
    crossings = _reset_request_crossings()
    assert crossings, (
        "no RstRqst reset-request crossing parsed from the .sdc — either the "
        "constraint was removed (the crossing now ships UNCONSTRAINED) or this "
        "guard's parser drifted. Both are failures."
    )


def _commands_by_kind():
    fp, nd, sk = [], [], []
    for cmd in _sdc_logical_commands():
        frm, to = _endpoints(cmd)
        key = (frm, to)
        if cmd.startswith("set_false_path"):
            fp.append(key)
        elif cmd.startswith("set_net_delay"):
            nd.append(key)
        elif cmd.startswith("set_max_skew"):
            sk.append(key)
    return fp, nd, sk


@pytest.mark.parametrize("crossing", _reset_request_crossings())
def test_reset_request_crossing_is_false_pathed(crossing):
    """The async-preset recovery/removal must still be cut (the file's reason
    for existing) — a set_false_path over the crossing endpoints."""
    fp, _, _ = _commands_by_kind()
    assert crossing in fp, (
        f"reset-request crossing {crossing} is no longer false-pathed — the "
        f"async-preset recovery/removal report will resurface as a false STA "
        f"violation (DDR-P2.10)."
    )


@pytest.mark.parametrize("crossing", _reset_request_crossings())
def test_reset_request_crossing_carries_net_delay_bound(crossing):
    """DDR-T1.3 core guard: the false-pathed reset-request crossing must ALSO
    carry a set_net_delay -max over the SAME endpoints, so the async-assert
    route latency is bounded (not a bare cut)."""
    _, nd, _ = _commands_by_kind()
    assert crossing in nd, (
        f"reset-request crossing {crossing} is false-pathed but has NO matching "
        f"set_net_delay -max — this is the DDR-T1.3 defect: a bare false path "
        f"leaves the async-preset route UNBOUNDED (route-dependent reset "
        f"release). Add set_net_delay -max over these endpoints. This bound "
        f"stands on the owner's class table alone ('a reset request crossing "
        f"must carry a bound'), NOT on any XDC parity (that story was retracted)."
    )


@pytest.mark.parametrize("crossing", _reset_request_crossings())
def test_reset_request_crossing_carries_max_skew_bound(crossing):
    """RstRqst is a multi-bit vector asserted together; a set_max_skew keeps the
    3 bits coherent (assert within one window) so the release is deterministic."""
    _, _, sk = _commands_by_kind()
    assert crossing in sk, (
        f"reset-request crossing {crossing} has no set_max_skew — the 3-bit "
        f"RstRqst vector can assert with unbounded inter-bit skew. Add "
        f"set_max_skew over these endpoints (multi-bit coherence)."
    )


def test_xdc_reset_request_crossing_is_false_pathed():
    """The TRUE .xdc relationship (replaces the retracted parity claim): the
    genuine cross-domain reset-request event is the async PRE/CLR pin of
    RstRqst*_reg, and the .xdc handles it with a `set_false_path` (section B) —
    NOT with the section-A `set_max_delay -datapath_only`, which bounds those
    flops' intra-domain D-shift. So the .sdc's bare `set_false_path` was a
    faithful translation of XDC section B, and the .sdc's added net_delay/skew
    bound is a NEW constraint, not an XDC-parity restoration.

    This asserts the XDC's section-B false path over the RstRqst PRE/CLR pins
    still exists, so the corrected narrative in the audit doc stays true to the
    file. If the XDC ever moved RstRqst to a datapath bound, THIS test (and the
    doc) would need updating."""
    assert XDC.is_file(), f"missing constraint file: {XDC}"
    text = XDC.read_text(encoding="utf-8", errors="replace")
    live = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )
    # Section B: a set_false_path whose -to selects the RstRqst PRE/CLR pins.
    fp_blocks = re.findall(r"set_false_path\b.*?(?=\nset_|\Z)", live, re.S)
    rstrqst_preset_fp = [
        b for b in fp_blocks
        if "RstRqst" in b and ("PRE" in b or "CLR" in b)
    ]
    assert rstrqst_preset_fp, (
        "XDC section B no longer false-paths the RstRqst async PRE/CLR crossing "
        "(the genuine cross-domain reset-request event). The audit doc's "
        "corrected narrative — 'the .xdc false-paths the preset, it does not "
        "bound it' — no longer matches the file; update both."
    )


# --- Owner done-when (d), desk half: prove section-C's crossing selector binds.
#
# The section-C selector targets *_base_addr_sys -> *_base_addr_meta. On the
# deepskopion static-base build those regs can constant-fold and be removed
# (TMC-20026 empty collection) UNLESS the RTL pins them against removal.
#
# This is DESK-LEVEL evidence only: it proves the retention attribute TEXT is
# present in the RTL. It is NOT owner clause (d) (a netlist match-count on the
# real Quartus build — audit §5.2), and it does NOT prove the crossing binds on
# silicon. What it DOES catch is the defect the first landing shipped: pinning
# the registers with the Vivado `keep : string is "true"` spelling ALONE, which
# Quartus documents for combinational nets — not registers. The deepskopion
# consumer builds through Quartus, whose register-retention attribute is
# `preserve`. So this guard requires BOTH spellings on BOTH tops (AXI + AVMM,
# which share the shipped SDC): the Vivado `keep` (for the .xdc/Vivado path) AND
# the Quartus `preserve` (for the .sdc/Quartus path the consumer actually reads).

RTL_AXI = REPO_ROOT / "rtl" / "ddr_stream_axi.vhd"
RTL_AVMM = REPO_ROOT / "rtl" / "ddr_stream_avmm.vhd"

# The section-C crossing endpoints (launch + first-capture) that MUST survive.
_SECTION_C_KEEP_SIGNALS = [
    "wr_base_addr_sys",
    "rd_base_addr_sys",
    "wr_base_addr_meta",
    "rd_base_addr_meta",
]

# Both RTL tops serve the same shipped scoped SDC, so section C must bind on both.
_SECTION_C_TOPS = [RTL_AXI, RTL_AVMM]


@pytest.mark.parametrize("rtl", _SECTION_C_TOPS, ids=lambda p: p.name)
@pytest.mark.parametrize("sig", _SECTION_C_KEEP_SIGNALS)
def test_section_c_crossing_regs_are_keep_protected(sig, rtl):
    """Each section-C crossing endpoint carries the Vivado `attribute keep ...
    "true"` in the RTL, so a Vivado/Xilinx build cannot fold it away."""
    assert rtl.is_file(), f"missing RTL: {rtl}"
    text = rtl.read_text(encoding="utf-8", errors="replace")
    # Match `attribute keep of <sig> : signal is "true";` with flexible spacing.
    pat = re.compile(
        rf'attribute\s+keep\s+of\s+{re.escape(sig)}\s*:\s*signal\s+is\s+"true"\s*;',
        re.IGNORECASE,
    )
    assert pat.search(text), (
        f"section-C crossing endpoint '{sig}' has no `attribute keep ... "
        f'"true"` in {rtl.name}. On the deepskopion static-base '
        f"build it can constant-fold and be removed, so the section-C SDC "
        f"selector matches 0 registers (TMC-20026) — the silent 0-match the "
        f"owner's audit forbids. Restore the KEEP (DDR-T1.3 §4a)."
    )


@pytest.mark.parametrize("rtl", _SECTION_C_TOPS, ids=lambda p: p.name)
@pytest.mark.parametrize("sig", _SECTION_C_KEEP_SIGNALS)
def test_section_c_crossing_regs_are_preserve_protected(sig, rtl):
    """Each section-C crossing endpoint ALSO carries the Quartus-correct
    `attribute preserve ... true` register-retention attribute.

    This is the defect the first landing refusal named: `keep : string is
    "true"` is the Vivado spelling, which Quartus documents for combinational
    nets — not register retention. The deepskopion consumer builds through
    Quartus and reads the .sdc twin, so a Vivado-only KEEP is plausibly inert on
    the exact tool whose TMC-20026 (section-C empty collection) it must resolve.
    `preserve` is Quartus's register-retention attribute. (Note: this is source
    text, NOT the netlist match-count of owner clause (d), which stays a bench
    check — audit §5.2.)"""
    assert rtl.is_file(), f"missing RTL: {rtl}"
    text = rtl.read_text(encoding="utf-8", errors="replace")
    # Match `attribute preserve of <sig> : signal is true;` (boolean, no quotes).
    pat = re.compile(
        rf"attribute\s+preserve\s+of\s+{re.escape(sig)}\s*:\s*signal\s+is\s+true\s*;",
        re.IGNORECASE,
    )
    assert pat.search(text), (
        f"section-C crossing endpoint '{sig}' has no Quartus `attribute "
        f"preserve ... true` in {rtl.name}. The Vivado `keep : string` alone is "
        f"documented by Quartus for combinational NETS, not register retention, "
        f"so on the Quartus deepskopion build the section-C register can still "
        f"fold away (TMC-20026). Add `attribute preserve of {sig} : signal is "
        f"true;` (DDR-T1.3 §4a)."
    )


# --- DDR-T2.18: the SW write-pointer MCP CDC chain must survive Vivado pruning.
#
# When enable_write_plane_g=false the ring starts empty (wr_ptr=rd_ptr=0 =>
# DdrEmpty=1). Vivado constant-propagates DdrEmpty='1' and walks the circular
# DdrEmpty=1 -> burst-idle -> no FIFO write -> VALID=0 -> no observable change
# path back to "these pointer FFs have no fanout", removing wr_ptr_addr_sys /
# wr_ptr_addr_ddr / wr_ptr_tgl_ddr_d (Synth 8-6014, observed in
# top_kv260_h264.vds naming exactly these three registers). csr_wr_ptr_load_i is
# a live input port and a PS producer advances the pointer at runtime, so the
# chain is real; the KEEP attribute pins it. This feature exists ONLY on the AXI
# top (the AVMM/deepskopion top has no SW write pointer), so this guard is
# AXI-scoped. `preserve` is the Quartus-correct companion spelling (see
# DDR-T1.3, base_addr, above); keep-alone is the known-inert anti-pattern.
#
# DESK-LEVEL evidence only: proves the retention attribute TEXT is present in the
# RTL. The netlist match-count on a real Vivado build stays a bench check.

# The three SW write-pointer MCP chain registers Vivado removed (Synth 8-6014).
_WR_PTR_KEEP_SIGNALS = [
    "wr_ptr_addr_sys",
    "wr_ptr_addr_ddr",
    "wr_ptr_tgl_ddr_d",
]


@pytest.mark.parametrize("sig", _WR_PTR_KEEP_SIGNALS)
def test_wr_ptr_cdc_regs_are_keep_protected(sig):
    """Each SW write-pointer MCP chain register carries the Vivado `attribute
    keep ... "true"`, so a Vivado/Xilinx build (kv260) cannot prune it away as
    Synth 8-6014 when enable_write_plane_g=false (DDR-T2.18)."""
    assert RTL_AXI.is_file(), f"missing RTL: {RTL_AXI}"
    text = RTL_AXI.read_text(encoding="utf-8", errors="replace")
    pat = re.compile(
        rf'attribute\s+keep\s+of\s+{re.escape(sig)}\s*:\s*signal\s+is\s+"true"\s*;',
        re.IGNORECASE,
    )
    assert pat.search(text), (
        f"SW write-pointer MCP register '{sig}' has no `attribute keep ... "
        f'"true"` in {RTL_AXI.name}. When enable_write_plane_g=false Vivado '
        f"constant-propagates DdrEmpty='1' and removes it (Synth 8-6014), "
        f"silently deleting the software write pointer a PS producer drives. "
        f"Restore the KEEP (DDR-T2.18)."
    )


@pytest.mark.parametrize("sig", _WR_PTR_KEEP_SIGNALS)
def test_wr_ptr_cdc_regs_are_preserve_protected(sig):
    """Each SW write-pointer MCP chain register ALSO carries the Quartus-correct
    `attribute preserve ... true`, mirroring the DDR-T1.3 base_addr fix — the
    Vivado `keep : string` spelling alone is documented for combinational nets,
    not register retention (DDR-T2.18)."""
    assert RTL_AXI.is_file(), f"missing RTL: {RTL_AXI}"
    text = RTL_AXI.read_text(encoding="utf-8", errors="replace")
    pat = re.compile(
        rf"attribute\s+preserve\s+of\s+{re.escape(sig)}\s*:\s*signal\s+is\s+true\s*;",
        re.IGNORECASE,
    )
    assert pat.search(text), (
        f"SW write-pointer MCP register '{sig}' has no Quartus `attribute "
        f"preserve ... true` in {RTL_AXI.name}. Add it alongside the KEEP so the "
        f"chain survives register-retention on both toolchains (DDR-T2.18)."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

"""DDR-T1.1 drift guard for constraints/ddr_stream_cdc_scoped.xdc.

WHY THIS TEST EXISTS
--------------------
`ddr_stream_cdc_scoped.xdc` constrains every asynchronous clk_sys<->clk_ddr
crossing in `ddr_stream_axi` by naming the DESTINATION (capturing-clock)
synchroniser registers in its `set_max_delay -datapath_only -to` / `set_false_path`
selectors.  Those selectors are matched against the *synthesised netlist* at
build time — and Vivado does NOT error when a `-to` pattern matches zero cells.
It silently drops the exception, leaving a genuine async crossing UNCONSTRAINED
(false WNS at best, metastability on silicon at worst).

So a rename of any crossing register — by an `olo` library bump (Reg0 / RegN /
RstRqst / i_rst_cc) or by an `rtl/` refactor (i_ddr_fifo_intf / i_sync_cdc /
*_base_addr_meta / *_ptr_addr_ddr / stages_r) — would let the constraint rot
WITHOUT breaking the build.  This test makes that drift LOUD: it parses every
`-to` selector pattern out of the XDC and asserts each pattern's identifier
token(s) still appear in the IP's VHDL source closure.

THE ORIGINAL DDR-T1.1 FALSE ALARM
---------------------------------
The ticket reported the constraint "drifted" because `grep Reg0/RegN/RstRqst/
i_rst_cc rtl/` returned nothing.  That grep was incomplete: those names belong
to the vendored `olo` synchroniser primitives under **libs/open-logic/**, not
rtl/.  A routed `report_cdc` on both validators (xc7z020 + xc7z010) shows 114
crossing endpoints, 0 Unsafe, 0 Unknown — the constraint is sound.  Hence this
guard scans BOTH rtl/ AND libs/open-logic/, which is the search the original
report should have done.

This is a SOURCE-level structural guard (no simulator / no Vivado needed): it
proves each selector token is still a real RTL identifier.  The cell-level
"does it bind in the netlist" proof is the routed `report_cdc` recorded in the
XDC header and re-run by the parent's ring_demo timing close.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
XDC = REPO_ROOT / "constraints" / "ddr_stream_cdc_scoped.xdc"

# Source roots that make up the ddr_stream_axi synthesis closure. The olo
# synchroniser primitives (Reg0/RegN/RstRqst/i_rst_cc) live under libs/, NOT rtl/.
SOURCE_ROOTS = [
    REPO_ROOT / "rtl",
    REPO_ROOT / "libs" / "open-logic",
]

# Pull the NAME =~ "PATTERN" selectors out of the *real* constraint commands
# only — never the comment block (we strip comment lines first).
_NAME_SELECTOR = re.compile(r'NAME\s*=~\s*"([^"]+)"')

# Pure-Vivado/netlist artefacts that are NOT RTL identifiers — a selector
# fragment equal to one of these (after stripping) carries no drift signal, so
# we don't require it to appear in source.
_NETLIST_ARTEFACT_FRAGMENTS = {"reg", ""}


def _xdc_command_text() -> str:
    """The XDC with comment (#) lines removed, so we only mine live selectors."""
    assert XDC.is_file(), f"missing constraint file: {XDC}"
    keep = []
    for line in XDC.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        keep.append(line)
    return "\n".join(keep)


def _selector_patterns():
    """Every distinct NAME =~ "<glob>" pattern from the live XDC commands."""
    text = _xdc_command_text()
    pats = sorted(set(_NAME_SELECTOR.findall(text)))
    assert pats, "no NAME =~ selectors parsed from the XDC commands — parser drift?"
    return pats


def _pattern_tokens(glob_pat: str) -> list[str]:
    """Reduce a Vivado glob selector to its load-bearing RTL identifier tokens.

    A selector can carry MULTIPLE literal fragments separated by '*' wildcards,
    e.g. '*i_rst_cc*RstRqst*_reg*' -> instance 'i_rst_cc' AND signal 'RstRqst'.
    Both are real RTL identifiers a rename would change, so we return every
    meaningful fragment (stripping the synthesised '_reg' / '[idx]' netlist
    suffix and dropping pure-netlist artefacts like a bare 'reg').

    Examples:
      '*i_ddr_fifo_intf*Reg0_reg*'  -> ['i_ddr_fifo_intf', 'Reg0']
      '*i_sync_cdc_*stages_r_reg*'  -> ['i_sync_cdc', 'stages_r']
      '*rd_ptr_addr_ddr_reg*'       -> ['rd_ptr_addr_ddr']
      '*i_rst_cc*RstRqst*_reg*'     -> ['i_rst_cc', 'RstRqst']
      '*_base_addr_meta_reg*'       -> ['base_addr_meta']
    """
    tokens = []
    for frag in glob_pat.split("*"):
        # Strip the Vivado '_reg' netlist suffix (with optional bus index).
        frag = re.sub(r"_reg\[?\d*\]?$", "", frag)
        frag = frag.strip("_")
        if frag.lower() in _NETLIST_ARTEFACT_FRAGMENTS:
            continue
        tokens.append(frag)
    assert tokens, f"selector {glob_pat!r} reduced to no RTL tokens"
    return tokens


def _token_in_sources(token: str) -> list[str]:
    """Files under SOURCE_ROOTS whose text contains `token` (case-sensitive)."""
    hits = []
    for root in SOURCE_ROOTS:
        if not root.is_dir():
            continue
        for vhd in root.rglob("*.vhd"):
            try:
                body = vhd.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if token in body:
                hits.append(str(vhd.relative_to(REPO_ROOT)))
    return hits


def test_source_roots_exist():
    assert (REPO_ROOT / "rtl").is_dir(), "rtl/ source root missing"
    olo = REPO_ROOT / "libs" / "open-logic"
    assert olo.is_dir(), (
        "libs/open-logic/ missing — the olo synchroniser primitives "
        "(Reg0/RegN/RstRqst/i_rst_cc) are part of the CDC constraint closure; "
        "the guard cannot validate the XDC without them."
    )


@pytest.mark.parametrize("glob_pat", _selector_patterns())
def test_every_to_selector_token_exists_in_rtl(glob_pat):
    """Each XDC `-to` selector must still name live RTL identifier(s).

    Fails LOUD on drift: if an olo bump or rtl refactor renames a crossing
    register, the token disappears from the source closure and this asserts —
    catching the rot at test time instead of in unconstrained silicon.
    """
    for token in _pattern_tokens(glob_pat):
        hits = _token_in_sources(token)
        assert hits, (
            f"CDC constraint DRIFT: XDC selector {glob_pat!r} carries RTL token "
            f"{token!r}, which no longer appears in ANY VHDL source under rtl/ or "
            f"libs/open-logic/. The set_max_delay/set_false_path for this crossing "
            f"will match 0 cells and Vivado will SILENTLY drop it, leaving an async "
            f"clk_sys<->clk_ddr crossing UNCONSTRAINED (metastability hazard). "
            f"Re-derive constraints/ddr_stream_cdc_scoped.xdc from the current CDC "
            f"structure (see the DDR-T1.1 audit note in the XDC header)."
        )


def test_known_crossing_tokens_are_all_covered():
    """Belt-and-braces: the full set of crossing-destination tokens the audit
    enumerated must each be present, so a selector being DELETED (not just
    renamed) is also caught."""
    expected = {
        "Reg0",            # olo_base_cc_bits gray-pointer + ack sync (1st stage)
        "RegN",            # olo_base_cc_bits gray-pointer + ack sync (Nth stage)
        "RstRqst",         # olo_base_cc_reset request shift regs
        "RstAck",          # olo_base_cc_reset ack regs (false_path PRE/CLR)
        "i_rst_cc",        # olo_base_fifo_async reset-CC instance
        "i_ddr_fifo_intf", # the FIFO interface that hosts both olo FIFOs
        "base_addr_meta",  # base-address MCP capture (clk_ddr meta flop)
        "i_sync_cdc",      # rr.sync_cdc instances (busy/empty/full/err/ptr toggles)
        "stages_r",        # rr.sync_cdc destination shift register
        "rd_ptr_addr_ddr", # SW read-pointer MCP capture (live in real consumers)
        "wr_ptr_addr_ddr", # SW write-pointer MCP capture (live in real consumers)
    }
    absent = sorted(t for t in expected if not _token_in_sources(t))
    assert not absent, (
        f"CDC constraint DRIFT: crossing token(s) {absent} are no longer present "
        f"in rtl/ or libs/open-logic/. Either the RTL was refactored (re-derive "
        f"the XDC) or the audit token list here is stale."
    )


def test_selectors_cover_the_known_token_set():
    """The XDC's live selectors must between them mention every audited crossing
    token — guards against a selector LINE being dropped from the XDC."""
    selector_tokens = set()
    for pat in _selector_patterns():
        selector_tokens.update(_pattern_tokens(pat))
    required = {
        "i_ddr_fifo_intf", "Reg0", "RegN", "i_rst_cc", "RstRqst",
        "base_addr_meta", "i_sync_cdc", "stages_r",
        "rd_ptr_addr_ddr", "wr_ptr_addr_ddr",
    }
    missing = sorted(required - selector_tokens)
    assert not missing, (
        f"XDC no longer constrains crossing token(s) {missing} — a set_max_delay "
        f"/ set_false_path selector covering them was removed. Restore it or the "
        f"crossing ships unconstrained."
    )


# ---------------------------------------------------------------------------
# DDR-P2.10 — the Altera/Quartus SDC twin gets the same drift guard.
# constraints/ddr_stream_cdc_scoped.sdc names the SAME crossing registers via
# Quartus `get_registers {glob ...}` patterns; a rename would let it silently
# match 0 registers exactly like a Vivado selector (Quartus only warns).
# ---------------------------------------------------------------------------

SDC = REPO_ROOT / "constraints" / "ddr_stream_cdc_scoped.sdc"

# get_registers [-nowarn] {pat1 pat2 ...} — capture the brace-list body.
_SDC_GET_REGISTERS = re.compile(r"get_registers\s+(?:-nowarn\s+)?\{([^}]+)\}")


def _sdc_selector_patterns():
    """Every distinct glob pattern from live (non-comment) SDC commands."""
    assert SDC.is_file(), f"missing constraint file: {SDC}"
    keep = []
    for line in SDC.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        keep.append(line)
    pats: set[str] = set()
    for body in _SDC_GET_REGISTERS.findall("\n".join(keep)):
        pats.update(body.split())
    assert pats, "no get_registers patterns parsed from the SDC — parser drift?"
    return sorted(pats)


def _sdc_pattern_tokens(glob_pat: str) -> list[str]:
    """Reduce a Quartus glob to its load-bearing RTL identifier tokens.

    Quartus names carry bus indices (``Reg0[3]``) rather than Vivado's
    ``_reg`` suffix, and ``|`` hierarchy separators. Examples:
      '*i_ddr_fifo_intf*RegIn[*]'  -> ['i_ddr_fifo_intf', 'RegIn']
      '*i_sync_cdc_*stages_r[0]'   -> ['i_sync_cdc', 'stages_r']
      '*i_rst_cc*RstALatch'        -> ['i_rst_cc', 'RstALatch']
    """
    tokens = []
    for frag in glob_pat.split("*"):
        frag = re.sub(r"\[[^\]]*\]?$", "", frag)  # strip bus-index suffix
        frag = frag.strip("|[]_")
        if frag.lower() in _NETLIST_ARTEFACT_FRAGMENTS:
            continue
        tokens.append(frag)
    assert tokens, f"SDC selector {glob_pat!r} reduced to no RTL tokens"
    return tokens


@pytest.mark.parametrize("glob_pat", _sdc_selector_patterns())
def test_every_sdc_selector_token_exists_in_rtl(glob_pat):
    """Each SDC get_registers pattern must still name live RTL identifier(s)."""
    for token in _sdc_pattern_tokens(glob_pat):
        hits = _token_in_sources(token)
        assert hits, (
            f"CDC constraint DRIFT: SDC selector {glob_pat!r} carries RTL token "
            f"{token!r}, which no longer appears in ANY VHDL source under rtl/ "
            f"or libs/open-logic/. Quartus will match 0 registers and only WARN, "
            f"leaving an async clk_sys<->clk_ddr crossing falsely timed (or "
            f"unconstrained). Re-derive constraints/ddr_stream_cdc_scoped.sdc "
            f"alongside the XDC (see its header translation table)."
        )


def test_sdc_selectors_cover_the_known_token_set():
    """The SDC's live selectors must between them mention every audited
    crossing token — guards against a constraint block being dropped.
    (RegN is deliberately absent: its D input is same-domain — see the SDC
    header. RstRqst is targeted per-direction: RstRqstA2B / RstRqstB2A.)"""
    selector_tokens = set()
    for pat in _sdc_selector_patterns():
        selector_tokens.update(_sdc_pattern_tokens(pat))
    required = {
        "i_ddr_fifo_intf", "RegIn", "Reg0",
        "i_rst_cc", "RstALatch", "RstBLatch", "RstRqstA2B", "RstRqstB2A",
        "rd_base_addr_sys", "wr_base_addr_sys",
        "rd_base_addr_meta", "wr_base_addr_meta",
        "i_sync_cdc", "stages_r",
        "rd_ptr_addr_sys", "wr_ptr_addr_sys",
        "rd_ptr_addr_ddr", "wr_ptr_addr_ddr",
    }
    missing = sorted(required - selector_tokens)
    assert not missing, (
        f"SDC no longer constrains crossing token(s) {missing} — a "
        f"set_false_path/set_net_delay/set_max_skew block covering them was "
        f"removed from ddr_stream_cdc_scoped.sdc. Restore it or Quartus "
        f"consumers regain the DDR-P2.10 false STA violations."
    )


# ---------------------------------------------------------------------------
# DDR-P2.20 — the Microchip (Libero SmartTime) SDC twin + its packaging guard.
#
# constraints/ddr_stream_cdc_scoped_microchip.sdc is the PolarFire SoC dialect
# of the same scoped CDC constraints (ddr_stream_fic backend). It names the SAME
# crossing registers via Libero `get_cells {glob ...}` patterns. Two failure
# modes must be caught LOUD:
#
#   1. STALE / RENAMED SELECTORS — a rename lets a get_cells pattern match 0
#      cells; Libero only WARNS, leaving an async crossing falsely timed exactly
#      like the Quartus/Vivado case. (Token-in-RTL guard, olo-dependent, below.)
#
#   2. CDC FAIL-OPEN ON THE PACKAGED VALIDATOR (the DDR-T2.20 class) — the
#      example/pfsoc_disco_ddr_validator project.yml names the SmartTime SDC in
#      its sources.sdc, but the file is MISSING from the tree (or the vendored
#      copy silently drifts from the IP canonical). The Microchip build then
#      proceeds with NO scoped CDC constraints and the metastability ships. The
#      TestMicrochipCdcPackaging class below asserts, at the surface the build
#      actually reaches, that the named file RESOLVES and is byte-identical to
#      the IP canonical — no fail-open, no silent staleness.
# ---------------------------------------------------------------------------

MICROCHIP_SDC = REPO_ROOT / "constraints" / "ddr_stream_cdc_scoped_microchip.sdc"
PFSOC_VALIDATOR = REPO_ROOT / "example" / "pfsoc_disco_ddr_validator"
PFSOC_PROJECT_YML = PFSOC_VALIDATOR / "project.yml"

# Libero SmartTime selector: get_cells [-hier] { pat1 pat2 ... } — but only
# BRACED lists carry per-crossing token globs; the bare launch-side wildcard
# (`get_cells -hierarchical {*}` on the -from side, or the older unbraced
# `get_cells *`) is intentionally NOT captured — it names no crossing.
_MICROCHIP_GET_CELLS = re.compile(r"get_cells\s+(?:-hier(?:archical)?\s+)?\{([^}]+)\}")

# The vendored path this validator's sources.sdc MUST name (the fail-open guard
# pins THIS file, not merely "some resolvable file").
_MICROCHIP_SDC_REF = "constraints/ddr_stream_cdc_scoped_microchip.sdc"

# The full set of crossing DESTINATION tokens the SmartTime twin must name — the
# same audited crossings as the XDC/Quartus SDC, expressed in the Synopsys
# destination-cell idiom (RegN IS named here: SmartTime set_max_delay bounds the
# whole synchroniser without cutting the settling stages). A selector block
# being dropped is caught here.
_MICROCHIP_REQUIRED_TOKENS = {
    "i_ddr_fifo_intf", "Reg0", "RegN",
    "i_rst_cc", "RstRqst", "RstAck",
    "base_addr_meta",
    "i_sync_cdc", "stages_r",
    "rd_ptr_addr_ddr", "wr_ptr_addr_ddr",
}

# Crossing classes the SmartTime dialect must treat DIFFERENTLY (DDR-T1.3):
#   * multi-bit data/MCP crossings carry a BOUND (set_max_delay) and must NEVER
#     be a bare set_false_path (an unbounded route/skew is a defect);
#   * single-bit level/toggle syncs and the async-reset assertion are cut
#     (set_false_path) — a 1-bit async capture / async-preset carries no
#     datapath bound.
_MICROCHIP_BOUNDED_DATA_TOKENS = {
    "Reg0", "RegN", "RstRqst", "base_addr_meta",
    "rd_ptr_addr_ddr", "wr_ptr_addr_ddr",
}
_MICROCHIP_CUT_TOKENS = {"stages_r", "RstAck"}


def _microchip_sdc_command_text() -> str:
    """The Microchip SDC with comment (#) lines stripped."""
    keep = []
    for line in MICROCHIP_SDC.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        keep.append(line)
    return "\n".join(keep)


def _microchip_selector_patterns() -> list[str]:
    """Every distinct braced glob from live (non-comment) Microchip SDC commands.

    The bare launch-side wildcard `{*}` (the `-from` of every set_max_delay) is
    dropped: it names no crossing and reduces to no RTL token, exactly like the
    unbraced `get_cells *` it replaced."""
    pats: set[str] = set()
    for body in _MICROCHIP_GET_CELLS.findall(_microchip_sdc_command_text()):
        for pat in body.split():
            if set(pat) <= {"*"}:  # bare wildcard, no crossing token
                continue
            pats.add(pat)
    return sorted(pats)


def _microchip_commands() -> list[tuple[str, list[str]]]:
    """Parse each live Microchip SDC command to (verb, destination-tokens).

    Commands are backslash-continued across lines; the verb is the leading word
    (set_max_delay / set_false_path) and the destination tokens come from the
    braced `-to [get_cells ... {PAT}]` selector. Lets a test assert that the
    data/MCP crossings are BOUNDED (set_max_delay) while the 1-bit sync / reset
    crossings are CUT (set_false_path) — token presence alone cannot tell a bare
    cut of a bounded crossing (the DDR-T1.3 defect) from the correct bound."""
    joined = re.sub(r"\\\s*\n\s*", " ", _microchip_sdc_command_text())
    commands: list[tuple[str, list[str]]] = []
    for line in joined.splitlines():
        line = line.strip()
        if not line.startswith("set_"):
            continue
        verb = line.split()[0]
        m = re.search(
            r"-to\s+\[get_cells\s+(?:-hier(?:archical)?\s+)?\{([^}]+)\}", line
        )
        if not m:
            continue
        tokens: list[str] = []
        for pat in m.group(1).split():
            if set(pat) <= {"*"}:
                continue
            tokens.extend(_sdc_pattern_tokens(pat))
        commands.append((verb, tokens))
    return commands


def _microchip_selector_tokens() -> set[str]:
    """Union of the load-bearing RTL tokens across every Microchip selector.

    Libero cell names carry `/` hierarchy separators and `[idx]` bus suffixes;
    _sdc_pattern_tokens already strips those (it is dialect-neutral for the
    fragment shapes both Quartus and Libero produce)."""
    tokens: set[str] = set()
    for pat in _microchip_selector_patterns():
        tokens.update(_sdc_pattern_tokens(pat))
    return tokens


class TestMicrochipCdcPackaging:
    """DDR-P2.20 packaging invariant — olo-INDEPENDENT (no libs/ install needed),
    so it runs green in a bare clone / the test-power runner. Guards the effect a
    Microchip build actually reaches: the named SmartTime SDC exists, still names
    every crossing, resolves from the validator's project.yml, and has not
    silently drifted from the IP canonical."""

    def test_microchip_sdc_file_exists(self):
        assert MICROCHIP_SDC.is_file(), (
            f"CDC FAIL-OPEN: the Microchip SmartTime twin {MICROCHIP_SDC} is "
            f"MISSING. ddr_stream_fic (PolarFire SoC) consumers then build with "
            f"NO scoped CDC constraints and ship the DDR-P2.10 metastability."
        )

    def test_microchip_sdc_selectors_cover_the_known_token_set(self):
        """The SmartTime SDC's live get_cells selectors must between them mention
        every audited crossing token — a dropped set_false_path/set_max_delay
        block is caught even though Libero would only warn on 0 matches."""
        assert MICROCHIP_SDC.is_file(), (
            f"CDC FAIL-OPEN: Microchip SmartTime twin {MICROCHIP_SDC} is missing."
        )
        selector_tokens = _microchip_selector_tokens()
        assert selector_tokens, (
            "no get_cells selectors parsed from the Microchip SDC — parser drift "
            "or an empty/renamed constraint file?"
        )
        missing = sorted(_MICROCHIP_REQUIRED_TOKENS - selector_tokens)
        assert not missing, (
            f"Microchip SDC no longer constrains crossing token(s) {missing} — a "
            f"set_false_path/set_max_delay block covering them was removed from "
            f"ddr_stream_cdc_scoped_microchip.sdc. Restore it or PolarFire SoC "
            f"consumers regain the DDR-P2.10 false SmartTime CDC violations."
        )

    def test_microchip_data_crossings_are_bounded_not_bare_cut(self):
        """DDR-T1.3 class rule: a multi-bit data/MCP crossing must carry a BOUND
        (SmartTime set_max_delay); a bare set_false_path on it is a defect
        (unbounded route latency + inter-bit skew). Single-bit level/toggle syncs
        and the async-reset assertion are the ONLY crossings that may be cut
        (set_false_path). Token presence alone cannot catch a bare cut of a
        bounded crossing, so this test reads the actual command verb per
        destination: deleting every set_max_delay, or swapping one for
        set_false_path, goes RED here even though the tokens still appear."""
        assert MICROCHIP_SDC.is_file(), (
            f"CDC FAIL-OPEN: Microchip SmartTime twin {MICROCHIP_SDC} is missing."
        )
        bounded: set[str] = set()   # tokens reached by a set_max_delay -to
        cut: set[str] = set()       # tokens reached by a set_false_path -to
        for verb, to_tokens in _microchip_commands():
            if verb == "set_max_delay":
                bounded.update(to_tokens)
            elif verb == "set_false_path":
                cut.update(to_tokens)
        for tok in sorted(_MICROCHIP_BOUNDED_DATA_TOKENS):
            assert tok in bounded, (
                f"Microchip SDC no longer BOUNDS the multi-bit crossing {tok!r} "
                f"with set_max_delay — a set_max_delay block was removed. Restore "
                f"it or the crossing ships an UNCONSTRAINED datapath (DDR-T1.3: a "
                f"crossing must carry a bound)."
            )
            assert tok not in cut, (
                f"Microchip SDC applies set_false_path to the multi-bit crossing "
                f"{tok!r} — a BARE CUT of a bounded data/MCP crossing. On a "
                f"Synopsys-style analyzer the false_path wins and the datapath "
                f"latency/skew go unbounded (the DDR-T1.3 defect). Bound it with "
                f"set_max_delay instead."
            )
        for tok in sorted(_MICROCHIP_CUT_TOKENS):
            assert tok in cut, (
                f"Microchip SDC no longer CUTS the single-bit sync / async-reset "
                f"crossing {tok!r} with set_false_path — restore it (a 1-bit async "
                f"capture / async-preset carries no datapath bound and must be "
                f"false-pathed, not timed)."
            )

    def test_pfsoc_validator_constraint_path_resolves_no_fail_open(self):
        """THE fail-open guard (DDR-T2.20 class): example/pfsoc_disco_ddr_validator
        names a scoped CDC SDC in its project.yml sources.sdc; every named file
        MUST resolve to a real file on disk. A named-but-missing constraint is a
        SILENT fail-open — the Microchip build proceeds unconstrained."""
        assert PFSOC_PROJECT_YML.is_file(), (
            f"CDC FAIL-OPEN: {PFSOC_PROJECT_YML} is missing — the PolarFire SoC "
            f"validator that packages the scoped CDC SDC does not exist, so the "
            f"DDR-P2.20 constraint-packaging invariant is unguarded."
        )
        doc = yaml.safe_load(PFSOC_PROJECT_YML.read_text(encoding="utf-8")) or {}
        sdc_refs = (((doc.get("sources") or {}).get("sdc")) or [])
        assert sdc_refs, (
            f"CDC FAIL-OPEN: {PFSOC_PROJECT_YML} declares no sources.sdc — the "
            f"PolarFire SoC validator ships NO scoped CDC constraint, so "
            f"ddr_stream_fic times its async crossings falsely on SmartTime."
        )
        # Pin the NAMED CDC file, not merely "some file resolves": pointing
        # sources.sdc at any other existing file would otherwise leave the
        # scoped Microchip CDC constraint absent while the guard stayed green.
        assert _MICROCHIP_SDC_REF in sdc_refs, (
            f"CDC FAIL-OPEN: pfsoc_disco_ddr_validator sources.sdc {sdc_refs!r} "
            f"does not name the scoped Microchip CDC constraint "
            f"{_MICROCHIP_SDC_REF!r}. ddr_stream_fic then builds without the "
            f"SmartTime CDC bounds even if the other listed files resolve."
        )
        for ref in sdc_refs:
            resolved = (PFSOC_PROJECT_YML.parent / ref).resolve()
            assert resolved.is_file(), (
                f"CDC FAIL-OPEN: pfsoc_disco_ddr_validator project.yml names "
                f"constraint {ref!r} but it does NOT resolve to a file on disk "
                f"({resolved}). rr would build with that constraint silently "
                f"dropped — an UNCONSTRAINED clk_sys<->clk_ddr crossing on "
                f"silicon. Vendor the file or fix the path."
            )

    def test_pfsoc_vendored_microchip_sdc_matches_canonical(self):
        """Staleness guard: the copy vendored into the validator must be
        byte-identical to the IP canonical constraints/ddr_stream_cdc_scoped_
        microchip.sdc, so the twin cannot silently drift out of sync when the
        canonical is re-derived (the vendored copy going stale is as dangerous
        as it being missing — it would apply OUTDATED selectors)."""
        vendored = PFSOC_VALIDATOR / "constraints" / "ddr_stream_cdc_scoped_microchip.sdc"
        assert vendored.is_file(), (
            f"CDC FAIL-OPEN: the validator's vendored SmartTime SDC {vendored} is "
            f"missing even though project.yml names it."
        )
        assert MICROCHIP_SDC.is_file(), (
            f"CDC FAIL-OPEN: IP canonical {MICROCHIP_SDC} is missing."
        )
        canonical_text = MICROCHIP_SDC.read_text(encoding="utf-8")
        vendored_text = vendored.read_text(encoding="utf-8")
        assert vendored_text == canonical_text, (
            "CDC constraint STALENESS: the SmartTime SDC vendored into "
            "example/pfsoc_disco_ddr_validator has DRIFTED from the IP canonical "
            "constraints/ddr_stream_cdc_scoped_microchip.sdc. Re-vendor it (copy "
            "the canonical over the stale copy) so the validator does not apply "
            "outdated CDC selectors."
        )

    def test_pfsoc_validator_targets_microchip_vendor(self):
        """The SmartTime dialect only makes sense for a Microchip build; guard
        that the validator that packages it actually declares vendor: microchip
        (a vendor flip would apply Libero-only get_cells syntax to the wrong
        toolchain)."""
        assert PFSOC_PROJECT_YML.is_file(), (
            f"CDC FAIL-OPEN: {PFSOC_PROJECT_YML} is missing."
        )
        doc = yaml.safe_load(PFSOC_PROJECT_YML.read_text(encoding="utf-8")) or {}
        vendor = ((doc.get("hardware") or {}).get("vendor"))
        assert vendor == "microchip", (
            f"pfsoc_disco_ddr_validator hardware.vendor is {vendor!r}, not "
            f"'microchip' — the SmartTime-dialect SDC it vendors would be applied "
            f"to the wrong toolchain."
        )


def test_every_microchip_sdc_selector_token_exists_in_rtl():
    """Each Microchip SDC get_cells pattern must still name live RTL identifier(s).

    olo-DEPENDENT (needs libs/open-logic/ populated, like the XDC/Quartus token
    guards above) — it is a plain loop, NOT a module-level parametrize, so a
    MISSING Microchip SDC never breaks collection; it fails cleanly instead."""
    assert MICROCHIP_SDC.is_file(), (
        f"CDC FAIL-OPEN: Microchip SmartTime twin {MICROCHIP_SDC} is missing."
    )
    for glob_pat in _microchip_selector_patterns():
        for token in _sdc_pattern_tokens(glob_pat):
            hits = _token_in_sources(token)
            assert hits, (
                f"CDC constraint DRIFT: Microchip SDC selector {glob_pat!r} carries "
                f"RTL token {token!r}, which no longer appears in ANY VHDL source "
                f"under rtl/ or libs/open-logic/. Libero will match 0 cells and only "
                f"WARN, leaving an async clk_sys<->clk_ddr crossing falsely timed. "
                f"Re-derive constraints/ddr_stream_cdc_scoped_microchip.sdc "
                f"alongside the XDC/SDC (see its header translation table)."
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

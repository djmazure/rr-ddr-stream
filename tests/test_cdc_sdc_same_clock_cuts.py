"""DDR-P2.23: scoped CDC cuts are conditional on distinct clocks, and the
read-plane filters are not coupled to the write plane.

WHY THIS TEST EXISTS
--------------------
constraints/ddr_stream_cdc_scoped.sdc is sourced by every Quartus consumer,
including Quartus Prime Standard 23.1 (a downstream integration) and the DE25 Agilex-5
validator, which ties clk_sys and clk_ddr to CLOCK2_50.

Two defects (DS-P1.72 / DS-T1.20):

1. An unconditional set_false_path between those endpoints cuts a same-clock
   path. Design Assistant reports CDC-50101 Intra-Clock False Path
   Synchronizer (100 of them on the DE25 datalogger).
2. A combined ``{*rd_base_addr_*[*] *wr_base_addr_*[*]}`` filter matches no
   read-plane register on a write-only build. Quartus reports that empty
   sub-pattern even when the write half binds.

The clock test is not allowed to call ``get_clocks -of`` or
``get_clocks -of_objects``. Standard 23.1's get_clocks accepts only a name
filter; both of those options are a second positional argument and abort
read_sdc. The legal query, verified inside read_sdc on 23.1std, is
``get_fanins -clock``.

This module evaluates the SDC under tclsh with a TimeQuest stand-in. That is
the surface a Quartus read_sdc reaches: the commands the file actually issues,
not a Python helper. The DE25 fit/STA witness that the stand-in cannot provide
is docs/internal/DDR_P2_23_BENCH_STA_WITNESS.md.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SDC = REPO_ROOT / "constraints" / "ddr_stream_cdc_scoped.sdc"

_TCL = r"""
set ::mode {MODE}
set ::plane {PLANE}
set ::cuts [list]
set ::net_delays [list]
set ::max_skews [list]
set ::post_messages [list]

proc get_registers {args} {
    set pat [lindex $args end]
    # Chip-wide collection. Section D must not use this: on a one-fabric-clock
    # DE25 it still contains JTAG/HPS flops. distinct_clocks keeps both fabric
    # domains so a reverted section D still applies its cut and the plane-filter
    # cut count does not move. same_clock stays a single node so the same-clock
    # guard test stays green when only section D is reverted.
    if {$pat eq "*"} {
        if {$::mode eq "mixed_unrelated"} {
            return [list "reg:*:0:CLOCK2_50" "reg:*:jtag:altera_reserved_tck"]
        }
        if {$::mode eq "same_clock"} {
            return [list "reg:*:0:CLOCK2_50" "reg:*:1:CLOCK2_50"]
        }
        return [list "reg:*:0:clk_sys" "reg:*:1:clk_ddr"]
    }
    if {$::plane eq "write_only" && [string match "*rd_*" $pat] && ![string match "*wr_*" $pat]} {
        return [list]
    }
    if {$::plane eq "read_only" && [string match "*wr_*" $pat] && ![string match "*rd_*" $pat]} {
        return [list]
    }
    if {$::mode eq "same_clock" || $::mode eq "mixed_unrelated"} {
        set clk CLOCK2_50
    } elseif {[string match "*_sys*" $pat] || [string match "*RegIn*" $pat] || [string match "*Latch*" $pat]} {
        set clk clk_sys
    } else {
        set clk clk_ddr
    }
    return [list "reg:${pat}:0:${clk}" "reg:${pat}:1:${clk}"]
}

proc get_fanins {args} {
    set col [lindex $args end]
    if {[lsearch -exact $args -synch] >= 0} {
        # Synchronous fan-in of stages_r[0] is the d_i launch flop, not JTAG.
        if {$::mode eq "same_clock" || $::mode eq "mixed_unrelated"} {
            return [list "reg:sync_launch:0:CLOCK2_50"]
        }
        return [list "reg:sync_launch:0:clk_sys"]
    }
    set nodes [list]
    foreach reg $col {
        set clk [lindex [split $reg :] end]
        set node "node:${clk}"
        if {[lsearch -exact $nodes $node] < 0} {
            lappend nodes $node
        }
    }
    return $nodes
}

# Prime Pro spelling. A correct SDC never calls this. If a mutant does, and
# it is a drop-in for get_fanins -clock, behavior stays the same so the
# structural guard is what goes red.
proc get_clocks {args} {
    set col [lindex $args end]
    if {[lsearch -exact $args -of] >= 0 || [lsearch -exact $args -of_objects] >= 0} {
        return [get_fanins -clock $col]
    }
    return [list]
}

proc get_node_info {args} {
    set id [lindex $args end]
    return [lindex [split $id :] end]
}

proc get_collection_size {col} {
    return [llength $col]
}

proc foreach_in_collection {var col body} {
    upvar 1 $var item
    foreach item $col {
        uplevel 1 $body
    }
}

proc set_false_path {args} {
    lappend ::cuts $args
}

proc set_net_delay {args} {
    lappend ::net_delays $args
}

proc set_max_skew {args} {
    lappend ::max_skews $args
}

proc post_message {args} {
    lappend ::post_messages $args
}

source "SDC_PATH"

set same_from [list "reg:probe:0:CLOCK2_50"]
set same_to [list "reg:probe:1:CLOCK2_50"]
set diff_from [list "reg:probe:0:clk_sys"]
set diff_to [list "reg:probe:0:clk_ddr"]

# Section D's -to collection is stages_r[0]. A chip-wide from-set on a
# one-fabric-clock part still contains JTAG (altera_reserved_tck), so the
# distinct-clock guard keeps the cut and CDC-50101 stays.
set section_d_unrelated 0
foreach c $::cuts {
    set to_idx [lsearch -exact $c -to]
    set from_idx [lsearch -exact $c -from]
    if {$to_idx < 0 || $from_idx < 0} {
        continue
    }
    set to_val [lindex $c [expr {$to_idx + 1}]]
    set from_val [lindex $c [expr {$from_idx + 1}]]
    if {[string match *stages_r* $to_val] && [string match *altera_reserved_tck* $from_val]} {
        set section_d_unrelated 1
    }
}
set chipwide [get_registers *]
set chipwide_unrelated [string match *altera_reserved_tck* $chipwide]
set synch [get_fanins -synch [get_registers {*i_sync_cdc_*stages_r[0]}]]
set synch_unrelated [string match *altera_reserved_tck* $synch]

puts "CUTS_COUNT=[llength $::cuts]"
puts "NET_DELAY_COUNT=[llength $::net_delays]"
puts "MAX_SKEW_COUNT=[llength $::max_skews]"
puts "POST_MSG_COUNT=[llength $::post_messages]"
puts "TEST_SAME_DISTINCT=[_rr_ds_are_clocks_distinct $same_from $same_to]"
puts "TEST_DIFF_DISTINCT=[_rr_ds_are_clocks_distinct $diff_from $diff_to]"
puts "SECTION_D_UNRELATED=$section_d_unrelated"
puts "CHIPWIDE_HAS_UNRELATED=$chipwide_unrelated"
puts "SYNCH_FANIN_HAS_UNRELATED=$synch_unrelated"
"""


def _sdc_text() -> str:
    assert SDC.is_file(), f"missing constraint file: {SDC}"
    lines = []
    for line in SDC.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _unconditional_false_paths() -> list[str]:
    text = _sdc_text()
    stack: list[str] = []
    i = 0
    n = len(text)
    pending: str | None = None
    found: list[str] = []
    while i < n:
        if text.startswith("if ", i) or text.startswith("if{", i):
            j = i + 2
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] == "{":
                depth = 1
                start = j + 1
                j += 1
                while j < n and depth:
                    if text[j] == "{":
                        depth += 1
                    elif text[j] == "}":
                        depth -= 1
                    j += 1
                pending = text[start : j - 1]
                i = j
                continue
        if text[i] == "{":
            stack.append(pending or "")
            pending = None
            i += 1
            continue
        if text[i] == "}":
            if stack:
                stack.pop()
            i += 1
            continue
        if text.startswith("set_false_path", i):
            end = i
            while end < n and text[end] not in "\n;":
                end += 1
            if not any("_rr_ds_are_clocks_distinct" in frame for frame in stack):
                found.append(text[i:end].strip())
            i = end
            continue
        i += 1
    return found


def _coupled_plane_selectors() -> list[str]:
    coupled = []
    for body in re.findall(r"get_registers\s+(?:-nowarn\s+)?\{([^}]+)\}", _sdc_text()):
        tokens = body.split()
        rd_base = any("rd_base_addr" in t for t in tokens)
        wr_base = any("wr_base_addr" in t for t in tokens)
        rd_ptr = any("rd_ptr_addr" in t for t in tokens)
        wr_ptr = any("wr_ptr_addr" in t for t in tokens)
        if (rd_base and wr_base) or (rd_ptr and wr_ptr):
            coupled.append(body.strip())
    return coupled


def _run_sdc(mode: str, plane: str = "both") -> dict[str, int]:
    tclsh = shutil.which("tclsh")
    assert tclsh, "tclsh is required to evaluate the SDC the way read_sdc does"
    script = (
        _TCL.replace("MODE", mode)
        .replace("PLANE", plane)
        .replace("SDC_PATH", str(SDC))
    )
    proc = subprocess.run(
        [tclsh], input=script, text=True, capture_output=True, check=False
    )
    assert proc.returncode == 0, (
        "CDC_SDC_UNCONDITIONAL_CUT_REFUSED: sourcing ddr_stream_cdc_scoped.sdc "
        f"under the TimeQuest stand-in failed:\n{proc.stdout}\n{proc.stderr}"
    )
    out: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key] = int(value)
    return out


def test_clock_query_is_quartus_standard_legal():
    """Standard 23.1 rejects get_clocks -of and get_clocks -of_objects.

    The distinct-clock decision has to come from get_fanins -clock, which
    read_sdc accepts on that toolchain.
    """
    text = _sdc_text()
    illegal = "-of_objects" in text or re.search(r"get_clocks\s+-of\b", text)
    missing = "get_fanins -clock" not in text
    assert not illegal and not missing, (
        "CDC_SDC_PRO_ONLY_CLOCK_QUERY_REFUSED: ddr_stream_cdc_scoped.sdc must "
        "decide clock identity with get_fanins -clock. Quartus Standard 23.1 "
        "rejects get_clocks -of and get_clocks -of_objects "
        f"(illegal={illegal}, fanin_present={not missing})."
    )


def test_false_paths_skipped_when_clocks_match():
    """Same clock source => zero set_false_path. Distinct clocks => the cuts apply.

    Bounds (set_net_delay / set_max_skew) stay in both modes.
    """
    text = _sdc_text()
    assert "proc _rr_ds_are_clocks_distinct" in text, (
        "CDC_SDC_UNCONDITIONAL_CUT_REFUSED: _rr_ds_are_clocks_distinct is missing."
    )
    unconditional = _unconditional_false_paths()
    assert not unconditional, (
        "CDC_SDC_UNCONDITIONAL_CUT_REFUSED: unconditional set_false_path "
        f"cuts remain: {unconditional!r}."
    )

    same = _run_sdc("same_clock")
    assert same["CUTS_COUNT"] == 0, (
        "CDC_SDC_UNCONDITIONAL_CUT_REFUSED: same-clock evaluation applied "
        f"{same['CUTS_COUNT']} set_false_path cuts. Those are CDC-50101 "
        "intra-clock false-path synchronizers on a DE25 CLOCK2_50 tie."
    )
    assert same["TEST_SAME_DISTINCT"] == 0, (
        "CDC_SDC_UNCONDITIONAL_CUT_REFUSED: _rr_ds_are_clocks_distinct returned "
        "1 for one shared clock source."
    )
    assert same["NET_DELAY_COUNT"] > 0, (
        "CDC_SDC_UNCONDITIONAL_CUT_REFUSED: set_net_delay bounds disappeared "
        "in same-clock mode."
    )
    assert same["MAX_SKEW_COUNT"] > 0, (
        "CDC_SDC_UNCONDITIONAL_CUT_REFUSED: set_max_skew bounds disappeared "
        "in same-clock mode."
    )

    distinct = _run_sdc("distinct_clocks")
    assert distinct["CUTS_COUNT"] > 0, (
        "CDC_SDC_UNCONDITIONAL_CUT_REFUSED: distinct-clock evaluation applied "
        f"{distinct['CUTS_COUNT']} cuts. A real clk_sys/clk_ddr split must still "
        "false-path the crossings."
    )
    assert distinct["TEST_DIFF_DISTINCT"] == 1, (
        "CDC_SDC_UNCONDITIONAL_CUT_REFUSED: _rr_ds_are_clocks_distinct returned "
        "0 for two clock sources."
    )


def test_read_and_write_plane_filters_are_separate():
    """An absent read plane must not be a sub-pattern of the write filter."""
    coupled = _coupled_plane_selectors()
    assert not coupled, (
        "CDC_SDC_COUPLED_PLANE_SELECTORS_REFUSED: read and write selectors are "
        f"still combined: {coupled!r}."
    )
    text = _sdc_text()
    for var in (
        "_rr_ds_wr_base_to",
        "_rr_ds_rd_base_to",
        "_rr_ds_wr_ptr_to",
        "_rr_ds_rd_ptr_to",
    ):
        assert re.search(rf"get_collection_size\s+\${var}\s*\]\s*>\s*0", text), (
            "CDC_SDC_COUPLED_PLANE_SELECTORS_REFUSED: missing independent "
            f"get_collection_size guard for ${var}."
        )

    write_only = _run_sdc("distinct_clocks", "write_only")
    assert write_only["POST_MSG_COUNT"] == 2, (
        "CDC_SDC_COUPLED_PLANE_SELECTORS_REFUSED: write-only evaluation emitted "
        f"{write_only['POST_MSG_COUNT']} absent-plane warnings, expected 2 "
        "(read base and read pointer)."
    )
    assert write_only["CUTS_COUNT"] == 6, (
        "CDC_SDC_COUPLED_PLANE_SELECTORS_REFUSED: write-only evaluation applied "
        f"{write_only['CUTS_COUNT']} cuts, expected 6 (read-plane cuts omitted)."
    )

    read_only = _run_sdc("distinct_clocks", "read_only")
    assert read_only["POST_MSG_COUNT"] == 2, (
        "CDC_SDC_COUPLED_PLANE_SELECTORS_REFUSED: read-only evaluation emitted "
        f"{read_only['POST_MSG_COUNT']} absent-plane warnings, expected 2 "
        "(write base and write pointer)."
    )
    assert read_only["CUTS_COUNT"] == 6, (
        "CDC_SDC_COUPLED_PLANE_SELECTORS_REFUSED: read-only evaluation applied "
        f"{read_only['CUTS_COUNT']} cuts, expected 6 (write-plane cuts omitted)."
    )


def test_section_d_launch_set_excludes_unrelated_clocks():
    """Section D's from-set is the synchronous fan-in of stages_r[0].

    A one-fabric-clock DE25 still has JTAG/HPS registers. ``get_registers *``
    puts those clocks in the launch set, the distinct-clock guard sees more
    than one node, and the intra-clock stages_r[0] cut stays (CDC-50101).
    ``get_fanins -synch`` returns only the d_i launch flop, which shares
    CLOCK2_50 with the capture flop, so the cut is skipped.

    The stand-in proves the hazard is real before the assertion: the chip-wide
    collection contains altera_reserved_tck and the synchronous fan-in does not.
    """
    mixed = _run_sdc("mixed_unrelated")
    assert mixed["CHIPWIDE_HAS_UNRELATED"] == 1, (
        "CDC_SDC_CHIP_WIDE_SYNC_FROM_REFUSED: positive control failed. "
        "The chip-wide register set does not include an unrelated clock, so "
        "a section D revert to get_registers * could not be observed."
    )
    assert mixed["SYNCH_FANIN_HAS_UNRELATED"] == 0, (
        "CDC_SDC_CHIP_WIDE_SYNC_FROM_REFUSED: positive control failed. "
        "get_fanins -synch included the unrelated clock, so it is not a "
        "narrower launch set than get_registers *."
    )
    assert mixed["SECTION_D_UNRELATED"] == 0, (
        "CDC_SDC_CHIP_WIDE_SYNC_FROM_REFUSED: section D applied set_false_path "
        "from a chip-wide launch set that includes an unrelated clock "
        "(altera_reserved_tck). The launch set must be get_fanins -synch of "
        "stages_r[0], not get_registers *."
    )

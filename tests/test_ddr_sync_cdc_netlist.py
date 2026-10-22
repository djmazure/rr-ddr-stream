# SPDX-License-Identifier: MIT
"""DDR-P2.25 regression guard: ddr_sync_cdc reset value specialization and caller wiring.

WHY THIS TEST EXISTS
--------------------
Vivado report_cdc on Zybo datalogger builds reported three ddr_sync_cdc instances
as "CDC-1 Critical: 1-bit unknown CDC circuitry (depth 0)" at stage 0:
  1. i_sync_cdc_empty in rtl/ddr_stream_axi.vhd
  2. i_sync_cdc_empty under GEN_UPLOADER_BLOCK in rtl/ddr_fifo_intf.vhd
  3. i_sync_cdc_pipe_busy in rtl/ddr_stream_axi.vhd
(and i_sync_cdc_empty in rtl/ddr_stream_avmm.vhd).

PROVEN NETLIST CAUSE:
In 7-series / UltraScale FPGA logic slices, flip-flop primitives support either
a dedicated synchronous reset (FDRE) or synchronous set (FDSE). No native slice
primitive supports both synchronous set and synchronous reset pins.
In the baseline RTL of ddr_sync_cdc.vhd:
  - stages_r initialized to '0'
  - The single synchronous process evaluated set_i, and then rst_i with reset priority.
Because rst_i had priority and the register initialized to '0', Vivado inferred
FDRE for all stages. To implement set_i, Vivado synthesized a combinational LUT2
gate on the D input of every stage (stages_r[0..N-1]_i_1 computing d_i OR set_i).
Because stage 0 was driven by a LUT2, and stage 1 was driven by a LUT2 from stage 0,
the register-to-register direct connection was broken. Vivado's CDC engine rejected
the synchronizer chain, reported Depth 0, and flagged CDC-1 Critical.

THE FIX:
1. rtl/support/ddr_sync_cdc.vhd specializes reset polarity using a generic:
     rst_val_g : std_logic := '0';
   and splits into two static generate branches (GEN_SET when rst_val_g = '1',
   and GEN_RST when rst_val_g = '0').
   - When rst_val_g = '1': Vivado infers pure FDSE using the dedicated S pin (0 LUTs).
   - When rst_val_g = '0': Vivado infers pure FDRE using the dedicated R pin (0 LUTs).
   With 0 LUTs on D, direct FF-to-FF connectivity is preserved and report_cdc
   recognizes all instances as CDC-3 Info Depth 3.
2. In all callers where reset forces the CDC output to '1' during reset,
   the instance explicitly passes rst_val_g => '1'.

This file guards both parts:
- Part 1: test_ddr_sync_cdc_architecture_splits_set_and_rst
  Asserts that ddr_sync_cdc.vhd declares rst_val_g and separates set from reset
  into GEN_SET and GEN_RST to prevent combinational logic on stage D inputs.
  Baseline: RED with CDC_SYNCHRONIZER_SET_RST_UNSPECIALIZED.
  Mutation: RED with CDC_SYNCHRONIZER_SET_RST_UNSPECIALIZED.

- Part 2: test_caller_set_instances_pass_rst_val_g
  Asserts that all caller instances driving set_i with a live reset pass rst_val_g => '1'.
  Baseline: RED with CALLER_CDC_SET_INSTANCE_MISSING_RST_VAL.
  Mutation: RED with CALLER_CDC_SET_INSTANCE_MISSING_RST_VAL.
"""
from __future__ import annotations

import re
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RTL_SYNC_CDC = REPO_ROOT / "rtl" / "support" / "ddr_sync_cdc.vhd"
RTL_AXI = REPO_ROOT / "rtl" / "ddr_stream_axi.vhd"
RTL_FIFO_INTF = REPO_ROOT / "rtl" / "ddr_fifo_intf.vhd"
RTL_AVMM = REPO_ROOT / "rtl" / "ddr_stream_avmm.vhd"


def _strip_comments(text: str) -> str:
    """Strip VHDL single-line comments (--)."""
    lines = []
    for line in text.splitlines():
        idx = line.find("--")
        if idx != -1:
            line = line[:idx]
        lines.append(line)
    return "\n".join(lines)


def _find_entity_generic(entity_text: str, generic_name: str) -> str | None:
    """Find generic declaration in entity block."""
    m = re.search(
        rf"\b{re.escape(generic_name)}\s*:\s*([^;)]+)",
        entity_text,
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


def _find_instance_generic_map(file_text: str, instance_name: str) -> str | None:
    """Find the generic map block for a named entity instance."""
    clean = _strip_comments(file_text)
    pattern = re.compile(
        rf"\b{re.escape(instance_name)}\s*:\s*entity\s+rr\.ddr_sync_cdc\s+"
        rf"(?:generic\s+map\s*\((.*?)\)\s*)?port\s+map",
        re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(clean)
    if not m:
        return None
    return m.group(1) or ""


def test_ddr_sync_cdc_architecture_splits_set_and_rst():
    """Part 1: ddr_sync_cdc must provide rst_val_g and specialize set vs reset.

    Evaluated at the VHDL definition:
    - entity ddr_sync_cdc must declare generic rst_val_g : std_logic := '0'.
    - architecture must contain GEN_SET for rst_val_g = '1' and GEN_RST for rst_val_g = '0'.
    - Neither branch may contain both sequential set_i and rst_i evaluations in a single
      process (which forces Vivado to synthesize a LUT2 on every stage's D pin).
    """
    assert RTL_SYNC_CDC.is_file(), f"missing {RTL_SYNC_CDC}"
    text = RTL_SYNC_CDC.read_text(encoding="utf-8")
    clean = _strip_comments(text)

    # 1. Generic declaration
    m_entity = re.search(
        r"entity\s+ddr_sync_cdc\s+is\s+generic\s*\((.*?)\)\s*;.*?end\s+(?:entity\s+)?ddr_sync_cdc",
        clean,
        re.DOTALL | re.IGNORECASE,
    )
    assert m_entity, (
        "CDC_SYNCHRONIZER_SET_RST_UNSPECIALIZED: entity ddr_sync_cdc block not found"
    )
    generic_block = m_entity.group(1)
    rst_val_decl = _find_entity_generic(generic_block, "rst_val_g")
    assert rst_val_decl is not None, (
        "CDC_SYNCHRONIZER_SET_RST_UNSPECIALIZED: entity ddr_sync_cdc is missing generic rst_val_g. "
        "A static reset value generic is required to avoid mutual set/reset priority "
        "and prevent combinational LUTs on synchronizer D pins."
    )
    assert "std_logic" in rst_val_decl.lower(), (
        f"CDC_SYNCHRONIZER_SET_RST_UNSPECIALIZED: rst_val_g must be std_logic, found: {rst_val_decl}"
    )

    # 2. Static generate specialization for '1' and '0'
    has_gen_set = bool(
        re.search(
            r"\bGEN_SET\s*:\s*if\s+rst_val_g\s*=\s*'1'\s+generate\b",
            clean,
            re.IGNORECASE,
        )
    )
    has_gen_rst = bool(
        re.search(
            r"\bGEN_RST\s*:\s*if\s+rst_val_g\s*=\s*'0'\s+generate\b",
            clean,
            re.IGNORECASE,
        )
    )
    assert has_gen_set and has_gen_rst, (
        f"CDC_SYNCHRONIZER_SET_RST_UNSPECIALIZED: ddr_sync_cdc must specialize into "
        f"GEN_SET (rst_val_g = '1') and GEN_RST (rst_val_g = '0') generates. "
        f"Found GEN_SET={has_gen_set}, GEN_RST={has_gen_rst}."
    )

    # 3. No unspecialized process evaluating set_i and rst_i with mutual priority
    # A single process with 'if set_i = 1 ... if rst_i = 1' creates LUT2 on D.
    unspecialized_mixed = re.search(
        r"if\s+set_i\s*=\s*'1'\s+then.*?end\s+if;.*?if\s+rst_i\s*=\s*'1'\s+then",
        clean,
        re.DOTALL | re.IGNORECASE,
    )
    assert not unspecialized_mixed, (
        "CDC_SYNCHRONIZER_SET_RST_UNSPECIALIZED: found unspecialized sequential set_i and rst_i "
        "evaluations in ddr_sync_cdc. This causes Vivado to infer FDRE with LUT2 on D, "
        "breaking synchronizer recognition."
    )


def test_caller_set_instances_pass_rst_val_g():
    """Part 2: All caller instances driving set_i with a live reset must set rst_val_g => '1'.

    The three Zybo datalogger instances reported as unknown CDC circuitry (depth 0)
    tie set_i to a live reset:
      1. ddr_stream_axi.vhd: i_sync_cdc_pipe_busy (set_i => rst_ddr_i)
      2. ddr_stream_axi.vhd: i_sync_cdc_empty (set_i => rst_sys_i)
      3. ddr_fifo_intf.vhd: i_sync_cdc_empty (set_i => rst_sys_i)
    and the corresponding AVMM instance:
      4. ddr_stream_avmm.vhd: i_sync_cdc_empty (set_i => rst_sys_i).

    Each must map generic rst_val_g => '1' so Vivado infers pure FDSE without
    combinational LUTs on stage D inputs.
    """
    instances = [
        (RTL_AXI, "i_sync_cdc_pipe_busy"),
        (RTL_AXI, "i_sync_cdc_empty"),
        (RTL_FIFO_INTF, "i_sync_cdc_empty"),
        (RTL_AVMM, "i_sync_cdc_empty"),
    ]

    for file_path, inst_name in instances:
        assert file_path.is_file(), f"missing {file_path}"
        gmap = _find_instance_generic_map(file_path.read_text(encoding="utf-8"), inst_name)
        assert gmap is not None, (
            f"CALLER_CDC_SET_INSTANCE_MISSING_RST_VAL: instance {inst_name} in {file_path.name} "
            f"could not be found or has no generic map."
        )
        has_rst_val = bool(re.search(r"\brst_val_g\s*=>\s*'1'", gmap, re.IGNORECASE))
        assert has_rst_val, (
            f"CALLER_CDC_SET_INSTANCE_MISSING_RST_VAL: {inst_name} in {file_path.name} "
            f"drives set_i with reset but does not set rst_val_g => '1'. "
            f"Generic map content: ({gmap.strip()}). "
            f"Without rst_val_g => '1', Vivado defaults to FDRE with a LUT2 on D, "
            f"breaking CDC depth recognition."
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

# SPDX-License-Identifier: MIT
"""DDR-T2.21 regression guard: single-plane CSR CDC registers must be plane-guarded.

WHY THIS TEST EXISTS
--------------------
Found during RTL-P3.1798 (2026-09-24). kv260_linux top_kv260_h264 instantiates
ddr_stream_axi twice with one plane each: the ring downloader
(enable_write_plane_g=false) and h264_fb_dma (enable_read_plane_g=false).
Queue synth 46aeb5c9c08c completed synth_design, but RouteRTL's post-synth gate
refused the build on Synth 8-6014 (critical sequential element removed) for 5
flip-flops in rtl/ddr_stream_axi.vhd:
  * wr_base_addr_ddr_reg
  * wr_ptr_reset_tgl_ddr_d_reg
  * wr_ptr_reset_ddr_reg
  * rd_ptr_reset_tgl_ddr_d_reg
  * rd_ptr_reset_ddr_reg

In the pre-fix code, these registers sat in shared, unconditional processes
(csr_base_addr_ddr_proc, csr_ptr_reset_ddr_proc) and instances
(i_sync_cdc_wr_ptr_reset, i_sync_cdc_rd_ptr_reset). When either plane was
disabled, the corresponding clk_ddr CSR crossing registers had no fanout
(the uploader or downloader was not instantiated), so Vivado pruned them as
unused sequential elements (Synth 8-6014). Because each register name matches
RouteRTL's critical FF pattern (?<![a-zA-Z0-9])(?:ptr|cdc|ddr)_, the RTL-P3.933
gate rightfully refused the build.

The fix splits the per-plane CSR CDC registers and ddr_sync_cdc instances into
positive `if enable_*_plane_g generate` blocks, and ties their outputs to reset defaults
(`C_DDR_BASE_RST`, `'0'`) in the disabled branch so no dead sequential elements
are instantiated.

This test asserts that:
  1. The 5 critical CSR CDC registers and ddr_sync_cdc instances are strictly
     instantiated inside POSITIVE `enable_*_plane_g` generate blocks (not inverted
     or substring matches), and tied to reset defaults when disabled.
  2. In both single-plane configurations (write-plane-off, read-plane-off),
     no unguarded CSR CDC sequential elements or CDC synchronizers are elaborated
     that would be pruned by Vivado and trip the Synth 8-6014 critical-FF gate.
  3. Shared unguarded processes (csr_base_addr_ddr_proc, csr_ptr_reset_ddr_proc)
     are never instantiated outside generate blocks.
  4. Both single-plane targets (targets/vivado_smoke_read_only.yml and
     targets/vivado_smoke_write_only.yml) configure the respective single-plane
     generics, and any synthesis runs through rr's gate complete cleanly without
     Synth 8-6014 warnings.

BASELINE: RED with SYNTH_8_6014_UNGUARDED_CDC_REFUSED (the 5 registers sat in
shared processes outside plane generate blocks).
MUTATION: RED with SYNTH_8_6014_UNGUARDED_CDC_REFUSED (inverting generate guards to
`not enable_*_plane_g`, hoisting or deleting CDC synchronizers, or adding clocked assigns).
"""
from __future__ import annotations

import re
from pathlib import Path
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RTL_AXI = REPO_ROOT / "rtl" / "ddr_stream_axi.vhd"
TARGET_READ_ONLY = REPO_ROOT / "targets" / "vivado_smoke_read_only.yml"
TARGET_WRITE_ONLY = REPO_ROOT / "targets" / "vivado_smoke_write_only.yml"
OUT_DIR = REPO_ROOT / "out"

# The 5 critical CSR CDC registers Vivado pruned as Synth 8-6014 in queue synth 46aeb5c9c08c
WR_CSR_CDC_REGS = [
    "wr_base_addr_ddr",
    "wr_ptr_reset_tgl_ddr_d",
    "wr_ptr_reset_ddr",
]

RD_CSR_CDC_REGS = [
    "rd_ptr_reset_tgl_ddr_d",
    "rd_ptr_reset_ddr",
    "rd_base_addr_ddr",
]

# RouteRTL RTL-P3.933 critical sequential element pattern:
CRITICAL_FF_RE = re.compile(r"(?<![a-zA-Z0-9])(?:ptr|cdc|ddr)_", re.IGNORECASE)


def _strip_vhdl_comments(text: str) -> str:
    """Return text with VHDL single-line comments removed."""
    lines = []
    for line in text.splitlines():
        idx = line.find("--")
        if idx != -1:
            line = line[:idx]
        lines.append(line)
    return "\n".join(lines)


def _find_generate_blocks(text: str) -> list[dict]:
    """Parse top-level generate blocks in the architecture body."""
    clean = _strip_vhdl_comments(text)
    pattern = re.compile(
        r"(\w+)\s*:\s*if\s+(.*?)\s+generate\b(.*?)(?:else\s+generate\b(.*?))?end\s+generate\s+\1\s*;",
        re.DOTALL | re.IGNORECASE,
    )
    blocks = []
    for m in pattern.finditer(clean):
        blocks.append({
            "label": m.group(1),
            "condition": m.group(2).strip(),
            "then_body": m.group(3),
            "else_body": m.group(4) or "",
        })
    return blocks


def _is_positive_plane_guard(cond: str, plane_generic: str) -> bool:
    """Return True iff cond is a positive condition guarding plane_generic.

    Strictly requires plane_generic in positive polarity (e.g. 'enable_write_plane_g'
    or 'enable_write_plane_g = true').
    Strictly rejects negated/inverted conditions such as 'not enable_write_plane_g',
    'enable_write_plane_g = false', or 'enable_write_plane_g /= true'.
    """
    cond_clean = cond.strip().lower()
    tokens = re.findall(r"\b\w+\b", cond_clean)
    if plane_generic.lower() not in tokens:
        return False
    if "not" in tokens or "false" in tokens or "/=" in cond_clean:
        return False
    return True


def _elaborate_text_single_plane(clean: str, disabled_generic: str) -> str:
    """Return the VHDL text that elaborates when disabled_generic is false.

    For any generate block guarded POSITIVELY by disabled_generic, its then_body
    is discarded (pruned by synthesis). Its else_body (if present) is retained.
    For any generate block guarded NEGATIVELY by disabled_generic (e.g. if not disabled_generic),
    its then_body IS retained because it elaborates when disabled_generic is false.
    Any top-level architecture code outside generate blocks is retained.
    """
    blocks = _find_generate_blocks(clean)
    text = clean
    for b in blocks:
        if _is_positive_plane_guard(b["condition"], disabled_generic):
            text = text.replace(b["then_body"], "")
    return text


def _find_dead_sequential_elements(elaborated_text: str, regs: list[str]) -> list[str]:
    """Find any register in regs that has a clocked sequential assignment in elaborated_text.

    Checks both:
      1. Clocked processes: process(...) begin ... if rising_edge(clk) ... <reg> <=
      2. Concurrent clocked assignments: <reg> <= ... when rising_edge(...)
    """
    dead_regs = []

    # 1. Clocked processes
    proc_matches = re.finditer(
        r"process\s*(?:\([^)]*\))?.*?\bbegin\b(.*?)end\s+process\b",
        elaborated_text,
        re.DOTALL | re.IGNORECASE,
    )
    for m in proc_matches:
        pbody = m.group(1)
        is_clocked = bool(
            re.search(r"\b(?:rising_edge|falling_edge)\b|'event\b", pbody, re.IGNORECASE)
            or re.search(r"\bclk\w*\b", m.group(0)[: m.start(1) - m.start(0)], re.IGNORECASE)
        )
        if is_clocked:
            for reg in regs:
                if re.search(rf"\b{re.escape(reg)}\s*<=", pbody) and reg not in dead_regs:
                    dead_regs.append(reg)

    # 2. Concurrent clocked assignments (outside process blocks)
    no_procs = re.sub(
        r"process\s*(?:\([^)]*\))?.*?\bbegin\b.*?end\s+process\b",
        "",
        elaborated_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    for reg in regs:
        if reg not in dead_regs:
            if re.search(rf"\b{re.escape(reg)}\s*<=[^;]*?\brising_edge\b[^;]*;", no_procs, re.IGNORECASE):
                dead_regs.append(reg)

    return dead_regs


def _find_unguarded_cdc_instances(elaborated_text: str, instance_names: list[str]) -> list[str]:
    """Find any CDC synchronizer instance elaborated outside positive plane guard."""
    found = []
    for inst in instance_names:
        if re.search(rf"\b{re.escape(inst)}\s*:", elaborated_text):
            found.append(inst)
    return found


def _check_single_plane_write_disabled(clean: str) -> None:
    """Core assertion for write-plane-disabled configuration (ring downloader)."""
    blocks = _find_generate_blocks(clean)
    wr_csr_blocks = [
        b for b in blocks
        if _is_positive_plane_guard(b["condition"], "enable_write_plane_g")
        and "csr" in b["label"].lower()
    ]
    assert wr_csr_blocks, (
        "SYNTH_8_6014_UNGUARDED_CDC_REFUSED: no positive enable_write_plane_g CSR CDC generate block "
        "found in ddr_stream_axi.vhd (condition must be positive 'if enable_write_plane_g generate', "
        "not inverted or substring)."
    )

    wr_then = wr_csr_blocks[0]["then_body"]
    assert re.search(r"\bi_sync_cdc_wr_ptr_reset\s*:\s*entity\s+rr\.ddr_sync_cdc\b", wr_then), (
        "SYNTH_8_6014_UNGUARDED_CDC_REFUSED: i_sync_cdc_wr_ptr_reset must be instantiated "
        "inside positive enable_write_plane_g generate block."
    )

    elaborated = _elaborate_text_single_plane(clean, "enable_write_plane_g")
    unguarded_insts = _find_unguarded_cdc_instances(elaborated, ["i_sync_cdc_wr_ptr_reset"])
    assert not unguarded_insts, (
        f"SYNTH_8_6014_UNGUARDED_CDC_REFUSED: CDC synchronizer instance(s) {unguarded_insts} "
        f"are elaborated when enable_write_plane_g=false. They have no fanout in clk_ddr, "
        f"Vivado prunes them as Synth 8-6014, and rr's gate refuses the build."
    )

    dead_wr_ffs = _find_dead_sequential_elements(elaborated, WR_CSR_CDC_REGS)
    assert not dead_wr_ffs, (
        f"SYNTH_8_6014_UNGUARDED_CDC_REFUSED: write-plane CSR CDC register(s) "
        f"{dead_wr_ffs} are clocked outside positive enable_write_plane_g generate blocks. "
        f"In single-plane mode (enable_write_plane_g=false, top_kv260_h264 ring downloader) "
        f"they have no fanout, Vivado prunes them as Synth 8-6014, and rr's gate refuses the build."
    )


def _check_single_plane_read_disabled(clean: str) -> None:
    """Core assertion for read-plane-disabled configuration (h264_fb_dma)."""
    blocks = _find_generate_blocks(clean)
    rd_csr_blocks = [
        b for b in blocks
        if _is_positive_plane_guard(b["condition"], "enable_read_plane_g")
        and "csr" in b["label"].lower()
    ]
    assert rd_csr_blocks, (
        "SYNTH_8_6014_UNGUARDED_CDC_REFUSED: no positive enable_read_plane_g CSR CDC generate block "
        "found in ddr_stream_axi.vhd (condition must be positive 'if enable_read_plane_g generate', "
        "not inverted or substring)."
    )

    rd_then = rd_csr_blocks[0]["then_body"]
    assert re.search(r"\bi_sync_cdc_rd_ptr_reset\s*:\s*entity\s+rr\.ddr_sync_cdc\b", rd_then), (
        "SYNTH_8_6014_UNGUARDED_CDC_REFUSED: i_sync_cdc_rd_ptr_reset must be instantiated "
        "inside positive enable_read_plane_g generate block."
    )

    elaborated = _elaborate_text_single_plane(clean, "enable_read_plane_g")
    unguarded_insts = _find_unguarded_cdc_instances(elaborated, ["i_sync_cdc_rd_ptr_reset"])
    assert not unguarded_insts, (
        f"SYNTH_8_6014_UNGUARDED_CDC_REFUSED: CDC synchronizer instance(s) {unguarded_insts} "
        f"are elaborated when enable_read_plane_g=false. They have no fanout in clk_ddr, "
        f"Vivado prunes them as Synth 8-6014, and rr's gate refuses the build."
    )

    dead_rd_ffs = _find_dead_sequential_elements(elaborated, RD_CSR_CDC_REGS)
    assert not dead_rd_ffs, (
        f"SYNTH_8_6014_UNGUARDED_CDC_REFUSED: read-plane CSR CDC register(s) "
        f"{dead_rd_ffs} are clocked outside positive enable_read_plane_g generate blocks. "
        f"In single-plane mode (enable_read_plane_g=false, top_kv260_h264 h264_fb_dma) "
        f"they have no fanout, Vivado prunes them as Synth 8-6014, and rr's gate refuses the build."
    )


def test_critical_ff_patterns():
    """Verify that all critical CSR CDC synthesized registers (which carry the Vivado
    '_reg' suffix) match RouteRTL's critical FF pattern."""
    for reg in WR_CSR_CDC_REGS + RD_CSR_CDC_REGS:
        reg_netlist_name = f"{reg}_reg"
        assert CRITICAL_FF_RE.search(reg_netlist_name), (
            f"Expected synthesized register {reg_netlist_name} to match RouteRTL "
            f"critical FF pattern {CRITICAL_FF_RE.pattern}"
        )


def test_no_shared_unguarded_csr_cdc_processes():
    """Assert that the legacy shared processes csr_base_addr_ddr_proc and
    csr_ptr_reset_ddr_proc are not present in rtl/ddr_stream_axi.vhd."""
    assert RTL_AXI.is_file(), f"missing RTL: {RTL_AXI}"
    text = _strip_vhdl_comments(RTL_AXI.read_text(encoding="utf-8"))

    legacy_procs = ["csr_base_addr_ddr_proc", "csr_ptr_reset_ddr_proc"]
    for proc_name in legacy_procs:
        assert not re.search(rf"\b{proc_name}\b", text, re.IGNORECASE), (
            f"SYNTH_8_6014_UNGUARDED_CDC_REFUSED: legacy shared process '{proc_name}' "
            f"found in ddr_stream_axi.vhd. It unconditionally clocks CDC registers for "
            f"both planes, causing Synth 8-6014 when either plane is disabled. "
            f"Split per-plane into enable_*_plane_g generate blocks (DDR-T2.21)."
        )


def test_single_plane_configurations_clean_under_8_6014_gate():
    """DDR-T2.21 core regression gate:
    Verify that in both single-plane configurations (write-plane-off, read-plane-off),
    the disabled plane's CSR CDC sequential elements and synchronizers are not elaborated.
    """
    assert RTL_AXI.is_file(), f"missing RTL: {RTL_AXI}"
    clean = _strip_vhdl_comments(RTL_AXI.read_text(encoding="utf-8"))
    _check_single_plane_write_disabled(clean)
    _check_single_plane_read_disabled(clean)


def test_single_plane_write_disabled_clean_under_8_6014_gate():
    """Independent regression gate for write-plane-disabled (ring downloader)."""
    assert RTL_AXI.is_file(), f"missing RTL: {RTL_AXI}"
    clean = _strip_vhdl_comments(RTL_AXI.read_text(encoding="utf-8"))
    _check_single_plane_write_disabled(clean)


def test_single_plane_read_disabled_clean_under_8_6014_gate():
    """Independent regression gate for read-plane-disabled (h264_fb_dma)."""
    assert RTL_AXI.is_file(), f"missing RTL: {RTL_AXI}"
    clean = _strip_vhdl_comments(RTL_AXI.read_text(encoding="utf-8"))
    _check_single_plane_read_disabled(clean)


def test_disabled_plane_csr_cdc_outputs_are_tied_to_reset_values():
    """Assert that when a plane is disabled, its CSR CDC outputs are tied to
    clean reset defaults in the else-generate branches."""
    assert RTL_AXI.is_file(), f"missing RTL: {RTL_AXI}"
    clean = _strip_vhdl_comments(RTL_AXI.read_text(encoding="utf-8"))

    blocks = _find_generate_blocks(clean)
    wr_csr_blocks = [
        b for b in blocks
        if _is_positive_plane_guard(b["condition"], "enable_write_plane_g")
        and "csr" in b["label"].lower()
    ]
    rd_csr_blocks = [
        b for b in blocks
        if _is_positive_plane_guard(b["condition"], "enable_read_plane_g")
        and "csr" in b["label"].lower()
    ]

    assert wr_csr_blocks, "missing positive write-plane CSR CDC generate block"
    assert rd_csr_blocks, "missing positive read-plane CSR CDC generate block"

    wr_else = wr_csr_blocks[0]["else_body"]
    rd_else = rd_csr_blocks[0]["else_body"]

    assert "wr_base_addr_ddr" in wr_else and "C_DDR_BASE_RST" in wr_else, (
        "wr_base_addr_ddr must be tied to C_DDR_BASE_RST when write plane is disabled"
    )
    assert "wr_ptr_reset_ddr" in wr_else and "'0'" in wr_else, (
        "wr_ptr_reset_ddr must be tied to '0' when write plane is disabled"
    )
    assert "rd_base_addr_ddr" in rd_else and "C_DDR_BASE_RST" in rd_else, (
        "rd_base_addr_ddr must be tied to C_DDR_BASE_RST when read plane is disabled"
    )
    assert "rd_ptr_reset_ddr" in rd_else and "'0'" in rd_else, (
        "rd_ptr_reset_ddr must be tied to '0' when read plane is disabled"
    )


def test_mutations_refused_by_gate():
    """Verify that every reviewer mutant (inverted condition, hoisted synchronizer,
    deleted synchronizer, concurrent clocked assignment) is strictly RED."""
    assert RTL_AXI.is_file(), f"missing RTL: {RTL_AXI}"
    clean = _strip_vhdl_comments(RTL_AXI.read_text(encoding="utf-8"))

    # 1. Inverted write generate condition
    mut_inv_wr = clean.replace(
        "g_wr_csr_cdc : if enable_write_plane_g generate",
        "g_wr_csr_cdc : if not enable_write_plane_g generate",
    )
    with pytest.raises(AssertionError, match="SYNTH_8_6014_UNGUARDED_CDC_REFUSED"):
        _check_single_plane_write_disabled(mut_inv_wr)

    # 2. Inverted read generate condition
    mut_inv_rd = clean.replace(
        "g_rd_csr_cdc : if enable_read_plane_g generate",
        "g_rd_csr_cdc : if not enable_read_plane_g generate",
    )
    with pytest.raises(AssertionError, match="SYNTH_8_6014_UNGUARDED_CDC_REFUSED"):
        _check_single_plane_read_disabled(mut_inv_rd)

    # 3. Hoisting i_sync_cdc_wr_ptr_reset out of g_wr_csr_cdc
    mut_hoist_wr = clean.replace(
        "i_sync_cdc_wr_ptr_reset : entity rr.ddr_sync_cdc",
        "-- hoisted\n",
    ) + "\ni_sync_cdc_wr_ptr_reset : entity rr.ddr_sync_cdc generic map (num_stages_g => 2, or_reduce_g => false) port map (clk_i => clk_ddr_i, rst_i => '0', set_i => '0', d_i => wr_ptr_reset_tgl_sys, q_o => wr_ptr_reset_tgl_ddr);"
    with pytest.raises(AssertionError, match="SYNTH_8_6014_UNGUARDED_CDC_REFUSED"):
        _check_single_plane_write_disabled(mut_hoist_wr)

    # 4. Deleting i_sync_cdc_wr_ptr_reset
    mut_del_wr = clean.replace(
        "i_sync_cdc_wr_ptr_reset : entity rr.ddr_sync_cdc",
        "-- deleted\n",
    )
    with pytest.raises(AssertionError, match="SYNTH_8_6014_UNGUARDED_CDC_REFUSED"):
        _check_single_plane_write_disabled(mut_del_wr)

    # 5. Concurrent clocked assignment to wr_base_addr_ddr
    mut_conc_wr = clean + "\nwr_base_addr_ddr <= wr_base_addr_meta when rising_edge(clk_ddr_i);\n"
    with pytest.raises(AssertionError, match="SYNTH_8_6014_UNGUARDED_CDC_REFUSED"):
        _check_single_plane_write_disabled(mut_conc_wr)


def test_single_plane_targets_and_synth_smoke_clean_under_8_6014():
    """Assert that both single-plane smoke targets declare the plane-elision generics,
    and verify that their synthesis logs (when present) completed cleanly under rr's
    Synth 8-6014 gate with 0 critical sequential element removals."""
    assert TARGET_READ_ONLY.is_file(), f"missing target: {TARGET_READ_ONLY}"
    assert TARGET_WRITE_ONLY.is_file(), f"missing target: {TARGET_WRITE_ONLY}"

    ro_cfg = yaml.safe_load(TARGET_READ_ONLY.read_text(encoding="utf-8"))
    wo_cfg = yaml.safe_load(TARGET_WRITE_ONLY.read_text(encoding="utf-8"))

    ro_generics = ro_cfg.get("build_options", {}).get("generics", {})
    wo_generics = wo_cfg.get("build_options", {}).get("generics", {})

    assert ro_generics.get("enable_write_plane_g") is False, "read-only target must have enable_write_plane_g=false"
    assert ro_generics.get("enable_read_plane_g") is True, "read-only target must have enable_read_plane_g=true"
    assert wo_generics.get("enable_write_plane_g") is True, "write-only target must have enable_write_plane_g=true"
    assert wo_generics.get("enable_read_plane_g") is False, "write-only target must have enable_read_plane_g=false"

    # If synthesis has been run (e.g. in out/), verify that the synth log
    # completed cleanly with 0 8-6014 warnings on critical sequential elements:
    log_candidates = [
        OUT_DIR / "vivado_smoke_read_only" / "reports" / "synthesis" / "ddr_stream_axi.vds",
        OUT_DIR / "vivado_smoke_read_only" / "project" / "rr_ddr_stream_vivado_smoke_read_only.runs" / "synth_1" / "ddr_stream_axi.vds",
        OUT_DIR / "vivado_smoke_write_only" / "reports" / "synthesis" / "ddr_stream_axi.vds",
        OUT_DIR / "vivado_smoke_write_only" / "project" / "rr_ddr_stream_vivado_smoke_write_only.runs" / "synth_1" / "ddr_stream_axi.vds",
    ]
    for log_path in log_candidates:
        if log_path.is_file():
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            assert "synth_design completed successfully" in log_text, (
                f"{log_path.name} did not complete synth_design successfully"
            )
            # Find all Synth 8-6014 warnings:
            removals = re.findall(r"\[Synth 8-6014\]\s*Unused sequential element\s+(\S+)\s+was removed", log_text)
            critical_removals = [r for r in removals if CRITICAL_FF_RE.search(r)]
            assert not critical_removals, (
                f"SYNTH_8_6014_UNGUARDED_CDC_REFUSED: critical sequential elements {critical_removals} "
                f"removed in {log_path}"
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""DDR-P2.16 — Regbank schema and RTL/C consistency tests for Zybo DDR validator.

Validates that example/zybo_z7_20_ddr_validator/project_regbank.yml:
1. Conforms strictly to the RouteRTL regbank YAML schema and fits the 4 KiB window
   defined in sw/linux/system_memory_map.yml.
2. CLI commands `rr regbank validate` and `rr regbank html` execute successfully
   with clean zero exit codes, and rendered HTML contains all registers, enums,
   and field mappings.
3. Accurately reconciles register word indices and bit fields against the RTL
   decode implementation in example/zybo_z7_20_ddr_validator/src/ddr_validator_csr.vhd.
4. Accurately reconciles byte offsets and bit masks against the C definitions in
   example/zybo_z7_20_ddr_validator/sw/linux/ddr_validator.c and the generated C header.
5. Correctly flags deliberate mismatches if any constant, offset, or bitfield drifts.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import pytest
import yaml

from routertl_core.generators.regbank.validate_schema import validate_schema
from routertl_core.generators.regbank.regbank_parser import load_schema, RegbankSchema
from routertl_core.generators.regbank.generate_c_header import generate_c_header

_REPO = Path(__file__).resolve().parents[1]
_EXAMPLE_DIR = _REPO / "example" / "zybo_z7_20_ddr_validator"
_SCHEMA_PATH = _EXAMPLE_DIR / "project_regbank.yml"
_CSR_VHDL = _EXAMPLE_DIR / "src" / "ddr_validator_csr.vhd"
_C_APP = _EXAMPLE_DIR / "sw" / "linux" / "ddr_validator.c"
_SYSTEM_MMAP = _EXAMPLE_DIR / "sw" / "linux" / "system_memory_map.yml"


def parse_rtl_constants(vhd_path: Path) -> dict[str, int]:
    """Parse C_<NAME> register word indices from VHDL architecture."""
    content = vhd_path.read_text(encoding="utf-8")
    constants = {}
    pattern = r"constant\s+(C_[A-Za-z0-9_]+)\s*:\s*natural\s*:=\s*16#([0-9A-Fa-f]+)#\s*/\s*4;"
    for match in re.finditer(pattern, content):
        name = match.group(1)
        byte_val = int(match.group(2), 16)
        constants[name] = byte_val // 4
    return constants


def parse_rtl_bitfields(vhd_path: Path) -> dict[str, int | tuple[int, int]]:
    """Parse decoded bit positions and slices from RTL process bodies."""
    content = vhd_path.read_text(encoding="utf-8")
    fields: dict[str, int | tuple[int, int]] = {}

    # Write strobe and field assignments
    m = re.search(r"start_p_r\s*<=\s*(?:s_axi_wdata_i|wdata_r)\((\d+)\)", content)
    assert m, "start_p_r bit not found in RTL"
    fields["CTRL.START"] = int(m.group(1))

    m = re.search(r"soft_reset_p_r\s*<=\s*(?:s_axi_wdata_i|wdata_r)\((\d+)\)", content)
    assert m, "soft_reset_p_r bit not found in RTL"
    fields["CTRL.SOFT_RESET"] = int(m.group(1))

    m = re.search(r"continuous_r\s*<=\s*(?:s_axi_wdata_i|wdata_r)\((\d+)\)", content)
    assert m, "continuous_r bit not found in RTL"
    fields["CTRL.CONTINUOUS"] = int(m.group(1))

    m = re.search(r"scenario_r\s*<=\s*(?:s_axi_wdata_i|wdata_r)\((\d+)\s+downto\s+(\d+)\)", content)
    assert m, "scenario_r slice not found in RTL"
    fields["SCENARIO.ID"] = (int(m.group(1)), int(m.group(2)))

    m = re.search(r"wr_base_load_p_r\s*<=\s*(?:s_axi_wdata_i|wdata_r)\((\d+)\)", content)
    assert m, "wr_base_load_p_r bit not found in RTL"
    fields["BASE_LOAD.WR_BASE_LOAD"] = int(m.group(1))

    m = re.search(r"rd_base_load_p_r\s*<=\s*(?:s_axi_wdata_i|wdata_r)\((\d+)\)", content)
    assert m, "rd_base_load_p_r bit not found in RTL"
    fields["BASE_LOAD.RD_BASE_LOAD"] = int(m.group(1))

    m = re.search(r"wr_ptr_reset_p_r\s*<=\s*(?:s_axi_wdata_i|wdata_r)\((\d+)\)", content)
    assert m, "wr_ptr_reset_p_r bit not found in RTL"
    fields["PTR_RESET.WR_PTR_RESET"] = int(m.group(1))

    m = re.search(r"rd_ptr_reset_p_r\s*<=\s*(?:s_axi_wdata_i|wdata_r)\((\d+)\)", content)
    assert m, "rd_ptr_reset_p_r bit not found in RTL"
    fields["PTR_RESET.RD_PTR_RESET"] = int(m.group(1))

    # Read status bit assignments in p_read
    for sig, name in [
        ("running_i", "STATUS.RUNNING"),
        ("done_i", "STATUS.DONE"),
        ("pass_i", "STATUS.PASS"),
        ("fail_i", "STATUS.FAIL"),
        ("write_idle_i", "STATUS.WRITE_IDLE"),
        ("read_idle_i", "STATUS.READ_IDLE"),
    ]:
        m = re.search(rf"r_data_r\((\d+)\)\s*<=\s*{sig}", content)
        assert m, f"Read mapping for {sig} not found in RTL"
        fields[name] = int(m.group(1))

    return fields


def parse_c_offsets(c_path: Path) -> dict[str, int]:
    """Parse REG_<NAME> byte offsets from ddr_validator.c."""
    content = c_path.read_text(encoding="utf-8")
    offsets = {}
    pattern = r"#define\s+(REG_[A-Za-z0-9_]+)\s+(0x[0-9A-Fa-f]+)"
    for match in re.finditer(pattern, content):
        name = match.group(1)
        val = int(match.group(2), 16)
        offsets[name] = val
    return offsets


def parse_c_masks(c_path: Path) -> dict[str, int]:
    """Parse bit masks from ddr_validator.c (CTRL_* and ST_*)."""
    content = c_path.read_text(encoding="utf-8")
    masks = {}
    pattern = r"#define\s+((?:CTRL|ST)_[A-Za-z0-9_]+)\s+\(1u\s*<<\s*(\d+)\)"
    for match in re.finditer(pattern, content):
        name = match.group(1)
        shift = int(match.group(2))
        masks[name] = 1 << shift
    return masks


def reconcile_with_rtl(schema: RegbankSchema, vhd_path: Path) -> None:
    """Verify that schema registers and bitfields match RTL constants and decoding."""
    rtl_constants = parse_rtl_constants(vhd_path)
    assert rtl_constants, f"Failed to extract RTL constants from {vhd_path}"

    for reg in schema.registers:
        const_name = f"C_{reg.name}"
        assert const_name in rtl_constants, (
            f"Schema register {reg.name} missing from RTL constants in {vhd_path.name}"
        )
        expected_word_idx = rtl_constants[const_name]
        assert reg.word_index == expected_word_idx, (
            f"RTL constant {const_name} mismatch: schema word index {reg.word_index} "
            f"!= RTL constant {expected_word_idx}"
        )

    # RTL bitfield and semantics checks
    vhd_text = vhd_path.read_text(encoding="utf-8")
    magic_match = re.search(r'constant\s+C_IDENT_MAGIC\s*:\s*std_logic_vector[^;]*:=\s*x"([0-9A-Fa-f]+)";', vhd_text)
    assert magic_match, "C_IDENT_MAGIC constant not found in RTL"
    rtl_magic = int(magic_match.group(1), 16)

    ident_reg = next((r for r in schema.registers if r.name == "IDENT"), None)
    assert ident_reg is not None, "IDENT register not found in schema"
    assert ident_reg.reset_value == rtl_magic, (
        f"IDENT reset_value 0x{ident_reg.reset_value:08X} != RTL magic 0x{rtl_magic:08X}"
    )

    # Lock field positions in RTL against schema definitions
    rtl_fields = parse_rtl_bitfields(vhd_path)
    single_bit_checks = [
        ("CTRL", "START", "CTRL.START"),
        ("CTRL", "SOFT_RESET", "CTRL.SOFT_RESET"),
        ("CTRL", "CONTINUOUS", "CTRL.CONTINUOUS"),
        ("BASE_LOAD", "WR_BASE_LOAD", "BASE_LOAD.WR_BASE_LOAD"),
        ("BASE_LOAD", "RD_BASE_LOAD", "BASE_LOAD.RD_BASE_LOAD"),
        ("PTR_RESET", "WR_PTR_RESET", "PTR_RESET.WR_PTR_RESET"),
        ("PTR_RESET", "RD_PTR_RESET", "PTR_RESET.RD_PTR_RESET"),
        ("STATUS", "RUNNING", "STATUS.RUNNING"),
        ("STATUS", "DONE", "STATUS.DONE"),
        ("STATUS", "PASS", "STATUS.PASS"),
        ("STATUS", "FAIL", "STATUS.FAIL"),
        ("STATUS", "WRITE_IDLE", "STATUS.WRITE_IDLE"),
        ("STATUS", "READ_IDLE", "STATUS.READ_IDLE"),
    ]
    for reg_name, field_name, rtl_key in single_bit_checks:
        reg = next((r for r in schema.registers if r.name == reg_name), None)
        assert reg is not None, f"Register {reg_name} not found in schema"
        field = next((f for f in reg.fields if f.name == field_name), None)
        assert field is not None, f"Field {field_name} not found in {reg_name}"
        expected_bit = rtl_fields[rtl_key]
        assert field.bits == [expected_bit], (
            f"RTL field {reg_name}.{field_name} bit mismatch: schema bits {field.bits} "
            f"!= RTL bit [{expected_bit}]"
        )

    # Check multi-bit range fields
    scenario_reg = next((r for r in schema.registers if r.name == "SCENARIO"), None)
    assert scenario_reg is not None, "SCENARIO register not found in schema"
    scenario_field = next((f for f in scenario_reg.fields if f.name == "ID"), None)
    assert scenario_field is not None, "SCENARIO.ID field not found in schema"
    expected_scenario_bits = list(rtl_fields["SCENARIO.ID"])
    assert scenario_field.bits == expected_scenario_bits, (
        f"RTL field SCENARIO.ID bits mismatch: schema {scenario_field.bits} != RTL {expected_scenario_bits}"
    )


def reconcile_with_c_abi(schema: RegbankSchema, c_path: Path) -> None:
    """Verify that schema byte offsets and field masks match C defines and generated header."""
    c_offsets = parse_c_offsets(c_path)
    assert c_offsets, f"Failed to extract C register offsets from {c_path}"

    for reg in schema.registers:
        define_name = f"REG_{reg.name}"
        assert define_name in c_offsets, (
            f"Schema register {reg.name} missing from C definitions in {c_path.name}"
        )
        expected_offset = c_offsets[define_name]
        assert reg.byte_offset == expected_offset, (
            f"C ABI {define_name} mismatch: schema byte offset 0x{reg.byte_offset:04X} "
            f"!= C define 0x{expected_offset:04X}"
        )

    # Validate against generated C header from rr regbank c-header logic directly
    header_content = generate_c_header(schema)
    for define_name, expected_offset in c_offsets.items():
        pattern = rf"#define\s+{define_name}\s+0x{expected_offset:04X}u"
        assert re.search(pattern, header_content), (
            f"Generated C header missing expected define for {define_name} (0x{expected_offset:04X}u)"
        )

    # Validate bitfield masks
    c_masks = parse_c_masks(c_path)
    expected_field_mappings = {
        "CTRL_START": ("CTRL", "START"),
        "CTRL_SOFT_RESET": ("CTRL", "SOFT_RESET"),
        "CTRL_CONTINUOUS": ("CTRL", "CONTINUOUS"),
        "ST_RUNNING": ("STATUS", "RUNNING"),
        "ST_DONE": ("STATUS", "DONE"),
        "ST_PASS": ("STATUS", "PASS"),
        "ST_FAIL": ("STATUS", "FAIL"),
        "ST_WRITE_IDLE": ("STATUS", "WRITE_IDLE"),
        "ST_READ_IDLE": ("STATUS", "READ_IDLE"),
    }
    for c_sym, (reg_name, field_name) in expected_field_mappings.items():
        assert c_sym in c_masks, f"C mask symbol {c_sym} not found in {c_path.name}"
        reg = next((r for r in schema.registers if r.name == reg_name), None)
        assert reg is not None, f"Register {reg_name} not found in schema"
        field = next((f for f in reg.fields if f.name == field_name), None)
        assert field is not None, f"Field {field_name} not found in register {reg_name}"
        assert field.bit_mask == c_masks[c_sym], (
            f"Field mask mismatch for {reg_name}.{field_name}: "
            f"schema mask 0x{field.bit_mask:X} != C mask 0x{c_masks[c_sym]:X}"
        )


def test_schema_validity():
    """Verify that project_regbank.yml validates strictly under RouteRTL and fits the 4 KiB system window."""
    assert _SCHEMA_PATH.is_file(), f"Missing schema at {_SCHEMA_PATH}"
    result = validate_schema(_SCHEMA_PATH, address_mode="words", strict=True)
    assert result["issues"] == [], f"Schema validation issues: {result['issues']}"
    assert result["warnings"] == [], f"Schema validation warnings: {result['warnings']}"
    assert result["reg_count"] == 15
    assert result["max_index"] == 15

    schema = load_schema(_SCHEMA_PATH)
    assert schema.name == "ddr_validator_csr"
    assert schema.base_address == 0x40000000
    assert schema.address_mode == "words"

    # Reconcile with sw/linux/system_memory_map.yml
    assert _SYSTEM_MMAP.is_file(), f"Missing system memory map at {_SYSTEM_MMAP}"
    with open(_SYSTEM_MMAP, "r", encoding="utf-8") as f:
        mmap = yaml.safe_load(f)
    devices = mmap.get("devices", [])
    csr_dev = next((d for d in devices if d.get("name") == "ddr_validator_csr"), None)
    assert csr_dev is not None, f"ddr_validator_csr device entry not found in {_SYSTEM_MMAP}"
    assert schema.base_address == csr_dev["base_addr"], (
        f"Base address mismatch: schema 0x{schema.base_address:08X} != system_memory_map 0x{csr_dev['base_addr']:08X}"
    )
    system_span = csr_dev["span"]
    assert system_span == 0x1000, f"Expected 4 KiB span (0x1000), got 0x{system_span:X}"
    raw_schema = yaml.safe_load(_SCHEMA_PATH.read_text(encoding="utf-8"))
    addr_width = raw_schema.get("addr_width", 12)
    assert (1 << addr_width) == system_span, (
        f"Schema addr_width {addr_width} (span {1 << addr_width}) != system span {system_span}"
    )
    # 4 KiB window containment check: all 15 registers fit well within 4096 bytes (1024 words)
    assert (schema.max_address + 1) * 4 <= system_span, (
        f"Register bank exceeds system window: max byte offset {(schema.max_address + 1) * 4} > span {system_span}"
    )

    # Exercise CLI exit code for rr regbank validate
    val_proc = subprocess.run(
        ["rr", "regbank", "validate", str(_SCHEMA_PATH)],
        capture_output=True, text=True, check=False
    )
    assert val_proc.returncode == 0, f"rr regbank validate returned non-zero ({val_proc.returncode}): {val_proc.stderr}"
    assert "Schema is valid" in val_proc.stdout, f"Unexpected validate output: {val_proc.stdout}"


def test_regbank_html_generation_and_inspection(tmp_path: Path):
    """Verify that rr regbank html generates documentation and inspect rendered contents."""
    out_html = tmp_path / "register-map.html"
    html_proc = subprocess.run(
        ["rr", "regbank", "html", str(_SCHEMA_PATH), "--output", str(out_html)],
        capture_output=True, text=True, check=False
    )
    assert html_proc.returncode == 0, f"rr regbank html returned non-zero ({html_proc.returncode}): {html_proc.stderr}"
    assert out_html.is_file(), f"Output HTML not created at {out_html}"

    content = out_html.read_text(encoding="utf-8")
    schema = load_schema(_SCHEMA_PATH)
    # Check that all 15 registers appear in rendered HTML
    for reg in schema.registers:
        assert reg.name in content, f"Register {reg.name} missing from rendered HTML"
    # Check offsets and version
    assert "0.2.0" in content, "Version 0.2.0 missing from HTML"
    assert "0x0000" in content, "CTRL offset 0x0000 missing from HTML"
    assert "0x003C" in content, "IDENT offset 0x003C missing from HTML"
    # Check enum values for SCENARIO
    for enum_val in ["ROUNDTRIP", "BOUNDARY", "RESET_RECOVERY", "PINGPONG"]:
        assert enum_val in content, f"Enum value {enum_val} missing from rendered HTML"



def test_schema_reconciles_with_rtl():
    """Verify that schema register indices match RTL constants and decode."""
    schema = load_schema(_SCHEMA_PATH)
    reconcile_with_rtl(schema, _CSR_VHDL)


def test_schema_reconciles_with_c_abi():
    """Verify that schema byte offsets and masks match C app definitions."""
    schema = load_schema(_SCHEMA_PATH)
    reconcile_with_c_abi(schema, _C_APP)


def test_reconciliation_detects_deliberate_rtl_mismatch(tmp_path: Path):
    """Prove test power: deliberate offset modification in RTL is caught."""
    vhd_text = _CSR_VHDL.read_text(encoding="utf-8")
    # Mutate C_STATUS from 16#04# / 4 (word 1) to 16#08# / 4 (word 2)
    mutated_vhd = vhd_text.replace("constant C_STATUS          : natural := 16#04# / 4;",
                                   "constant C_STATUS          : natural := 16#08# / 4;")
    assert mutated_vhd != vhd_text
    mutant_file = tmp_path / "ddr_validator_csr_mutant.vhd"
    mutant_file.write_text(mutated_vhd, encoding="utf-8")

    schema = load_schema(_SCHEMA_PATH)
    with pytest.raises(AssertionError, match=r"RTL constant C_STATUS mismatch"):
        reconcile_with_rtl(schema, mutant_file)


def test_reconciliation_detects_deliberate_c_mismatch(tmp_path: Path):
    """Prove test power: deliberate offset modification in C is caught."""
    c_text = _C_APP.read_text(encoding="utf-8")
    # Mutate REG_STATUS from 0x04 to 0x08
    mutated_c = c_text.replace("#define REG_STATUS          0x04",
                               "#define REG_STATUS          0x08")
    assert mutated_c != c_text
    mutant_file = tmp_path / "ddr_validator_mutant.c"
    mutant_file.write_text(mutated_c, encoding="utf-8")

    schema = load_schema(_SCHEMA_PATH)
    with pytest.raises(AssertionError, match=r"C ABI REG_STATUS mismatch"):
        reconcile_with_c_abi(schema, mutant_file)


def test_reconciliation_detects_deliberate_field_bit_mismatch(tmp_path: Path):
    """Prove test power: deliberate bit position modification in RTL is caught."""
    vhd_text = _CSR_VHDL.read_text(encoding="utf-8")
    # Mutate start_p_r bit from 0 to 3
    mutated_vhd = vhd_text.replace("start_p_r      <= s_axi_wdata_i(0);",
                                   "start_p_r      <= s_axi_wdata_i(3);")
    assert mutated_vhd != vhd_text
    mutant_file = tmp_path / "ddr_validator_csr_field_mutant.vhd"
    mutant_file.write_text(mutated_vhd, encoding="utf-8")

    schema = load_schema(_SCHEMA_PATH)
    with pytest.raises(AssertionError, match=r"RTL field CTRL\.START bit mismatch"):
        reconcile_with_rtl(schema, mutant_file)

# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DDR-P2.14 / DS-T1.12 product-geometry write path regression contract guard.

Validates that:
  1. The package-level cocotb regression `sim/cocotb/tests/test_ddr_uploader_product_base.py`
     exists and implements deepskopion's exact DE25 product geometry:
       - enable_read_plane_g = false (read plane elided; write-only)
       - ddr_data_width_g = 256 (256-bit f2sdram width)
       - ddr_addr_width_g = 32
       - ddr_base_addr_g = 0xA0000000 (upper half >= 0x8000_0000)
       - buffer_width_g = 28 (G_DDR_WIN_WIDTH = 28, 256 MiB ring)
       - max_burst_len_g = 16 (G_AXI_MAX_BURST = 16, AXI3 cap)
       - fifo_depth_g = 9
  2. The test drives deepskopion sniffer cadence (short bursts, 1-word frames, and
     0-word FIFO empty edges).
  3. The test asserts AW is issued, AWADDR carries product base 0xA0000000,
     WRITE_IDLE deasserts during write activity, and WR_COMMIT_ADDR (RING_COMMIT_PTR)
     advances off 0 proportionally.
  4. The test docstring explicitly documents what it can and cannot catch,
     recording the DS-T1.14 waiver (metastability / reset deassertion is invisible
     in simulation).
  5. requirements.yml defines DDR-REQ-208 for product geometry write path and
     declares WR_COMMIT_ADDR in port_contract scalars.
"""
from __future__ import annotations

import re
from pathlib import Path
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = REPO_ROOT / "sim" / "cocotb" / "tests" / "test_ddr_uploader_product_base.py"
REQ_FILE = REPO_ROOT / "requirements.yml"


def test_product_base_cocotb_regression_configures_high_base():
    """Verify test_ddr_uploader_product_base.py configures product base 0xA000_0000."""
    assert TEST_FILE.is_file(), f"Missing regression test file: {TEST_FILE}"
    content = TEST_FILE.read_text(encoding="utf-8")
    assert "0xA000_0000" in content, (
        "Regression test must configure PRODUCT_BASE 0xA0000000 (upper half >= 0x8000_0000)"
    )


def test_product_base_cocotb_regression_configures_256bit_width():
    """Verify test_ddr_uploader_product_base.py configures 256-bit DDR width."""
    assert TEST_FILE.is_file(), f"Missing regression test file: {TEST_FILE}"
    content = TEST_FILE.read_text(encoding="utf-8")
    assert bool(re.search(r"^\s*DDR_DATA_WIDTH\s*=\s*256\b", content, re.MULTILINE)) or '"G_DDR_DATA_WIDTH":     256' in content or '"G_DDR_DATA_WIDTH": 256' in content, (
        "Regression test must configure G_DDR_DATA_WIDTH=256 (DE25 f2sdram width)"
    )


def test_product_base_cocotb_regression_exists_and_declares_exact_generics():
    """Verify test_ddr_uploader_product_base.py defines exact DE25 product generics."""
    assert TEST_FILE.is_file(), f"Missing regression test file: {TEST_FILE}"
    content = TEST_FILE.read_text(encoding="utf-8")

    # Generic map checks
    assert '"G_ENABLE_READ_PLANE":  False' in content or '"G_ENABLE_READ_PLANE": False' in content, (
        "Regression test must configure G_ENABLE_READ_PLANE=False (deepskopion write-only architecture)"
    )
    assert bool(re.search(r"^\s*DDR_DATA_WIDTH\s*=\s*256\b", content, re.MULTILINE)) or '"G_DDR_DATA_WIDTH":     256' in content or '"G_DDR_DATA_WIDTH": 256' in content, (
        "Regression test must configure G_DDR_DATA_WIDTH=256 (DE25 f2sdram width)"
    )
    assert "0xA000_0000" in content, (
        "Regression test must configure PRODUCT_BASE 0xA0000000 (upper half >= 0x8000_0000)"
    )
    assert "BUFFER_WIDTH = 28" in content or '"G_BUFFER_WIDTH":       28' in content or '"G_BUFFER_WIDTH": 28' in content, (
        "Regression test must configure G_BUFFER_WIDTH=28 (deepskopion G_DDR_WIN_WIDTH)"
    )
    assert "MAX_BURST_LEN = 16" in content or '"G_MAX_BURST_LEN":      16' in content or '"G_MAX_BURST_LEN": 16' in content, (
        "Regression test must configure G_MAX_BURST_LEN=16 (deepskopion G_AXI_MAX_BURST)"
    )
    assert "FIFO_DEPTH = 9" in content or '"G_FIFO_DEPTH":         9' in content or '"G_FIFO_DEPTH": 9' in content, (
        "Regression test must configure G_FIFO_DEPTH=9"
    )


def test_product_base_cocotb_docstring_states_capabilities_and_limitations():
    """Verify docstring explains what it can catch (functional path) and cannot (metastability)."""
    assert TEST_FILE.is_file(), f"Missing regression test file: {TEST_FILE}"
    content = TEST_FILE.read_text(encoding="utf-8")

    assert "WHAT THIS TEST CAN CATCH" in content, (
        "Docstring must declare what the test CAN catch (functional write path at product geometry)"
    )
    assert "WHAT THIS TEST CANNOT CATCH" in content, (
        "Docstring must declare what the test CANNOT catch (metastability / placement-marginal fault)"
    )
    assert "DS-T1.14" in content or "metastability" in content, (
        "Docstring must cite the DS-T1.14 silicon finding explaining why sim cannot catch metastability"
    )


def test_product_base_cocotb_exercises_sniffer_cadence_and_asserts_commit_advance():
    """Verify test drives sniffer cadence and asserts key silicon invariants."""
    assert TEST_FILE.is_file(), f"Missing regression test file: {TEST_FILE}"
    content = TEST_FILE.read_text(encoding="utf-8")

    # Ingress cadence
    assert "1-word" in content, "Test must exercise 1-word FIFO edge"
    assert "0-word" in content, "Test must exercise 0-word FIFO empty edge"
    assert "zs_msg" in content or "6 words" in content or "6-word" in content, (
        "Test must exercise sniffer multi-word frame cadence"
    )

    # Invariants asserted
    assert "write_idle_dropped" in content, "Test must assert WRITE_IDLE deasserts during traffic"
    assert "awvalid_ever" in content, "Test must assert M_AXI_AWVALID is issued"
    assert "WR_COMMIT_ADDR" in content, "Test must assert WR_COMMIT_ADDR advances off 0"
    assert "commit > 0" in content, "Test must assert commit pointer advances off 0"
    assert "(addr >> 28) == 0xA" in content, "Test must assert AWADDR carries product base 0xA"


def test_requirements_declares_ddr_req_208():
    """Verify requirements.yml functional_contract includes DDR-REQ-208."""
    assert REQ_FILE.is_file(), f"Missing requirements file: {REQ_FILE}"
    reqs = yaml.safe_load(REQ_FILE.read_text(encoding="utf-8"))
    funcs = reqs.get("functional_contract", [])
    req_ids = [r.get("id") for r in funcs]
    assert "DDR-REQ-208" in req_ids, (
        "requirements.yml functional_contract must declare DDR-REQ-208"
    )
    req_208 = next(r for r in funcs if r.get("id") == "DDR-REQ-208")
    desc = req_208.get("desc", "")
    assert "G_ENABLE_READ_PLANE" in desc
    assert "G_DDR_BASE_ADDR" in desc
    assert "G_DDR_DATA_WIDTH" in desc
    assert "WR_COMMIT_ADDR" in desc
    assert "M_AXI_AWVALID" in desc


def test_requirements_declares_wr_commit_addr():
    """Verify requirements.yml port_contract.scalars includes WR_COMMIT_ADDR."""
    assert REQ_FILE.is_file(), f"Missing requirements file: {REQ_FILE}"
    reqs = yaml.safe_load(REQ_FILE.read_text(encoding="utf-8"))
    scalars = reqs.get("port_contract", {}).get("scalars", [])
    scalar_names = [s.get("name") for s in scalars]
    assert "WR_COMMIT_ADDR" in scalar_names, (
        "requirements.yml port_contract.scalars must declare WR_COMMIT_ADDR"
    )


def test_requirements_declares_ddr_req_208_and_wr_commit_addr():
    """Verify requirements.yml includes DDR-REQ-208 and WR_COMMIT_ADDR scalar."""
    assert REQ_FILE.is_file(), f"Missing requirements file: {REQ_FILE}"
    reqs = yaml.safe_load(REQ_FILE.read_text(encoding="utf-8"))

    # Port contract check
    scalars = reqs.get("port_contract", {}).get("scalars", [])
    scalar_names = [s.get("name") for s in scalars]
    assert "WR_COMMIT_ADDR" in scalar_names, (
        "requirements.yml port_contract.scalars must declare WR_COMMIT_ADDR"
    )

    # Functional contract check
    funcs = reqs.get("functional_contract", [])
    req_ids = [r.get("id") for r in funcs]
    assert "DDR-REQ-208" in req_ids, (
        "requirements.yml functional_contract must declare DDR-REQ-208"
    )

    req_208 = next(r for r in funcs if r.get("id") == "DDR-REQ-208")
    desc = req_208.get("desc", "")
    assert "G_ENABLE_READ_PLANE" in desc
    assert "G_DDR_BASE_ADDR" in desc
    assert "G_DDR_DATA_WIDTH" in desc
    assert "WR_COMMIT_ADDR" in desc
    assert "M_AXI_AWVALID" in desc

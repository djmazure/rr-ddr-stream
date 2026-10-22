# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""DDR-P3.39 Quartus Std 23.1 high-base smoke scaffold guard.

The defect is an INTEGER generic that cannot carry 0x80000000 on 32-bit-INTEGER
tools.  The caller-visible surface of this remainder is a consumer wrapper that
maps ``ddr_base_addr_g => x"80000000"`` and a synth target pinned to Quartus
Prime Standard 23.1.  This test asserts that surface in source; the live
Quartus elaboration is ``rr synth run --target quartus_std_high_base``.

BASELINE: only the wrapper and project tests are red on the pre-wrapper
tree (missing wrapper / missing x"80000000" map / missing 23.1std pin).
They do not re-assert the already-landed public-top std_logic_vector
generic.  MUTATION: replace the hex vector map with an INTEGER
16#80000000# (or drop the wrapper as top) and the wrapper/project tests
fail.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WRAPPER = (
    REPO
    / "example"
    / "quartus_std_high_base"
    / "src"
    / "ddr_stream_avmm_high_base.vhd"
)
PROJECT = REPO / "example" / "quartus_std_high_base" / "project.yml"

_HIGH_HEX = re.compile(
    r'ddr_base_addr_g\s*=>\s*x"8000_?0000"',
    re.IGNORECASE,
)
_INTEGER_HIGH = re.compile(
    r"ddr_base_addr_g\s*=>\s*(16#8000_?0000#|2147483648)",
    re.IGNORECASE,
)


def test_wrapper_maps_unsigned_upper_half_as_hex_vector():
    text = WRAPPER.read_text(encoding="utf-8")
    assert _HIGH_HEX.search(text), (
        "wrapper must map ddr_base_addr_g => x\"80000000\" so Quartus Std "
        "never parses 0x80000000 as INTEGER"
    )
    assert not _INTEGER_HIGH.search(text), (
        "wrapper must not map ddr_base_addr_g as an INTEGER high-half literal"
    )


def test_synth_project_pins_quartus_std_23_1_and_wrapper_top():
    cfg = yaml.safe_load(PROJECT.read_text(encoding="utf-8"))
    assert cfg["project"]["top_module"] == "ddr_stream_avmm_high_base"
    assert cfg["build_options"]["tool_version"] == "23.1std"
    assert cfg["hardware"]["edition"] == "std"
    assert str(cfg["hardware"]["part"]).startswith("10AX")
    assert cfg["toolchain"]["tool"] == "quartus-std-23.1"
    # DDR-P3.46: the resolver builds the compile set from paths.sources, so
    # the wrapper and the IP top must be reachable from those roots.
    roots = cfg["paths"]["sources"]
    assert "example/quartus_std_high_base/src" in roots
    assert "rtl" in roots

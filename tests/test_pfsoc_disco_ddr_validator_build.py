# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DDR-P2.22 — regression guard for buildable pfsoc_disco_ddr_validator.

Guards that example/pfsoc_disco_ddr_validator/ provides a fully buildable
top module and source tree:
  * project.yml declares auto_sources: true and paths.sources
  * src/top_pfsoc_disco_ddr_validator.vhd exists and instantiates pfsoc_mss and pfsoc_ddr_validator_core
  * resolve_auto_sources walks from top_pfsoc_disco_ddr_validator to compile closure
  * mss/disco_mss.cxz has the trained DDR4 reference settings (row width 16, tRFC 350)
  * tcl/pfsoc_mss_subsys.tcl clocks FIC_2_ACLK and FIC_3_PCLK from CCC (not tied to GND)
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import zipfile
import yaml

from routertl_core.project_manager import resolve_auto_sources

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_DIR = REPO_ROOT / "example" / "pfsoc_disco_ddr_validator"
PROJECT_YML = EXAMPLE_DIR / "project.yml"

TRAINED_MSS_SHA256 = "802d13bb184705a5210f650def4c77bee4de0174f71c431fde318720ac799512"


def _verify_smartdesign_tcl_clocks(tcl_text: str) -> tuple[bool, str]:
    """Verify that FIC_2_ACLK and FIC_3_PCLK are explicitly connected to CCC output clock

    and neither is tied to GND. Comments are stripped so commented-out connections or
    references never satisfy the connection requirements (DDR-P2.18 / DDR-P2.22).
    """
    clean_lines = [
        line.split("#")[0].strip()
        for line in tcl_text.splitlines()
        if not line.strip().startswith("#")
    ]
    clean_tcl = "\n".join(clean_lines)

    if re.search(r"u_mss:FIC_2_ACLK\s+GND", clean_tcl):
        return False, "FIC_2_ACLK must not be tied to GND (causes DLL unlock / DDR-PLL unlock)"
    if re.search(r"u_mss:FIC_3_PCLK\s+GND", clean_tcl):
        return False, "FIC_3_PCLK must not be tied to GND"

    commands = []
    current_cmd: list[str] = []
    for line in clean_lines:
        if not line:
            continue
        if line.endswith("\\"):
            current_cmd.append(line[:-1].strip())
        else:
            current_cmd.append(line)
            commands.append(" ".join(current_cmd))
            current_cmd = []

    conn_cmds = [cmd for cmd in commands if cmd.startswith("sd_connect_pins")]

    fic2_connected = any(
        "u_ccc:OUT0_FABCLK_0" in cmd and "u_mss:FIC_2_ACLK" in cmd
        for cmd in conn_cmds
    )
    if not fic2_connected:
        return False, "FIC_2_ACLK must be connected to u_ccc:OUT0_FABCLK_0 in SmartDesign TCL"

    fic3_connected = any(
        "u_ccc:OUT0_FABCLK_0" in cmd and "u_mss:FIC_3_PCLK" in cmd
        for cmd in conn_cmds
    )
    if not fic3_connected:
        return False, "FIC_3_PCLK must be connected to u_ccc:OUT0_FABCLK_0 in SmartDesign TCL"

    return True, "ok"


def _check_buildable_invariants(tcl_text_override: str | None = None) -> tuple[bool, str]:
    if not PROJECT_YML.is_file():
        return False, f"{PROJECT_YML} is missing"

    cfg = yaml.safe_load(PROJECT_YML.read_text(encoding="utf-8")) or {}
    if cfg.get("auto_sources") is not True:
        return False, "project.yml auto_sources is not True"

    paths_cfg = cfg.get("paths") or {}
    raw_sources = paths_cfg.get("sources")
    if not raw_sources or "src" not in raw_sources:
        return False, "project.yml paths.sources missing or does not include 'src'"

    top_file = EXAMPLE_DIR / "src" / "top_pfsoc_disco_ddr_validator.vhd"
    if not top_file.is_file():
        return False, f"{top_file} is missing"

    core_file = EXAMPLE_DIR / "src" / "pfsoc_ddr_validator_core.vhd"
    if not core_file.is_file():
        return False, f"{core_file} is missing"

    top_text = top_file.read_text(encoding="utf-8")
    if "entity top_pfsoc_disco_ddr_validator is" not in top_text:
        return False, "top entity top_pfsoc_disco_ddr_validator not found in top_file"
    if "component pfsoc_mss is" not in top_text:
        return False, "component pfsoc_mss not declared in top_file"
    if "pfsoc_ddr_validator_core" not in top_text:
        return False, "pfsoc_ddr_validator_core not instantiated in top_file"

    # Check MSS DDR4 training configuration (.cxz)
    mss_cxz = EXAMPLE_DIR / "mss" / "disco_mss.cxz"
    if not mss_cxz.is_file():
        return False, f"{mss_cxz} is missing"

    actual_hash = hashlib.sha256(mss_cxz.read_bytes()).hexdigest()
    if actual_hash != TRAINED_MSS_SHA256:
        with zipfile.ZipFile(mss_cxz) as z:
            cfg_bytes = z.read("disco_mss.cfg").decode("utf-8", errors="replace")
            if "DDR4_ROW_ADDR_WIDTH                                 16" not in cfg_bytes:
                return False, f"disco_mss.cxz row addr width is not 16 (hash {actual_hash})"
            if "DDR4_TIMING_RFC                                     350" not in cfg_bytes:
                return False, f"disco_mss.cxz timing RFC is not 350 (hash {actual_hash})"

    # Check SmartDesign TCL
    if tcl_text_override is not None:
        tcl_text = tcl_text_override
    else:
        tcl_file = EXAMPLE_DIR / "tcl" / "pfsoc_mss_subsys.tcl"
        if not tcl_file.is_file():
            return False, f"{tcl_file} is missing"
        tcl_text = tcl_file.read_text(encoding="utf-8")

    ok, reason = _verify_smartdesign_tcl_clocks(tcl_text)
    if not ok:
        return False, reason

    # Verify RouteRTL DependencyResolver resolves compilation closure
    res_cfg, resolver = resolve_auto_sources(cfg, str(EXAMPLE_DIR), synthesis=True)
    syn_files = (res_cfg.get("sources") or {}).get("syn") or []
    if not syn_files:
        return False, "resolve_auto_sources produced empty sources.syn"

    syn_names = {Path(f).name for f in syn_files}
    required = {
        "top_pfsoc_disco_ddr_validator.vhd",
        "pfsoc_ddr_validator_core.vhd",
        "ddr_stream_fic.vhd",
        "ddr_stream_pkg.vhd",
        "ddr_validator_bist.vhd",
        "ddr_validator_csr.vhd",
    }
    missing_syn = required - syn_names
    if missing_syn:
        return False, f"resolve_auto_sources missing required compile units: {sorted(missing_syn)}"

    return True, "ok"


def test_pfsoc_disco_ddr_validator_buildable_top_and_sources():
    """Verify that example/pfsoc_disco_ddr_validator provides buildable top + src."""
    ok, reason = _check_buildable_invariants()
    assert ok, (
        f"pfsoc_disco_ddr_validator must enable auto_sources and deliver buildable top + src (DDR-P2.22): {reason}"
    )


def test_pfsoc_disco_ddr_validator_tcl_clock_mutations_refused():
    """Verify that removing FIC2/3 CCC clock connections or grounding them is strictly REFUSED.

    Guards against the DDR-P2.18 failure mode where FIC_2 embedded DLL fails to lock.
    """
    tcl_file = EXAMPLE_DIR / "tcl" / "pfsoc_mss_subsys.tcl"
    assert tcl_file.is_file(), f"missing {tcl_file}"
    tcl_text = tcl_file.read_text(encoding="utf-8")

    # Baseline on unmodified TCL must pass
    ok, reason = _verify_smartdesign_tcl_clocks(tcl_text)
    assert ok, f"baseline TCL clocks must pass: {reason}"

    # Mutant 1: Delete FIC_2_ACLK connection line (leave comments untouched)
    mut_del_fic2 = "\n".join(
        line for line in tcl_text.splitlines()
        if not ("u_mss:FIC_2_ACLK" in line and "sd_connect_pins" in line)
    )
    ok1, reason1 = _verify_smartdesign_tcl_clocks(mut_del_fic2)
    assert not ok1, "deleting FIC_2_ACLK connection must fail"
    assert "FIC_2_ACLK must be connected to u_ccc:OUT0_FABCLK_0" in reason1

    # Mutant 2: Delete FIC_3_PCLK connection line (leave comments untouched)
    mut_del_fic3 = "\n".join(
        line for line in tcl_text.splitlines()
        if not ("u_mss:FIC_3_PCLK" in line and "sd_connect_pins" in line)
    )
    ok2, reason2 = _verify_smartdesign_tcl_clocks(mut_del_fic3)
    assert not ok2, "deleting FIC_3_PCLK connection must fail"
    assert "FIC_3_PCLK must be connected to u_ccc:OUT0_FABCLK_0" in reason2

    # Mutant 3: Ground FIC_2_ACLK (salvage tree defect)
    mut_gnd_fic2 = tcl_text.replace(
        "sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {u_ccc:OUT0_FABCLK_0 u_mss:FIC_2_ACLK}",
        "sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {u_mss:FIC_2_ACLK GND}",
    )
    ok3, reason3 = _verify_smartdesign_tcl_clocks(mut_gnd_fic2)
    assert not ok3, "grounding FIC_2_ACLK must fail"
    assert "FIC_2_ACLK must not be tied to GND" in reason3

    # Mutant 4: Ground FIC_3_PCLK (salvage tree defect)
    mut_gnd_fic3 = tcl_text.replace(
        "sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {u_ccc:OUT0_FABCLK_0 u_mss:FIC_3_PCLK}",
        "sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {u_mss:FIC_3_PCLK GND}",
    )
    ok4, reason4 = _verify_smartdesign_tcl_clocks(mut_gnd_fic3)
    assert not ok4, "grounding FIC_3_PCLK must fail"
    assert "FIC_3_PCLK must not be tied to GND" in reason4

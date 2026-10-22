# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DDR-T2.20 regression guard: DE25 Agilex-5 validator scoped SDC packaging.

WHY THIS TEST EXISTS
--------------------
Found by the DDR-P3.37 nightly runbook (2026-07-26, Quartus Pro 25.3.1, post-fit
STA on example/de25_agilex5_ddr_validator).

OBSERVED DEFECT: the fitted project's QSF carried exactly ONE SDC_FILE —
routertl/boards/de25-standard/master.sdc. None of constraints/ddr_stream_cdc_scoped.sdc
was present, so the shipped DE25 build had NO false_path / net_delay / max_skew
on any ddr_stream CDC crossing (fail-open). Two causes:
  (a) example/de25_agilex5_ddr_validator/project.yml declared no sources.sdc, and
  (b) example/de25_agilex5_ddr_validator did not vendor the scoped SDC file.

FIX:
  1. Vendor constraints/ddr_stream_cdc_scoped.sdc into
     example/de25_agilex5_ddr_validator/constraints/ddr_stream_cdc_scoped.sdc
     (byte-identical to the IP canonical).
  2. Declare `sources.sdc: [constraints/ddr_stream_cdc_scoped.sdc]` in
     example/de25_agilex5_ddr_validator/project.yml.

This test asserts at the surface the Quartus build and routertl constraints
resolver reach:
  - project.yml explicitly declares sources.sdc naming the scoped SDC;
  - the vendored file exists on disk and resolves;
  - the vendored file matches the IP canonical (no silent staleness);
  - routertl's resolve_constraint_files returns the scoped SDC for the build;
  - hardware.vendor is altera (so Quartus SDC flow applies).
"""
from __future__ import annotations

from pathlib import Path
import pytest
import yaml

from routertl_core.constraints_resolver import resolve_constraint_files

REPO_ROOT = Path(__file__).resolve().parent.parent
DE25_DIR = REPO_ROOT / "example" / "de25_agilex5_ddr_validator"
DE25_PROJECT_YML = DE25_DIR / "project.yml"
SDC_CANONICAL = REPO_ROOT / "constraints" / "ddr_stream_cdc_scoped.sdc"
_SDC_REF = "constraints/ddr_stream_cdc_scoped.sdc"


def test_de25_validator_declares_sources_sdc():
    """Fail-open guard: example/de25_agilex5_ddr_validator/project.yml must
    declare sources.sdc naming constraints/ddr_stream_cdc_scoped.sdc."""
    assert DE25_PROJECT_YML.is_file(), f"CDC FAIL-OPEN: {DE25_PROJECT_YML} is missing."
    doc = yaml.safe_load(DE25_PROJECT_YML.read_text(encoding="utf-8")) or {}
    sdc_refs = (((doc.get("sources") or {}).get("sdc")) or [])
    assert _SDC_REF in sdc_refs, (
        f"CDC FAIL-OPEN: de25_agilex5_ddr_validator sources.sdc does not name "
        f"{_SDC_REF} — declared refs {sdc_refs!r} (DDR-T2.20)."
    )


def test_de25_vendored_sdc_present_and_resolves():
    """File presence guard: the vendored SDC must exist on disk under the validator."""
    vendored = DE25_DIR / _SDC_REF
    assert vendored.is_file(), (
        f"CDC FAIL-OPEN: the validator's vendored Quartus SDC {vendored} is "
        f"missing from disk — unconstrained async crossings (DDR-T2.20)."
    )
    assert SDC_CANONICAL.is_file(), f"CDC FAIL-OPEN: IP canonical {SDC_CANONICAL} is missing."


def test_de25_vendored_sdc_matches_canonical():
    """Staleness guard: the copy vendored into example/de25_agilex5_ddr_validator
    must be byte-identical to constraints/ddr_stream_cdc_scoped.sdc."""
    vendored = DE25_DIR / _SDC_REF
    assert vendored.is_file(), f"Vendored SDC {vendored} missing."
    assert SDC_CANONICAL.is_file(), f"Canonical SDC {SDC_CANONICAL} missing."
    vendored_text = vendored.read_text(encoding="utf-8")
    canonical_text = SDC_CANONICAL.read_text(encoding="utf-8")
    assert vendored_text == canonical_text, (
        "CDC constraint STALENESS: the Quartus SDC vendored into "
        "example/de25_agilex5_ddr_validator has DRIFTED from the IP canonical "
        "constraints/ddr_stream_cdc_scoped.sdc. Re-vendor it (copy the canonical "
        "over the stale copy) so the validator does not apply outdated CDC selectors."
    )


def test_de25_resolved_sdc_via_routertl_resolver():
    """Caller-surface guard: routertl's canonical constraint resolver must
    include constraints/ddr_stream_cdc_scoped.sdc in SDC_FILES for the DE25 build."""
    assert DE25_PROJECT_YML.is_file(), f"{DE25_PROJECT_YML} missing."
    doc = yaml.safe_load(DE25_PROJECT_YML.read_text(encoding="utf-8")) or {}
    resolved = resolve_constraint_files(
        doc, DE25_DIR, categories=("sdc",), vendor="altera",
    )
    assert _SDC_REF in resolved, (
        f"CDC FAIL-OPEN: routertl constraints_resolver resolved SDC {resolved!r}, "
        f"which does not contain {_SDC_REF!r} — Quartus build will omit the scoped SDC (DDR-T2.20)."
    )


def test_de25_validator_targets_altera_vendor():
    """Toolchain dialect guard: the validator must target vendor 'altera'."""
    assert DE25_PROJECT_YML.is_file(), f"{DE25_PROJECT_YML} missing."
    doc = yaml.safe_load(DE25_PROJECT_YML.read_text(encoding="utf-8")) or {}
    vendor = ((doc.get("hardware") or {}).get("vendor"))
    assert vendor == "altera", (
        f"de25_agilex5_ddr_validator hardware.vendor is {vendor!r}, not 'altera' — "
        f"the Quartus-dialect SDC would be applied to the wrong toolchain."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

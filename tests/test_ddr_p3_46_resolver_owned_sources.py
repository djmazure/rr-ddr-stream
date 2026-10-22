# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DDR-P3.46 regression guard: the example/ and target configs let the
resolver place their sources.

Pure-routertl rule: a project hand-places sources only where rr cannot. On
2026-10-20 the example and target configs carried ten hand-placed sites:

* Seven ``paths.libraries`` blocks. ``paths.libraries`` is not a schema key.
  The engine never reads it (routertl sdk/cli/commands/hardware.py names it a
  typo for ``sources.libraries``), so each block looked like a library mapping
  and did nothing. Removing all seven left every ``build_env.tcl`` /
  ``build_env.mk`` byte-identical. The control run confirmed this: a bogus
  library under ``paths.libraries`` never reached ``g_libraries``, and the
  same entry under ``sources.libraries`` did.
* Three hand-listed ``sources.syn`` lists (de25_led_smoke,
  quartus_std_high_base, targets/de25_agilex5_smoke). The resolver now places
  the same files into the same libraries on its own: the Intel xpm-body stubs,
  ``rr`` for the rtl/ units, and ``olo`` for the open-logic packages.
  RTL-P3.672 (closed) removed the reason docs/de25_agilex5_bringup.md gave for
  the manual ``syn`` + ``libraries.rr`` pair on Quartus Pro.

These tests keep that from regressing. Each site that still hand-places
sources must be on the census below, with the reason it stays.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Hand-placed source sites that remain, with the reason each one stays. Each
# entry must still hand-place sources; an entry that no longer does is stale
# and fails the census.
_HAND_PLACED_CENSUS = {
    # These four are outside the DDR-P3.46 measured list and still need a
    # follow-up ticket. quartus_smoke has the same shape as the migrated
    # targets/de25_agilex5_smoke.yml. vivado_smoke cites routertl RTL-P3.512
    # (per-vendor ip.yml synthesis.sources union); the two single-plane
    # variants cite nothing.
    "targets/quartus_smoke.yml": "outside DDR-P3.46 scope; follow-up to migrate",
    "targets/vivado_smoke.yml": "RTL-P3.512",
    "targets/vivado_smoke_read_only.yml": "outside DDR-P3.46 scope; follow-up to migrate",
    "targets/vivado_smoke_write_only.yml": "outside DDR-P3.46 scope; follow-up to migrate",
}


def _config_files() -> list[Path]:
    files = set(_REPO_ROOT.glob("example/*/project.yml"))
    files |= set(_REPO_ROOT.glob("example/*/targets/*.yml"))
    files |= set(_REPO_ROOT.glob("targets/*.yml"))
    return sorted(files)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _rel(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


def test_config_census_is_not_empty() -> None:
    """The glob must reach the configs this guard is about."""
    rels = {_rel(p) for p in _config_files()}
    for expected in (
        "example/zybo_z7_20_ddr_validator/project.yml",
        "example/axe5_eagle_lpddr4_validator/targets/jtag.yml",
        "example/de25_led_smoke/project.yml",
        "targets/de25_agilex5_smoke.yml",
    ):
        assert expected in rels, f"census glob missed {expected}"


def test_no_inert_paths_libraries() -> None:
    """``paths.libraries`` is not a schema key, and rr ignores it."""
    offenders = [
        _rel(p)
        for p in _config_files()
        if isinstance(_load(p).get("paths"), dict) and "libraries" in _load(p)["paths"]
    ]
    assert not offenders, (
        "INERT_PATHS_LIBRARIES: rr never reads paths.libraries, so it looks like a "
        "library mapping and does nothing. Remove it, or use sources.libraries "
        f"and cite the open RTL gap that needs it: {offenders}"
    )


def test_hand_placed_sources_are_on_census() -> None:
    """Each sources.syn / sources.libraries site must be on the census."""
    found: dict[str, list[str]] = {}
    for p in _config_files():
        sources = _load(p).get("sources")
        if not isinstance(sources, dict):
            continue
        keys = [k for k in ("syn", "libraries") if sources.get(k)]
        if keys:
            found[_rel(p)] = keys

    uncited = {f: k for f, k in found.items() if f not in _HAND_PLACED_CENSUS}
    assert not uncited, (
        "UNCITED_HAND_PLACEMENT: these configs hand-place sources the resolver "
        "can place. Remove them, or add a census entry that cites the open RTL "
        f"ticket for the gap: {uncited}"
    )

    stale = sorted(set(_HAND_PLACED_CENSUS) - set(found))
    assert not stale, f"STALE_CENSUS: these entries no longer hand-place sources: {stale}"

    for rel, reason in _HAND_PLACED_CENSUS.items():
        if reason.startswith("RTL-"):
            text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
            assert reason in text, f"{rel} must cite {reason} in a comment"


def test_de25_bringup_doc_does_not_prescribe_manual_libraries() -> None:
    """The DE25 doc must not tell readers to hand-list syn + libraries.rr."""
    doc = (_REPO_ROOT / "docs" / "de25_agilex5_bringup.md").read_text(encoding="utf-8")
    assert "REQUIRED for Quartus Pro" not in doc, (
        "STALE_MANUAL_PATTERN: docs/de25_agilex5_bringup.md still prescribes the "
        "manual sources.syn + sources.libraries.rr pair. The resolver now does this "
        "on its own (RTL-P3.672 is closed; DDR-P3.46 synth evidence)."
    )
    assert "BOTH" not in doc or "sources.libraries.rr" not in doc, (
        "STALE_MANUAL_PATTERN: the doc still says each rtl unit goes in BOTH "
        "sources.syn and sources.libraries.rr"
    )

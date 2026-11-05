# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""DDR-P3.42 regression guard: the AXE5-Eagle example ships publicly.

The first public snapshot dropped example/axe5_eagle_lpddr4_validator/ through
PRIVATE_PATHS, because restricted names ran through it and its build depended
on a private repository: the HPS subsystem came from an absolute bench path,
through a generator script that called SDK internals, and project.yml pinned a
host Quartus install. The example is now rewritten as an rr-only build and is
part of the public tree.

These guards need no private input. The restricted-name scan of the same tree
is test_ddr_p2_31_public_release.py::test_head_public_tree_is_clean and
test_public_tree_clean.py (owner-bench scanner).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = "example/axe5_eagle_lpddr4_validator"
sys.path.insert(0, str(REPO_ROOT / "tools"))
import public_release  # noqa: E402

# An absolute path on a build host. A public example must not name one.
HOST_PATH = re.compile(r"(?<![\w.:/])/(home|opt|tools|mnt|srv|Users|root)/")


def _tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "--", EXAMPLE],
                         capture_output=True, text=True, check=True,
                         env=public_release._git_env()).stdout
    return [f for f in out.splitlines() if f]


def _project() -> dict:
    return yaml.safe_load((REPO_ROOT / EXAMPLE / "project.yml").read_text()) or {}


def test_axe5_example_is_in_the_public_tree():
    """The tree a public snapshot would ship contains the example."""
    tree = public_release.public_tree(REPO_ROOT, "HEAD")
    files = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-tree", "-r",
                            "--name-only", tree], capture_output=True, text=True,
                           check=True, env=public_release._git_env()).stdout.split()
    shipped = {f for f in files if f.startswith(EXAMPLE + "/")}
    for need in ("project.yml", "ip.yml", "README.md",
                 "src/top_axe5_eagle_lpddr4_validator.vhd"):
        assert f"{EXAMPLE}/{need}" in shipped, (
            f"AXE5_EXAMPLE_NOT_PUBLIC: {EXAMPLE}/{need} is missing from the public "
            f"tree of HEAD (PRIVATE_PATHS={public_release.PRIVATE_PATHS})")


def test_axe5_example_is_self_contained():
    """No host path, no pinned install, every build input inside the example."""
    leaks = []
    for rel in _tracked():
        text = (REPO_ROOT / rel).read_text(errors="replace")
        for ln, line in enumerate(text.splitlines(), 1):
            if HOST_PATH.search(line):
                leaks.append(f"{rel}:{ln}")
    assert not leaks, f"AXE5_HOST_PATH: absolute host paths in the example: {leaks}"

    proj = _project()
    assert "tool_path" not in (proj.get("hardware") or {}), (
        "AXE5_PINNED_INSTALL: project.yml pins hardware.tool_path; pin a host "
        "install in a local target yml, not in the public project")

    inputs = [h["script"] for h in (proj.get("hooks") or {}).get("pre_build", [])]
    for stage in (proj.get("tcl_hooks") or {}).values():
        inputs += list(stage)
    inputs += [a["source"] for a in (proj.get("linux") or {}).get("apps", [])]
    inputs.append(proj["memory_map"])
    tracked = set(_tracked())
    outside = [p for p in inputs
               if f"{EXAMPLE}/{p}" not in tracked or ".." in Path(p).parts]
    assert not outside, (
        f"AXE5_INPUT_OUTSIDE_EXAMPLE: build inputs not tracked in the example: {outside}")


def test_axe5_hps_is_generated_by_rr():
    """The HPS comes from the rr ip.yml declaration, not a private generator."""
    ip = yaml.safe_load((REPO_ROOT / EXAMPLE / "ip.yml").read_text()) or {}
    system = ((ip.get("platform") or {}).get("hps") or {}).get("system")
    assert system == "hps_subsys", f"AXE5_NO_RR_HPS: ip.yml platform.hps.system={system!r}"
    hps = [i for i in ip.get("ips", []) if i.get("type") == "intel_agilex_5_soc"]
    assert len(hps) == 1, f"AXE5_NO_RR_HPS: expected one HPS ip, got {hps}"
    tracked = set(_tracked())
    for fact in ("ip/agilex_hps/agilex_hps.ip", "ip/emif_io96b_hps/emif_io96b_hps.ip"):
        assert f"{EXAMPLE}/{fact}" in tracked, f"AXE5_BOARD_FACT_MISSING: {fact}"

    sdk_users = [rel for rel in _tracked() if rel.endswith(".py")
                 and re.search(r"^\s*(from|import)\s+sdk\b",
                               (REPO_ROOT / rel).read_text(), re.M)]
    assert not sdk_users, (
        f"AXE5_PRIVATE_GENERATOR: example scripts call rr SDK internals: {sdk_users}")

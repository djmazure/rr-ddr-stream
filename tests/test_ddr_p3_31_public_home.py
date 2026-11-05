# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""DDR-P3.31 regression guard — the public mirror's documents tell a public
reader where the package lives, how to install it, and under which license.

0.3.5 and 0.3.6 were published from github.com/djmazure/rr-ddr-stream
(DDR-P2.31), but the README those snapshots carry still said the license was
"TBD — slated for the routertl public-registry pass", never named the public
repository or the install command, and linked its lineage to a private
Bitbucket repository a public reader cannot open. ip.yml and LICENSE said MIT
the whole time, so the registry and the README disagreed.

Every check reads the git index, not the working tree: a fresh clone and the
mirror snapshot get the tracked files, and during a commit the index holds the
staged files. Paths in ``PRIVATE_PATHS`` never reach the mirror and are not
checked. No network.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
from public_release import PRIVATE_PATHS, PUBLIC_MIRROR_URL  # noqa: E402

# Hosts of repositories that are private to the owner. A link to them from a
# public document is a dead link for every public reader.
PRIVATE_HOSTS = ("bitbucket.org",)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True,
                          capture_output=True, text=True).stdout


def _indexed(path: str) -> str:
    return _git("show", f":{path}")


def _public_markdown() -> list[str]:
    files = _git("ls-files", "-z", "--", "*.md").split("\0")
    return [f for f in files
            if f and not any(f.startswith(p) for p in PRIVATE_PATHS)]


def _manifest() -> dict:
    return yaml.safe_load(_indexed("ip.yml")) or {}


def _section(markdown: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)",
                  markdown, re.M | re.S)
    assert m, f"README_SECTION_MISSING: no '## {heading}' section in README.md"
    return m.group(1)


def test_readme_license_matches_manifest():
    license_id = str(_manifest().get("license", ""))
    assert license_id, "ip.yml declares no license"
    assert license_id in _indexed("LICENSE").splitlines()[0], (
        f"LICENSE does not start with the ip.yml license {license_id}")
    body = _section(_indexed("README.md"), "License")
    assert license_id in body and "LICENSE" in body and "TBD" not in body, (
        f"README_LICENSE_MISMATCH: README License section does not state the "
        f"ip.yml license {license_id} and point at LICENSE:\n{body.strip()}")


def test_readme_and_manifest_name_public_home():
    manifest = _manifest()
    ref = f"{manifest['namespace']}/{manifest['name']}"
    readme = _indexed("README.md")
    host_path = PUBLIC_MIRROR_URL.removeprefix("https://")
    missing = [s for s in (PUBLIC_MIRROR_URL, f"rr pkg add {ref}",
                           "tools/public_release.py") if s not in readme]
    assert not missing, (
        f"README_PUBLIC_HOME_MISSING: README.md does not name {missing}")
    assert host_path in _indexed("ip.yml"), (
        f"MANIFEST_PUBLIC_HOME_MISSING: ip.yml does not name {host_path}")


def test_public_markdown_has_no_private_host_links():
    hits = []
    for path in _public_markdown():
        for n, line in enumerate(_indexed(path).splitlines(), 1):
            if any(h in line for h in PRIVATE_HOSTS):
                hits.append(f"{path}:{n}: {line.strip()}")
    assert not hits, (
        "PRIVATE_HOST_LINK: public Markdown links a private repository:\n"
        + "\n".join(hits))

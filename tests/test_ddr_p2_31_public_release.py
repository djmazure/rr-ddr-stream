# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""DDR-P2.31 regression guard — the public release channel.

rr_ddr_stream 0.3.2 and 0.3.4 were published with ``rr pkg publish`` from the
private checkout, so the registry recorded the private repository as their
``git_url``: 0.3.4 could not be fetched by a public consumer and 0.3.2 named a
tree that carries a restricted name. Nothing between the tag and the publish
scanned the tree that the registry would hand out.

``tools/public_release.py`` is now the release path. These tests drive its CLI
(exit code and output, the surface the release runbook calls) against local
fixture repositories. No network: every remote is a local path, and ``rr`` is
a fake that only records that it ran. The restricted-name list stays private,
so a fixture scanner with the same CLI contract flags the token below.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "tools" / "public_release.py"
TOKEN = "RESTRICTED_FIXTURE_TOKEN"

FAKE_SCANNER = textwrap.dedent(f"""\
    import argparse, subprocess, sys
    ap = argparse.ArgumentParser(); ap.add_argument("repo")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--exclude", nargs="*", default=[]); a = ap.parse_args()
    files = subprocess.run(["git", "-C", a.repo, "ls-tree", "-r", "--name-only",
                            a.ref], capture_output=True, text=True,
                           check=True).stdout.split("\\n")
    hits = 0
    for f in filter(None, files):
        if any(f.startswith(p) for p in a.exclude):
            continue
        t = subprocess.run(["git", "-C", a.repo, "show", f"{{a.ref}}:{{f}}"],
                           capture_output=True, text=True).stdout
        for ln, line in enumerate(t.splitlines(), 1):
            if "{TOKEN}" in line:
                hits += 1; print(f"name#0 {{f}}:{{ln}}")
    print(f"HITS: {{hits}}"); sys.exit(1 if hits else 0)
""")

FAKE_RR = textwrap.dedent("""\
    #!/bin/sh
    echo "$PWD $*" > "$RR_FAKE_LOG"
""")


def _env(tmp_path: Path, scanner: Path | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
    env["PUBLIC_TREE_SCAN_SCRIPT"] = str(scanner) if scanner else str(tmp_path / "absent.py")
    env["RR_FAKE_LOG"] = str(tmp_path / "rr.log")
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        env[k] = v
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True,
                          env=_env(repo, None)).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _source(tmp_path: Path, *, rtl: str = "-- clean\n",
            internal: str = "evidence\n", version: str = "0.9.1") -> Path:
    repo = tmp_path / "src"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "ip.yml", f'name: rr_ddr_stream\nversion: "{version}"\n')
    _write(repo, "rtl/x.vhd", rtl)
    _write(repo, "docs/internal/witness.md", internal)
    _write(repo, "example/axe5_eagle_lpddr4_validator/a.tcl", internal)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "src")
    return repo


def _mirror(tmp_path: Path, origin: str) -> Path:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _git(mirror, "init", "-q", "-b", "main")
    _write(mirror, "README.md", "previous public release\n")
    _git(mirror, "add", "-A")
    _git(mirror, "commit", "-q", "-m", "rr_ddr_stream 0.9.0")
    _git(mirror, "remote", "add", "origin", origin)
    return mirror


@pytest.fixture
def scanner(tmp_path: Path) -> Path:
    p = tmp_path / "scan.py"
    p.write_text(FAKE_SCANNER)
    return p


@pytest.fixture
def fake_rr(tmp_path: Path) -> Path:
    p = tmp_path / "rr"
    p.write_text(FAKE_RR)
    p.chmod(0o755)
    return p


def _run(tmp_path: Path, scanner: Path | None, *args: str):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True,
                          text=True, env=_env(tmp_path, scanner))


def test_check_refuses_restricted_name_in_public_tree(tmp_path, scanner):
    src = _source(tmp_path, rtl=f"-- built for {TOKEN}\n")
    r = _run(tmp_path, scanner, "check", "--repo", str(src), "--ref", "HEAD")
    assert r.returncode == 1 and "PUBLIC_RELEASE_REFUSED" in r.stderr, (
        f"RESTRICTED_NAME_NOT_REFUSED rc={r.returncode}\n{r.stdout}{r.stderr}")
    assert "rtl/x.vhd:1" in r.stderr


def test_check_ignores_private_paths_only(tmp_path, scanner):
    src = _source(tmp_path, internal=f"{TOKEN} bench evidence\n")
    r = _run(tmp_path, scanner, "check", "--repo", str(src), "--ref", "HEAD")
    assert r.returncode == 0 and "PUBLIC_RELEASE_CLEAN" in r.stdout, (
        f"PRIVATE_PATH_NOT_DROPPED rc={r.returncode}\n{r.stdout}{r.stderr}")


def test_check_refuses_without_scanner(tmp_path):
    src = _source(tmp_path)
    r = _run(tmp_path, None, "check", "--repo", str(src), "--ref", "HEAD")
    assert r.returncode == 2 and "PUBLIC_RELEASE_SCANNER_MISSING" in r.stderr, (
        f"UNSCANNED_TREE_PASSED rc={r.returncode}\n{r.stdout}{r.stderr}")


def test_snapshot_squashes_public_tree_onto_mirror(tmp_path, scanner):
    src = _source(tmp_path)
    mirror = _mirror(tmp_path, "https://github.com/djmazure/rr-ddr-stream.git")
    before = _git(mirror, "rev-parse", "HEAD")
    r = _run(tmp_path, scanner, "snapshot", "--repo", str(src), "--ref", "HEAD",
             "--mirror", str(mirror))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOT pushed" in r.stdout
    files = _git(mirror, "ls-tree", "-r", "--name-only", "v0.9.1").splitlines()
    leaked = [f for f in files if f.startswith(("docs/internal/", "example/axe5"))]
    assert not leaked, f"PRIVATE_PATH_IN_SNAPSHOT: {leaked}"
    assert files == ["ip.yml", "rtl/x.vhd"]
    assert _git(mirror, "rev-parse", "v0.9.1^{commit}") == _git(mirror, "rev-parse", "HEAD")
    assert _git(mirror, "rev-parse", "HEAD^") == before
    assert not _git(src, "for-each-ref", "refs/public-release/")


def test_snapshot_refuses_and_leaves_mirror_untouched(tmp_path, scanner):
    src = _source(tmp_path, rtl=f"-- built for {TOKEN}\n")
    mirror = _mirror(tmp_path, "https://github.com/djmazure/rr-ddr-stream.git")
    before = _git(mirror, "rev-parse", "HEAD")
    r = _run(tmp_path, scanner, "snapshot", "--repo", str(src), "--ref", "HEAD",
             "--mirror", str(mirror))
    assert r.returncode == 1 and "PUBLIC_RELEASE_REFUSED" in r.stderr, (
        f"DIRTY_SNAPSHOT_NOT_REFUSED rc={r.returncode}\n{r.stdout}{r.stderr}")
    assert _git(mirror, "rev-parse", "HEAD") == before
    assert not _git(mirror, "tag", "--list")


def _tagged_mirror(tmp_path: Path, origin: str, body: str) -> Path:
    mirror = _mirror(tmp_path, origin)
    _write(mirror, "ip.yml", 'name: rr_ddr_stream\nversion: "0.9.1"\n')
    _write(mirror, "rtl/x.vhd", body)
    _git(mirror, "add", "-A")
    _git(mirror, "commit", "-q", "-m", "rr_ddr_stream 0.9.1")
    _git(mirror, "tag", "-a", "v0.9.1", "-m", "v0.9.1")
    return mirror


def test_publish_refuses_private_origin(tmp_path, scanner, fake_rr):
    mirror = _tagged_mirror(tmp_path, "git@bitbucket.org:dasjimaz/rr-ddr-stream.git",
                            "-- clean\n")
    r = _run(tmp_path, scanner, "publish", "--mirror", str(mirror), "--rr", str(fake_rr))
    assert r.returncode == 1 and "not the public mirror" in r.stderr, (
        f"PRIVATE_ORIGIN_PUBLISHED rc={r.returncode}\n{r.stdout}{r.stderr}")
    assert not (tmp_path / "rr.log").exists(), "rr pkg publish ran"


def test_publish_refuses_restricted_name(tmp_path, scanner, fake_rr):
    mirror = _tagged_mirror(tmp_path, "git@github.com:djmazure/rr-ddr-stream.git",
                            f"-- built for {TOKEN}\n")
    r = _run(tmp_path, scanner, "publish", "--mirror", str(mirror), "--rr", str(fake_rr))
    assert r.returncode == 1 and "PUBLIC_RELEASE_REFUSED" in r.stderr, (
        f"RESTRICTED_NAME_PUBLISHED rc={r.returncode}\n{r.stdout}{r.stderr}")
    assert not (tmp_path / "rr.log").exists(), "rr pkg publish ran"


def test_publish_refuses_untagged_head(tmp_path, scanner, fake_rr):
    mirror = _tagged_mirror(tmp_path, "https://github.com/djmazure/rr-ddr-stream",
                            "-- clean\n")
    _git(mirror, "tag", "-d", "v0.9.1")
    r = _run(tmp_path, scanner, "publish", "--mirror", str(mirror), "--rr", str(fake_rr))
    assert r.returncode == 1 and "is not tagged v0.9.1" in r.stderr, r.stdout + r.stderr
    assert not (tmp_path / "rr.log").exists(), "rr pkg publish ran"


def test_publish_runs_rr_from_clean_public_mirror(tmp_path, scanner, fake_rr):
    mirror = _tagged_mirror(tmp_path, "git@github.com:djmazure/rr-ddr-stream.git",
                            "-- clean\n")
    r = _run(tmp_path, scanner, "publish", "--mirror", str(mirror), "--rr", str(fake_rr))
    assert r.returncode == 0, r.stdout + r.stderr
    log = (tmp_path / "rr.log").read_text().split()
    assert log == [str(mirror), "pkg", "publish"]



def test_head_public_tree_is_clean():
    """This repository's HEAD is clean outside the private paths.

    Main was red here before DDR-P2.31: three comment lines added under
    DDR-P2.23 named a downstream project. It calls the scanner directly with a
    literal exclusion list, so it depends on neither the release tool nor its
    drop list; test_snapshot_squashes_public_tree_onto_mirror covers those.
    Needs the owner-bench scanner, so it skips elsewhere unless
    RR_REQUIRE_PUBLIC_SCAN=1 (then a missing scanner fails).
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
    scanner = Path(env.get("PUBLIC_TREE_SCAN_SCRIPT")
                   or Path.home() / ".agent-wt" / "mc" / "bin" / "public_tree_scan.py")
    if not scanner.is_file():
        assert env.get("RR_REQUIRE_PUBLIC_SCAN") != "1", f"scanner missing: {scanner}"
        pytest.skip("restricted-name scanner is private to the owner bench")
    r = subprocess.run([sys.executable, str(scanner), str(REPO_ROOT), "--ref", "HEAD",
                        "--exclude", "docs/internal/",
                        "example/axe5_eagle_lpddr4_validator/"],
                       capture_output=True, text=True, env=env)
    lines = (r.stdout + r.stderr).splitlines()
    assert r.returncode == 0 and "HITS: 0" in lines, (
        f"HEAD_PUBLIC_TREE_NOT_CLEAN rc={r.returncode}\n" + "\n".join(lines[-20:]))

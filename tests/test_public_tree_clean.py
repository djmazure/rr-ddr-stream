# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""DDR-P2.26 regression guard — scrub restricted names and bench identifiers.

Verifies that the public tree (everything except docs/internal/ and
example/axe5_eagle_lpddr4_validator/) is free of restricted client names
and bench identifiers (cable serials, USB serials, home directory paths).

The restricted-name list is private by design and is never copied into this
repo, so the guard needs the owner-bench scanner (``public_tree_scan.py``,
or the path in ``PUBLIC_TREE_SCAN_SCRIPT``). Where the scanner is absent
(a public clone) the tests SKIP with an explicit reason, unless
``RR_REQUIRE_PUBLIC_SCAN=1`` is set, in which case a missing scanner FAILS.

The scanner's exit code is authoritative: 0 plus a ``HITS: 0`` line = clean;
1 with ``name#``/``ident#`` hit lines = hits (fail, the lines are printed);
anything else (crash, missing deps, silent output) = FAIL, never pass.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_REASON = (
    "restricted-name scanner is private to the owner bench; the public "
    "snapshot is scanned at publish time by the publisher"
)


def _find_scanner() -> Path:
    override = os.environ.get("PUBLIC_TREE_SCAN_SCRIPT")
    if override and Path(override).is_file():
        return Path(override)
    candidate = Path.home() / ".agent-wt" / "mc" / "bin" / "public_tree_scan.py"
    if candidate.is_file():
        return candidate
    if os.environ.get("RR_REQUIRE_PUBLIC_SCAN") == "1":
        pytest.fail(
            "RR_REQUIRE_PUBLIC_SCAN=1 but the public-tree scanner is missing"
        )
    pytest.skip(SKIP_REASON)


def _run_scan(repo: Path, ref: str = "HEAD") -> tuple[int, str]:
    scanner = _find_scanner()
    cmd = [
        sys.executable,
        str(scanner),
        str(repo),
        "--ref",
        ref,
        "--exclude",
        "docs/internal/",
        "example/axe5_eagle_lpddr4_validator/",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def _assert_clean(repo: Path = REPO_ROOT) -> None:
    rc, out = _run_scan(repo)
    lines = out.splitlines()
    hits = [l for l in lines if l.startswith(("name#", "ident#"))]
    if rc == 0 and "HITS: 0" in lines and not hits:
        return
    # rc 1 is "hits" only when the scanner actually printed hit lines; a
    # crash (e.g. SystemExit("msg")) also exits 1 but prints none.
    if rc == 1 and hits:
        pytest.fail(
            "PUBLIC_TREE_NOT_CLEAN: scanner reported hits:\n"
            + "\n".join(hits[:20])
        )
    pytest.fail(
        f"PUBLIC_TREE_SCAN_ERROR: scanner exited {rc} without a clean HITS: 0 "
        "verdict or hit lines:\n"
        + "\n".join(lines[-20:])
    )


def test_no_restricted_names():
    """No restricted names in the public git tree (scanner rc must be 0)."""
    _assert_clean()


def test_no_bench_identifiers():
    """No bench identifiers in the public git tree (scanner rc must be 0)."""
    _assert_clean()


def test_guard_fails_when_scanner_crashes(tmp_path, monkeypatch):
    """A scanner that crashes and prints nothing must FAIL the guard."""
    crash = tmp_path / "crash_scan.py"
    crash.write_text('raise SystemExit("crashed")\n')
    monkeypatch.setenv("PUBLIC_TREE_SCAN_SCRIPT", str(crash))
    with pytest.raises(pytest.fail.Exception, match="PUBLIC_TREE_SCAN_ERROR"):
        _assert_clean()


def test_guard_fails_when_scanner_reports_hits(tmp_path, monkeypatch):
    """Exit 1 (hits) must FAIL the guard and surface the hit lines."""
    fake = tmp_path / "hit_scan.py"
    fake.write_text('print("name#R1 x.txt:1 context")\nraise SystemExit(1)\n')
    monkeypatch.setenv("PUBLIC_TREE_SCAN_SCRIPT", str(fake))
    with pytest.raises(pytest.fail.Exception, match="PUBLIC_TREE_NOT_CLEAN"):
        _assert_clean()


def test_missing_scanner_fails_when_required(monkeypatch):
    """RR_REQUIRE_PUBLIC_SCAN=1 turns a missing scanner into a failure."""
    monkeypatch.setenv("PUBLIC_TREE_SCAN_SCRIPT", "/nonexistent/scan.py")
    monkeypatch.setenv("HOME", "/nonexistent-home")
    monkeypatch.setenv("RR_REQUIRE_PUBLIC_SCAN", "1")
    # pytest's skip/fail outcomes are not Exception subclasses: catch broadly so
    # a regression that SKIPS instead of failing turns this test red, not skipped.
    with pytest.raises(BaseException) as ei:
        _find_scanner()
    assert ei.type is pytest.fail.Exception, f"expected a failure, got {ei.type.__name__}"
    assert "RR_REQUIRE_PUBLIC_SCAN" in str(ei.value)


def test_missing_scanner_skips_by_default(monkeypatch):
    """Without the env var a missing scanner skips, stating why."""
    monkeypatch.setenv("PUBLIC_TREE_SCAN_SCRIPT", "/nonexistent/scan.py")
    monkeypatch.setenv("HOME", "/nonexistent-home")
    monkeypatch.delenv("RR_REQUIRE_PUBLIC_SCAN", raising=False)
    with pytest.raises(pytest.skip.Exception, match="private to the owner bench"):
        _find_scanner()

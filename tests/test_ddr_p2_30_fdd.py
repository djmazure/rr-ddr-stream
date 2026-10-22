# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DDR-P2.30 regression guard: rr_ddr_stream ships a complete, current FDD.

The v0.3.4 publish printed the advisory missing-fdd finding: the IP had no
FDD.md. The FDD is an aggregate view of requirements.yml / ip.yml plus
engineer-authored prose (``rr fdd --help``). These tests keep it from going
missing, from regressing to the scaffold placeholders, and from drifting away
from the declarative files or the tree it cites.

* ``rr fdd check --strict --full`` is the gate a caller reaches: presence,
  every prose section filled in, and the machine-owned block re-rendered and
  diffed against the file (so an edit to requirements.yml or ip.yml without
  ``rr fdd generate`` fails here).
* Every repo path the prose cites in backticks must exist, so a renamed test
  file or RTL unit does not leave the verification summary pointing at nothing.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FDD = _REPO_ROOT / "FDD.md"
_IP = "rr_ddr_stream"

# Directories whose files the FDD prose cites by path or bare file name.
_CITED_ROOTS = ("rtl", "sim", "tests", "constraints", "docs", "example")


def _tracked_copy(dest: Path) -> Path:
    """Copy the git-tracked files of the working tree into ``dest``.

    ``rr fdd check`` discovers every requirements.yml under its root. Untracked
    copies of this repo (a nested checkout, a scratch dir) would be checked as
    well, so the gate is run on the tracked tree only — what a clean checkout
    and ``rr pkg publish`` see.
    """
    proc = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_REPO_ROOT, capture_output=True, check=True,
    )
    for rel in proc.stdout.decode().split("\0"):
        src = _REPO_ROOT / rel
        if rel and src.is_file():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    return dest


def test_fdd_check_strict_full_passes(tmp_path: Path) -> None:
    """The documentation gate is green for this IP in strict, full mode."""
    if shutil.which("rr") is None:
        pytest.skip("rr is not on PATH")
    root = _tracked_copy(tmp_path / "tree")
    proc = subprocess.run(
        ["rr", "fdd", "check", "--ip", _IP, "--strict", "--full", "--json"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    start = proc.stdout.find("{")
    assert start >= 0, f"rr fdd check printed no JSON:\n{proc.stdout}\n{proc.stderr}"
    report = json.loads(proc.stdout[start:])
    findings = [
        f"{f['kind']}: {f['detail']}" for f in report.get("findings", [])
        if f.get("ip") == _IP
    ]
    assert not findings, "rr fdd check findings for rr_ddr_stream:\n" + "\n".join(findings)
    assert report.get("checked") == 1, f"rr fdd check did not check {_IP}: {report}"
    assert proc.returncode == 0, f"rr fdd check exit {proc.returncode}:\n{proc.stdout}\n{proc.stderr}"


def _authored_text() -> str:
    text = _FDD.read_text(encoding="utf-8")
    head, rest = text.split("<!-- rr:fdd:generated:begin -->", 1)
    _, tail = rest.split("<!-- rr:fdd:generated:end -->", 1)
    return head + tail


def _repo_file_names() -> set[str]:
    names: set[str] = set()
    for root in _CITED_ROOTS:
        for path in (_REPO_ROOT / root).rglob("*"):
            if path.is_file():
                names.add(path.name)
    return names


def test_fdd_cited_paths_exist() -> None:
    """Every file the authored prose names in backticks is in the tree."""
    assert _FDD.is_file(), "FDD.md is missing"
    authored = _authored_text()
    spans = re.findall(r"`([^`\s]+)`", authored)
    paths = [s for s in spans if s.split("/", 1)[0] in _CITED_ROOTS and "/" in s]
    files = [s for s in spans if "/" not in s and re.fullmatch(r"[\w*-]+\.(py|vhd|md|xdc|sdc)", s)]
    assert paths and files, "FDD prose cites no repo paths; the extraction is broken"

    missing = [p for p in paths if "*" not in p and not (_REPO_ROOT / p.rstrip("/")).exists()]
    names = _repo_file_names()
    for name in files:
        if "*" in name:
            rx = re.compile(re.escape(name).replace(r"\*", ".*"))
            if not any(rx.fullmatch(n) for n in names):
                missing.append(name)
        elif name not in names:
            missing.append(name)
    assert not missing, "FDD.md cites files that do not exist:\n" + "\n".join(sorted(set(missing)))


def _rtl(name: str) -> str:
    return (_REPO_ROOT / "rtl" / name).read_text(encoding="utf-8")


def test_fdd_empty_cdc_is_upload_fifo() -> None:
    """The 3-stage empty crossing is the upload FIFO's, and the FDD says so."""
    rtl = _rtl("ddr_fifo_intf.vhd")
    up = rtl.split("GEN_UPLOADER_BLOCK", 1)[1].split("GEN_DOWNLOADER_BLOCK", 1)[0]
    down = rtl.split("GEN_DOWNLOADER_BLOCK", 1)[1]
    assert "i_sync_cdc_empty" in up and "ddr_sync_cdc" not in down
    row = [l for l in _authored_text().splitlines() if "i_sync_cdc_empty" in l]
    assert len(row) == 1, "FDD must have one CDC row for i_sync_cdc_empty"
    assert row[0].startswith("| Upload FIFO empty"), row[0]
    assert "Download FIFO empty, inside" not in _authored_text()
    assert "`clk_ddr_i`" in row[0] and "`clk_sys_i`" in row[0]


def test_fdd_pipebusy_gating_is_per_backend() -> None:
    """pipeBusyDdr chip-select gating is AXI only; Avalon-MM uses rst_ddr_i."""
    axi, avmm = _rtl("ddr_stream_axi.vhd"), _rtl("ddr_stream_avmm.vhd")
    assert "pipeBusyDdr = '0'" in axi
    assert "pipeBusyDdr" not in avmm
    reset = _authored_text().split("### Reset", 1)[1].split("## Architecture", 1)[0]
    lines = [l for l in reset.splitlines() if "chip selects" in l and "pipeBusyDdr" in l or "rst_sys_i` alone" in l]
    assert lines, "Reset section lost the chip-select guard bullets"
    for line in lines:
        if "`pipeBusyDdr`" in line or "reset-busy period" in line:
            assert "`ddr_stream_axi`" in line, line
    assert any("`ddr_stream_avmm`" in l and "rst_ddr_i" in l for l in reset.splitlines())

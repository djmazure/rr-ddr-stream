"""DDR-P2.29 guard: the base-address CDC is waived only with its protocol.

RR-CDC-005 (routertl RTL-P2.1672, profile rr-cdc-vhdl-provisional) reports a
multi-bit value captured in another clock domain with no synchroniser. The IP
has four such captures, clk_sys -> clk_ddr:

    rtl/ddr_stream_axi.vhd   wr_base_addr_meta <= wr_base_addr_sys;
    rtl/ddr_stream_axi.vhd   rd_base_addr_meta <= rd_base_addr_sys;
    rtl/ddr_stream_avmm.vhd  wr_base_addr_meta <= wr_base_addr_sys;
    rtl/ddr_stream_avmm.vhd  rd_base_addr_meta <= rd_base_addr_sys;

They are quasi-static, not synchronised: the bits can resolve on different
clk_ddr edges, so the crossing is safe only while the source holds still.
DDR-REQ-211 states the protocol that holds it (RTL refuses a load while the
plane is busy; the host loads only while the plane holds no data). Each
capture carries ``-- rr-lint-disable-line RR-CDC-005`` naming that REQ, and
the path is bounded by the scoped XDC/SDC selector ``*_base_addr_meta``
(guarded separately by test_cdc_xdc_drift.py and
test_reset_request_crossing_bounded.py).

The guards:

* every capture carries the waiver AND names DDR-REQ-211 (a bare waiver hides
  the crossing without stating why it is safe);
* DDR-REQ-211 exists in requirements.yml and a cocotb test maps it, so the
  protocol the waivers lean on is a contract item with a test, not prose;
* ``rr lint --profile rr-cdc-vhdl-provisional`` reports no RR-CDC-005 on rtl/.
  This one needs an rr that ships RR-CDC-005; on an older rr it SKIPS, and
  says so, rather than passing on a rule that never ran.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REQ_ID = "DDR-REQ-211"
PROFILE = "rr-cdc-vhdl-provisional"
RULE = "RR-CDC-005"

# The live captures; the plane-disabled `else generate` tie-offs assign the
# reset constant and do not cross a domain.
_CAPTURE_RE = re.compile(r"^\s*(wr|rd)_base_addr_meta\s*<=\s*\1_base_addr_sys\s*;(.*)$")
_WAIVER_RE = re.compile(r"--\s*rr-lint-disable-line\s+RR-CDC-005\b(.*)$")

EXPECTED_CAPTURES = {
    "rtl/ddr_stream_axi.vhd": {"wr", "rd"},
    "rtl/ddr_stream_avmm.vhd": {"wr", "rd"},
}


def _captures(rel: str) -> list[tuple[int, str, str]]:
    found = []
    for lineno, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
        m = _CAPTURE_RE.match(line)
        if m:
            found.append((lineno, m.group(1), m.group(2)))
    return found


def test_base_addr_captures_carry_rr_cdc_005_waiver():
    problems = []
    for rel, planes in EXPECTED_CAPTURES.items():
        caps = _captures(rel)
        assert {plane for _, plane, _ in caps} == planes and len(caps) == len(planes), (
            f"{rel}: expected one {sorted(planes)} base_addr_meta capture each, "
            f"found {[(n, p) for n, p, _ in caps]} — the crossing moved; re-audit it"
        )
        for lineno, plane, tail in caps:
            m = _WAIVER_RE.search(tail)
            if not m:
                problems.append(f"{rel}:{lineno} {plane}_base_addr_meta: RR-CDC-005 waiver missing")
            elif REQ_ID not in m.group(1) or "quasi-static" not in m.group(1):
                problems.append(
                    f"{rel}:{lineno} {plane}_base_addr_meta: RR-CDC-005 waiver does not "
                    f"state its quasi-static protocol ({REQ_ID})"
                )
    assert not problems, "\n".join(problems)


def test_waiver_protocol_is_pinned_by_a_mapped_req():
    contract = yaml.safe_load((ROOT / "requirements.yml").read_text())
    reqs = {r["id"]: r for r in contract["functional_contract"]}
    assert REQ_ID in reqs, (
        f"{REQ_ID} missing from requirements.yml: the RR-CDC-005 waivers name a "
        "protocol no contract item states"
    )
    desc = reqs[REQ_ID]["desc"]
    for token in ("`CSR_WR_BASE_LOAD`", "`WRITE_IDLE`", "`CSR_RD_BASE_LOAD`", "`READ_IDLE`", RULE):
        assert token in desc, f"{REQ_ID} desc does not name {token}"

    mapped = [
        p.name
        for p in sorted((ROOT / "sim" / "cocotb" / "tests").glob("test_*.py"))
        if re.search(rf'@requires\(\s*"{REQ_ID}"', p.read_text())
    ]
    assert mapped, f"{REQ_ID} is not mapped by any @requires in sim/cocotb/tests"


def _rr_has_rule() -> bool:
    proc = subprocess.run(
        ["rr", "lint-profiles", "show", PROFILE],
        cwd=ROOT, capture_output=True, text=True,
    )
    return proc.returncode == 0 and RULE in proc.stdout


def _untracked() -> set[str]:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return {line[3:].strip() for line in proc.stdout.splitlines() if line.startswith("??")}


def test_rr_lint_reports_no_rr_cdc_005(tmp_path):
    if shutil.which("rr") is None:
        pytest.skip("rr is not on PATH")
    if not _rr_has_rule():
        pytest.skip(f"installed rr predates {RULE} (routertl RTL-P2.1672); lint surface not checked")

    # rr lint pre-flights libs/, which is gitignored; sync it, then drop
    # debris so the checkout stays clean. The committed ip.lock is current, so
    # sync leaves it unchanged. DDR-P3.47 guards that; it is not restored here.
    before = _untracked()
    findings_path = tmp_path / "findings.json"
    try:
        sync = subprocess.run(["rr", "workspace", "sync"], cwd=ROOT, capture_output=True, text=True)
        assert sync.returncode == 0, f"rr workspace sync failed:\n{sync.stdout}\n{sync.stderr}"
        proc = subprocess.run(
            ["rr", "lint", "--profile", PROFILE, "--src", "rtl", "--findings", str(findings_path)],
            cwd=ROOT, capture_output=True, text=True,
        )
    finally:
        for rel in _untracked() - before:
            path = ROOT / rel
            if path.is_dir():
                shutil.rmtree(path)
            elif path.is_file() or path.is_symlink():
                path.unlink()

    assert findings_path.is_file(), f"rr lint wrote no findings.json:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
    findings = json.loads(findings_path.read_text())["findings"]
    hits = [f"{f['file']}:{f['line']}" for f in findings if f.get("rule_id") == RULE]
    assert not hits, f"rr lint reports {RULE} on rtl/: {hits}"

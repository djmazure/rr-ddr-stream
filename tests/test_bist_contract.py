# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""RTL-T2.87 regression guard — the ddr_validator reference examples must ship a
VALID, TURNKEY BIST contract.

The RTL-P2.781 capstone (`rr hw bist zybo-z7-20 --bitstream ...`) promised a
zero-hand-scripting on-silicon verdict, but shipped no contract for its own
reference project, so CP1 of runbook RTL-P3.1005 aborted on real silicon with
"no BIST contract (looked for .../runtimes/hw.yml)". This guard asserts three
things that would have caught that:

1. The example ships `runtimes/hw.yml` (existence — the direct T2.87 miss).
2. It VALIDATES against rr.hw.contract/v1 (a malformed contract is as useless as
   a missing one).
3. The recipe is CORRECT: a healthy-board register image yields req_status=pass,
   and a mismatch image yields a non-pass — asserting the EFFECT (the verdict),
   not merely that a file is present (the detection-vs-effect discipline the
   runbook queue exists to enforce).

Skips cleanly when the routertl `sdk` package is not importable (contract engine
lives there), so the rr-ddr-stream suite stays runnable standalone.
"""
from __future__ import annotations

from pathlib import Path

import pytest

sdk_hw = pytest.importorskip("sdk.engine.hardware.contract")
from sdk.engine.hardware.contract import ContractValidationError, load_contract
from sdk.engine.hardware.executor import find_requirement, run_contract
from sdk.engine.hardware.mock_probe import MockProbe

_REPO = Path(__file__).resolve().parents[1]
_CONTRACT = _REPO / "example" / "zybo_z7_20_ddr_validator" / "runtimes" / "hw.yml"
_REQ = "BIST-DDR-001"


def test_zybo_z7_20_bist_contract_is_shipped():
    """The exact file `rr hw bist zybo-z7-20` looks for (else it aborts CP1)."""
    assert _CONTRACT.is_file(), (
        f"no BIST contract at {_CONTRACT} — `rr hw bist zybo-z7-20` aborts "
        "turnkey (RTL-T2.87)"
    )


def test_zybo_z7_20_bist_contract_validates():
    error_msg = None
    try:
        contract = load_contract(_CONTRACT)
    except ContractValidationError as exc:  # pragma: no cover - failure detail
        error_msg = str(exc)
    assert error_msg is None, f"{_CONTRACT} failed rr.hw.contract/v1 validation:\n{error_msg}"
    assert contract["schema"] == "rr.hw.contract/v1"
    find_requirement(contract, _REQ)  # raises KeyError if the REQ id drifted


def _seed(status: str, ident: str = "0x0dd50200", err: str = "0x00000000") -> MockProbe:
    """A MockProbe imaged like a real board — values in the lowercase 8-digit
    form UbootConsoleProbe.peek returns (case/width matters: the executor does an
    exact string compare)."""
    p = MockProbe()
    p.set_register("0x4000003c", ident)   # IDENT
    p.set_register("0x40000004", status)  # STATUS
    p.set_register("0x40000010", err)     # ERROR_COUNT
    p.script_wait_for("0x40000004", satisfies_after_s=0.01, final_observed=status)
    return p


def _verdict(probe: MockProbe) -> str:
    contract = load_contract(_CONTRACT)
    run_log = run_contract(
        contract, _REQ, probe, mode="replay",
        project="zybo_z7_20", sof_sha256="x", git_rev="x",
    )
    return run_log["conclusion"]["req_status"]


def test_healthy_board_image_passes():
    # STATUS 0x36 = done|pass|write_idle|read_idle, ERROR_COUNT 0 — the proven
    # Zynq-7 silicon PASS image (reference_zynq7_uboot_fabric_bist_verdict).
    assert _verdict(_seed("0x00000036")) == "pass"


def test_mismatch_board_image_does_not_pass():
    # done|fail with 5 mismatches — the BIST must NOT report pass.
    assert _verdict(_seed("0x0000000a", err="0x00000005")) != "pass"


def test_wrong_ident_does_not_pass():
    # A dead/absent CSR (IDENT reads 0) must fail the sanity probe, not pass.
    assert _verdict(_seed("0x00000036", ident="0x00000000")) != "pass"


def test_zybo_z7_20_bist_contract_declares_falsifier():
    """DDR-P2.28 regression guard — BIST-DDR-001 must name a demonstrated_broken sibling."""
    error_msg = None
    try:
        contract = load_contract(_CONTRACT)
    except ContractValidationError as exc:
        error_msg = str(exc)
    assert error_msg is None, f"{_CONTRACT} failed rr.hw.contract/v1 validation:\n{error_msg}"
    req = find_requirement(contract, _REQ)
    falsifier_id = req.get("falsifier")
    assert falsifier_id is not None, f"{_REQ} missing falsifier key"
    assert falsifier_id != "none", f"{_REQ} cannot waive falsifier"
    falsifier = find_requirement(contract, falsifier_id)
    assert falsifier["expected_outcome"] == "demonstrated_broken"
    assert falsifier["falsifier"] == "none"
    assert falsifier.get("falsifier_waiver"), "falsifier sibling requires falsifier_waiver"


def test_falsifier_corrupted_pattern_demonstrates_broken():
    """DDR-P2.28 — negative control reports demonstrated_broken when mismatch occurs."""
    contract = load_contract(_CONTRACT)
    req = find_requirement(contract, _REQ)
    falsifier_id = req["falsifier"]
    # Seed a failing probe: STATUS 0x3a = done|fail|write_idle|read_idle, ERROR_COUNT 5
    mismatch_probe = _seed("0x0000003a", err="0x00000005")
    run_log = run_contract(
        contract, falsifier_id, mismatch_probe, mode="replay",
        project="zybo_z7_20", sof_sha256="x", git_rev="x",
    )
    assert run_log["conclusion"]["req_status"] == "demonstrated_broken"


def test_falsifier_healthy_image_yields_unexpected_pass():
    """DDR-P2.28 — clean assertions must NOT pass unconditionally under the negative control."""
    contract = load_contract(_CONTRACT)
    req = find_requirement(contract, _REQ)
    falsifier_id = req["falsifier"]
    # Healthy board (clean 0x36, 0 errors): assertions do NOT fail, so the bug did NOT reproduce
    clean_probe = _seed("0x00000036", err="0x00000000")
    run_log = run_contract(
        contract, falsifier_id, clean_probe, mode="replay",
        project="zybo_z7_20", sof_sha256="x", git_rev="x",
    )
    assert run_log["conclusion"]["req_status"] == "unexpected_pass"

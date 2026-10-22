# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DDR-P3.44 regression guard — structured delegated_to on DDR-REQ-RST-5.

Migration for RTL-P2.1691 / TIC-P2.246: REQs that delegate obligations to open
tickets must carry structured delegated_to entries with {ref, obligation}
rather than relying on prose alone, while preserving the prose context.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXPECTED_DELEGATION = [
    {
        "ref": "DDR-T2.15",
        "obligation": "pointer registers must not carry stale pre-reset position across reset",
    }
]


def test_ddr_req_rst_5_delegated_to() -> None:
    """DDR-REQ-RST-5 must carry the structured delegated_to entry for DDR-T2.15."""
    try:
        from sdk.cli.commands.contract import contract_group
    except ImportError:
        pytest.skip("RouteRTL SDK not importable")

    res = CliRunner().invoke(contract_group, ["report", "--json"])
    assert res.exit_code == 0, f"contract report failed:\n{res.output}"

    data = json.loads(res.output[res.output.index("{"):])
    reqs = {r["id"]: r for r in data["data"]["requirements"]}
    assert "DDR-REQ-RST-5" in reqs, "DDR-REQ-RST-5 not found in contract report"

    rst5 = reqs["DDR-REQ-RST-5"]
    delegated = rst5.get("delegated_to", [])
    assert delegated == _EXPECTED_DELEGATION, (
        f"DELEGATION_MISSING: DDR-REQ-RST-5 delegated_to {delegated} != {_EXPECTED_DELEGATION}"
    )


def test_prose_delegation_context_preserved() -> None:
    """The prose citing DDR-T2.15 must be preserved in DDR-REQ-RST-5."""
    try:
        from sdk.cli.requirements import load_requirements
    except ImportError:
        pytest.skip("RouteRTL SDK not importable")

    reqs = load_requirements(_REPO_ROOT / "requirements.yml")
    rst5 = next((r for r in reqs.functional_contract if r.id == "DDR-REQ-RST-5"), None)
    assert rst5 is not None, "DDR-REQ-RST-5 not found in requirements.yml"
    assert "DDR-T2.15" in rst5.desc, (
        "PROSE_STRIPPED: DDR-REQ-RST-5 description lost DDR-T2.15 citation"
    )

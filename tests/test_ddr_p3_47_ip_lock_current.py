# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DDR-P3.47 regression guard: ``rr workspace sync`` must leave the committed
ip.lock byte-identical.

On 2026-10-20 the v0.3.4 publish ran in a clean detached checkout of tag
v0.3.4 (8cfd7c9). ``rr workspace sync`` rewrote ip.lock (+18/-4): it added the
``_meta`` header, ``provides_packages`` for en_cl_fix_resize and
``requested_version`` on every package, and changed ``library: work`` to
``library: olo`` on the four open-logic packages. ``rr pkg publish`` uploads
the working tree, so the published package carried the regenerated lock and
not the lock in the tag. Every RouteRTL worktree showed the same unstaged
drift.

``library: olo`` is the correct value. The RTL instantiates
``entity olo.olo_base_fifo_async`` / ``olo.olo_base_fifo_sync`` /
``olo.olo_base_wconv_*`` (rtl/support/rr_fifo_*.vhd), and the example projects
pin the same packages with ``library: olo``.

This test runs the real sync, the same command a publish runs, on an export of
the git index (the same files a fresh clone has). It fails if sync would
modify the lock. To fix a failure, commit the regenerated lock after you have
reviewed its diff. Do not skip or edit this test to clear it.

Environment: needs ``rr`` on PATH and read access to the package registry and
git cache. Without ``rr`` the test skips. Set ``RR_REQUIRE_LOCK_SYNC=1`` to make
a missing ``rr`` a failure.
"""
from __future__ import annotations

import difflib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOCK = "ip.lock"


def _export_index(dest: Path) -> None:
    """Write the tracked tree as git records it into ``dest``.

    This is the index, not the working tree. In a clean checkout the index
    equals HEAD, which is what a fresh clone has. During a commit it holds the
    staged lock. An in-place sync that already rewrote the working-tree lock
    cannot hide the drift: the test receipt harness does this when it
    provisions libs/, and so does any earlier run of rr.
    """
    subprocess.run(
        ["git", "checkout-index", "--all", f"--prefix={dest}/"],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
    )


def _require_rr() -> str:
    rr = shutil.which("rr")
    if rr is None:
        if os.environ.get("RR_REQUIRE_LOCK_SYNC") == "1":
            pytest.fail("RR_REQUIRE_LOCK_SYNC=1 but rr is not on PATH")
        pytest.skip("rr is not on PATH")
    return rr


def test_workspace_sync_leaves_committed_lock_unchanged(tmp_path):
    rr = _require_rr()
    work = tmp_path / "clone"
    _export_index(work)
    committed = (work / _LOCK).read_text(encoding="utf-8")

    proc = subprocess.run(
        [rr, "workspace", "sync"],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"rr workspace sync failed (rc={proc.returncode}):\n"
        f"{proc.stdout}\n{proc.stderr}"
    )

    synced = (work / _LOCK).read_text(encoding="utf-8")
    diff = "".join(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            synced.splitlines(keepends=True),
            fromfile="ip.lock (committed)",
            tofile="ip.lock (after rr workspace sync)",
        )
    )
    # A plain assert, so the stale-lock verdict is a recorded assertion and not
    # only a pytest.fail outcome.
    assert diff == "", (
        "IP_LOCK_STALE: rr workspace sync rewrites the committed ip.lock, "
        "so a clean tag checkout is dirty before publish (DDR-P3.47). "
        "Review the diff and commit the regenerated lock:\n" + diff
    )

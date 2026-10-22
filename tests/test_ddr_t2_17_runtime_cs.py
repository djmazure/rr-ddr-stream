"""DDR-T2.17 behavioural guards.

Each test runs one cocotb testcase against the elaborated IP. The desk-side
regex scan of rtl/ddr_stream_axi.vhd is not a guard and is not used here.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = "test_ddr_t2_17_runtime_cs"


def _untracked() -> set[str]:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    found: set[str] = set()
    for line in proc.stdout.splitlines():
        if line.startswith("??"):
            found.add(line[3:].strip())
    return found


def _sim(testcase: str) -> None:
    # libs/ is gitignored, so a fresh checkout (and a receipt checkout after
    # git clean) has no OpenLogic sources until workspace sync materialises them.
    before = _untracked()
    sync = subprocess.run(
        ["rr", "workspace", "sync"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    try:
        if sync.returncode != 0:
            raise AssertionError(
                "rr workspace sync failed "
                f"(exit {sync.returncode}):\n{sync.stdout}\n{sync.stderr}"
            )
        env = os.environ.copy()
        # Cocotb matches this against the qualified name
        # ``<module>.<test>`` via re.search, so the pattern is the test
        # function name with no anchors.
        env["COCOTB_TEST_FILTER"] = testcase
        proc = subprocess.run(
            ["rr", "sim", "run", MODULE, "--timeout", "90"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        blob = f"{proc.stdout}\n{proc.stderr}"
        # The sim log is large. Keep every line that carries a cocotb
        # assertion so a truncated tail cannot hide the failure the receipt
        # matches on.
        kept = [
            line for line in blob.splitlines()
            if (
                "Chip Select asserted during FIFO reset-busy" in line
                or "CS re-released before C_RESET_INIT_TIMEOUT" in line
                or "AssertionError" in line
            )
        ]
        assert proc.returncode == 0, (
            f"rr sim run {MODULE} {testcase} failed (exit {proc.returncode}):\n"
            + "\n".join(kept[-30:])
        )
    finally:
        # Drop untracked sim debris so a green run is not a dirty checkout.
        # The committed ip.lock is current, so sync leaves it unchanged
        # (guarded by DDR-P3.47); it is not restored here.
        for rel in _untracked() - before:
            path = ROOT / rel
            if path.is_dir():
                shutil.rmtree(path)
            elif path.is_file() or path.is_symlink():
                path.unlink()


def test_cs_low_every_cycle_while_pipe_busy():
    _sim("cs_low_every_cycle_while_pipe_busy")


def test_cs_rearms_init_timeout_after_runtime_busy():
    _sim("cs_rearms_init_timeout_after_runtime_busy")

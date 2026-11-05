"""DDR-P1.2 — the DDR-T2.15 invariant still fires in simulation.

DDR-P1.2 puts ``-- synthesis translate_off`` / ``translate_on`` around the
``postponed assert`` in ``rtl/ddr_stream_axi.vhd`` so that Synplify compiles
the file. Simulators must still run the check. This guard runs the cocotb
module ``test_ddr_p1_2_cs_invariant_fires``, which forces ``uploader_cs``
high while ``pipeBusyDdr`` is 1, and requires the VHDL report text in the
simulator log. A deleted assertion, or a pragma that reaches the simulator,
leaves the log without that line.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = "test_ddr_p1_2_cs_invariant_fires"
LOG = ROOT / "sim" / "cocotb" / "logs" / MODULE / f"{MODULE}.log"
# The `report` string in rtl/ddr_stream_axi.vhd. The cocotb test never prints
# it, so only the VHDL assertion can put it in the log.
VHDL_REPORT = (
    "ddr_stream_axi: Chip Select asserted during FIFO reset-busy (DDR-T2.15)"
)


def _untracked() -> set[str]:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return {line[3:].strip() for line in proc.stdout.splitlines()
            if line.startswith("??")}


def test_cs_invariant_still_fires_in_sim():
    before = _untracked()
    # A log left by an earlier run must not satisfy this one.
    if LOG.exists():
        LOG.unlink()
    try:
        # libs/ is gitignored, so a fresh (receipt) checkout has no OpenLogic
        # sources until workspace sync materialises them.
        sync = subprocess.run(
            ["rr", "workspace", "sync"],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert sync.returncode == 0, (
            f"rr workspace sync failed (exit {sync.returncode}):\n"
            f"{sync.stdout}\n{sync.stderr}")
        proc = subprocess.run(
            ["rr", "sim", "run", MODULE, "--wait", "--timeout", "120"],
            cwd=ROOT, capture_output=True, text=True,
        )
        blob = f"{proc.stdout}\n{proc.stderr}"
        log = LOG.read_text(errors="replace") if LOG.is_file() else ""
        assert proc.returncode == 0, (
            f"rr sim run {MODULE} failed (exit {proc.returncode}):\n"
            + "\n".join(blob.splitlines()[-30:]))
        assert "TESTS=1 PASS=1" in log, (
            f"{MODULE} did not report 1 passing test:\n"
            + "\n".join(log.splitlines()[-30:]))
        assert VHDL_REPORT in log, (
            "DDR-T2.15 invariant did not fire in simulation: the VHDL "
            "assertion report is missing from the sim log after forcing "
            "uploader_cs=1 with pipeBusyDdr=1")
    finally:
        for rel in _untracked() - before:
            path = ROOT / rel
            if path.is_dir():
                shutil.rmtree(path)
            elif path.is_file() or path.is_symlink():
                path.unlink()

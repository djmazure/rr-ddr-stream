"""DDR-T1.3 — the 16-boot interleaved DE25 cold-boot gate, asserted on CAPTURED DATA.

The ticket's acceptance is silicon, not sim: the DS-T1.12 wedge (RING_COMMIT_PTR
stuck at 0 while RING_WRITE_PTR advances, so no decoded capture ever reaches the
wire) never reproduced in any testbench, and an STA-clean fit can still be bad on
silicon. So this file judges the evidence committed with the witness
(``docs/internal/ddr_t1_3_witness/``), re-deriving every boot's verdict from the
RAW register readbacks and decoded bytes — never from the harness's own
``verdict`` field:

  * ``judge_gate`` — 16 boots, A/B strictly alternating (8 each), every boot
    programmed with the manifest's exact ``_hps.sof`` (md5) and running that image
    on silicon (BUILD_DATE/GIT_HASH read back over lwh2f), COMMIT advancing across
    the stimulus bursts and whenever WRITE advanced, COMMIT tracking WRITE at rest,
    and the daemon-armed SPI + UART bursts decoded byte-exact off the DDR ring.
  * The checker is proven falsifiable: the DS-T1.12 silicon snapshot (COMMIT=0,
    WRITE advancing) and each other mutant below is REJECTED.
  * The manifest's vendored rr_ddr_stream file hashes are checked against the git
    objects of the commit each image claims to carry (A = 55bb0af, the consumer's
    pin; B = the release-candidate main HEAD), so the witness cannot silently
    describe a different write plane than the one that was fitted.
  * The power-audit ledger quoted in the witness README is parsed: every line is
    this ticket's holder on outlet 5, every ON is followed by an OFF, the final
    state is OFF.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WITNESS = REPO / "docs" / "internal" / "ddr_t1_3_witness"
LEDGER = WITNESS / "boots.jsonl"
MANIFEST = WITNESS / "gate_manifest.json"
README = REPO / "docs" / "internal" / "DDR_T1_3_SILICON_GATE_WITNESS.md"

GATE_FAIL = "DDR-T1.3 SILICON GATE NOT MET"
SNAPS = ("R0", "R1", "R2", "R3")
REQUIRED_REGS = ("GIT_HASH", "BUILD_DATE", "RING_WRITE_PTR", "RING_COMMIT_PTR")
#: COMMIT is a byte offset into the ring, WRITE a count of enqueued 32-bit words.
#: Each enqueued word occupies >= 4 B of committed DDR, and a 256-bit (32 B) beat
#: carries >= 1 word — so a COMMIT that tracks WRITE moves 4..8 B per word. Measured
#: in the pilot (not assumed): every 6-word zs_msg frame lands as ONE 32 B beat
#: (SPI burst: dWRITE=102=17x6, dCOMMIT=544=17x32), i.e. 5.33 B/word, mid-band.
BEAT_BYTES = 32
MIN_BYTES_PER_WORD = 4
MAX_BYTES_PER_WORD = 8


def _require_witness(*paths: Path) -> None:
    for p in paths:
        if not p.is_file():
            if os.environ.get("RR_REQUIRE_PRIVATE_WITNESS") == "1":
                pytest.fail(
                    f"RR_REQUIRE_PRIVATE_WITNESS=1 but witness evidence is missing: {p}"
                )
            pytest.skip(f"witness evidence is absent under {p}")


def load_ledger(path: Path = LEDGER) -> list[dict]:
    _require_witness(path)
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def load_manifest(path: Path = MANIFEST) -> dict:
    _require_witness(path)
    return json.loads(path.read_text())


def judge_boot(rec: dict, manifest: dict) -> list[str]:
    """Reasons this boot is NOT a pass (empty list = pass)."""
    img = manifest["images"].get(rec.get("image"))
    why: list[str] = []
    if img is None:
        return [f"boot {rec.get('boot')}: unknown image {rec.get('image')!r}"]
    tag = f"boot {rec.get('boot')} ({rec.get('image')})"
    if rec.get("sof_md5") != img["hps_sof_md5"]:
        why.append(f"{tag}: programmed SOF md5 {rec.get('sof_md5')} is not the manifest's")
    if rec.get("program_rc") != 0:
        why.append(f"{tag}: rr program run rc={rec.get('program_rc')}")
    regs = rec.get("regs") or {}
    if any(not isinstance(regs.get(s), dict)
           or any(not isinstance(regs[s].get(r), int) for r in REQUIRED_REGS)
           for s in SNAPS):
        return why + [f"{tag}: register snapshots R0..R3 incomplete"]
    for s in SNAPS:
        if regs[s]["BUILD_DATE"] != int(img["build_date"], 16):
            why.append(f"{tag}: silicon BUILD_DATE {regs[s]['BUILD_DATE']:#010x} at {s} "
                       f"is not image {rec['image']}'s {img['build_date']}")
            break
    for s in SNAPS:
        if regs[s]["GIT_HASH"] != int(img["git_hash"], 16):
            why.append(f"{tag}: silicon GIT_HASH {regs[s]['GIT_HASH']:#010x} mismatch")
            break
    w = [regs[s]["RING_WRITE_PTR"] for s in SNAPS]
    c = [regs[s]["RING_COMMIT_PTR"] for s in SNAPS]
    if not c[2] > c[0]:
        why.append(f"{tag}: COMMIT did not advance across the bursts ({c[0]:#x} -> {c[2]:#x})")
    for a, b in ((0, 1), (1, 2)):
        dw, dc = w[b] - w[a], c[b] - c[a]
        if dw <= 0:
            why.append(f"{tag}: stimulus R{a}->R{b} produced no ingress "
                       f"(WRITE {w[a]:#x} -> {w[b]:#x})")
        elif not MIN_BYTES_PER_WORD * dw <= dc <= MAX_BYTES_PER_WORD * dw:
            why.append(f"{tag}: COMMIT did not track WRITE R{a}->R{b} "
                       f"(dWRITE={dw} words, dCOMMIT={dc} B)")
    if w[3] > w[2] and not c[3] > c[2]:
        why.append(f"{tag}: WRITE advanced R2->R3 but COMMIT did not")
    if c[3] == 0:
        why.append(f"{tag}: COMMIT is 0 at rest")
    if not MIN_BYTES_PER_WORD * w[3] - BEAT_BYTES < c[3] <= MAX_BYTES_PER_WORD * w[3] + BEAT_BYTES:
        why.append(f"{tag}: COMMIT does not track WRITE at rest "
                   f"(WRITE*4={w[3] * 4:#x}, COMMIT={c[3]:#x})")
    for proto in ("spi", "uart"):
        want = manifest["contract"][proto]
        got = (rec.get(proto) or {}).get("observed")
        if got != want:
            why.append(f"{tag}: {proto} burst decoded {got} off the ring, contract {want}")
    return why


def judge_gate(boots: list[dict], manifest: dict) -> list[str]:
    """Reasons the GATE is not met (empty list = gate PASS)."""
    plan = manifest["plan"].split(",")
    why: list[str] = []
    if len(plan) != 16 or plan != ["A", "B"] * 8:
        why.append(f"manifest plan is not 16 strictly interleaved A/B boots: {plan}")
    got = [(b.get("boot"), b.get("image")) for b in boots]
    want = list(zip(range(1, len(plan) + 1), plan))
    if got != want:
        why.append(f"ledger boots {got} != planned {want} (a boot was dropped, "
                   "retried or reordered)")
    for b in boots:
        why += judge_boot(b, manifest)
    return why


# ── the done-when: the COMMITTED silicon evidence meets the gate ───────────────

def test_committed_silicon_gate_is_met():
    _require_witness(LEDGER, MANIFEST)
    reasons = (judge_gate(load_ledger(), load_manifest())
               if LEDGER.is_file() and MANIFEST.is_file()
               else [f"witness evidence missing under {WITNESS}"])
    assert not reasons, GATE_FAIL + ":\n  " + "\n  ".join(reasons)


# ── the checker can fail: each mutant of the real evidence is rejected ────────

def _real():
    return load_ledger(), load_manifest()


def test_checker_rejects_the_ds_t1_12_wedge():
    """The DS-T1.12 silicon snapshot: WRITE advancing, COMMIT pinned at 0."""
    boots, man = _real()
    bad = copy.deepcopy(boots)
    for i, s in enumerate(SNAPS):
        bad[0]["regs"][s]["RING_WRITE_PTR"] = 0x3F2E + 0x5EE * i
        bad[0]["regs"][s]["RING_COMMIT_PTR"] = 0
    reasons = judge_gate(bad, man)
    assert any("COMMIT did not advance" in r for r in reasons)
    assert any("COMMIT is 0" in r for r in reasons)


def test_checker_rejects_commit_lagging_write():
    boots, man = _real()
    bad = copy.deepcopy(boots)
    # COMMIT stops one beat into the UART burst (a partial stall the per-interval
    # "did it move at all" check alone would miss).
    for s in ("R2", "R3"):
        bad[1]["regs"][s]["RING_COMMIT_PTR"] = bad[1]["regs"]["R1"]["RING_COMMIT_PTR"] + BEAT_BYTES
    assert any("did not track WRITE R1->R2" in r for r in judge_gate(bad, man))
    assert any("does not track WRITE at rest" in r for r in judge_gate(bad, man))


def test_checker_rejects_a_wrong_decode():
    boots, man = _real()
    bad = copy.deepcopy(boots)
    bad[2]["spi"]["observed"] = bad[2]["spi"]["observed"][:-1]
    assert any("spi burst decoded" in r for r in judge_gate(bad, man))


def test_checker_rejects_the_wrong_image_on_silicon():
    boots, man = _real()
    bad = copy.deepcopy(boots)
    other = "B" if bad[0]["image"] == "A" else "A"
    for s in SNAPS:
        bad[0]["regs"][s]["BUILD_DATE"] = int(man["images"][other]["build_date"], 16)
    assert any("silicon BUILD_DATE" in r for r in judge_gate(bad, man))


def test_checker_rejects_a_dropped_or_retried_boot():
    boots, man = _real()
    assert any("dropped" in r for r in judge_gate(boots[:-1], man))
    retried = copy.deepcopy(boots)
    retried[3]["boot"] = retried[2]["boot"]
    assert any("dropped" in r for r in judge_gate(retried, man))


def test_checker_rejects_a_sof_that_is_not_the_manifests():
    boots, man = _real()
    bad = copy.deepcopy(boots)
    bad[5]["sof_md5"] = "0" * 32
    assert any("md5" in r for r in judge_gate(bad, man))


# ── provenance: each image carried the write plane the manifest names ─────────

def _git_blob_sha256(commit: str, path: str) -> str:
    blob = subprocess.run(["git", "-C", str(REPO), "show", f"{commit}:{path}"],
                          capture_output=True, check=True).stdout
    return hashlib.sha256(blob).hexdigest()


@pytest.mark.parametrize("image", ["A", "B"])
def test_vendored_write_plane_matches_the_named_commit(image):
    man = load_manifest()
    img = man["images"][image]
    assert img["vendored"], f"image {image}: no vendored rr_ddr_stream hashes recorded"
    for rel, digest in sorted(img["vendored"].items()):
        assert _git_blob_sha256(img["rr_ddr_stream_commit"], rel) == digest, (
            f"image {image}: vendored {rel} is not {img['rr_ddr_stream_commit'][:7]}'s "
            "— the witness describes a write plane that was not the one fitted")


# ── the power-audit ledger quoted in the README (RTL-P3.1632) ─────────────────

_LEDGER_RE = re.compile(
    r"^(?P<ts>\S+Z) board=(?P<board>\S+) outlet=(?P<outlet>\d) action=(?P<action>on|off) "
    r"state=(?P<state>on|off|unknown) holder=(?P<holder>\S+) source=anel_power\.sh$")


def _ledger_lines() -> list[dict]:
    _require_witness(README)
    text = README.read_text()
    block = text.split("<!-- POWER-LEDGER -->", 1)[1].split("```", 2)[1]
    out = []
    for line in block.strip().splitlines():
        m = _LEDGER_RE.match(line.strip())
        assert m, f"unparseable power-audit line: {line!r}"
        out.append(m.groupdict())
    return out


def test_power_ledger_is_this_ticket_and_ends_off():
    lines = _ledger_lines()
    assert lines, "no power-audit lines quoted"
    for l in lines:
        assert "DDR-T1.3" in l["holder"], f"foreign holder in the ledger: {l}"
        assert (l["board"], l["outlet"]) == ("de25-standard", "5"), l
        assert l["state"] == l["action"], f"outlet never reached the wanted state: {l}"
    assert [l["action"] for l in lines] == (["on", "off"] * len(lines))[:len(lines)], \
        "every ON must be followed by an OFF"
    assert lines[-1]["state"] == "off", "board left powered: the final ledger state is not OFF"


# ── witness presence / skip contract (DDR-P2.27) ──────────────────────────────

def test_missing_witness_fails_when_required(monkeypatch, tmp_path):
    """RR_REQUIRE_PRIVATE_WITNESS=1 turns a missing witness into a failure."""
    fake = tmp_path / "absent_witness.json"
    monkeypatch.setenv("RR_REQUIRE_PRIVATE_WITNESS", "1")
    # pytest's skip/fail outcomes are not Exception subclasses: catch broadly so
    # a regression that SKIPS instead of failing turns this test red, not skipped.
    with pytest.raises(BaseException) as ei:
        _require_witness(fake)
    assert ei.type is pytest.fail.Exception, f"expected a failure, got {ei.type.__name__}"
    assert "RR_REQUIRE_PRIVATE_WITNESS" in str(ei.value)
    assert str(fake) in str(ei.value)


def test_missing_witness_skips_by_default(monkeypatch, tmp_path):
    """Without the env var a missing witness skips, stating the private path."""
    fake = tmp_path / "absent_witness.json"
    monkeypatch.delenv("RR_REQUIRE_PRIVATE_WITNESS", raising=False)
    with pytest.raises(pytest.skip.Exception) as ei:
        _require_witness(fake)
    assert str(fake) in str(ei.value)

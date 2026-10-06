"""DDR-P2.18 — the PolarFire DDR4 witness contract, asserted on CAPTURED DATA.

The ticket's done-when is a silicon write->commit->read round-trip returning the
written data. A grep of project.yml / TCL / VHDL cannot prove that (the refused
delivery's receipt was exactly such a grep and stayed green with no capture at
all), so this file asserts on what the REA actually recorded on the board:

  * ``check_roundtrip`` — a pure checker over an ``rr ila capture`` CSV. It
    demands the whole contract at once: the pattern words 1..N come back on the
    M_AXIS handshake in order, ``wr_commit_addr`` reached ``N*4`` BEFORE the first
    returned beat (write -> commit -> read), ``words_written == words_read == N``,
    and the verdict latched ``bist_done & bist_pass & ~bist_fail``.
  * The checker is proven falsifiable: a synthetic good capture passes and each of
    the mutants below is REJECTED — dropping any one clause turns a test RED.
  * The REAL capture committed with the witness (``rea_capture_live_no_ddr.csv``,
    the design running on silicon with the DDR path idle) is a NEGATIVE control:
    the checker must reject genuine silicon data that shows no round-trip.
  * ``rea_capture.csv`` — the actual round-trip capture — does not exist yet
    (MSS DDR-PLL never locked, see the witness README). Until it lands, the
    committed silicon evidence IS the idle no-DDR capture, and the done-when node
    ``test_committed_silicon_roundtrip_capture_satisfies_the_contract`` EVALUATES
    that committed artifact against the full round-trip contract. On the idle
    capture the contract is unmet (no commit, no handshake, no distinct read
    data), so the node goes RED with a ``SILICON_ROUNDTRIP_UNMET`` signal — the
    honest outstanding-proof state, produced by reading real silicon data, NOT by
    a missing file or an xfail marker. It turns GREEN only when a real
    ``rea_capture.csv`` round-trip is committed and satisfies the contract.
  * The witness README's power-audit ledger (RTL-P3.1632) is parsed, not grepped:
    every ON has a later OFF, the final state is OFF, every line carries the full
    ticket id.
"""

from __future__ import annotations

import csv
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WITNESS = ROOT / "docs" / "internal" / "ddr_p2_18_witness"
REAL_ROUNDTRIP = WITNESS / "rea_capture.csv"
REAL_NO_DDR = WITNESS / "rea_capture_live_no_ddr.csv"
README = WITNESS / "README.md"

N_WORDS = 64  # ddr_validator_csr n_words reset value
COMMIT_BYTES = N_WORDS * 4  # wr_commit_addr must reach n_words * 4 == 0x100
TICKET_ID = "DDR-P2.18"

# `rr ila capture` CSV header names -> short names (debug/ddr_roundtrip.yml order).
_COLS = {
    "read_data": "read_data[31:0]",
    "write_data": "write_data[31:0]",
    "commit": "wr_commit_addr[31:0]",
    "words_read": "words_read[15:0]",
    "words_written": "words_written[15:0]",
    "valid": "m_axis_valid",
    "ready": "m_axis_ready",
    "done": "bist_done",
    "pass": "bist_pass",
    "fail": "bist_fail",
}


def _hex(row: dict, key: str) -> int:
    return int(row[_COLS[key]], 16)


def _require_witness(*paths: Path) -> None:
    for p in paths:
        if not p.is_file():
            if os.environ.get("RR_REQUIRE_PRIVATE_WITNESS") == "1":
                pytest.fail(
                    f"RR_REQUIRE_PRIVATE_WITNESS=1 but witness evidence is missing: {p}"
                )
            pytest.skip(f"witness evidence is absent under {p}")


def load_capture(path: Path) -> list[dict]:
    _require_witness(path)
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    missing = [c for c in _COLS.values() if rows and c not in rows[0]]
    assert not missing, f"{path.name}: capture lacks probe columns {missing}"
    return rows


def check_roundtrip(rows: list[dict], n_words: int = N_WORDS) -> list[str]:
    """Return the list of contract violations (empty == a proven round-trip)."""
    problems: list[str] = []
    if not rows:
        return ["empty capture"]
    commit_bytes = n_words * 4

    beats: list[int] = []  # returned words, in handshake order
    first_beat_idx = None
    first_commit_idx = None
    for i, r in enumerate(rows):
        if first_commit_idx is None and _hex(r, "commit") >= commit_bytes:
            first_commit_idx = i
        if _hex(r, "valid") and _hex(r, "ready"):
            if first_beat_idx is None:
                first_beat_idx = i
            beats.append(_hex(r, "read_data"))

    if first_beat_idx is None:
        problems.append("no M_AXIS handshake beat: nothing came back from DDR")
    else:
        # The window may not hold every beat (pre/post-trigger), but every beat it
        # DOES hold must be the next counter word — a read of wrong/stale data fails.
        start = beats[0]
        if start < 1 or start > n_words:
            problems.append(f"first returned word {start:#x} is outside 1..{n_words}")
        expect = list(range(start, start + len(beats)))
        if beats != expect:
            bad = next(k for k, (g, e) in enumerate(zip(beats, expect)) if g != e)
            problems.append(f"returned data mismatch at beat {bad}: got {beats[bad]:#x} expected {expect[bad]:#x}")
        if start + len(beats) - 1 > n_words:
            problems.append(f"more than {n_words} words returned")

    if first_commit_idx is None:
        problems.append(f"wr_commit_addr never reached {commit_bytes:#x}")
    elif first_beat_idx is not None and first_beat_idx < first_commit_idx:
        problems.append("a word was read back BEFORE wr_commit_addr covered the write (commit ordering)")

    last = rows[-1]
    if _hex(last, "words_written") != n_words:
        problems.append(f"words_written ends at {_hex(last, 'words_written')} != {n_words}")
    if _hex(last, "words_read") != n_words:
        problems.append(f"words_read ends at {_hex(last, 'words_read')} != {n_words}")
    if not _hex(last, "done"):
        problems.append("bist_done never latched")
    if not _hex(last, "pass"):
        problems.append("bist_pass never latched")
    if _hex(last, "fail"):
        problems.append("bist_fail latched")
    return problems


# --------------------------------------------------------------------------- #
# Synthetic captures (positive control + mutants). Column keys are the CSV names.
# --------------------------------------------------------------------------- #
def _row(read=0, write=1, commit=0, wr_n=0, rd_n=0, valid=0, ready=0, done=0, ok=0, bad=0):
    return {
        _COLS["read_data"]: f"{read:08x}",
        _COLS["write_data"]: f"{write:08x}",
        _COLS["commit"]: f"{commit:08x}",
        _COLS["words_read"]: f"{rd_n:04x}",
        _COLS["words_written"]: f"{wr_n:04x}",
        _COLS["valid"]: str(valid),
        _COLS["ready"]: str(ready),
        _COLS["done"]: str(done),
        _COLS["pass"]: str(ok),
        _COLS["fail"]: str(bad),
    }


def good_capture(n: int = N_WORDS) -> list[dict]:
    rows = [_row() for _ in range(4)]
    for k in range(1, n + 1):  # write phase: counter words issued, pointer advancing
        rows.append(_row(write=k, commit=(k - 1) * 4, wr_n=k - 1))
    rows.append(_row(write=n, commit=n * 4, wr_n=n))  # commit covers all bytes
    for k in range(1, n + 1):  # read phase: one beat per word
        rows.append(_row(read=k, commit=n * 4, wr_n=n, rd_n=k - 1, valid=1, ready=1))
    rows.append(_row(read=n, commit=n * 4, wr_n=n, rd_n=n, done=1, ok=1))
    return rows


def test_checker_accepts_a_real_shaped_roundtrip():
    assert check_roundtrip(good_capture()) == []


def _delay_commit_past_first_beat(rows: list[dict]) -> None:
    """Make the pointer cover the write only AFTER the first word is read back."""
    first = next(i for i, r in enumerate(rows) if r[_COLS["valid"]] == "1")
    for r in rows[: first + 1]:
        r[_COLS["commit"]] = f"{COMMIT_BYTES - 4:08x}"


def _mutate(fn):
    rows = good_capture()
    fn(rows)
    return check_roundtrip(rows)


@pytest.mark.parametrize(
    "name, mutation, needle",
    [
        ("wrong data", lambda rows: rows[-8].__setitem__(_COLS["read_data"], "deadbeef"), "returned data mismatch"),
        (
            "no beats at all",
            lambda rows: [r.__setitem__(_COLS["valid"], "0") for r in rows],
            "nothing came back",
        ),
        (
            "commit never covers the write",
            lambda rows: [r.__setitem__(_COLS["commit"], "00000080") for r in rows],
            "never reached",
        ),
        ("read before commit", lambda rows: _delay_commit_past_first_beat(rows), "BEFORE wr_commit_addr"),
        ("bist_pass missing", lambda rows: rows[-1].__setitem__(_COLS["pass"], "0"), "bist_pass never"),
        ("bist_fail set", lambda rows: rows[-1].__setitem__(_COLS["fail"], "1"), "bist_fail latched"),
        ("bist_done missing", lambda rows: rows[-1].__setitem__(_COLS["done"], "0"), "bist_done never"),
        ("short write count", lambda rows: rows[-1].__setitem__(_COLS["words_written"], "0020"), "words_written ends"),
        ("short read count", lambda rows: rows[-1].__setitem__(_COLS["words_read"], "0020"), "words_read ends"),
    ],
)
def test_checker_rejects_each_mutant(name, mutation, needle):
    problems = _mutate(mutation)
    assert any(needle in p for p in problems), f"{name}: expected a '{needle}' violation, got {problems}"


# --------------------------------------------------------------------------- #
# The committed silicon evidence.
# --------------------------------------------------------------------------- #
def test_real_silicon_capture_without_ddr_is_rejected():
    """Negative control on GENUINE data: the design ran on the board (4096 samples,
    REA identity proven) but no DDR traffic happened — the checker must say so."""
    _require_witness(REAL_NO_DDR)
    assert REAL_NO_DDR.is_file(), "the live no-DDR capture is the committed silicon evidence"
    rows = load_capture(REAL_NO_DDR)
    assert len(rows) == 4096
    problems = check_roundtrip(rows)
    assert problems, "a capture with an idle DDR path must NOT satisfy the round-trip contract"
    assert any("nothing came back" in p for p in problems)
    assert any("never reached" in p for p in problems)
    # The fabric WAS alive and the BIST did run its write pass: the IP accepted all
    # 64 words (words_written reaches 0x40) ...
    written = [_hex(r, "words_written") for r in rows]
    assert set(written) == {0, N_WORDS}, "capture shape changed: re-derive the witness README table"
    # ... but nothing was ever committed to DDR, nothing came back, no verdict latched.
    assert {_hex(r, "commit") for r in rows} == {0}, "wr_commit_addr must never have advanced in this capture"
    assert not any(_hex(r, "valid") for r in rows)
    assert not any(_hex(r, "done") or _hex(r, "pass") or _hex(r, "fail") for r in rows)


def _committed_silicon_capture() -> Path:
    """The best silicon capture committed so far. A real write->commit->read
    round-trip (rea_capture.csv) supersedes the idle bring-up capture the moment
    it lands; until then the idle no-DDR capture IS the committed silicon
    evidence, and the done-when node certifies against it (and goes RED on it,
    honestly — the idle capture cannot satisfy the round-trip contract)."""
    return REAL_ROUNDTRIP if REAL_ROUNDTRIP.is_file() else REAL_NO_DDR


def test_committed_silicon_roundtrip_capture_satisfies_the_contract():
    """THE silicon done-when. This gate is RED until a real write->commit->read
    is captured on the board, and GREEN only on a distinct-data round-trip.

    DDR-P2.21: this was ``xfail(strict=True)`` — a missing ``rea_capture.csv``
    made it an *expected fail* that PASSED the suite (pytest exit 0) with NO
    silicon round-trip ever observed. That is a vacuous gate (the refused
    DDR-P2.18 land, tich-pm-data 0c7511759d): green suite, zero silicon evidence.

    The marker is GONE, and — unlike a mere ``assert file.is_file()`` — this node
    now goes RED by EVALUATING the committed silicon artifact. Today that is the
    idle no-DDR capture (MSS DDR-PLL never locked on the programmed image —
    docs/internal/ddr_p2_18_witness/README.md): wr_commit_addr never leaves 0,
    m_axis_valid is never asserted, and read_data holds a single unchanged value.
    check_roundtrip therefore reports violations and this node fails with the
    ``SILICON_ROUNDTRIP_UNMET`` signal — the honest outstanding-proof state,
    derived from real silicon data. It turns GREEN the moment a real
    ``rea_capture.csv`` lands and satisfies the contract with distinct read-back
    data — nothing weakens it to pass without a genuine round-trip."""
    capture = _committed_silicon_capture()
    _require_witness(capture)
    rows = load_capture(capture)
    problems = check_roundtrip(rows)
    # Distinct written-vs-read evidence: a real round-trip returns the DISTINCT
    # counter words 1..N, never a single stuck/constant value a wedged bus (or an
    # idle DDR path replaying one read_data) would show. This is both the GREEN
    # guard against a degenerate "all-same" capture passing the ordering checks by
    # accident, and — on the idle capture — the explicit "read_data unchanged"
    # symptom the done-when requires as a RED cause.
    beats = [_hex(r, "read_data") for r in rows if _hex(r, "valid") and _hex(r, "ready")]
    if len(set(beats)) <= 1:
        problems.append(
            "read_data is unchanged across the capture (single constant value / no "
            "distinct read-back) — a stuck bus or idle DDR path, not a real "
            "distinct-data DDR round-trip"
        )
    assert problems == [], (
        f"SILICON_ROUNDTRIP_UNMET [{capture.name}]: the committed silicon capture "
        f"does NOT satisfy the write->commit->read round-trip contract: {problems}. "
        "While this is the idle bring-up capture (no rea_capture.csv round-trip "
        "committed yet — README), this RED is the honest state of DDR-P2.18's "
        "silicon done-when, evaluated on real captured data; it turns GREEN only "
        "on a genuine distinct-data round-trip."
    )


# --------------------------------------------------------------------------- #
# Power-audit ledger (RTL-P3.1632) quoted in the witness README — parsed.
# --------------------------------------------------------------------------- #
_LEDGER = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\dT[\d:.]+Z) board=(?P<board>\S+) outlet=(?P<outlet>\d+) "
    r"action=(?P<action>on|off) state=(?P<state>on|off|unknown) holder=(?P<holder>\S+) source=anel_power\.sh$"
)


def _ledger_lines() -> list[re.Match]:
    _require_witness(README)
    text = README.read_text()
    block = re.search(r"```text\n(.*?)```", text, re.S)
    assert block, "witness README must quote the power-audit ledger in a ```text block"
    out = []
    for line in block.group(1).splitlines():
        m = _LEDGER.match(line.strip())
        assert m, f"unparseable ledger line: {line!r}"
        out.append(m)
    return out


def test_ledger_lines_carry_ticket_id_and_pair_on_with_off():
    lines = _ledger_lines()
    assert len(lines) >= 2
    for m in lines:
        assert TICKET_ID in m["holder"], f"ledger holder lacks the full ticket id: {m['holder']}"
        assert m["board"] == "polarfire-soc-discovery" and m["outlet"] == "6"
        assert m["action"] == m["state"], f"switch did not reach the wanted state: {m.group(0)}"
    stamps = [m["ts"] for m in lines]
    assert stamps == sorted(stamps), "ledger must be chronological"
    depth = 0
    for m in lines:
        depth += 1 if m["action"] == "on" else -1
        assert depth in (0, 1), f"unbalanced on/off at {m['ts']} (powered twice or off while off)"
    assert lines[-1]["action"] == "off" and depth == 0, "board must end OFF"


# ── witness presence / skip contract (DDR-P2.27) ──────────────────────────────

def test_missing_witness_fails_when_required(monkeypatch, tmp_path):
    """RR_REQUIRE_PRIVATE_WITNESS=1 turns a missing witness into a failure."""
    fake = tmp_path / "absent_capture.csv"
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
    fake = tmp_path / "absent_capture.csv"
    monkeypatch.delenv("RR_REQUIRE_PRIVATE_WITNESS", raising=False)
    with pytest.raises(pytest.skip.Exception) as ei:
        _require_witness(fake)
    assert str(fake) in str(ei.value)

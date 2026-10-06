"""Adversarial tests for the Annex-B NAL demux + record framing (de25_nal).

Phase 1 of the DE25 NALU end-goal. Validates the streaming demuxer against
synthetic streams (3-/4-byte start codes, chunk-split boundaries, emulation-
prevention bytes, trailing NAL, garbage) and against the real h264-dec
Constrained-Baseline vector (qcif_idr = SPS+PPS+IDR).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from de25_nal import NalDemux, nal_record, records_from_annexb  # noqa: E402

_SC3 = b"\x00\x00\x01"
_SC4 = b"\x00\x00\x00\x01"


def _build(nals, sc4=False):
    """Assemble an Annex-B stream from raw NAL byte-strings."""
    sc = _SC4 if sc4 else _SC3
    return b"".join(sc + n for n in nals)


def _hdr(ntype, ref_idc=3, forbidden=0):
    return bytes([((forbidden & 1) << 7) | ((ref_idc & 3) << 5) | (ntype & 0x1F)])


# ── header decode ────────────────────────────────────────────────────────
def test_nal_record_decodes_sps_pps_idr():
    for ntype, name in ((7, "SPS"), (8, "PPS"), (5, "IDR slice"), (1, "non-IDR slice")):
        r = nal_record(_hdr(ntype, ref_idc=3) + b"\xaa\xbb")
        assert r["value"] == ntype
        assert r["kind"] == f"h264_nal_{ntype}"
        assert name in r["text"]
        assert r["is_error"] is False
        assert dict(r["fields"][-3:])["nal_ref_idc"] == "3"


def test_nal_record_flags_forbidden_zero_bit():
    r = nal_record(_hdr(5, forbidden=1) + b"\x00")
    assert r["is_error"] is True
    assert "forbidden_zero" in r["text"]
    assert r["color"] == "#c0392b"


def test_nal_record_ref_idc_and_payload_carries_raw_bytes():
    nal = _hdr(5, ref_idc=2) + b"\xde\xad\xbe\xef"
    r = nal_record(nal)
    assert r["payload"]["nal_ref_idc"] == 2
    assert r["payload"]["size"] == len(nal)
    assert r["payload"]["nal_bytes"] == nal          # NALU-ready: full raw NAL
    assert r["payload"]["rbsp_head_hex"].startswith("deadbeef")


def test_empty_nal_is_error():
    r = nal_record(b"")
    assert r["is_error"] is True and r["value"] == 0


# ── start-code variants ──────────────────────────────────────────────────
@pytest.mark.parametrize("sc4", [False, True])
def test_three_and_four_byte_start_codes(sc4):
    nals = [_hdr(7) + b"\x42\x00\x28", _hdr(8) + b"\xce", _hdr(5) + b"\x88\x84\x02"]
    recs = records_from_annexb(_build(nals, sc4=sc4))
    assert [r["value"] for r in recs] == [7, 8, 5]


def test_leading_zeros_before_first_start_code_ignored():
    # extra 0x00 padding (and a 4-byte start code) before the first NAL
    stream = b"\x00\x00\x00\x00" + _build([_hdr(7) + b"\x42", _hdr(5) + b"\x88"])
    recs = records_from_annexb(stream)
    assert [r["value"] for r in recs] == [7, 5]


# ── emulation prevention must NOT split a NAL ─────────────────────────────
def test_emulation_prevention_bytes_do_not_split():
    # A conformant RBSP contains 00 00 03 (the EPB) but never a bare 00 00 01.
    rbsp = b"\x88\x00\x00\x03\x00\x00\x03\x01\x99"
    nals = [_hdr(5) + rbsp, _hdr(1) + b"\x11"]
    recs = records_from_annexb(_build(nals))
    assert [r["value"] for r in recs] == [5, 1]
    assert recs[0]["payload"]["size"] == len(_hdr(5) + rbsp)


# ── streaming: chunk boundaries must not change the result ────────────────
def test_streaming_byte_by_byte_equals_whole():
    nals = [_hdr(7) + b"\x42\x00\x28\xf4", _hdr(8) + b"\xce\x38\x80",
            _hdr(5) + bytes(range(1, 40))]
    stream = _build(nals, sc4=True)
    whole = records_from_annexb(stream)
    # feed one byte at a time — start codes split across chunks
    dmx = NalDemux()
    got = []
    for b in stream:
        got += dmx.feed(bytes([b]))
    got += dmx.flush()
    assert [n[0] & 0x1F for n in got] == [r["value"] for r in whole]
    assert got == [r["payload"]["nal_bytes"] for r in whole]


def test_streaming_odd_chunk_sizes():
    nals = [_hdr(7) + bytes(range(5)), _hdr(8) + bytes(range(7)),
            _hdr(5) + bytes(range(100))]
    stream = _build(nals)
    for chunk in (1, 2, 3, 4, 5, 13, 64):
        dmx = NalDemux()
        out = []
        for k in range(0, len(stream), chunk):
            out += dmx.feed(stream[k:k + chunk])
        out += dmx.flush()
        assert [n[0] & 0x1F for n in out] == [7, 8, 5], f"chunk={chunk}"


def test_no_start_code_garbage_yields_nothing_and_is_bounded():
    dmx = NalDemux(max_buffer=1024)
    for _ in range(100):
        assert dmx.feed(b"\xff" * 4096) == []
    assert len(dmx._buf) <= 2          # buffer must not grow unbounded
    assert dmx.flush() == []


# ── real Constrained-Baseline vector (vendored fixture; source: h264-dec
#    vectors/smoke/qcif_idr/stream.264, a JM-encoded CB IDR stream) ─────────
_QCIF_IDR = os.path.join(os.path.dirname(__file__), "testdata", "qcif_idr.264")


@pytest.mark.skipif(not os.path.exists(_QCIF_IDR), reason="qcif_idr fixture absent")
def test_real_qcif_idr_is_sps_pps_idr():
    with open(_QCIF_IDR, "rb") as f:
        recs = records_from_annexb(f.read())
    types = [r["value"] for r in recs]
    # qcif_idr = one SPS(7), one PPS(8), one IDR(5) — possibly trailing slices
    assert types[:3] == [7, 8, 5], types
    assert all(r["is_error"] is False for r in recs), "no forbidden_zero in a clean stream"
    # SPS profile_idc byte is 0x42 (66 = Baseline) in this vector
    assert recs[0]["payload"]["rbsp_head_hex"].startswith("42")


@pytest.mark.skipif(not os.path.exists(_QCIF_IDR), reason="qcif_idr fixture absent")
def test_real_stream_matches_oracle_split_nals():
    """Cross-check the demux against the oracle's split_nals (single source).

    Best-effort: only runs when the sibling h264-dec repo is checked out
    alongside (the vendored fixture covers the standalone case).
    """
    oracle = os.path.join(os.path.dirname(__file__),
                          "../../../../../h264-dec/tools/jm-oracle")
    if not os.path.exists(os.path.join(oracle, "scout_smoke.py")):
        pytest.skip("oracle absent")
    sys.path.insert(0, oracle)
    from scout_smoke import split_nals  # noqa: E402
    with open(_QCIF_IDR, "rb") as f:
        data = f.read()
    dmx = NalDemux()
    mine = dmx.feed(data) + dmx.flush()
    assert mine == split_nals(data)

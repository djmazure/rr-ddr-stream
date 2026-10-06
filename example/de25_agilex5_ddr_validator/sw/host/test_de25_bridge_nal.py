"""End-to-end (headless) check: the bridge frames an H.264 NAL stream into the
exact wave_stream SAMPLES + ANNOTATIONS that RouteWave's Protocol Analyzer
consumes — without a socket, so it's deterministic and CI-safe.

Drives De25Source's iterators the way wave_stream._drive_connection does: one
iter_samples pull then one iter_annotations pull per record (lockstep).
"""
import os
import queue
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from de25_nal import records_from_annexb  # noqa: E402
from de25_wave_bridge import De25Source    # noqa: E402

_QCIF_IDR = os.path.join(os.path.dirname(__file__), "testdata", "qcif_idr.264")


def _drive(src, n):
    si = src.iter_samples(1024, 10)
    ai = src.iter_annotations()
    samples, annots = [], []
    for _ in range(n):
        sb = next(si)
        assert sb, "expected exactly one sample per record (non-empty batch)"
        samples.append(sb[0])
        ab = next(ai)
        assert ab, "expected an annotation per non-empty sample batch"
        annots.append(ab[0])
    return samples, annots


@pytest.mark.skipif(not os.path.exists(_QCIF_IDR), reason="qcif_idr fixture absent")
def test_bridge_frames_real_nal_stream():
    recs = records_from_annexb(open(_QCIF_IDR, "rb").read())
    q: queue.Queue = queue.Queue()
    for r in recs:
        q.put(r)
    samples, annots = _drive(De25Source(q), len(recs))

    # SAMPLES carry the nal_unit_type sequence (the waveform step).
    assert [s["value"] for s in samples] == [r["value"] for r in recs]
    assert [s["value"] for s in samples][:3] == [7, 8, 5]   # SPS, PPS, IDR

    # ANNOTATIONS are the Protocol-Analyzer packets.
    assert annots[0]["kind"] == "h264_nal_7" and "SPS" in annots[0]["text"]
    assert annots[1]["kind"] == "h264_nal_8" and "PPS" in annots[1]["text"]
    assert annots[2]["kind"] == "h264_nal_5" and "IDR slice" in annots[2]["text"]

    # decoded NAL header reaches the analyzer as columns (fields)…
    f_sps = dict(annots[0]["fields"])
    assert f_sps["nal_unit_type"] == "7" and f_sps["nal_ref_idc"] == "3"
    # …per-NAL colouring is applied (SPS=config blue, IDR=key green)…
    assert annots[0]["color"] == "#3a7bd5"
    assert annots[2]["color"] == "#2e9e4f"
    # …and the raw NAL bytes ride along in the payload (NALU-ready).
    assert (annots[2]["payload"]["nal_bytes"][0] & 0x1F) == 5
    assert all(a["is_error"] is False for a in annots)


def test_bridge_keepalive_when_queue_empty():
    # An empty queue must yield a keepalive (empty sample batch) and NOT advance
    # the annotation iterator — so records are framed 1:1 with no phantom packets.
    src = De25Source(queue.Queue())
    sb = next(src.iter_samples(8, 10))
    assert sb == []

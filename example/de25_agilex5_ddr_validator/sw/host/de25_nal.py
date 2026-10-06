#!/usr/bin/env python3
"""H.264 Annex-B NAL demux + wave_stream record framing (DE25 FPGA→Eth→RouteWave).

Phase 1 of the NALU end-goal: turn an H.264 Annex-B byte stream (eventually
emitted by the FPGA fabric, crossing Ethernet) into per-NAL packets that the
bridge frames as wave_stream ANNOTATIONS for RouteWave's Protocol Analyzer.

Semantics mirror h264-dec's `src/nal_demux.vhd` and the oracle's
`tools/jm-oracle/scout_smoke.py:split_nals` (single source of truth for the
start-code split): a NAL is delimited by the Annex-B start code `00 00 01`
(any number of leading `00`), and the first byte is the NAL header:

    bit 7    forbidden_zero_bit   (MUST be 0; nonzero ⇒ corruption)
    bits 6:5 nal_ref_idc
    bits 4:0 nal_unit_type        (= byte & 0x1F)

Emulation-prevention `00 00 03 → 00 00` removal is the RBSP-parser's job
(deeper than NAL-level); start codes never appear inside a conformant RBSP
(that's the whole point of emulation prevention), so a raw `00 00 01` scan is
a safe NAL splitter. This module does NOT de-emulate — it carries the raw NAL
bytes (NALU-ready) and decodes only the 1-byte header for the packet view.
"""
from __future__ import annotations

from typing import Any

# H.264 / Annex A Table 7-1 nal_unit_type → human name.
NAL_TYPE_NAMES: dict[int, str] = {
    0: "unspecified",
    1: "non-IDR slice",
    2: "slice partition A",
    3: "slice partition B",
    4: "slice partition C",
    5: "IDR slice",
    6: "SEI",
    7: "SPS",
    8: "PPS",
    9: "AUD",
    10: "end of sequence",
    11: "end of stream",
    12: "filler",
    13: "SPS extension",
    14: "prefix NAL",
    15: "subset SPS",
    19: "aux slice",
    20: "slice extension",
}

# Protocol-Analyzer colours by role (CSS hex).
_COLOR_CONFIG = "#3a7bd5"   # SPS/PPS — parameter sets (blue)
_COLOR_KEY = "#2e9e4f"      # IDR slice — keyframe (green)
_COLOR_SLICE = "#8a8f98"    # non-IDR slice (grey)
_COLOR_INFO = "#c9a227"     # SEI / AUD (amber)
_COLOR_OTHER = "#6b6f76"    # everything else (dim)
_COLOR_ERR = "#c0392b"      # forbidden_zero_bit set (red)

_START_CODE = b"\x00\x00\x01"


def _color_for(ntype: int) -> str:
    if ntype in (7, 8, 13, 15):
        return _COLOR_CONFIG
    if ntype == 5:
        return _COLOR_KEY
    if ntype in (1, 2, 3, 4, 19, 20):
        return _COLOR_SLICE
    if ntype in (6, 9):
        return _COLOR_INFO
    return _COLOR_OTHER


def nal_record(nal: bytes, index: int | None = None) -> dict[str, Any]:
    """Frame one raw NAL (header byte + RBSP, no start code) as a bridge record.

    The record is consumed by de25_wave_bridge.De25Source: ``value`` drives the
    SAMPLE waveform (the nal_unit_type step) and the rest drives the Protocol
    Analyzer ANNOTATION. ``payload.nal_bytes`` carries the raw NAL (NALU-ready).
    """
    if not nal:
        return {
            "kind": "h264_nal_empty", "text": "NAL empty", "value": 0,
            "is_error": True, "color": _COLOR_ERR,
            "payload": {"size": 0}, "fields": [["error", "zero-length NAL"]],
        }
    hdr = nal[0]
    forbidden = (hdr >> 7) & 0x1
    ref_idc = (hdr >> 5) & 0x3
    ntype = hdr & 0x1F
    name = NAL_TYPE_NAMES.get(ntype, f"reserved {ntype}")
    size = len(nal)
    text = f"{name} · {size}B · ref={ref_idc}"
    if forbidden:
        text = "⚠ " + text + " forbidden_zero=1"
    fields = [
        ["nal_unit_type", str(ntype)],
        ["name", name],
        ["nal_ref_idc", str(ref_idc)],
        ["forbidden_zero_bit", str(forbidden)],
        ["size_bytes", str(size)],
    ]
    if index is not None:
        fields.insert(0, ["nal_index", str(index)])
    payload: dict[str, Any] = {
        "nal_unit_type": ntype,
        "nal_ref_idc": ref_idc,
        "forbidden_zero_bit": forbidden,
        "size": size,
        "rbsp_head_hex": nal[1:17].hex(),
        "nal_bytes": bytes(nal),  # NALU-ready: full raw NAL carried opaquely
    }
    return {
        "kind": f"h264_nal_{ntype}",
        "text": text,
        "value": ntype,
        "is_error": bool(forbidden),
        "color": _COLOR_ERR if forbidden else _color_for(ntype),
        "payload": payload,
        "fields": fields,
    }


class NalDemux:
    """Streaming Annex-B NAL demuxer.

    Feed arbitrary byte chunks (e.g. TCP segments from the board); ``feed``
    returns the list of complete NALs whose end was proven by the *next* start
    code. ``flush`` emits the final NAL (whose end is EOF). Output for a given
    total byte stream equals ``scout_smoke.split_nals`` of the same stream.

    A NAL's end is only known when the next start code arrives, so bytes from
    the last seen start code onward are retained across ``feed`` calls.
    """

    def __init__(self, max_buffer: int = 64 << 20) -> None:
        self._buf = bytearray()
        self._max_buffer = max_buffer  # guard vs a never-start-coded garbage feed

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        return self._drain(final=False)

    def flush(self) -> list[bytes]:
        return self._drain(final=True)

    def _drain(self, final: bool) -> list[bytes]:
        buf = bytes(self._buf)
        # All start-code positions currently in the buffer.
        positions: list[int] = []
        i = 0
        while True:
            j = buf.find(_START_CODE, i)
            if j < 0:
                break
            positions.append(j)
            i = j + 3
        if not positions:
            # No NAL boundary yet. Bound the buffer so a garbage feed (no start
            # code ever) can't grow without limit — keep only the tail that could
            # still begin a start code.
            if not final and len(self._buf) > self._max_buffer:
                self._buf = self._buf[-2:]
            elif final:
                self._buf = bytearray()
            return []
        out: list[bytes] = []
        # Complete NALs: between consecutive start codes (RBSP starts +3 after
        # the start code; ends where the next start code begins).
        for a, b in zip(positions, positions[1:]):
            out.append(buf[a + 3:b])
        if final:
            out.append(buf[positions[-1] + 3:])
            self._buf = bytearray()
        else:
            # Retain from the last start code on — that NAL is still open.
            self._buf = bytearray(buf[positions[-1]:])
        return out


def records_from_annexb(data: bytes) -> list[dict[str, Any]]:
    """Demux a complete Annex-B blob into bridge records (one per NAL)."""
    dmx = NalDemux()
    nals = dmx.feed(data) + dmx.flush()
    return [nal_record(n, idx) for idx, n in enumerate(nals)]

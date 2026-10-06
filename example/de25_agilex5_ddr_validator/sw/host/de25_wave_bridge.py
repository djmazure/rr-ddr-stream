#!/usr/bin/env python3
"""DE25 → RouteWave bridge — reuse of the deepskopion wave_stream pipeline.

The DE25's busybox initramfs has no Python, so the producer can't run on the
board. Instead the board ships FPGA-sourced records over Ethernet (busybox
`nc`); THIS host-side bridge reframes each record as a wave_stream_v1 frame and
serves a Unix socket that RouteWave `stream attach`es — exactly the split the
deepskopion daemon uses (board → wave_stream → RouteWave Protocol Analyzer).

Data path:

    DE25 FPGA (CSR / DDR counter / eventually H.264 NALUs)
      → devmem/dd  →  busybox nc 192.168.50.1:<feed-port>   (over Ethernet)
        → THIS bridge (TCP listener thread → queue)
          → De25Source.iter_samples()/iter_annotations()
            → wave_stream_v1 SAMPLES + ANNOTATIONS frames (msgpack)
              → Unix socket /tmp/de25_wave.sock
                → RouteWave `stream attach`  → Protocol Analyzer panel

Board feed line protocol (newline-delimited, one record per line):

    <kind> <hexvalue> [text...]

e.g.  `csr@0x3c 0x0DD50200 IDENT`  or  `ddr@0xa0000000 0x00000001`.
Eventually a NALU record:  `nal 0x65 SLICE_IDR <hexbytes...>` — the bridge stuffs
the raw bytes into the annotation `payload` (NALU-ready; see --nalu note below).

This is a host dev/visualisation adapter (analogue of deepskopion's daemon), not
a build/sim/program step — it does NOT bypass the pure-routertl flow.
"""
from __future__ import annotations

import argparse
import os
import queue
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Reuse the canonical wave_stream_v1 producer helpers (single source of truth).
if "ROUTERTL_DIR" in os.environ:
    sys.path.insert(0, os.environ["ROUTERTL_DIR"])
from sdk.cli.wave_stream import SampleSource, serve_unix_socket  # noqa: E402

# H.264 Annex-B NAL demux + record framing (Phase 1 of the NALU end-goal).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from de25_nal import NalDemux, nal_record, records_from_annexb  # noqa: E402

PROTO_COLOR = "#3a7bd5"   # DE25/fabric blue in the Protocol Analyzer
ERR_COLOR = "#c0392b"
TS_STEP_NS = 1_000_000    # 1 ms of virtual sample-time per record


class De25Source(SampleSource):
    """wave_stream source fed by FPGA records arriving over Ethernet.

    iter_samples yields one sample per record (so the value also shows as a
    waveform trace), and iter_annotations yields the matching packet for the
    Protocol Analyzer. They run in lockstep: the driver pulls one sample batch
    then one annotation batch per loop, and skips the annotation when the
    sample batch is empty (keepalive), so `_pending` is consumed exactly once.
    """

    def __init__(self, feed_q: "queue.Queue[dict[str, Any]]") -> None:
        self.channels = [
            {"id": 0, "name": "de25_fpga", "width": 32, "type": "bus", "radix": "Hex"},
        ]
        self.sample_clk_hz = 100_000_000  # nominal; RouteWave times against this
        self.producer_name = "de25-fpga-bridge/0.1.0"
        self._q = feed_q
        self._ts = 0
        self._pending: dict[str, Any] | None = None

    def supports_annotations(self) -> bool:
        return True

    def iter_samples(self, batch_size: int, dt_ns: int) -> Iterator[list[dict[str, Any]]]:
        while True:
            try:
                rec = self._q.get(timeout=0.2)
            except queue.Empty:
                yield []  # keepalive tick — no record, no annotation this loop
                continue
            self._pending = rec
            ts = self._ts
            self._ts += TS_STEP_NS
            yield [{"ts": ts, "value": int(rec["value"]) & 0xFFFFFFFF}]

    def iter_annotations(self) -> Iterator[list[dict[str, Any]]]:
        while True:
            rec = self._pending
            if rec is None:
                yield []
                continue
            self._pending = None
            ts = self._ts - TS_STEP_NS
            ann = {
                "ts_start": ts,
                "ts_end": ts + 80_000,
                "channel_id": 0,
                "kind": rec["kind"],
                "text": rec["text"],
                "payload": rec.get("payload", {"value": int(rec["value"]) & 0xFFFFFFFF}),
                # per-record colour (NAL-type colouring) wins; else error/normal.
                "color": rec.get("color") or (ERR_COLOR if rec.get("is_error") else PROTO_COLOR),
                "is_error": bool(rec.get("is_error", False)),
            }
            if rec.get("fields"):
                ann["fields"] = rec["fields"]  # decoded NAL header → analyzer columns
            yield [ann]


def _parse_record(line: str) -> dict[str, Any] | None:
    """Parse a board feed line `<kind> <hexvalue> [text...]` into a record."""
    parts = line.strip().split(None, 2)
    if len(parts) < 2:
        return None
    kind, hexval = parts[0], parts[1]
    text = parts[2] if len(parts) > 2 else hexval
    try:
        value = int(hexval, 0)
    except ValueError:
        return None
    payload: dict[str, Any] = {"value": value & 0xFFFFFFFF, "hex": hexval}
    # NALU-ready: if the trailing text is a hex byte run, capture it as raw bytes.
    return {"kind": kind, "text": f"{kind}={text}", "value": value, "payload": payload}


def _feed_listener(host: str, port: int, feed_q: "queue.Queue[dict[str, Any]]",
                   log) -> None:
    """Background thread: accept the board's nc connection, parse records."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    log(f"feed: listening for board on {host}:{port}")
    while True:
        conn, peer = srv.accept()
        log(f"feed: board connected from {peer}")
        buf = b""
        with conn:
            while True:
                data = conn.recv(4096)
                if not data:
                    log("feed: board disconnected")
                    break
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    rec = _parse_record(raw.decode("utf-8", "replace"))
                    if rec is not None:
                        feed_q.put(rec)


def _feed_listener_nal(host: str, port: int, feed_q: "queue.Queue[dict[str, Any]]",
                       log) -> None:
    """NAL-aware feed: the board ships a raw H.264 Annex-B byte stream over the
    TCP link; demux it into per-NAL records on the fly (Phase 1 of the NALU
    end-goal). One NAL → one wave_stream packet in RouteWave's Protocol
    Analyzer. A fresh NalDemux + nal index per connection (per bitstream)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    log(f"feed(nal): listening for board H.264 Annex-B on {host}:{port}")
    while True:
        conn, peer = srv.accept()
        log(f"feed(nal): board connected from {peer}")
        dmx = NalDemux()
        idx = 0
        with conn:
            while True:
                data = conn.recv(4096)
                if not data:
                    for n in dmx.flush():           # emit the final NAL at EOF
                        feed_q.put(nal_record(n, idx)); idx += 1
                    log(f"feed(nal): stream end — {idx} NAL(s)")
                    break
                for n in dmx.feed(data):
                    feed_q.put(nal_record(n, idx)); idx += 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--feed-host", default="192.168.50.1",
                    help="host iface the board nc's into (default 192.168.50.1)")
    ap.add_argument("--feed-port", type=int, default=9000)
    ap.add_argument("--sock", default="/tmp/de25_wave.sock",
                    help="Unix socket RouteWave attaches to")
    ap.add_argument("--self-test", action="store_true",
                    help="inject a synthetic counter feed (no board needed)")
    ap.add_argument("--replay", metavar="FILE",
                    help="replay FPGA records captured over Ethernet from FILE "
                         "(lines '<kind> <hexvalue> [text]'); loops forever")
    ap.add_argument("--nal", action="store_true",
                    help="treat the TCP feed as a raw H.264 Annex-B byte stream "
                         "and demux NALs → Protocol Analyzer packets")
    ap.add_argument("--replay-bin", metavar="FILE",
                    help="replay an H.264 Annex-B .264 file as NAL packets; loops")
    args = ap.parse_args()

    feed_q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=10000)
    log = lambda m: print(f"[de25-bridge] {m}", flush=True)  # noqa: E731

    if args.self_test:
        def _synth() -> None:
            i = 0
            import time
            while True:
                feed_q.put({"kind": "selftest", "text": f"selftest=0x{i:08x}",
                            "value": i, "payload": {"value": i}})
                i += 1
                time.sleep(0.05)
        threading.Thread(target=_synth, daemon=True).start()
        log("self-test feed running (synthetic counter)")
    elif args.replay:
        records = [r for r in (_parse_record(ln)
                   for ln in Path(args.replay).read_text().splitlines()) if r]
        log(f"replay: {len(records)} FPGA records loaded from {args.replay}")

        def _replay() -> None:
            import time
            while True:
                for r in records:
                    feed_q.put(r)
                    time.sleep(0.03)
        threading.Thread(target=_replay, daemon=True).start()
    elif args.replay_bin:
        records = records_from_annexb(Path(args.replay_bin).read_bytes())
        types = [r["value"] for r in records]
        log(f"replay-bin: {len(records)} NAL(s) from {args.replay_bin} — types {types[:12]}"
            + (" ..." if len(types) > 12 else ""))

        def _replay_nal() -> None:
            import time
            while True:
                for r in records:
                    feed_q.put(r)
                    time.sleep(0.05)
        threading.Thread(target=_replay_nal, daemon=True).start()
    else:
        listener = _feed_listener_nal if args.nal else _feed_listener
        threading.Thread(target=listener,
                         args=(args.feed_host, args.feed_port, feed_q, log),
                         daemon=True).start()

    src = De25Source(feed_q)
    log(f"serving wave_stream on {args.sock} — attach in RouteWave with:")
    log(f"    stream attach {args.sock}")
    return serve_unix_socket(Path(args.sock), src, accept_timeout_s=600.0, log=log)


if __name__ == "__main__":
    raise SystemExit(main())

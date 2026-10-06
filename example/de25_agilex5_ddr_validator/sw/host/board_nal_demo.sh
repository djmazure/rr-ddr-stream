#!/bin/bash
# Board-in-the-loop NAL-over-Ethernet demo (Phase 1, transport proof).
#
# Sends a REAL H.264 Constrained-Baseline stream FROM the DE25 over Ethernet and
# demuxes it into RouteWave Protocol-Analyzer packets — no fabric change yet (the
# bytes come from the board FS; the FPGA-side NAL generator is Phase 1b). This is
# the procedure to run once the board is booted with eth0 up.
#
# Prereqs (see project_de25_ethernet memory): board power-cycled, _hps.sof
# JTAG-loaded (Quartus Pro 25.3), booted, `ifconfig eth0 192.168.50.2` up, host
# eno1 = 192.168.50.1/24, direct cable.
set -euo pipefail
HOST=$(dirname "$(readlink -f "$0")")
TTY=${TTY:-/dev/ttyUSB0}
HOST_IP=${HOST_IP:-192.168.50.1}
STREAM="$HOST/testdata/qcif_idr.264"

echo "1) Transfer $STREAM to the board over serial (base64-chunked, ~46 lines)."
echo "   (the board's busybox has base64 -d; same pattern as the BIST transfer)"
B64=$(base64 -w0 "$STREAM"); echo "$B64" | fold -w110 > /tmp/qcif_b64.txt
stty -F "$TTY" 115200 -echo -icrnl -ixon raw
printf ': > /tmp/q.b64\r\n' > "$TTY"; sleep 0.3
while IFS= read -r c; do printf "printf '%%s' '%s' >> /tmp/q.b64\r\n" "$c" > "$TTY"; sleep 0.3; done < /tmp/qcif_b64.txt
printf 'base64 -d /tmp/q.b64 > /tmp/q.264; md5sum /tmp/q.264\r\n' > "$TTY"; sleep 1.5
echo "   -> compare the board md5 with: $(md5sum "$STREAM" | cut -d' ' -f1)"

echo "2) Host listens; board nc's the stream over Ethernet."
( timeout 14 nc -l -p 9000 > /tmp/qcif_from_board.264 ) &
sleep 1
printf 'cat /tmp/q.264 | nc -w 2 %s 9000; echo SENT_$?\r\n' "$HOST_IP" > "$TTY"
wait
echo "   captured $(wc -c < /tmp/qcif_from_board.264) bytes; md5 $(md5sum /tmp/qcif_from_board.264 | cut -d' ' -f1)"
echo "   (must equal $(md5sum "$STREAM" | cut -d' ' -f1) → the H.264 stream crossed the DE25 Ethernet intact)"

echo "3) Demux the board-delivered stream into NAL packets:"
python3 "$HOST/de25_wave_bridge.py" --replay-bin /tmp/qcif_from_board.264 --sock /tmp/de25_nal.sock &
BPID=$!; sleep 1
python3 - <<'PY' || true
import socket,msgpack,time
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect("/tmp/de25_nal.sock")
u=msgpack.Unpacker(raw=False); ok=False; s.settimeout(4); t=time.time(); n=[]
while time.time()-t<4 and len(n)<6:
    d=s.recv(65536)
    if not d: break
    u.feed(d)
    for o in u:
        if isinstance(o,list) and len(o)==2:
            tag,pl=o
            if tag==0 and not ok: s.sendall(msgpack.packb([1,{"consumer":"demo"}],use_bin_type=True)); ok=True
            if tag==6:
                for a in pl["annotations"]: print("  NAL:", a["kind"], a["text"])
PY
kill $BPID 2>/dev/null || true
echo "4) For the live RouteWave view: run de25_wave_bridge.py --nal (TCP feed) and"
echo "   'stream attach' in RouteWave, then board: cat /tmp/q.264 | nc \$HOST_IP 9000"

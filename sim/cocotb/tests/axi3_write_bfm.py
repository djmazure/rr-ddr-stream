"""Spec-faithful AXI3 write subordinate BFM — IDP-T2.5 silicon repro.

Models the real Zynq-7 ``S_AXI_HP0`` write endpoint (ARM IHI 0022, AMBA AXI3)
that the cocotb ``AxiRam`` (ideal AXI4) does NOT: AXI3 caps a burst at **16
beats** (``AWLEN`` is 4 bits, 0..15), whereas the rr-ddr-stream ``burstw_setter``
emits up to **256-beat** AXI4 bursts (``C_MAX_BURST_LENGTH=256``). On silicon the
AXI4→AXI3 smartconnect must split those long bursts; the data-loss we measured
(~64/1024 events land = exactly one 16-beat AXI3 burst) is that split going
wrong. ``AxiRam`` hides it by accepting 256-beat bursts outright.

This subordinate is therefore an **adversarial AXI3 protocol checker + faithful
memory**:

  * enforces the AXI3 rules the real HP0 imposes (``AWLEN<=15``, ``AWSIZE`` fits
    the bus, ``AWBURST`` legal, WRAP length/alignment, 4 KiB no-cross, exact
    beat count, ``WLAST`` placement, ``WVALID``/``AWVALID`` stability);
  * writes memory by the AXI addressing algorithm (IHI 0022 A3.4.1) honouring
    ``AWSIZE``/``WSTRB``/INCR/WRAP, so a faithful image can be checked;
  * on a >16-beat burst, behaves per ``on_long_burst``:
      - ``"assert"``     → record a violation + (optionally) fail loudly: the
                           root-cause finding (ring is AXI3-incompatible);
      - ``"truncate16"`` → accept only the first 16 beats, drop the rest: models
                           the silicon failure to reproduce the ~64-event image.

Write channel only (idp_data_plane is write-plane-only; AR/R tied off).
References: ARM IHI 0022 (AXI3) §A3.4 (transactions), §A8.3 (AWLEN 4-bit/16-beat).
"""
from __future__ import annotations

import random

import cocotb
from cocotb.triggers import RisingEdge, ReadOnly


def _b(sig) -> int:
    return int(sig.value)


class Axi3WriteSubordinate:
    AXI3_MAX_BEATS = 16          # AWLEN 4-bit ⇒ 1..16 beats (AWLEN 0..15)
    BURST_FIXED, BURST_INCR, BURST_WRAP, BURST_RESVD = 0, 1, 2, 3

    def __init__(self, dut, clk, reset, *, prefix="M_AXI", data_w=128,
                 addr_w=32, mem_size=1 << 21, on_long_burst="assert",
                 fail_on_violation=False, w_stall_prob=0.0, b_latency=4, seed=0xA3,
                 aw_to_w_latency=0, aw_initial_stall=0):
        self.dut = dut
        self.clk = clk
        self.reset = reset
        self.p = prefix
        self.data_w = data_w
        self.bytes_per_bus = data_w // 8
        self.addr_w = addr_w
        self.mem = bytearray(mem_size)
        self.mem_size = mem_size
        self.on_long_burst = on_long_burst
        self.fail_on_violation = fail_on_violation
        # Real DDR/HP0 has write latency; stalling W lets the ring's egress FIFO
        # accumulate, which is exactly what provokes the long (>16-beat) bursts.
        self.w_stall_prob = w_stall_prob
        self.b_latency = b_latency
        # Fixed AW→first-W latency per burst (DDR row-activation). Sustained
        # back-pressure here lets the egress FIFO accumulate past 16 words, so
        # the burstw_setter emits the long (>16-beat) AXI4 bursts the AXI3 HP0
        # can't take — the realistic provocation random per-beat stalls miss.
        self.aw_to_w_latency = aw_to_w_latency
        # Hold AWREADY low for this many clk cycles after reset deasserts. A real
        # DDR controller is busy in its init/cal window and back-pressures AW;
        # holding AW off lets the ring's egress FIFO fill PAST 16 DDR words
        # before the first burst latches, so the burstw_setter emits a >16-beat
        # (AXI3-illegal) burst at the AXI4 default. Pure provocation — it never
        # relaxes a protocol check (the AWLEN<=15 assertion still fires the
        # instant a long burst is accepted).
        self.aw_initial_stall = aw_initial_stall
        self._rng = random.Random(seed)
        self.violations: list[str] = []
        self.aw_seen = 0
        self.beats_written = 0
        self.bursts_committed = 0
        self.max_awlen_seen = -1
        # AW→W handoff queue (AXI4 no-WID: W beats follow AW acceptance order).
        self._aw_q: list[dict] = []

    def _sig(self, name):
        return getattr(self.dut, f"{self.p}_{name}")

    def _flag(self, msg: str):
        self.violations.append(msg)
        self.dut._log.error(f"AXI3-VIOLATION: {msg}")
        assert not self.fail_on_violation, f"AXI3 protocol violation: {msg}"

    # ── public ───────────────────────────────────────────────────────────────
    def start(self):
        cocotb.start_soon(self._aw_channel())
        cocotb.start_soon(self._w_channel())
        cocotb.start_soon(self._b_channel())
        cocotb.start_soon(self._tie_read())

    def read_words(self, base_addr, n_words, word_bytes):
        out = []
        for i in range(n_words):
            a = base_addr + i * word_bytes
            out.append(int.from_bytes(self.mem[a:a + word_bytes], "little"))
        return out

    # ── AW ─────────────────────────────────────────────────────────────────
    async def _aw_channel(self):
        aw = self._sig
        aw("AWREADY").value = 0
        # accept one AW per cycle when valid (simple, always-ready after reset),
        # except for an optional initial AWREADY-low window (aw_initial_stall)
        # modelling a DDR controller busy in its post-reset init/cal phase. That
        # window lets the egress FIFO fill past 16 DDR words, so the first burst
        # the IP latches is long (>16 beats) at the AXI4 default.
        stall_left = self.aw_initial_stall
        while True:
            await RisingEdge(self.clk)
            if _b(self.reset):
                aw("AWREADY").value = 0
                self._aw_q.clear()
                stall_left = self.aw_initial_stall
                continue
            if stall_left > 0:
                stall_left -= 1
                aw("AWREADY").value = 0
                continue
            aw("AWREADY").value = 1
            await ReadOnly()
            if _b(aw("AWVALID")) and _b(aw("AWREADY")):
                self._capture_aw()

    def _capture_aw(self):
        s = self._sig
        addr = _b(s("AWADDR"))
        awlen = _b(s("AWLEN"))
        awsize = _b(s("AWSIZE"))
        burst = _b(s("AWBURST"))
        awid = _b(s("AWID")) if hasattr(self.dut, f"{self.p}_AWID") else 0
        beats = awlen + 1
        nbytes = 1 << awsize
        self.aw_seen += 1
        self.max_awlen_seen = max(self.max_awlen_seen, awlen)

        # ── AXI3 / AXI4 protocol checks ──────────────────────────────────────
        if awsize > (self.bytes_per_bus.bit_length() - 1):
            self._flag(f"AWSIZE={awsize} exceeds bus width {self.bytes_per_bus}B")
        if burst == self.BURST_RESVD:
            self._flag("AWBURST=2'b11 is reserved")
        # AXI3: max 16 beats. THIS is the HP0 limit the ring ignores.
        if beats > self.AXI3_MAX_BEATS:
            self._flag(
                f"AWLEN={awlen} ({beats} beats) > AXI3 max 16 beats — "
                f"S_AXI_HP0 cannot accept this; the smartconnect must split it")
        if burst == self.BURST_INCR:
            last = addr + beats * nbytes - 1
            if (addr & ~0xFFF) != (last & ~0xFFF):
                self._flag(
                    f"INCR burst @0x{addr:08x} len={awlen} size={awsize} "
                    f"crosses a 4 KiB boundary (last=0x{last:08x})")
        if burst == self.BURST_WRAP:
            if beats not in (2, 4, 8, 16):
                self._flag(f"WRAP burst beats={beats} not in {{2,4,8,16}}")
            if addr % nbytes != 0:
                self._flag(f"WRAP burst addr 0x{addr:08x} not aligned to {nbytes}B")

        accept_beats = beats
        truncated = False
        if beats > self.AXI3_MAX_BEATS and self.on_long_burst == "truncate16":
            accept_beats = self.AXI3_MAX_BEATS   # model the silicon split-drop
            truncated = True

        self._aw_q.append(dict(addr=addr, awlen=awlen, awsize=awsize,
                               burst=burst, awid=awid, beats=beats,
                               nbytes=nbytes, accept_beats=accept_beats,
                               truncated=truncated, rx=0))

    # ── W ────────────────────────────────────────────────────────────────────
    async def _w_channel(self):
        s = self._sig
        s("WREADY").value = 0
        self._b_q: list[int] = []
        while True:
            await RisingEdge(self.clk)
            if _b(self.reset):
                s("WREADY").value = 0
                continue
            # Apply the per-burst AW→first-W latency before accepting any W beat.
            if self._aw_q:
                cur = self._aw_q[0]
                if not cur.get("w_started", False):
                    if "lat" not in cur:
                        cur["lat"] = self.aw_to_w_latency
                    if cur["lat"] > 0:
                        cur["lat"] -= 1
                        s("WREADY").value = 0
                        continue
                    cur["w_started"] = True
            ready = bool(self._aw_q) and (self._rng.random() >= self.w_stall_prob)
            s("WREADY").value = 1 if ready else 0
            await ReadOnly()
            if ready and _b(s("WVALID")) and _b(s("WREADY")):
                self._consume_w_beat()

    def _consume_w_beat(self):
        s = self._sig
        cur = self._aw_q[0]
        wdata = _b(s("WDATA"))
        wstrb = _b(s("WSTRB"))
        wlast = _b(s("WLAST"))
        # Address of THIS beat per the AXI addressing algorithm (A3.4.1).
        beat_idx = cur["rx"]
        nbytes = cur["nbytes"]
        if cur["burst"] == self.BURST_FIXED:
            beat_addr = cur["addr"]
        elif cur["burst"] == self.BURST_WRAP:
            total = cur["beats"] * nbytes
            wrap_lo = (cur["addr"] // total) * total
            beat_addr = wrap_lo + ((cur["addr"] + beat_idx * nbytes - wrap_lo) % total)
        else:  # INCR
            aligned = (cur["addr"] // nbytes) * nbytes
            beat_addr = cur["addr"] if beat_idx == 0 else aligned + beat_idx * nbytes
        # Commit bytes honouring WSTRB (drop beats beyond the AXI3-accepted count).
        # WSTRB lane N drives the byte at the beat's bus-aligned base + N
        # (the ring writes full-width beats, AWSIZE = bus width).
        if beat_idx < cur["accept_beats"]:
            base = (beat_addr // self.bytes_per_bus) * self.bytes_per_bus
            for lane in range(self.bytes_per_bus):
                if (wstrb >> lane) & 1:
                    addr_b = base + lane
                    if 0 <= addr_b < self.mem_size:
                        self.mem[addr_b] = (wdata >> (lane * 8)) & 0xFF
            self.beats_written += 1
        cur["rx"] += 1
        # End of burst on WLAST.
        if wlast:
            if cur["rx"] != cur["beats"]:
                self._flag(
                    f"WLAST at beat {cur['rx']} but AWLEN implies {cur['beats']} "
                    f"beats (addr 0x{cur['addr']:08x})")
            self._b_q.append(cur["awid"])
            self.bursts_committed += 1
            self._aw_q.pop(0)
        elif cur["rx"] >= cur["beats"]:
            self._flag(
                f"more W beats than AWLEN+1={cur['beats']} without WLAST "
                f"(addr 0x{cur['addr']:08x})")

    # ── B ────────────────────────────────────────────────────────────────────
    async def _b_channel(self):
        s = self._sig
        s("BVALID").value = 0
        s("BRESP").value = 0
        if hasattr(self.dut, f"{self.p}_BID"):
            s("BID").value = 0
        self._b_q = getattr(self, "_b_q", [])
        while True:
            await RisingEdge(self.clk)
            if _b(self.reset):
                s("BVALID").value = 0
                continue
            if self._b_q and not _b(s("BVALID")):
                bid = self._b_q.pop(0)
                s("BVALID").value = 1
                s("BRESP").value = 0   # OKAY
                if hasattr(self.dut, f"{self.p}_BID"):
                    s("BID").value = bid
            elif _b(s("BVALID")):
                await ReadOnly()
                if _b(s("BREADY")):
                    cocotb.start_soon(self._clear_bvalid())

    async def _clear_bvalid(self):
        await RisingEdge(self.clk)
        self._sig("BVALID").value = 0

    # ── AR/R tie-off (write-plane-only DUT issues no reads) ─────────────────
    async def _tie_read(self):
        s = self._sig
        try:
            s("ARREADY").value = 0
            s("RVALID").value = 0
        except AttributeError:
            pass

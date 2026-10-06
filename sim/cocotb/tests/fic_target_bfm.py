# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""Spec-faithful PolarFire SoC MSS FIC target subordinate BFM (DDR-P2.15).

Models the Microchip PolarFire SoC MSS Fabric Interface Controller (FIC) AXI4
target port connecting FPGA fabric to MSS LPDDR4 memory.

PolarFire SoC FIC features / constraints:
  - 64-bit native fabric data width (8 bytes/beat).
  - Maximum burst length: 64 beats (AWLEN <= 63, ARLEN <= 63).
    The PolarFire SoC CoreAXI4Interconnect / FIC target hardware strictly caps
    bursts at 64 beats. Bursts exceeding 64 beats are protocol violations.
  - 4 KiB boundary crossing prevention (standard AXI4 rule).
  - Memory-backed write (AW, W, B) and read (AR, R) channels with full data integrity.
"""
from __future__ import annotations

import random
import cocotb
from cocotb.triggers import ReadOnly, RisingEdge


def _b(sig) -> int:
    return int(sig.value)


class FicTargetSubordinate:
    """Adversarial PolarFire SoC MSS FIC AXI4 subordinate checker + faithful memory."""

    FIC_MAX_BURST_BEATS = 64  # PolarFire SoC FIC burst cap: AWLEN/ARLEN <= 63 (1..64 beats)
    BURST_FIXED, BURST_INCR, BURST_WRAP, BURST_RESVD = 0, 1, 2, 3

    def __init__(
        self,
        dut,
        clk,
        reset,
        *,
        prefix="M_AXI",
        data_w=64,
        addr_w=32,
        mem_size=1 << 21,
        on_long_burst="assert",
        fail_on_violation=False,
        w_stall_prob=0.0,
        b_latency=2,
        ar_to_r_latency=2,
        seed=0x0F1C,
        aw_to_w_latency=0,
        aw_initial_stall=0,
    ):
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
        self.w_stall_prob = w_stall_prob
        self.b_latency = b_latency
        self.ar_to_r_latency = ar_to_r_latency
        self.aw_to_w_latency = aw_to_w_latency
        self.aw_initial_stall = aw_initial_stall
        self._rng = random.Random(seed)

        self.violations: list[str] = []
        self.aw_seen = 0
        self.ar_seen = 0
        self.beats_written = 0
        self.beats_read = 0
        self.bursts_committed = 0
        self.max_awlen_seen = -1
        self.max_arlen_seen = -1

        self._aw_q: list[dict] = []
        self._ar_q: list[dict] = []
        self._b_q: list[dict] = []

    def _sig(self, name: str):
        return getattr(self.dut, f"{self.p}_{name}")

    def _flag(self, msg: str):
        self.violations.append(msg)
        self.dut._log.error(f"FIC-VIOLATION: {msg}")
        assert not self.fail_on_violation, f"FIC target protocol violation: {msg}"

    # ── public ───────────────────────────────────────────────────────────────
    def start(self):
        cocotb.start_soon(self._aw_channel())
        cocotb.start_soon(self._w_channel())
        cocotb.start_soon(self._b_channel())
        cocotb.start_soon(self._ar_channel())
        cocotb.start_soon(self._r_channel())

    def read_words(self, base_addr: int, n_words: int, word_bytes: int = 4) -> list[int]:
        out = []
        for i in range(n_words):
            a = base_addr + i * word_bytes
            out.append(int.from_bytes(self.mem[a : a + word_bytes], "little"))
        return out

    def write_words(self, base_addr: int, words: list[int], word_bytes: int = 4) -> None:
        for i, val in enumerate(words):
            a = base_addr + i * word_bytes
            self.mem[a : a + word_bytes] = val.to_bytes(word_bytes, "little")

    # ── AW channel ───────────────────────────────────────────────────────────
    async def _aw_channel(self):
        aw = self._sig
        aw("AWREADY").value = 0
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

        # ── PolarFire SoC FIC protocol checks ────────────────────────────────
        if awsize > (self.bytes_per_bus.bit_length() - 1):
            self._flag(f"AWSIZE={awsize} exceeds bus width {self.bytes_per_bus}B")
        if burst == self.BURST_RESVD:
            self._flag("AWBURST=2'b11 is reserved")
        if beats > self.FIC_MAX_BURST_BEATS:
            self._flag(
                f"AWLEN={awlen} ({beats} beats) > PolarFire SoC FIC max {self.FIC_MAX_BURST_BEATS} beats — "
                f"FIC target port cannot accept this"
            )
        if burst == self.BURST_INCR:
            last = addr + beats * nbytes - 1
            if (addr & ~0xFFF) != (last & ~0xFFF):
                self._flag(
                    f"INCR write burst @0x{addr:08x} len={awlen} size={awsize} "
                    f"crosses a 4 KiB boundary (last=0x{last:08x})"
                )

        accept_beats = beats
        truncated = False
        if beats > self.FIC_MAX_BURST_BEATS and self.on_long_burst == "truncate64":
            accept_beats = self.FIC_MAX_BURST_BEATS
            truncated = True

        self._aw_q.append(
            dict(
                addr=addr,
                awlen=awlen,
                awsize=awsize,
                burst=burst,
                awid=awid,
                beats=beats,
                nbytes=nbytes,
                accept_beats=accept_beats,
                truncated=truncated,
                rx=0,
            )
        )

    # ── W channel ────────────────────────────────────────────────────────────
    async def _w_channel(self):
        s = self._sig
        s("WREADY").value = 0
        while True:
            await RisingEdge(self.clk)
            if _b(self.reset):
                s("WREADY").value = 0
                continue
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

        if beat_idx < cur["accept_beats"]:
            base = (beat_addr // self.bytes_per_bus) * self.bytes_per_bus
            for lane in range(self.bytes_per_bus):
                if (wstrb >> lane) & 1:
                    addr_b = base + lane
                    if 0 <= addr_b < self.mem_size:
                        self.mem[addr_b] = (wdata >> (lane * 8)) & 0xFF
            self.beats_written += 1
        cur["rx"] += 1

        if wlast:
            if cur["rx"] != cur["beats"]:
                self._flag(
                    f"WLAST at beat {cur['rx']} but AWLEN implies {cur['beats']} "
                    f"beats (addr 0x{cur['addr']:08x})"
                )
            self._b_q.append(dict(bid=cur["awid"], lat=self.b_latency))
            self.bursts_committed += 1
            self._aw_q.pop(0)
        elif cur["rx"] >= cur["beats"]:
            self._flag(
                f"more W beats than AWLEN+1={cur['beats']} without WLAST "
                f"(addr 0x{cur['addr']:08x})"
            )

    # ── B channel ────────────────────────────────────────────────────────────
    async def _b_channel(self):
        s = self._sig
        s("BVALID").value = 0
        s("BRESP").value = 0
        if hasattr(self.dut, f"{self.p}_BID"):
            s("BID").value = 0
        while True:
            await RisingEdge(self.clk)
            if _b(self.reset):
                s("BVALID").value = 0
                self._b_q.clear()
                continue

            if _b(s("BVALID")):
                if _b(s("BREADY")):
                    s("BVALID").value = 0
                else:
                    continue

            if not _b(s("BVALID")) and self._b_q:
                item = self._b_q[0]
                if item["lat"] > 0:
                    item["lat"] -= 1
                    continue
                self._b_q.pop(0)
                s("BVALID").value = 1
                s("BRESP").value = 0  # OKAY
                if hasattr(self.dut, f"{self.p}_BID"):
                    s("BID").value = item["bid"]

    # ── AR channel ───────────────────────────────────────────────────────────
    async def _ar_channel(self):
        s = self._sig
        s("ARREADY").value = 0
        while True:
            await RisingEdge(self.clk)
            if _b(self.reset):
                s("ARREADY").value = 0
                self._ar_q.clear()
                continue
            s("ARREADY").value = 1
            await ReadOnly()
            if _b(s("ARVALID")) and _b(s("ARREADY")):
                self._capture_ar()

    def _capture_ar(self):
        s = self._sig
        addr = _b(s("ARADDR"))
        arlen = _b(s("ARLEN"))
        arsize = _b(s("ARSIZE"))
        burst = _b(s("ARBURST"))
        arid = _b(s("ARID")) if hasattr(self.dut, f"{self.p}_ARID") else 0
        beats = arlen + 1
        nbytes = 1 << arsize
        self.ar_seen += 1
        self.max_arlen_seen = max(self.max_arlen_seen, arlen)

        if arsize > (self.bytes_per_bus.bit_length() - 1):
            self._flag(f"ARSIZE={arsize} exceeds bus width {self.bytes_per_bus}B")
        if burst == self.BURST_RESVD:
            self._flag("ARBURST=2'b11 is reserved")
        if beats > self.FIC_MAX_BURST_BEATS:
            self._flag(
                f"ARLEN={arlen} ({beats} beats) > PolarFire SoC FIC max {self.FIC_MAX_BURST_BEATS} beats — "
                f"FIC target port cannot accept this"
            )
        if burst == self.BURST_INCR:
            last = addr + beats * nbytes - 1
            if (addr & ~0xFFF) != (last & ~0xFFF):
                self._flag(
                    f"INCR read burst @0x{addr:08x} len={arlen} size={arsize} "
                    f"crosses a 4 KiB boundary (last=0x{last:08x})"
                )

        self._ar_q.append(
            dict(
                addr=addr,
                arlen=arlen,
                arsize=arsize,
                burst=burst,
                arid=arid,
                beats=beats,
                nbytes=nbytes,
                tx=0,
                lat=self.ar_to_r_latency,
            )
        )

    # ── R channel ────────────────────────────────────────────────────────────
    async def _r_channel(self):
        s = self._sig
        s("RVALID").value = 0
        s("RRESP").value = 0
        s("RLAST").value = 0
        s("RDATA").value = 0
        if hasattr(self.dut, f"{self.p}_RID"):
            s("RID").value = 0

        while True:
            await RisingEdge(self.clk)
            if _b(self.reset):
                s("RVALID").value = 0
                s("RLAST").value = 0
                continue

            if _b(s("RVALID")):
                if _b(s("RREADY")):
                    cur = self._ar_q[0]
                    cur["tx"] += 1
                    self.beats_read += 1
                    if cur["tx"] >= cur["beats"]:
                        self._ar_q.pop(0)
                        s("RVALID").value = 0
                        s("RLAST").value = 0
                    else:
                        self._drive_r_beat(cur)
                        continue
                else:
                    continue

            if not self._ar_q:
                s("RVALID").value = 0
                s("RLAST").value = 0
                continue

            cur = self._ar_q[0]
            if cur["lat"] > 0:
                cur["lat"] -= 1
                s("RVALID").value = 0
                s("RLAST").value = 0
                continue

            self._drive_r_beat(cur)

    def _drive_r_beat(self, cur: dict):
        s = self._sig
        beat_idx = cur["tx"]
        nbytes = cur["nbytes"]
        if cur["burst"] == self.BURST_FIXED:
            beat_addr = cur["addr"]
        else:  # INCR
            aligned = (cur["addr"] // nbytes) * nbytes
            beat_addr = cur["addr"] if beat_idx == 0 else aligned + beat_idx * nbytes

        base = (beat_addr // self.bytes_per_bus) * self.bytes_per_bus
        rdata_val = 0
        for lane in range(self.bytes_per_bus):
            addr_b = base + lane
            if 0 <= addr_b < self.mem_size:
                byte_val = self.mem[addr_b]
            else:
                byte_val = 0
            rdata_val |= byte_val << (lane * 8)

        s("RDATA").value = rdata_val
        s("RRESP").value = 0
        if hasattr(self.dut, f"{self.p}_RID"):
            s("RID").value = cur["arid"]
        s("RLAST").value = 1 if (beat_idx + 1 == cur["beats"]) else 0
        s("RVALID").value = 1

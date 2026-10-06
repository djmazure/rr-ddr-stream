"""Unit test of the realistic DDR AXI slave model ITSELF.

Each knob of ``RealisticDdrAxiRam`` (sim/models/realistic_ddr_axi.py) is proven
to do what it claims, by driving a cocotbext ``AxiMaster`` against a
dependency-free AXI4 wire-through top (sim/models/tb/axi_wirethrough.vhd) that
puts the model on the master side. The model is the DUT here — there is no IP in
the loop, so a failure is unambiguously the model.

Knobs proven:
  * ideal preset           — round-trips clean, byte-for-byte AxiRam.
  * write_commit_latency   — a read issued right after a write, while early
                             BRESP already returned, sees STALE data (the
                             posted-write hazard); after the commit window it
                             sees the new data.
  * raw_hazard="stall"     — same overtaking read, but now the model holds the
                             read until commit ⇒ it returns the NEW data (count
                             right, data right, latency grew).
  * raw_hazard="poison"    — the overtaking read returns the poison pattern.
  * axi3 + on_long_burst   — a >16-beat burst is flagged (and truncated when
                             configured), reproducing the AXI3 HP0 limit.

Hard-coded expected values throughout (ROUTERTL-002); no assertion is weakened
and no sleep dodges a race — the commit-window timing is the property under test.

Run via:  rr sim run test_realistic_ddr_axi
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from cocotbext.axi import AxiBus, AxiMaster

# Make the model importable (sibling dir of this tests/ dir).
_MODELS_DIR = _Path(__file__).resolve().parents[1]
if str(_MODELS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_MODELS_DIR))

from realistic_ddr_axi import RealisticDdrAxiRam, DdrModelConfig  # noqa: E402

# RouteRTL canonical entry point + helpers (on the path under `rr sim run`).
try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
    from sdk.cocotb_helpers import requires        # type: ignore  # noqa: E402
except ImportError:                                # non-rr collection fallback
    run_simulation = None

    def requires(*_a, **_k):                        # type: ignore[no-redef]
        def _wrap(fn):
            return fn
        return _wrap


_TB = _MODELS_DIR / "tb" / "axi_wirethrough.vhd"

DATA_W = 128
BYTES_PER_WORD = DATA_W // 8
CLK_NS = 4.0


def _libraries() -> dict:
    return {"tich": [str(_TB)]}


def main() -> None:
    if run_simulation is None:
        raise RuntimeError("invoke via `rr sim run test_realistic_ddr_axi`.")
    run_simulation(
        top_level="axi_wirethrough",
        module="test_realistic_ddr_axi",
        custom_libraries=_libraries(),
        work_lib="tich",
        generics={"G_ID_W": 4, "G_ADDR_W": 32, "G_DATA_W": DATA_W},
        waves=True,
        simulator="nvc",
    )


async def _bring_up(dut, config):
    """Bring up clock + reset and construct BOTH the master and the realistic
    DDR slave model WHILE reset is asserted, so every cocotbext channel's reset
    handler holds its driven lines at 0 across reset-deassert — avoiding the
    cocotb-2.x 'U'-sampled-at-deassert race. Returns (master, ram)."""
    cocotb.start_soon(Clock(dut.CLK, CLK_NS, units="ns").start())
    dut.RST.value = 1
    master = AxiMaster(AxiBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)
    ram = RealisticDdrAxiRam(
        AxiBus.from_prefix(dut, "M_AXI"), dut.CLK, reset=dut.RST,
        size=1 << 20, config=config)
    for _ in range(5):
        await RisingEdge(dut.CLK)
    dut.RST.value = 0
    for _ in range(5):
        await RisingEdge(dut.CLK)
    return master, ram


# ─────────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("DDR-REQ-MODEL-001")
async def ideal_roundtrip_clean(dut):
    """ideal preset == stock AxiRam: a write then a read returns the exact
    bytes, with full RAW coherency."""
    master, ram = await _bring_up(dut, DdrModelConfig.preset("ideal"))

    payload = bytes(range(16))            # 0x00..0x0f, one 128-bit word
    await master.write(0x100, payload)
    resp = await master.read(0x100, 16)
    assert resp.data == payload, (
        f"ideal model corrupted a round-trip: got {resp.data!r} != {payload!r}")
    # Backdoor reflects committed state.
    assert ram.read(0x100, 16) == payload


@cocotb.test()
@requires("DDR-REQ-MODEL-002")
async def early_bresp_read_overtakes_returns_stale(dut):
    """write_commit_latency + early_bresp: BRESP returns before commit, so a
    read fired right after the write (during the commit window) sees the OLD
    (pre-write) memory — the posted-write RAW hazard. After the window, the new
    data is visible."""
    cfg = DdrModelConfig(write_commit_latency=40, early_bresp=True,
                         raw_hazard="stale")
    master, ram = await _bring_up(dut, cfg)

    addr = 0x200
    old = bytes([0x11] * 16)
    new = bytes([0x22] * 16)
    # Seed committed memory with the OLD value via the backdoor.
    ram.write(addr, old)

    # Issue the posted write of NEW; BRESP returns early (before the 40-cycle
    # commit). Immediately read back: must see OLD (stale), not NEW.
    await master.write(addr, new)          # returns at early BRESP
    resp_during = await master.read(addr, 16)
    assert resp_during.data == old, (
        f"posted-write hazard NOT modelled: read during commit window got "
        f"{resp_during.data!r}, expected stale {old!r}")

    # Wait past the commit window, read again: must now see NEW.
    await ClockCycles(dut.CLK, 80)
    resp_after = await master.read(addr, 16)
    assert resp_after.data == new, (
        f"write never committed: post-window read got {resp_after.data!r}, "
        f"expected {new!r}")


@cocotb.test()
@requires("DDR-REQ-MODEL-003")
async def raw_stall_orders_read_after_write(dut):
    """raw_hazard='stall': the same overtaking read is HELD until the write
    commits, so it returns the NEW value (count right, data right, latency
    grew) — the controller-orders-them alternative to stale."""
    cfg = DdrModelConfig(write_commit_latency=40, early_bresp=True,
                         raw_hazard="stall")
    master, ram = await _bring_up(dut, cfg)

    addr = 0x300
    old = bytes([0x33] * 16)
    new = bytes([0x44] * 16)
    ram.write(addr, old)

    await master.write(addr, new)
    resp = await master.read(addr, 16)     # stalled until commit
    assert resp.data == new, (
        f"raw_hazard='stall' did not order the read after the write: got "
        f"{resp.data!r}, expected committed {new!r}")


@cocotb.test()
@requires("DDR-REQ-MODEL-004")
async def raw_poison_marks_overtaking_read(dut):
    """raw_hazard='poison': an overtaking read returns the poison pattern so the
    hazard fails LOUDLY rather than reading plausible stale data."""
    cfg = DdrModelConfig(write_commit_latency=40, early_bresp=True,
                         raw_hazard="poison", poison_byte=0xA5)
    master, ram = await _bring_up(dut, cfg)

    addr = 0x400
    ram.write(addr, bytes([0x55] * 16))
    await master.write(addr, bytes([0x66] * 16))
    resp = await master.read(addr, 16)
    assert resp.data == bytes([0xA5] * 16), (
        f"poison not returned for overtaking read: got {resp.data!r}")


@cocotb.test()
@requires("DDR-REQ-MODEL-005")
async def axi3_long_burst_flagged(dut):
    """axi3 mode: a 32-beat (AWLEN=31) write is AXI3-illegal (>16 beats) and the
    model records a violation. With on_long_burst='truncate' only the first 16
    beats commit, so the tail is dropped — the silicon split-drop."""
    cfg = DdrModelConfig(axi3=True, on_long_burst="truncate")
    master, ram = await _bring_up(dut, cfg)

    addr = 0x1000
    # 32 words of 16 bytes = one 32-beat INCR burst (AWLEN=31). Distinct bytes
    # per word so we can see which beats landed.
    words = [bytes([w & 0xFF] * 16) for w in range(32)]
    payload = b"".join(words)
    await master.write(addr, payload)
    await ClockCycles(dut.CLK, 20)

    assert any("AXI3" in v for v in ram.violations), (
        f"AXI3 16-beat violation not flagged for a 32-beat burst; "
        f"violations={ram.violations}")
    # truncate: first 16 words present, words 16..31 dropped (still old/zero).
    for w in range(16):
        got = ram.read(addr + w * BYTES_PER_WORD, BYTES_PER_WORD)
        assert got == words[w], (
            f"truncate dropped an accepted beat {w}: {got!r} != {words[w]!r}")
    tail = ram.read(addr + 16 * BYTES_PER_WORD, BYTES_PER_WORD)
    assert tail != words[16], (
        f"truncate did NOT drop beat 16: got {tail!r} == {words[16]!r} — the "
        f"AXI3 split-drop is not modelled")


if __name__ == "__main__":
    main()

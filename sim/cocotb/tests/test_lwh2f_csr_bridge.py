"""DDR-T2.4 — attack the lwh2f (AXI4-full) → AXI-Lite CSR glue with a real
full-AXI4 master BFM.

The standalone ``test_ddr_validator_csr`` drives the CSR's AXI-Lite slave port
DIRECTLY — it never exercises the hand-written ``lwh2f_axilite_bridge`` (id echo,
rlast tie, the full-master AW/W/B/AR/R handshake). On the DE25 silicon, a read of
the validator CSR over the lwhps2fpga master raises a synchronous external abort
(RTL-P3.739); the glue is the prime fabric-side suspect precisely BECAUSE it was
unverified. This test closes that gap: an ``Axi4Master`` (the rr full-AXI4 BFM,
with awid/arid + rlast) drives ``tb_lwh2f_csr`` = bridge + ddr_validator_csr.

Outcome semantics:
  • every single-beat write/read must COMPLETE (no ``AxiTimeoutError``) — a hang
    here is the on-silicon abort reproduced in sim;
  • responses must be OKAY and read-back values must match (hard-coded, per
    ROUTERTL-002);
  • bid/rid must echo the request id (the glue's reason to exist).
A green bar EXONERATES the glue and refocuses RTL-P3.739 on the fabric reset /
firewall; a red bar is the bug.
"""

from __future__ import annotations

from pathlib import Path as _Path

import cocotb
from cocotb.triggers import ClockCycles

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None

from rr_cocotb_tb.drivers.axi4 import Axi4Master, AxiResp
from rr_cocotb_tb.env import TbEnv

# Register offsets — must match ddr_validator_csr (docs/DESIGN.md §4).
REG_STATUS = 0x04
REG_SCENARIO = 0x08
REG_N_WORDS = 0x0C
REG_IDENT = 0x3C
IDENT_MAGIC = 0x0DD50200


async def _setup(dut):
    env = TbEnv(dut, clk="clk_i", rst="rst_i", period_ns=10)
    await env.start_clock()
    await env.reset(cycles=5, active_low=False)  # CSR rst_i is active-high
    master = Axi4Master(env, prefix="s_axi", raise_on_resp=False)
    await ClockCycles(dut.clk_i, 2)
    return env, master


@cocotb.test()
async def ident_reads_through_glue(dut):
    """The headline on-silicon op: read IDENT over the full-AXI4 glue. Must
    complete and return the magic — not hang (which is the silicon abort)."""
    _env, master = await _setup(dut)
    data, resp = await master.read(REG_IDENT, beats=1)
    assert resp == AxiResp.OKAY, f"IDENT read resp={resp!r} (not OKAY)"
    assert data[0] == IDENT_MAGIC, f"IDENT=0x{data[0]:08X}, expected 0x{IDENT_MAGIC:08X}"


@cocotb.test()
async def id_echo_on_read_and_write(dut):
    """bid/rid must echo the request id — the bridge's whole purpose. Drive a
    non-zero id and confirm the response carries it back."""
    _env, master = await _setup(dut)
    # Read with id=0xA — the bridge latches arid and drives rid back.
    await master.read(REG_IDENT, beats=1, id=0xA)
    assert int(dut.s_axi_rid.value) == 0xA, f"rid=0x{int(dut.s_axi_rid.value):X}, expected 0xA"
    # Write with id=0x5 — bid must echo it.
    await master.write(REG_N_WORDS, [0x0000_2000], id=0x5)
    assert int(dut.s_axi_bid.value) == 0x5, f"bid=0x{int(dut.s_axi_bid.value):X}, expected 0x5"


@cocotb.test()
async def write_readback_rw_register(dut):
    """A RW register must round-trip through the glue (write then read)."""
    _env, master = await _setup(dut)
    resp = await master.write(REG_N_WORDS, [0x1234_5678])
    assert resp == AxiResp.OKAY, f"N_WORDS write resp={resp!r}"
    data, resp = await master.read(REG_N_WORDS, beats=1)
    assert resp == AxiResp.OKAY
    assert data[0] == 0x1234_5678, f"N_WORDS readback=0x{data[0]:08X}"
    # second RW reg, distinct value (catches address-decode bleed)
    await master.write(REG_SCENARIO, [0x0000_0003])
    data, _ = await master.read(REG_SCENARIO, beats=1)
    assert data[0] == 0x0000_0003, f"SCENARIO readback=0x{data[0]:08X}"


@cocotb.test()
async def back_to_back_no_deadlock(dut):
    """Sustained back-to-back transactions must not wedge the glue handshake —
    the kind of cadence the real lwh2f master uses."""
    _env, master = await _setup(dut)
    for i in range(8):
        await master.write(REG_N_WORDS, [0x1000 + i])
        data, resp = await master.read(REG_N_WORDS, beats=1)
        assert resp == AxiResp.OKAY, f"iter {i}: resp={resp!r}"
        assert data[0] == 0x1000 + i, f"iter {i}: got 0x{data[0]:08X}"
    # IDENT still readable after the burst of traffic
    data, _ = await master.read(REG_IDENT, beats=1)
    assert data[0] == IDENT_MAGIC


def main() -> None:
    """`rr sim run test_lwh2f_csr_bridge` entry point."""
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke via `rr sim run test_lwh2f_csr_bridge`."
        )
    ip_root = _Path(__file__).resolve().parents[3]  # rr-ddr-stream/
    src = ip_root / "example" / "de25_agilex5_ddr_validator" / "src"
    run_simulation(
        top_level="tb_lwh2f_csr",
        module="test_lwh2f_csr_bridge",
        custom_libraries={
            "rr": [
                str(src / "lwh2f_axilite_bridge.vhd"),
                str(src / "ddr_validator_csr.vhd"),
                str(src / "tb_lwh2f_csr.vhd"),
            ],
        },
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

"""Phase 1b integration — lwh2f → demux → {validator CSR, synthetic NAL ROM}.

The lwh2f subsystem the DE25 top will use once the synthetic NAL generator is
mapped in. Proves the VALIDATED CSR path survives the de25_axil_demux2 (IDENT
0x0DD50200 still reads at 0x000, RW registers still round-trip) AND the NAL ROM
is reachable at 0x800 — and that interleaved access to both never wedges. This
de-risks the top integration before any synth / on-board run.

Address map (sel bit 11, inside the real 4 KiB lwh2f aperture — DDR-T2.7):
CSR @0x000..0x7FF, de25_nal_rom @0x800..0xFFF.
Expected values hard-coded (ROUTERTL-002).
"""
from __future__ import annotations

import sys
from pathlib import Path as _Path

import cocotb
from cocotb.triggers import ClockCycles

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None

from rr_cocotb_tb.drivers.axi4 import Axi4Master, AxiResp
from rr_cocotb_tb.env import TbEnv

# CSR (m0)
CSR_IDENT = 0x03C
CSR_N_WORDS = 0x00C
CSR_IDENT_MAGIC = 0x0DD50200
# NAL ROM (m1) — base 0x800 (sel bit 11, inside the 4 KiB lwh2f window)
NAL_BASE = 0x800
NAL_IDENT = NAL_BASE + 0x000
NAL_LEN = NAL_BASE + 0x004
NAL_WORDS = NAL_BASE + 0x008
NAL_ROM = NAL_BASE + 0x100
NAL_IDENT_MAGIC = 0x0DD50A10

EXPECTED_WORDS = [
    0x00000001, 0x67420028, 0xF4162720,
    0x00000001, 0x68CE3880,
    0x00000001, 0x65888402,
]
EXPECTED_NAL_TYPES = [7, 8, 5]


async def _setup(dut):
    env = TbEnv(dut, clk="clk_i", rst="rst_i", period_ns=10)
    await env.start_clock()
    await env.reset(cycles=5, active_low=False)
    master = Axi4Master(env, prefix="s_axi", raise_on_resp=False)
    await ClockCycles(dut.clk_i, 2)
    return env, master


@cocotb.test()
async def csr_path_survives_the_demux(dut):
    """CSR IDENT still reads through the demux, and a RW register round-trips —
    the validated lwh2f CSR access must be unaffected by inserting the demux."""
    _env, master = await _setup(dut)
    data, resp = await master.read(CSR_IDENT, beats=1)
    assert resp == AxiResp.OKAY, f"CSR IDENT resp={resp!r}"
    assert data[0] == CSR_IDENT_MAGIC, f"CSR IDENT=0x{data[0]:08X}"
    resp = await master.write(CSR_N_WORDS, [0x0000_2000])
    assert resp == AxiResp.OKAY, f"CSR write resp={resp!r}"
    data, _ = await master.read(CSR_N_WORDS, beats=1)
    assert data[0] == 0x0000_2000, f"CSR N_WORDS readback=0x{data[0]:08X}"


@cocotb.test()
async def nal_rom_reachable_above_0x800(dut):
    """The synthetic NAL ROM is mapped at 0x800 and serves IDENT/LEN/WORDS."""
    _env, master = await _setup(dut)
    data, resp = await master.read(NAL_IDENT, beats=1)
    assert resp == AxiResp.OKAY, f"NAL IDENT resp={resp!r}"
    assert data[0] == NAL_IDENT_MAGIC, f"NAL IDENT=0x{data[0]:08X}"
    assert (await master.read(NAL_LEN, beats=1))[0][0] == 4 * len(EXPECTED_WORDS)
    assert (await master.read(NAL_WORDS, beats=1))[0][0] == len(EXPECTED_WORDS)


@cocotb.test()
async def nal_stream_through_demux_demuxes_to_sps_pps_idr(dut):
    """Read the NAL ROM words THROUGH the demux, reconstruct, and demux with the
    host de25_nal — SPS(7)+PPS(8)+IDR(5)."""
    _env, master = await _setup(dut)
    stream = bytearray()
    for i in range(len(EXPECTED_WORDS)):
        w = (await master.read(NAL_ROM + 4 * i, beats=1))[0][0]
        assert w == EXPECTED_WORDS[i], f"word{i}=0x{w:08X} != 0x{EXPECTED_WORDS[i]:08X}"
        stream += bytes([(w >> 24) & 0xFF, (w >> 16) & 0xFF, (w >> 8) & 0xFF, w & 0xFF])
    sys.path.insert(0, str(_Path(__file__).resolve().parents[3]
                          / "example" / "de25_agilex5_ddr_validator" / "sw" / "host"))
    from de25_nal import records_from_annexb  # noqa: E402
    recs = records_from_annexb(bytes(stream))
    assert [r["value"] for r in recs] == EXPECTED_NAL_TYPES, [r["value"] for r in recs]


@cocotb.test()
async def interleaved_no_crosstalk(dut):
    """Alternating reads to CSR (m0) and NAL ROM (m1) must each return THEIR own
    value — no response cross-talk, no handshake wedge across the demux."""
    _env, master = await _setup(dut)
    for _ in range(6):
        assert (await master.read(CSR_IDENT, beats=1))[0][0] == CSR_IDENT_MAGIC
        assert (await master.read(NAL_IDENT, beats=1))[0][0] == NAL_IDENT_MAGIC
        assert (await master.read(NAL_ROM, beats=1))[0][0] == EXPECTED_WORDS[0]
    # A write to the CSR while reads target the NAL ROM — write must route to m0.
    await master.write(CSR_N_WORDS, [0x0000_1234])
    assert (await master.read(NAL_IDENT, beats=1))[0][0] == NAL_IDENT_MAGIC
    assert (await master.read(CSR_N_WORDS, beats=1))[0][0] == 0x0000_1234


def main() -> None:
    """`rr sim run test_de25_lwh2f_nal` entry point."""
    if run_simulation is None:
        raise RuntimeError("run via `rr sim run test_de25_lwh2f_nal`")
    ip_root = _Path(__file__).resolve().parents[3]
    src = ip_root / "example" / "de25_agilex5_ddr_validator" / "src"
    run_simulation(
        top_level="tb_de25_lwh2f_nal",
        module="test_de25_lwh2f_nal",
        custom_libraries={
            "rr": [
                str(src / "lwh2f_axilite_bridge.vhd"),
                str(src / "de25_axil_demux2.vhd"),
                str(src / "ddr_validator_csr.vhd"),
                str(src / "de25_nal_rom.vhd"),
                str(src / "tb_de25_lwh2f_nal.vhd"),
            ],
        },
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

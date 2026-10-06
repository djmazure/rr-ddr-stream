"""Phase 1b — the synthetic fabric NAL generator (de25_nal_rom) over the lwh2f glue.

Reads the fabric NAL ROM the way the HPS will (full-AXI4 master → lwh2f bridge →
de25_nal_rom), reconstructs the Annex-B byte stream from the ROM words, and
cross-checks that it demuxes to SPS(7) + PPS(8) + IDR(5) using the SAME host
de25_nal demuxer the bridge uses. This proves the fabric serves a correct NAL
stream end-to-end — the data RouteWave's Protocol Analyzer will show — with no
real bitstream or h264-dec dependency.

Expected values are hard-coded (ROUTERTL-002): the ROM contents are a fixed
synthetic stream, not derived from the DUT at runtime.
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

# Register / ROM map — must match de25_nal_rom.vhd.
REG_IDENT = 0x000
REG_NAL_LEN = 0x004
REG_NAL_WORDS = 0x008
ROM_BASE = 0x100
IDENT_MAGIC = 0x0DD50A10

# The synthetic stream the ROM holds (big-endian per word) and its NAL breakdown.
EXPECTED_WORDS = [
    0x00000001, 0x67420028, 0xF4162720,   # start code + SPS
    0x00000001, 0x68CE3880,               # start code + PPS
    0x00000001, 0x65888402,               # start code + IDR stub
]
EXPECTED_BYTES = bytes.fromhex(
    "00000001" "67420028" "F4162720"
    "00000001" "68CE3880"
    "00000001" "65888402"
)
EXPECTED_NAL_TYPES = [7, 8, 5]  # SPS, PPS, IDR


async def _setup(dut):
    env = TbEnv(dut, clk="clk_i", rst="rst_i", period_ns=10)
    await env.start_clock()
    await env.reset(cycles=5, active_low=False)  # active-high rst_i
    master = Axi4Master(env, prefix="s_axi", raise_on_resp=False)
    await ClockCycles(dut.clk_i, 2)
    return env, master


@cocotb.test()
async def ident_len_words(dut):
    """IDENT magic + length/word-count headers read back over the lwh2f glue."""
    _env, master = await _setup(dut)
    data, resp = await master.read(REG_IDENT, beats=1)
    assert resp == AxiResp.OKAY, f"IDENT resp={resp!r}"
    assert data[0] == IDENT_MAGIC, f"IDENT=0x{data[0]:08X} != 0x{IDENT_MAGIC:08X}"
    data, _ = await master.read(REG_NAL_LEN, beats=1)
    assert data[0] == len(EXPECTED_BYTES), f"NAL_LEN={data[0]} != {len(EXPECTED_BYTES)}"
    data, _ = await master.read(REG_NAL_WORDS, beats=1)
    assert data[0] == len(EXPECTED_WORDS), f"NAL_WORDS={data[0]} != {len(EXPECTED_WORDS)}"


@cocotb.test()
async def rom_words_match(dut):
    """Each ROM word reads back its exact synthetic-stream value."""
    _env, master = await _setup(dut)
    for i, want in enumerate(EXPECTED_WORDS):
        data, resp = await master.read(ROM_BASE + 4 * i, beats=1)
        assert resp == AxiResp.OKAY, f"word{i} resp={resp!r}"
        assert data[0] == want, f"word{i}=0x{data[0]:08X} != 0x{want:08X}"


@cocotb.test()
async def reconstructed_stream_demuxes_to_sps_pps_idr(dut):
    """Reconstruct the Annex-B bytes from the ROM (big-endian per the CONTRACT)
    and demux them with the host de25_nal — must be SPS(7)+PPS(8)+IDR(5)."""
    _env, master = await _setup(dut)
    nwords = (await master.read(REG_NAL_WORDS, beats=1))[0][0]
    nlen = (await master.read(REG_NAL_LEN, beats=1))[0][0]
    stream = bytearray()
    for i in range(nwords):
        w = (await master.read(ROM_BASE + 4 * i, beats=1))[0][0]
        stream += bytes([(w >> 24) & 0xFF, (w >> 16) & 0xFF, (w >> 8) & 0xFF, w & 0xFF])
    stream = bytes(stream[:nlen])
    assert stream == EXPECTED_BYTES, f"reconstructed {stream.hex()} != {EXPECTED_BYTES.hex()}"

    # Cross-check with the SAME demuxer the host bridge uses.
    sys.path.insert(0, str(_Path(__file__).resolve().parents[3]
                          / "example" / "de25_agilex5_ddr_validator" / "sw" / "host"))
    from de25_nal import records_from_annexb  # noqa: E402
    recs = records_from_annexb(stream)
    assert [r["value"] for r in recs] == EXPECTED_NAL_TYPES, \
        f"NAL types {[r['value'] for r in recs]} != {EXPECTED_NAL_TYPES}"
    assert "SPS" in recs[0]["text"] and "PPS" in recs[1]["text"] and "IDR" in recs[2]["text"]


@cocotb.test()
async def read_only_and_unmapped(dut):
    """Writes are OKAY-acked but the ROM is immutable; unmapped offsets read 0;
    sustained back-to-back reads don't wedge the handshake."""
    _env, master = await _setup(dut)
    # A write must complete (OKAY) and NOT change the ROM.
    resp = await master.write(ROM_BASE, [0xDEADBEEF])
    assert resp == AxiResp.OKAY, f"write resp={resp!r}"
    data, _ = await master.read(ROM_BASE, beats=1)
    assert data[0] == EXPECTED_WORDS[0], "ROM must be immutable (read-only)"
    # Unmapped offset → 0.
    data, _ = await master.read(0x080, beats=1)
    assert data[0] == 0x00000000, f"unmapped read=0x{data[0]:08X} (expected 0)"
    # Back-to-back: IDENT then every ROM word, no deadlock.
    for _ in range(2):
        assert (await master.read(REG_IDENT, beats=1))[0][0] == IDENT_MAGIC
        for i in range(len(EXPECTED_WORDS)):
            assert (await master.read(ROM_BASE + 4 * i, beats=1))[0][0] == EXPECTED_WORDS[i]


def main() -> None:
    """`rr sim run test_de25_nal_rom` entry point."""
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke via `rr sim run test_de25_nal_rom`."
        )
    ip_root = _Path(__file__).resolve().parents[3]  # rr-ddr-stream/
    src = ip_root / "example" / "de25_agilex5_ddr_validator" / "src"
    run_simulation(
        top_level="tb_de25_nal_rom",
        module="test_de25_nal_rom",
        custom_libraries={
            "rr": [
                str(src / "lwh2f_axilite_bridge.vhd"),
                str(src / "de25_nal_rom.vhd"),
                str(src / "tb_de25_nal_rom.vhd"),
            ],
        },
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

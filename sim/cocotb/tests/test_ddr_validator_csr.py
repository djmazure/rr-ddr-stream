"""Unit-level cocotb tests for ddr_validator_csr — standalone, no contract attribution.

The CSR is the example/zybo_z7_20_ddr_validator AXI4-Lite control/status
register file. It is NOT part of any IP boundary contract (it lives under
example/), so these tests carry no @requires() decorators — they are
development instrumentation, in the same spirit as test_ptr_mgr.py.

But the CSR is *not* trivial, and these tests attack the parts of an AXI-Lite
slave that actually bite (feedback_adversarial_testing,
feedback_test_success_is_bugs_found):

  - W1P (write-1-to-pulse) self-clear: start / soft_reset / base_load /
    ptr_reset must assert for EXACTLY one cycle and not stick. A stuck pulse
    would re-trigger the BIST every cycle.
  - AW+W co-acceptance handshake: back-to-back writes must both commit (no
    deadlock when awready/wready self-clear).
  - Byte-strobe writes: partial WSTRB on the 32-bit RW registers must mask
    per byte lane.
  - Read mux partial-override (`r <= (others=>'0')` then per-bit): STATUS /
    CTRL bit packing must be exactly as specified.
  - Read-only registers: writes to STATUS must be OKAY-acked no-ops.

Hard-coded expected values throughout (ROUTERTL-002) — never values
re-derived from the same runtime inputs.

The CSR is a flat-port entity (std_logic / std_logic_vector only — no
record ports), so cocotb attaches directly to the entity; no flat-AXI
wrapper, and the DUT depends only on `ieee` (no olo / no IP), so it builds
and runs even while the full-IP sim is blocked on the upstream olo registry.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from pathlib import Path as _Path

try:
    from engine.simulation import run_simulation  # type: ignore  # noqa: E402
except ImportError:
    run_simulation = None


_IP_ROOT = _Path(__file__).resolve().parents[3]   # rr-ddr-stream/
_CSR_VHD = _IP_ROOT / "example" / "zybo_z7_20_ddr_validator" / "src" / "ddr_validator_csr.vhd"


def main() -> None:
    """`rr sim run test_ddr_validator_csr` entry point.

    Compiles only ddr_validator_csr.vhd — it is package-free (ieee only).
    """
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke this test via `rr sim run test_ddr_validator_csr`."
        )

    run_simulation(
        top_level="ddr_validator_csr",
        module="test_ddr_validator_csr",
        custom_libraries={
            "rr": [
                str(_CSR_VHD),
            ],
        },
        generics={"addr_width_g": 12},
        waves=True,
        simulator="nvc",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Register offsets (docs/DESIGN.md §4) — hard-coded, must match the RTL.
# ──────────────────────────────────────────────────────────────────────────────
CTRL            = 0x00
STATUS          = 0x04
SCENARIO        = 0x08
N_WORDS         = 0x0C
ERROR_COUNT     = 0x10
WORDS_WRITTEN   = 0x14
WORDS_READ      = 0x18
FIRST_ERR_INDEX = 0x1C
FIRST_ERR_EXP   = 0x20
FIRST_ERR_GOT   = 0x24
WR_BASE         = 0x28
RD_BASE         = 0x2C
BASE_LOAD       = 0x30
PTR_RESET       = 0x34
IDENT           = 0x3C

IDENT_MAGIC = 0x0DD50200
N_WORDS_DEFAULT = 8192


# ──────────────────────────────────────────────────────────────────────────────
# AXI4-Lite master BFM. Holds the natural perpetual-ready stance on the
# response channels (feedback_bfm_no_dut_workarounds) — never pulses bready /
# rready low to coax the DUT.
# ──────────────────────────────────────────────────────────────────────────────
async def _bring_up(dut, period_ns: float = 10.0):
    cocotb.start_soon(Clock(dut.clk_i, period_ns, unit="ns").start())
    dut.rst_i.value           = 1
    dut.s_axi_awaddr_i.value  = 0
    dut.s_axi_awprot_i.value  = 0
    dut.s_axi_awvalid_i.value = 0
    dut.s_axi_wdata_i.value   = 0
    dut.s_axi_wstrb_i.value   = 0
    dut.s_axi_wvalid_i.value  = 0
    dut.s_axi_bready_i.value  = 0
    dut.s_axi_araddr_i.value  = 0
    dut.s_axi_arprot_i.value  = 0
    dut.s_axi_arvalid_i.value = 0
    dut.s_axi_rready_i.value  = 0
    # Status inputs idle.
    for sig in ("running_i", "done_i", "pass_i", "fail_i",
                "write_idle_i", "read_idle_i"):
        getattr(dut, sig).value = 0
    for sig in ("error_count_i", "words_written_i", "words_read_i",
                "first_err_index_i", "first_err_exp_i", "first_err_got_i"):
        getattr(dut, sig).value = 0
    for _ in range(5):
        await RisingEdge(dut.clk_i)
    dut.rst_i.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk_i)


async def axil_write(dut, addr: int, data: int, strb: int = 0xF):
    """Single AXI-Lite write. AW and W presented together (the DUT requires
    AWVALID and WVALID coincident to accept)."""
    dut.s_axi_awaddr_i.value  = addr & 0xFFF
    dut.s_axi_awvalid_i.value = 1
    dut.s_axi_wdata_i.value   = data & 0xFFFFFFFF
    dut.s_axi_wstrb_i.value   = strb & 0xF
    dut.s_axi_wvalid_i.value  = 1
    dut.s_axi_bready_i.value  = 1
    # Catch the 1-cycle awready+wready window.
    while True:
        await RisingEdge(dut.clk_i)
        if int(dut.s_axi_awready_o.value) == 1 and int(dut.s_axi_wready_o.value) == 1:
            break
    dut.s_axi_awvalid_i.value = 0
    dut.s_axi_wvalid_i.value  = 0
    # Await the write response.
    while int(dut.s_axi_bvalid_o.value) == 0:
        await RisingEdge(dut.clk_i)
    assert int(dut.s_axi_bresp_o.value) == 0, (
        f"write @0x{addr:02x}: BRESP={int(dut.s_axi_bresp_o.value)} (expected OKAY=0)"
    )
    await RisingEdge(dut.clk_i)
    dut.s_axi_bready_i.value = 0


async def axil_read(dut, addr: int) -> int:
    """Single AXI-Lite read; returns RDATA, asserts OKAY response."""
    dut.s_axi_araddr_i.value  = addr & 0xFFF
    dut.s_axi_arvalid_i.value = 1
    dut.s_axi_rready_i.value  = 1
    while True:
        await RisingEdge(dut.clk_i)
        if int(dut.s_axi_arready_o.value) == 1:
            break
    dut.s_axi_arvalid_i.value = 0
    while int(dut.s_axi_rvalid_o.value) == 0:
        await RisingEdge(dut.clk_i)
    data = int(dut.s_axi_rdata_o.value)
    assert int(dut.s_axi_rresp_o.value) == 0, (
        f"read @0x{addr:02x}: RRESP={int(dut.s_axi_rresp_o.value)} (expected OKAY=0)"
    )
    await RisingEdge(dut.clk_i)
    dut.s_axi_rready_i.value = 0
    return data


async def _count_pulse_during_write(dut, pulse_name: str, addr: int, data: int) -> int:
    """Run an AXI-Lite write while counting the cycles `pulse_name` is high.
    Returns the number of high cycles — must be exactly 1 for a W1P bit."""
    counts = [0]
    stop = [False]
    sig = getattr(dut, pulse_name)

    async def _mon():
        while not stop[0]:
            await RisingEdge(dut.clk_i)
            if int(sig.value) == 1:
                counts[0] += 1

    m = cocotb.start_soon(_mon())
    await axil_write(dut, addr, data)
    for _ in range(4):          # trailing window to catch a late / stuck pulse
        await RisingEdge(dut.clk_i)
    stop[0] = True
    await RisingEdge(dut.clk_i)
    m.kill()
    return counts[0]


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────
@cocotb.test()
async def ident_readback(dut):
    """IDENT reads the hard magic 0x0DD50200 — host sanity probe."""
    await _bring_up(dut)
    val = await axil_read(dut, IDENT)
    assert val == IDENT_MAGIC, f"IDENT=0x{val:08x}, expected 0x{IDENT_MAGIC:08x}"


@cocotb.test()
async def defaults_after_reset(dut):
    """N_WORDS resets to 8192; SCENARIO/CTRL/bases reset to 0."""
    await _bring_up(dut)
    assert await axil_read(dut, N_WORDS) == N_WORDS_DEFAULT
    assert await axil_read(dut, SCENARIO) == 0
    assert await axil_read(dut, CTRL) == 0
    assert await axil_read(dut, WR_BASE) == 0
    assert await axil_read(dut, RD_BASE) == 0


@cocotb.test()
async def scenario_write_readback(dut):
    """SCENARIO[3:0] is RW; upper bits read back 0."""
    await _bring_up(dut)
    await axil_write(dut, SCENARIO, 0xFFFFFFF2)   # only low nibble should latch
    assert await axil_read(dut, SCENARIO) == 0x2


@cocotb.test()
async def n_words_full_and_byte_strobed(dut):
    """N_WORDS is a full 32-bit RW register honouring per-byte WSTRB."""
    await _bring_up(dut)
    await axil_write(dut, N_WORDS, 0x12345678)
    assert await axil_read(dut, N_WORDS) == 0x12345678
    # Strobe only byte lane 0 — bytes 1..3 must keep their old value.
    await axil_write(dut, N_WORDS, 0xAABBCCDD, strb=0x1)
    assert await axil_read(dut, N_WORDS) == 0x123456DD, "byte-strobe lane masking wrong"


@cocotb.test()
async def wr_rd_base_readback(dut):
    """WR_BASE / RD_BASE are independent 32-bit RW registers."""
    await _bring_up(dut)
    await axil_write(dut, WR_BASE, 0x10000000)
    await axil_write(dut, RD_BASE, 0x20000000)
    assert await axil_read(dut, WR_BASE) == 0x10000000
    assert await axil_read(dut, RD_BASE) == 0x20000000


@cocotb.test()
async def continuous_bit_is_rw(dut):
    """CTRL[8] (continuous) is a sticky RW bit; CTRL[0]/[1] read back as 0 (W1P)."""
    await _bring_up(dut)
    await axil_write(dut, CTRL, (1 << 8) | 0x1, strb=0x3)   # set continuous + start
    val = await axil_read(dut, CTRL)
    assert val == (1 << 8), f"CTRL readback=0x{val:08x}; continuous sticky, start/W1P read 0"


@cocotb.test()
async def start_is_one_cycle_pulse(dut):
    """CTRL.start (W1P) asserts start_o for EXACTLY one cycle, then self-clears."""
    await _bring_up(dut)
    highs = await _count_pulse_during_write(dut, "start_o", CTRL, 0x1)
    assert highs == 1, f"start_o high for {highs} cycles (W1P must be exactly 1)"


@cocotb.test()
async def soft_reset_is_one_cycle_pulse(dut):
    """CTRL.soft_reset (W1P) → soft_reset_o exactly one cycle."""
    await _bring_up(dut)
    highs = await _count_pulse_during_write(dut, "soft_reset_o", CTRL, 0x2)
    assert highs == 1, f"soft_reset_o high for {highs} cycles (W1P must be exactly 1)"


@cocotb.test()
async def base_load_pulses(dut):
    """BASE_LOAD[0]/[1] → wr/rd_base_load_o, each exactly one cycle, independent."""
    await _bring_up(dut)
    assert await _count_pulse_during_write(dut, "wr_base_load_o", BASE_LOAD, 0x1) == 1
    assert await _count_pulse_during_write(dut, "rd_base_load_o", BASE_LOAD, 0x2) == 1
    # bit0 write must not pulse the rd load.
    assert await _count_pulse_during_write(dut, "rd_base_load_o", BASE_LOAD, 0x1) == 0


@cocotb.test()
async def ptr_reset_pulses(dut):
    """PTR_RESET[0]/[1] → wr/rd_ptr_reset_o, each exactly one cycle (DDR-P3.11 hook)."""
    await _bring_up(dut)
    assert await _count_pulse_during_write(dut, "wr_ptr_reset_o", PTR_RESET, 0x1) == 1
    assert await _count_pulse_during_write(dut, "rd_ptr_reset_o", PTR_RESET, 0x2) == 1


@cocotb.test()
async def status_reflects_inputs(dut):
    """STATUS packs running/done/pass/fail/write_idle/read_idle at bits 0..5."""
    await _bring_up(dut)
    # Drive a known pattern: running=1, pass=1, read_idle=1 → bits 0,2,5.
    dut.running_i.value   = 1
    dut.done_i.value      = 0
    dut.pass_i.value      = 1
    dut.fail_i.value      = 0
    dut.write_idle_i.value = 0
    dut.read_idle_i.value  = 1
    await RisingEdge(dut.clk_i)
    val = await axil_read(dut, STATUS)
    assert val == (1 << 0) | (1 << 2) | (1 << 5), f"STATUS=0x{val:08x} mispacked"

    # Flip to done+fail+write_idle → bits 1,3,4.
    dut.running_i.value   = 0
    dut.pass_i.value      = 0
    dut.done_i.value      = 1
    dut.fail_i.value      = 1
    dut.write_idle_i.value = 1
    dut.read_idle_i.value  = 0
    await RisingEdge(dut.clk_i)
    val = await axil_read(dut, STATUS)
    assert val == (1 << 1) | (1 << 3) | (1 << 4), f"STATUS=0x{val:08x} mispacked (2nd)"


@cocotb.test()
async def status_counters_readback(dut):
    """ERROR_COUNT / WORDS_* / FIRST_ERR_* mirror their live input ports."""
    await _bring_up(dut)
    dut.error_count_i.value     = 0x00000007
    dut.words_written_i.value   = 0x00002000
    dut.words_read_i.value      = 0x00001FFF
    dut.first_err_index_i.value = 0x00000123
    dut.first_err_exp_i.value   = 0xDEADBEEF
    dut.first_err_got_i.value   = 0xCAFEF00D
    await RisingEdge(dut.clk_i)
    assert await axil_read(dut, ERROR_COUNT)     == 0x00000007
    assert await axil_read(dut, WORDS_WRITTEN)   == 0x00002000
    assert await axil_read(dut, WORDS_READ)      == 0x00001FFF
    assert await axil_read(dut, FIRST_ERR_INDEX) == 0x00000123
    assert await axil_read(dut, FIRST_ERR_EXP)   == 0xDEADBEEF
    assert await axil_read(dut, FIRST_ERR_GOT)   == 0xCAFEF00D


@cocotb.test()
async def write_to_readonly_is_acked_noop(dut):
    """A write to STATUS (read-only) is OKAY-acked and changes nothing."""
    await _bring_up(dut)
    dut.running_i.value = 1
    await RisingEdge(dut.clk_i)
    await axil_write(dut, STATUS, 0xFFFFFFFF)   # axil_write asserts BRESP==OKAY
    val = await axil_read(dut, STATUS)
    assert val == (1 << 0), f"STATUS should still mirror inputs only; got 0x{val:08x}"


@cocotb.test()
async def unmapped_read_returns_zero(dut):
    """A read of a reserved offset (0x38, between PTR_RESET and IDENT) returns
    0 — exercises the read-mux `when others` arm."""
    await _bring_up(dut)
    # Drive nonzero status so a stray decode would leak a real value.
    dut.error_count_i.value = 0xFFFFFFFF
    await RisingEdge(dut.clk_i)
    assert await axil_read(dut, 0x38) == 0, "reserved offset 0x38 must read 0"


@cocotb.test()
async def wstrb_zero_write_is_noop(dut):
    """A write with WSTRB=0 must not change a register (and still OKAY-acks)."""
    await _bring_up(dut)
    await axil_write(dut, N_WORDS, 0xA5A5A5A5)
    await axil_write(dut, N_WORDS, 0x5A5A5A5A, strb=0x0)   # no lanes enabled
    assert await axil_read(dut, N_WORDS) == 0xA5A5A5A5, "WSTRB=0 must be a no-op"


@cocotb.test()
async def continuous_bit_clears(dut):
    """CTRL[8] continuous is settable AND clearable (not a sticky-set bit)."""
    await _bring_up(dut)
    await axil_write(dut, CTRL, (1 << 8), strb=0x3)
    assert await axil_read(dut, CTRL) == (1 << 8)
    await axil_write(dut, CTRL, 0x0, strb=0x3)
    assert await axil_read(dut, CTRL) == 0, "continuous bit failed to clear"


@cocotb.test()
async def back_to_back_writes_both_commit(dut):
    """Two writes in succession both land — the AW/W self-clearing handshake
    must not deadlock or drop the second transaction."""
    await _bring_up(dut)
    await axil_write(dut, N_WORDS, 0x11112222)
    await axil_write(dut, WR_BASE, 0x33334444)
    assert await axil_read(dut, N_WORDS) == 0x11112222
    assert await axil_read(dut, WR_BASE) == 0x33334444


if __name__ == "__main__":
    main()

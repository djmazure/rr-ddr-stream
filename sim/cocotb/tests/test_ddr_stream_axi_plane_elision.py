"""
DDR-P1.1 plane-elision attack tests.

Validates the contract added to ddr_stream_axi by the new generics
`enable_write_plane_g` and `enable_read_plane_g`:

  - When enable_read_plane_g=false:  m_axis_valid_o == '0' and read_idle_o == '1'
    forever, and no ARVALID transaction ever appears on the AXI bus.
  - When enable_write_plane_g=false: s_axis_ready_o == '0' and write_idle_o == '1'
    forever, and no AWVALID/WVALID transaction ever appears on the AXI bus.

Run via:
    rr sim run test_ddr_stream_axi_plane_elision

main() drives two `run_simulation` invocations against the same wrapper
(`wrapper_ddr_stream_axi`) — once with G_ENABLE_READ_PLANE=false, once
with G_ENABLE_WRITE_PLANE=false. Each invocation runs all four tests
below; the ones that don't match the active generic self-skip via an
early return after reading `dut.G_ENABLE_*_PLANE`.
"""
from __future__ import annotations

import cocotb
import logging
from cocotb.triggers import RisingEdge, ClockCycles
from cocotb.clock import Clock
from pathlib import Path as _Path

log_file_path = _Path(__file__).parent / "ddr_stream_axi_plane_elision_test.log"
logging.basicConfig(
    filename=log_file_path,
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

from cocotbext.axi import AxiBus, AxiRam

try:
    from engine.simulation import run_simulation  # type: ignore
except ImportError:
    run_simulation = None


_IP_ROOT     = _Path(__file__).resolve().parents[3]
_RTL_DIR     = _IP_ROOT / "rtl"
_SUPPORT_DIR = _RTL_DIR / "support"
_TB_DIR      = _IP_ROOT / "sim" / "tb"
_OLO_SYNC_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_sync"  / "src" / "base" / "vhdl"
_OLO_ASYNC_DIR = _IP_ROOT / "libs" / "open-logic" / "olo_base_fifo_async" / "src" / "base" / "vhdl"
_OLO_WCN_DIR   = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_n2xn" / "src" / "base" / "vhdl"
_OLO_WCXN_DIR  = _IP_ROOT / "libs" / "open-logic" / "olo_base_wconv_xn2n" / "src" / "base" / "vhdl"

_BASE_GENERICS = {
    "G_AXI_ID":          0,
    "G_FIFO_DEPTH":      9,
    "G_USER_DATA_WIDTH": 32,
    "G_DDR_DATA_WIDTH":  128,
    "G_DDR_ADDR_WIDTH":  32,
    "G_BUFFER_WIDTH":    20,
    "G_FIFO_IMPL":       "olo",
}

CLK_SYS_NS = 8   # 125 MHz
CLK_DDR_NS = 4   # 250 MHz


def _bool_generic(handle) -> bool:
    """Read a VHDL boolean generic exposed by NVC. Boolean generics
    surface as 0/1 ints — tolerate both that and a 'true'/'false' string."""
    try:
        return int(handle.value) == 1
    except (ValueError, TypeError):
        return str(handle.value).lower() == "true"


async def _bootstrap(dut):
    """Start clocks, hold reset, drive defaults, then wait past the
    DUT's internal C_RESET_INIT_TIMEOUT=64 ddr-cycle warm-up."""
    cocotb.start_soon(Clock(dut.CLK_SYS, CLK_SYS_NS, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_DDR, CLK_DDR_NS, units="ns").start())
    dut.RST_SYS.value          = 1
    dut.RST_DDR.value          = 1
    dut.S_AXIS_DATA.value      = 0
    dut.S_AXIS_VALID.value     = 0
    dut.M_AXIS_READY.value     = 0
    dut.CSR_WR_BASE_ADDR.value = 0
    dut.CSR_WR_BASE_LOAD.value = 0
    dut.CSR_RD_BASE_ADDR.value = 0
    dut.CSR_RD_BASE_LOAD.value = 0
    await ClockCycles(dut.CLK_DDR, 50)
    dut.RST_SYS.value = 0
    dut.RST_DDR.value = 0
    await ClockCycles(dut.CLK_DDR, 80)


def _attach_ram(dut):
    """Attach a permissive AxiRam slave so the live plane can drain into
    real backing memory rather than stalling on no-response. The elided
    plane's channels are tied off in the DUT so the ram never sees them."""
    return AxiRam(
        AxiBus.from_prefix(dut, "M_AXI"),
        dut.CLK_DDR,
        dut.RST_DDR,
        size=2 ** 16,
    )


# ---------------------------------------------------------------------------
# Read-plane-off attack tests
# ---------------------------------------------------------------------------

@cocotb.test()
async def read_plane_off_no_ar_test(dut):
    """With read plane elided, ARVALID must never assert — regardless of
    how active the write path is."""
    if _bool_generic(dut.G_ENABLE_READ_PLANE):
        cocotb.log.info("Skipping: G_ENABLE_READ_PLANE=true")
        return
    await _bootstrap(dut)
    _attach_ram(dut)

    # Keep the write path saturated so the IP is doing real work.
    dut.S_AXIS_VALID.value = 1
    dut.S_AXIS_DATA.value  = 0xDEAD_BEEF

    ar_seen: list[int] = []

    async def watch_ar():
        while True:
            await RisingEdge(dut.CLK_DDR)
            if int(dut.M_AXI_ARVALID.value) == 1:
                ar_seen.append(int(cocotb.utils.get_sim_time(units="ns")))

    cocotb.start_soon(watch_ar())
    await ClockCycles(dut.CLK_DDR, 2000)

    assert not ar_seen, (
        f"ARVALID observed {len(ar_seen)} times with read plane disabled "
        f"(first at {ar_seen[0]}ns)"
    )


@cocotb.test()
async def read_plane_off_m_axis_silent_test(dut):
    """With read plane elided, m_axis_valid must hold low and read_idle
    must hold high for the entire simulation, regardless of m_axis_ready
    or write-path activity."""
    if _bool_generic(dut.G_ENABLE_READ_PLANE):
        cocotb.log.info("Skipping: G_ENABLE_READ_PLANE=true")
        return
    await _bootstrap(dut)
    _attach_ram(dut)

    dut.S_AXIS_VALID.value = 1
    dut.S_AXIS_DATA.value  = 0x1234_5678
    dut.M_AXIS_READY.value = 1   # try to provoke any latent egress path

    for cycle in range(1000):
        await RisingEdge(dut.CLK_SYS)
        if int(dut.M_AXIS_VALID.value) != 0:
            raise AssertionError(f"M_AXIS_VALID asserted at sys-cycle {cycle}")
        if int(dut.READ_IDLE.value) != 1:
            raise AssertionError(f"READ_IDLE dropped at sys-cycle {cycle}")


# ---------------------------------------------------------------------------
# Write-plane-off attack tests
# ---------------------------------------------------------------------------

@cocotb.test()
async def write_plane_off_s_axis_blocked_test(dut):
    """With write plane elided, s_axis_ready must hold low and write_idle
    must hold high for the entire simulation, regardless of how the host
    drives s_axis_valid."""
    if _bool_generic(dut.G_ENABLE_WRITE_PLANE):
        cocotb.log.info("Skipping: G_ENABLE_WRITE_PLANE=true")
        return
    await _bootstrap(dut)
    _attach_ram(dut)

    for cycle in range(1000):
        dut.S_AXIS_VALID.value = 1 if (cycle % 2) else 0
        dut.S_AXIS_DATA.value  = cycle & 0xFFFF_FFFF
        await RisingEdge(dut.CLK_SYS)
        if int(dut.S_AXIS_READY.value) != 0:
            raise AssertionError(f"S_AXIS_READY asserted at sys-cycle {cycle}")
        if int(dut.WRITE_IDLE.value) != 1:
            raise AssertionError(f"WRITE_IDLE dropped at sys-cycle {cycle}")


@cocotb.test()
async def sw_write_ptr_advances_commit_addr(dut):
    """DDR-P2.19 regression guard. With the write plane elided there is no HW
    uploader, so wr_commit_addr_o must track the SOFTWARE-managed ring write
    pointer (csr_wr_ptr_*) — that is the only signal a PS producer has to tell
    a consumer how far it has committed. The pre-fix RTL drove
    wr_commit_addr_o straight from uploader_addr, which the else-generate ties
    to 0 when the write plane is elided, so the commit address was frozen at 0
    forever and the software pointer never surfaced. The fix drives it from
    wr_ptr_sel (the DDR-P3.16 ring-pointer mux), which carries the CDC'd SW
    pointer in this configuration.

    RED-first: against the pre-fix RTL WR_COMMIT_ADDR stays 0 after the load,
    so the equality assertion below fails with 'commit addr never advanced'.
    """
    if _bool_generic(dut.G_ENABLE_WRITE_PLANE):
        cocotb.log.info("Skipping: G_ENABLE_WRITE_PLANE=true")
        return
    await _bootstrap(dut)
    _attach_ram(dut)

    # Sanity: after reset, with no SW pointer loaded, the commit address is 0.
    assert int(dut.WR_COMMIT_ADDR.value) == 0, (
        f"WR_COMMIT_ADDR should be 0 at reset, got "
        f"{int(dut.WR_COMMIT_ADDR.value):#x}"
    )

    target = 0x0000_0400
    # Present the SW write pointer and pulse the load strobe for one clk_sys
    # cycle (the p_wr_ptr_sys process captures on the rising edge while load=1).
    dut.CSR_WR_PTR_ADDR.value = target
    dut.CSR_WR_PTR_LOAD.value = 1
    await RisingEdge(dut.CLK_SYS)
    dut.CSR_WR_PTR_LOAD.value = 0

    # Wait out the clk_sys->clk_ddr toggle MCP crossing (2 sync stages + edge
    # detect + capture), then let it settle.
    await ClockCycles(dut.CLK_DDR, 40)

    got = int(dut.WR_COMMIT_ADDR.value)
    assert got == target, (
        f"commit addr never advanced: WR_COMMIT_ADDR={got:#x}, "
        f"expected the loaded software write pointer {target:#x} "
        f"(write plane elided => commit addr must follow csr_wr_ptr, not "
        f"the tied-off uploader_addr)"
    )

    # A second, distinct load must also propagate (guards a stuck-after-first
    # capture, and that the mux is live not one-shot).
    target2 = 0x0000_0C00
    dut.CSR_WR_PTR_ADDR.value = target2
    dut.CSR_WR_PTR_LOAD.value = 1
    await RisingEdge(dut.CLK_SYS)
    dut.CSR_WR_PTR_LOAD.value = 0
    await ClockCycles(dut.CLK_DDR, 40)

    got2 = int(dut.WR_COMMIT_ADDR.value)
    assert got2 == target2, (
        f"commit addr never advanced on second load: WR_COMMIT_ADDR={got2:#x}, "
        f"expected {target2:#x}"
    )


@cocotb.test()
async def write_plane_off_no_aw_test(dut):
    """With write plane elided, AWVALID and WVALID must never assert —
    regardless of how active the read path is."""
    if _bool_generic(dut.G_ENABLE_WRITE_PLANE):
        cocotb.log.info("Skipping: G_ENABLE_WRITE_PLANE=true")
        return
    await _bootstrap(dut)
    _attach_ram(dut)

    # Provoke read activity via a CSR base-address load + drain m_axis.
    dut.CSR_RD_BASE_ADDR.value = 0x100
    dut.CSR_RD_BASE_LOAD.value = 1
    await ClockCycles(dut.CLK_SYS, 4)
    dut.CSR_RD_BASE_LOAD.value = 0
    dut.M_AXIS_READY.value     = 1

    aw_seen: list[int] = []
    w_seen:  list[int] = []

    async def watch_writes():
        while True:
            await RisingEdge(dut.CLK_DDR)
            if int(dut.M_AXI_AWVALID.value) == 1:
                aw_seen.append(int(cocotb.utils.get_sim_time(units="ns")))
            if int(dut.M_AXI_WVALID.value) == 1:
                w_seen.append(int(cocotb.utils.get_sim_time(units="ns")))

    cocotb.start_soon(watch_writes())
    await ClockCycles(dut.CLK_DDR, 2000)

    if aw_seen:
        raise AssertionError(
            f"AWVALID observed {len(aw_seen)} times with write plane disabled "
            f"(first at {aw_seen[0]}ns)"
        )
    if w_seen:
        raise AssertionError(
            f"WVALID observed {len(w_seen)} times with write plane disabled "
            f"(first at {w_seen[0]}ns)"
        )


# ---------------------------------------------------------------------------
# Engine entry point
# ---------------------------------------------------------------------------

def _custom_libraries() -> dict[str, list[str]]:
    # RR--T2.1: open-logic in its own `olo` library so cross-library
    # `entity olo.olo_base_*` refs in rr_fifo_*.vhd resolve. See
    # test_ddr_stream_axi.py for the full note.
    return {
        "olo": [
            str(_OLO_ASYNC_DIR / "olo_base_pkg_array.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_pkg_math.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_pkg_logic.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_pkg_string.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_pkg_attribute.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_cc_bits.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_cc_reset.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_ram_sdp.vhd"),
            str(_OLO_SYNC_DIR  / "olo_base_fifo_sync.vhd"),
            str(_OLO_ASYNC_DIR / "olo_base_fifo_async.vhd"),
            str(_OLO_WCN_DIR   / "olo_base_wconv_n2xn.vhd"),
            str(_OLO_WCXN_DIR  / "olo_base_wconv_xn2n.vhd"),
        ],
        "rr": [
            str(_RTL_DIR / "ddr_stream_pkg.vhd"),
            str(_SUPPORT_DIR / "functions_pkg.vhd"),
            str(_SUPPORT_DIR / "ddr_sync_cdc.vhd"),
            str(_SUPPORT_DIR / "skid_buffer.vhd"),
            str(_SUPPORT_DIR / "xilinx" / "rr_fifo_sync_xpm_body.vhd"),
            str(_SUPPORT_DIR / "xilinx" / "rr_fifo_async_mixed_xpm_body.vhd"),
            str(_SUPPORT_DIR / "rr_fifo_sync.vhd"),
            str(_SUPPORT_DIR / "rr_fifo_async_mixed.vhd"),
            str(_RTL_DIR / "ddr_fifo_intf.vhd"),
            str(_RTL_DIR / "ptr_mgr.vhd"),
            str(_RTL_DIR / "burstw_setter.vhd"),
            str(_RTL_DIR / "burstr_setter.vhd"),
            str(_RTL_DIR / "axi4w.vhd"),
            str(_RTL_DIR / "axi4r.vhd"),
            str(_RTL_DIR / "downloader.vhd"),
            str(_RTL_DIR / "uploader.vhd"),
            str(_RTL_DIR / "ddr_stream_axi.vhd"),
            str(_TB_DIR / "wrappers" / "wrapper_ddr_stream_axi.vhd"),
        ],
    }


def main() -> None:
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation not available — "
            "invoke this test via `rr sim run test_ddr_stream_axi_plane_elision`."
        )

    custom_libraries = _custom_libraries()

    print("\n" + "=" * 72)
    print("=== test_ddr_stream_axi_plane_elision — write-only build "
          "(G_ENABLE_READ_PLANE=false)")
    print("=" * 72 + "\n")
    run_simulation(
        top_level="wrapper_ddr_stream_axi",
        module="test_ddr_stream_axi_plane_elision",
        custom_libraries=custom_libraries,
        # RR--T2.1: explicit work_lib so elaborate hits `rr` not `olo`.
        work_lib="rr",
        generics={
            **_BASE_GENERICS,
            "G_ENABLE_WRITE_PLANE": True,
            "G_ENABLE_READ_PLANE":  False,
        },
        waves=True,
        simulator="nvc",
    )

    print("\n" + "=" * 72)
    print("=== test_ddr_stream_axi_plane_elision — read-only build "
          "(G_ENABLE_WRITE_PLANE=false)")
    print("=" * 72 + "\n")
    run_simulation(
        top_level="wrapper_ddr_stream_axi",
        module="test_ddr_stream_axi_plane_elision",
        custom_libraries=custom_libraries,
        # RR--T2.1: explicit work_lib so elaborate hits `rr` not `olo`.
        work_lib="rr",
        generics={
            **_BASE_GENERICS,
            "G_ENABLE_WRITE_PLANE": False,
            "G_ENABLE_READ_PLANE":  True,
        },
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()

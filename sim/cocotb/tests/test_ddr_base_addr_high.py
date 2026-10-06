"""DDR-P3.39: elaborate both public tops with base address x"8000_0000".

The caller-visible contract here is elaboration itself.  On 32-bit-INTEGER NVC,
the pre-fix integer generic dies before time zero with "value 2147483648 outside
of INTEGER range".  A passing run proves both flattened integration surfaces
accept and preserve the same upper-half 32-bit value.

Run via: ``rr sim run test_ddr_base_addr_high``.
"""
from __future__ import annotations

from pathlib import Path

import cocotb

try:
    from engine.simulation import run_simulation  # type: ignore
    from sdk.cocotb_helpers import requires  # type: ignore
except ImportError:
    run_simulation = None

    def requires(*_args, **_kwargs):
        def _wrap(fn):
            return fn
        return _wrap


ROOT = Path(__file__).resolve().parents[3]
RTL = ROOT / "rtl"
SUPPORT = RTL / "support"
TB = ROOT / "sim" / "tb" / "wrappers"
OLO_ASYNC = ROOT / "libs/open-logic/olo_base_fifo_async/src/base/vhdl"
OLO_SYNC = ROOT / "libs/open-logic/olo_base_fifo_sync/src/base/vhdl"
OLO_N2XN = ROOT / "libs/open-logic/olo_base_wconv_n2xn/src/base/vhdl"
OLO_XN2N = ROOT / "libs/open-logic/olo_base_wconv_xn2n/src/base/vhdl"

HIGH_BASE = 0x8000_0000
HIGH_BASE_BITS = f"{HIGH_BASE:032b}"


@cocotb.test()
@requires("DDR-REQ-205")
async def upper_half_base_elaborates(dut):
    """Assert the value at the wrapper boundary after successful elaboration."""
    observed = int(dut.G_DDR_BASE_ADDR.value)
    assert observed == HIGH_BASE, (
        f"G_DDR_BASE_ADDR lost the unsigned upper-half value: "
        f"expected 0x{HIGH_BASE:08x}, observed 0x{observed:08x}"
    )


def _common_libraries() -> dict[str, list[str]]:
    return {
        "olo": [
            str(OLO_ASYNC / "olo_base_pkg_array.vhd"),
            str(OLO_ASYNC / "olo_base_pkg_math.vhd"),
            str(OLO_ASYNC / "olo_base_pkg_logic.vhd"),
            str(OLO_ASYNC / "olo_base_pkg_string.vhd"),
            str(OLO_ASYNC / "olo_base_pkg_attribute.vhd"),
            str(OLO_ASYNC / "olo_base_cc_bits.vhd"),
            str(OLO_ASYNC / "olo_base_cc_reset.vhd"),
            str(OLO_ASYNC / "olo_base_ram_sdp.vhd"),
            str(OLO_SYNC / "olo_base_fifo_sync.vhd"),
            str(OLO_ASYNC / "olo_base_fifo_async.vhd"),
            str(OLO_N2XN / "olo_base_wconv_n2xn.vhd"),
            str(OLO_XN2N / "olo_base_wconv_xn2n.vhd"),
        ],
        "rr": [
            str(RTL / "ddr_stream_pkg.vhd"),
            str(SUPPORT / "functions_pkg.vhd"),
            str(SUPPORT / "ddr_sync_cdc.vhd"),
            str(SUPPORT / "skid_buffer.vhd"),
            str(SUPPORT / "xilinx/rr_fifo_sync_xpm_body.vhd"),
            str(SUPPORT / "xilinx/rr_fifo_async_mixed_xpm_body.vhd"),
            str(SUPPORT / "rr_fifo_sync.vhd"),
            str(SUPPORT / "rr_fifo_async_mixed.vhd"),
            str(RTL / "ddr_fifo_intf.vhd"),
            str(RTL / "ptr_mgr.vhd"),
            str(RTL / "burstw_setter.vhd"),
            str(RTL / "burstr_setter.vhd"),
        ],
    }


def _run(top: str, backend_sources: list[Path]) -> None:
    libraries = _common_libraries()
    libraries["rr"].extend(str(path) for path in backend_sources)
    run_simulation(
        top_level=top,
        module="test_ddr_base_addr_high",
        custom_libraries=libraries,
        work_lib="rr",
        # NVC's -g parser accepts STD_LOGIC_VECTOR values as an unquoted bit
        # string.  Decimal text is intentionally not used: it is parsed only
        # for INTEGER generics, which would recreate the defect under test.
        generics={"G_DDR_BASE_ADDR": HIGH_BASE_BITS},
        simulator="nvc",
    )


def main() -> None:
    if run_simulation is None:
        raise RuntimeError(
            "engine.simulation.run_simulation unavailable; run through rr sim"
        )

    _run(
        "wrapper_ddr_stream_axi",
        [
            RTL / "axi4w.vhd",
            RTL / "axi4r.vhd",
            RTL / "uploader.vhd",
            RTL / "downloader.vhd",
            RTL / "ddr_stream_axi.vhd",
            TB / "wrapper_ddr_stream_axi.vhd",
        ],
    )
    _run(
        "wrapper_ddr_stream_avmm",
        [
            RTL / "avmm/avmm_w.vhd",
            RTL / "avmm/avmm_r.vhd",
            RTL / "avmm/uploader_avmm.vhd",
            RTL / "avmm/downloader_avmm.vhd",
            RTL / "ddr_stream_avmm.vhd",
            TB / "wrapper_ddr_stream_avmm.vhd",
        ],
    )


if __name__ == "__main__":
    main()

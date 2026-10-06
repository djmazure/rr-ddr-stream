"""DDR-P2.19 guard: wr_commit_addr_o must follow the ring write-pointer mux.

WHY THIS TEST EXISTS
--------------------
`wr_commit_addr_o` is the ONLY signal a PS consumer has to learn how far the
DMA write side has committed into the DDR ring; it must walk the ring only up to
this address (DS-T2.20). The address has to be correct in EVERY plane
configuration, including the write-plane-elided build (`enable_write_plane_g =
false`) where there is no HW uploader and a software producer advances the ring
write pointer through the `csr_wr_ptr_*` CSR (DDR-P3.16).

The pre-fix top drove the output straight from the architecture-internal
`uploader_addr`:

    wr_commit_addr_o <= uploader_addr;

but the write-plane else-generate ties `uploader_addr` to 0. So on a
write-plane-elided build the commit address was FROZEN at 0 forever and the
software write pointer never surfaced — a consumer would believe nothing had
ever been committed. (When the write plane is present the bug is invisible:
`wr_ptr_sel <= uploader_addr` there, so the two sources are identical.)

The fix drives the output from `wr_ptr_sel`, the same DDR-P3.16 ring
write-pointer mux the read side already consumes: `uploader_addr` when the write
plane is present (byte-identical to the legacy behaviour) and the CDC'd software
pointer `wr_ptr_addr_ddr` when it is elided.

RELATION TO THE BEHAVIOURAL GUARD
---------------------------------
The true behavioural regression guard is the cocotb port sim
`sim/cocotb/tests/test_ddr_stream_axi_plane_elision.py::sw_write_ptr_advances_commit_addr`,
which drives `csr_wr_ptr_*` on a write-plane-elided elaboration and asserts
WR_COMMIT_ADDR actually advances (RED "commit addr never advanced" on the
pre-fix RTL, GREEN after). That sim needs nvc + the open-logic package install +
the sim queue, so it cannot run inside the pytest-only, isolated-checkout
test-power runner. This desk-side twin asserts the SOURCE property that
DETERMINES that behaviour: which architecture-scope signal drives the port. It
is a complete predictor because the concurrent assignment is the only driver of
`wr_commit_addr_o`.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RTL = REPO_ROOT / "rtl" / "ddr_stream_axi.vhd"

# Match the architecture-scope concurrent driver of the output port:
#   wr_commit_addr_o <= <signal>;
# (NOT the port declaration `wr_commit_addr_o : out ...`, which uses ':').
_DRIVER = re.compile(r"^\s*wr_commit_addr_o\s*<=\s*([A-Za-z]\w*)\s*;", re.MULTILINE)


def _driver_rhs() -> str:
    assert RTL.is_file(), f"missing RTL file: {RTL}"
    text = RTL.read_text(encoding="utf-8", errors="replace")
    matches = _DRIVER.findall(text)
    assert len(matches) == 1, (
        f"expected exactly one concurrent driver of wr_commit_addr_o, found "
        f"{len(matches)}: {matches!r}. The parser or the RTL structure changed — "
        f"re-derive this DDR-P2.19 guard against the new top."
    )
    return matches[0]


def test_wr_commit_addr_driven_from_wr_ptr_sel():
    """wr_commit_addr_o must be driven from wr_ptr_sel (the DDR-P3.16 ring
    write-pointer mux), so it carries the software write pointer when the write
    plane is elided instead of the tied-to-0 uploader_addr (DDR-P2.19)."""
    rhs = _driver_rhs()
    assert rhs == "wr_ptr_sel", (
        f"wr_commit_addr_o must be driven from wr_ptr_sel, but it is driven from "
        f"'{rhs}'. Driving it from uploader_addr freezes the commit address at 0 "
        f"on a write-plane-elided build (uploader_addr is tied off there), so the "
        f"software ring write pointer never surfaces to the consumer (DDR-P2.19). "
        f"Use wr_ptr_sel — it equals uploader_addr when the write plane is present "
        f"and the CDC'd SW pointer when it is elided."
    )

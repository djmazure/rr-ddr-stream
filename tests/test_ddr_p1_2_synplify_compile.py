"""DDR-P1.2 — rr_ddr_stream must compile under Synplify (Libero, PolarFire SoC).

DS-P1.83 measured the failure on the DeepSkopion pfsoc_datalogger build
(rr queue job 552f2b7c4210, rr_ddr_stream 0.3.6): the Synplify compiler log
showed

* ``@E: CD590 rr_fifo_async_mixed.vhd:222`` and ``rr_fifo_sync.vhd:143``,
  "Instantiated entity rr_fifo_*_xpm_body has not been analyzed". The
  routertl Libero flow leaves out every ``*/support/xilinx/*`` file on a
  non-Xilinx build, and no stub reached the build either. A direct
  ``entity work.`` instance needs an analyzed entity even inside a generate
  branch that is never taken.
* ``@E: CD214 ddr_stream_axi.vhd:509`` "Not a concurrent statement", on the
  DDR-T2.15 ``postponed assert``. Synplify then stopped ("Too many errors").

These tests run the Synplify compiler itself (``project -run compile``) and
check its compiler log, which is where the consumer build fails.

* The consumer compile uses the same source set that build handed Synplify:
  OpenLogic plus the rr_ddr_stream RTL closure of ``ddr_stream_fic``, with NO
  xpm body file and NO stub. The wrapper test and the whole-compile test read
  this log.
* The ddr_stream_axi test reads a second compile that also has the
  ``rtl/support/intel`` stub bodies. Synplify stops parsing after about ten
  errors, so in the consumer compile the wrapper errors alone can end the run
  before it reaches ``ddr_stream_axi.vhd``. With the stubs present, the
  wrappers cannot fail, and that test judges ``ddr_stream_axi.vhd`` alone.

Each test also requires that Synplify parsed the files it judges. A log in
which the compiler stopped before a file has no errors in that file, and
must not count as a pass.

Needs Synplify Pro from the Libero install and a reachable Microchip license
server. The test skips only when the Synplify binary is not installed; a
license or run failure is a test failure.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SYNPLIFY_CANDIDATES = (
    os.environ.get("RR_SYNPLIFY_PRO", ""),
    "/opt/microchip/libero_soc/Libero_SoC/Synplify_Pro/bin/synplify_pro",
)

OLO = "libs/open-logic"
# Order copied from the DS-P1.83 Synplify project (run_options.txt): OpenLogic
# into library olo, then the rr_ddr_stream files into library rr. No
# rtl/support/xilinx/* body and no rtl/support/intel/* stub, as on that build.
OLO_FILES = (
    f"{OLO}/olo_base_fifo_async/src/base/vhdl/olo_base_pkg_array.vhd",
    f"{OLO}/olo_base_fifo_async/src/base/vhdl/olo_base_pkg_math.vhd",
    f"{OLO}/olo_base_fifo_async/src/base/vhdl/olo_base_pkg_logic.vhd",
    f"{OLO}/olo_base_fifo_async/src/base/vhdl/olo_base_pkg_string.vhd",
    f"{OLO}/olo_base_fifo_async/src/base/vhdl/olo_base_pkg_attribute.vhd",
    f"{OLO}/olo_base_fifo_async/src/base/vhdl/olo_base_cc_bits.vhd",
    f"{OLO}/olo_base_fifo_async/src/base/vhdl/olo_base_cc_reset.vhd",
    f"{OLO}/olo_base_fifo_async/src/base/vhdl/olo_base_ram_sdp.vhd",
    f"{OLO}/olo_base_fifo_async/src/base/vhdl/olo_base_fifo_async.vhd",
    f"{OLO}/olo_base_wconv_n2xn/src/base/vhdl/olo_base_wconv_n2xn.vhd",
    f"{OLO}/olo_base_wconv_xn2n/src/base/vhdl/olo_base_wconv_xn2n.vhd",
    f"{OLO}/olo_base_fifo_sync/src/base/vhdl/olo_base_fifo_sync.vhd",
)
RR_FILES = (
    "rtl/ddr_stream_pkg.vhd",
    "rtl/support/functions_pkg.vhd",
    "rtl/support/ddr_sync_cdc.vhd",
    "rtl/support/rr_fifo_async_mixed.vhd",
    "rtl/support/rr_fifo_sync.vhd",
    # Not in the ddr_stream_fic closure, but it is the third wrapper with the
    # same GEN_XPM shape, so it is compiled too.
    "rtl/support/rr_fifo_async.vhd",
    "rtl/ddr_fifo_intf.vhd",
    "rtl/axi4r.vhd",
    "rtl/ptr_mgr.vhd",
    "rtl/burstr_setter.vhd",
    "rtl/downloader.vhd",
    "rtl/support/skid_buffer.vhd",
    "rtl/axi4w.vhd",
    "rtl/burstw_setter.vhd",
    "rtl/uploader.vhd",
    "rtl/ddr_stream_axi.vhd",
    "rtl/avmm/avmm_r.vhd",
    "rtl/avmm/downloader_avmm.vhd",
    "rtl/avmm/avmm_w.vhd",
    "rtl/avmm/uploader_avmm.vhd",
    "rtl/ddr_stream_avmm.vhd",
    "rtl/ddr_stream_fic.vhd",
)
FIFO_WRAPPERS = (
    "rr_fifo_sync.vhd",
    "rr_fifo_async_mixed.vhd",
    "rr_fifo_async.vhd",
)
# Entity-only bodies for the second compile, analyzed before the wrappers.
STUB_FILES = (
    "rtl/support/intel/rr_fifo_sync_xpm_body_stub.vhd",
    "rtl/support/intel/rr_fifo_async_mixed_xpm_body_stub.vhd",
    "rtl/support/intel/rr_fifo_async_xpm_body_stub.vhd",
)
ERROR_LINE = re.compile(r"^@E:")


def _synplify() -> str | None:
    for cand in SYNPLIFY_CANDIDATES:
        if cand and Path(cand).is_file():
            return cand
    return shutil.which("synplify_pro")


def _license_env() -> dict[str, str]:
    """Point FlexNet at the Microchip server unless the caller already did.

    The bench license file (``~/.config/microchip/License.dat``) names a
    ``SERVER <host> <hostid> <port>`` line; Synplify takes ``port@host``.
    """
    env = os.environ.copy()
    if env.get("SNPSLMD_LICENSE_FILE") or env.get("LM_LICENSE_FILE"):
        return env
    lic = Path.home() / ".config" / "microchip" / "License.dat"
    if lic.is_file():
        for line in lic.read_text(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "SERVER":
                server = f"{parts[3]}@{parts[1]}"
                env["LM_LICENSE_FILE"] = server
                env["SNPSLMD_LICENSE_FILE"] = server
                break
    return env


def _untracked() -> set[str]:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return {line[3:].strip() for line in proc.stdout.splitlines()
            if line.startswith("??")}


def _ensure_olo() -> set[str]:
    """libs/ is gitignored: a fresh (receipt) checkout must sync it first.

    Returns the untracked paths that existed before, so the caller can drop
    only what the sync added.
    """
    before = _untracked()
    if not all((ROOT / f).is_file() for f in OLO_FILES):
        sync = subprocess.run(
            ["rr", "workspace", "sync"],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert sync.returncode == 0, (
            f"rr workspace sync failed (exit {sync.returncode}):\n"
            f"{sync.stdout}\n{sync.stderr}")
    missing = [f for f in OLO_FILES if not (ROOT / f).is_file()]
    assert not missing, f"OpenLogic sources missing after sync: {missing}"
    return before


def _compile(tmp_path_factory, label: str, rr_files: tuple[str, ...]):
    """Run one Synplify compile; return (exit code, @E lines, parsed files)."""
    exe = _synplify()
    if exe is None:
        pytest.skip("Synplify Pro (Libero) is not installed on this host")
    before = _ensure_olo()
    work = tmp_path_factory.mktemp(f"synplify_ddr_p1_2_{label}")
    lines = [f'add_file -vhdl -lib olo "{ROOT / f}"' for f in OLO_FILES]
    lines += [f'add_file -vhdl -lib rr "{ROOT / f}"' for f in rr_files]
    lines += [
        "impl -add synthesis -type fpga",
        "set_option -technology PolarFireSoC",
        "set_option -part MPFS095T",
        "set_option -package FCSG325",
        "set_option -speed_grade -1",
        'set_option -top_module "rr.ddr_stream_fic"',
        "set_option -vhdl2008 1",
        'project -result_file "./synthesis/ddr_stream_fic.vm"',
        'impl -active "synthesis"',
        "project -run compile",
    ]
    prj = work / "ddr_p1_2.prj"
    prj.write_text("\n".join(lines) + "\n")
    try:
        proc = subprocess.run(
            [exe, "-batch", str(prj)],
            cwd=work, env=_license_env(),
            capture_output=True, text=True, timeout=600,
        )
    finally:
        for rel in _untracked() - before:
            path = ROOT / rel
            if path.is_dir():
                shutil.rmtree(path)
            elif path.is_file() or path.is_symlink():
                path.unlink()
    srr = work / "synthesis" / "synlog" / "ddr_stream_fic_compiler.srr"
    out = f"{proc.stdout}\n{proc.stderr}"
    assert "Cannot find license file" not in out, (
        f"Synplify did not get a license:\n{out[-2000:]}")
    assert srr.is_file(), (
        f"Synplify wrote no compiler log (exit {proc.returncode}):\n{out[-2000:]}")
    text = srr.read_text(errors="replace")
    errors = [ln for ln in text.splitlines() if ERROR_LINE.match(ln)]
    # Synplify logs "Using the VHDL 2008 Standard for file '<path>'" for each
    # file it starts to parse.
    parsed = set(re.findall(r"for file '([^']+)'", text))
    return proc.returncode, errors, parsed


@pytest.fixture(scope="module")
def synplify_compile(tmp_path_factory):
    """The consumer compile: no xpm body file and no stub."""
    return _compile(tmp_path_factory, "consumer", RR_FILES)


@pytest.fixture(scope="module")
def synplify_compile_with_stubs(tmp_path_factory):
    """Same set plus the stub bodies, so the wrappers cannot fail."""
    rr_files = RR_FILES[:3] + STUB_FILES + RR_FILES[3:]
    return _compile(tmp_path_factory, "stubs", rr_files)


def _unparsed(parsed: set[str], names: tuple[str, ...]) -> list[str]:
    return [n for n in names
            if not any(p.endswith("/" + n) for p in parsed)]


def _errors_in(errors: list[str], names: tuple[str, ...]) -> list[str]:
    return [ln for ln in errors if any(f'/{n}"' in ln for n in names)]


def test_fifo_wrappers_compile_without_an_xpm_body(synplify_compile):
    """No xpm body and no stub in the file set: the wrappers still analyze."""
    _, errors, parsed = synplify_compile
    missing = _unparsed(parsed, FIFO_WRAPPERS)
    assert not missing, f"Synplify never parsed the wrappers {missing}"
    bad = _errors_in(errors, FIFO_WRAPPERS)
    assert not bad, (
        "Synplify errors in the rr_fifo_* wrappers with no xpm body file "
        "(DDR-P1.2 CD590):\n" + "\n".join(bad))


def test_ddr_stream_axi_compiles_under_synplify(synplify_compile_with_stubs):
    """The DDR-T2.15 invariant must not stop the Synplify compile."""
    _, errors, parsed = synplify_compile_with_stubs
    early = _errors_in(errors, FIFO_WRAPPERS)
    assert not early, (
        "the stub bodies did not stop the wrapper errors, so this compile "
        "cannot judge ddr_stream_axi.vhd:\n" + "\n".join(early))
    bad = _errors_in(errors, ("ddr_stream_axi.vhd",))
    assert not bad, (
        "Synplify errors in ddr_stream_axi.vhd (DDR-P1.2 CD214):\n"
        + "\n".join(bad))
    missing = _unparsed(parsed, ("ddr_stream_axi.vhd",))
    assert not missing, (
        "Synplify stopped before it parsed ddr_stream_axi.vhd:\n"
        + "\n".join(errors))


def test_synplify_compile_has_no_errors(synplify_compile):
    """The whole consumer compile is clean: every file parsed, no @E, exit 0."""
    rc, errors, parsed = synplify_compile
    assert not errors and rc == 0, (
        f"Synplify compile of ddr_stream_fic failed (exit {rc}), "
        f"{len(errors)} @E line(s):\n" + "\n".join(errors))
    missing = _unparsed(parsed, tuple(Path(f).name for f in RR_FILES))
    assert not missing, f"Synplify never parsed {missing}"

#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""rr pre_build hook: generate the fabric LPDDR4 controller (emif_bank2a_fpga).

The controller configuration is the .ip file in ip/emif_a5/. This hook runs
qsys-generate on it and checks that the synthesis .qip was written;
tcl/project_options.tcl adds that .qip to the Quartus project.

qsys-generate is taken from $QUARTUS_ROOTDIR/sopc_builder/bin, else from PATH.
Every path is relative to this example, so the hook runs from any checkout.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

EXAMPLE_DIR = Path(__file__).resolve().parent.parent
IP_FILE = (EXAMPLE_DIR / "ip" / "emif_a5" / "golden_ghrd" / "emif_subsystem"
           / "emif_bank2a_fpga.ip")
QIP = IP_FILE.parent / "emif_bank2a_fpga" / "emif_bank2a_fpga.qip"
FAMILY = "Agilex 5"
PART = "A5ED065BB32AE4SR0"


def qsys_generate() -> str:
    root = os.environ.get("QUARTUS_ROOTDIR")
    if root:
        candidate = Path(root) / "sopc_builder" / "bin" / "qsys-generate"
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("qsys-generate")
    if found:
        return found
    sys.exit("fabric EMIF: qsys-generate not found (set QUARTUS_ROOTDIR or PATH "
             "to a Quartus Prime Pro 26.1 install)")


def main() -> None:
    if not IP_FILE.is_file():
        sys.exit(f"fabric EMIF: {IP_FILE} is missing")
    subprocess.run([qsys_generate(), str(IP_FILE), "--synthesis=VERILOG",
                    f"--family={FAMILY}", f"--part={PART}"],
                   cwd=IP_FILE.parent, check=True)
    if not QIP.is_file():
        sys.exit(f"fabric EMIF: qsys-generate did not write {QIP}")
    print(f"fabric EMIF: generated {QIP.relative_to(EXAMPLE_DIR)}")


if __name__ == "__main__":
    main()

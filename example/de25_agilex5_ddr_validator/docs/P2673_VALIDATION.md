# RTL-P2.673 — GSRD-pin bench validation (DE25-Standard, Agilex 5)

**Result: PASS on silicon, 2026-07-15.** The routertl socfpga boot chain, with the
GSRD version pin (RTL-P2.673), builds a version-consistent SPL + ATF + U-Boot and
boots this example to a Linux shell on the DE25.

## What was validated
RTL-P2.673 makes `rr linux` pin `u-boot-socfpga` / `arm-trusted-firmware` / kernel to
the GSRD tag matching the active Quartus version, instead of drifting to HEAD. Here
the pin **auto-derived** from `hardware.tool_path: /opt/altera_pro/25.3` →
`QPDS25.3_REL_GSRD_PR` (no `linux.gsrd_version` needed). The fix also sources ATF from
the **altera fork** (`altera-opensource/arm-trusted-firmware`), which carries the
`QPDS*_REL_GSRD_PR` tags — the ARM-software upstream default does not.

Verified on the bench build:
- `sw/linux/src/u-boot-socfpga` → `altera-opensource` @ `QPDS25.3_REL_GSRD_PR`
- `sw/linux/src/arm-trusted-firmware` → `altera-opensource` (fork) @ `QPDS25.3_REL_GSRD_PR`

## Delivery
Full pinned rebuild (`rr linux uboot --refresh` → ATF BL31 + SPL + FIT), SPL embedded
into the SOF via `rr linux sof`, SD staged with the pinned `u-boot.itb`, then
`quartus_pgm` of the `_hps.sof` over JTAG (fpga-first). Console = `/dev/ttyUSB2`
(board USB-Blaster III serial from `rr hardware doctor`, DTR/RTS asserted — Haz.107).

## Boot-log evidence (`P2673_gsrd_pin_bootlog_20260715.txt`)
```
U-Boot SPL 2025.07-ge5f40a8ed1ec-dirty (Jul 15 2026 - 16:13:32)   # pinned SPL (QPDS25.3, e5f40a8e)
DDR: Calibration success  /  DDR4: 1024 MiB
Bloblist at 72000 not found (err=-2)                              # ticket's exact msg — NON-FATAL here
## Checking hash(es) for Image uboot ... crc32+ OK               # SPL loads the FIT from MMC
NOTICE:  BL31: v2.13.0(release):QPDS25.3_REL_GSRD_PR-dirty        # SPL->ATF handoff SUCCEEDS (ATF @ pinned tag)
U-Boot 2025.07-ge5f40a8ed1ec-dirty ... socfpga_agilex5           # U-Boot proper (QPDS25.3)
Starting kernel ...  ->  Run /init as init process
~ #    (Linux 6.18.2 aarch64, "SoCFPGA Agilex5 SoCDK")
```

## Key finding
`Bloblist at 72000 not found (err=-2)` prints even on a **healthy** boot — it is a
non-fatal SPL probe, not the failure. The ticket's *hang* was the version-**coupled**
SPL→ATF handoff (a golden 2025.01 SPL against a drifted 2026.01 FIT). Pinning every
stage to a single GSRD version removes the coupling, so a consistent chain sails past
the probe to `NOTICE: BL31` and on to Linux. The `NOTICE: BL31: … QPDS25.3_REL_GSRD_PR`
banner is direct proof the ATF came from the pinned altera-fork clone.

Board powered off after the test (Haz.108 discipline).

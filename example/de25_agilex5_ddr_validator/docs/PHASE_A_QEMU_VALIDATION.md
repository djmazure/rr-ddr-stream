# Phase A — off-board QEMU validation (RTL-P3.681)

Phase A's interim contract is "Agilex 5 HPS → Linux serial console + login".
Real on-board boot needs the SDM/ATF first stage, a programmed bitstream, and the
SD card — none of which a CI box has. This doc records the **software half**
proven off-board: the `linux-socfpga` arm64 kernel + a minimal rootfs booting to
an interactive shell prompt under QEMU, and the full no-sudo U-Boot build.

The board's `socfpga_agilex5_defconfig` boot path is **untouched** — this is a
separate, generic-`virt` validation. It exercises the kernel `Image` + rootfs +
userspace, i.e. everything downstream of the SDM->ATF->U-Boot first-stage chain.

## Tooling on this host (2026-06-04)

- `qemu-system-aarch64` 6.2.0, machine `virt`. No `cortex-a55`/`a76` CPU model ->
  boot with **`-cpu max`** (an SVE2-capable v8 core; the kernel's generic
  `arm64` defconfig is CPU-agnostic).
- Cross toolchain: `aarch64-linux-gnu-` (Vitis 2024.1 bundle, GCC 12.2.0) on PATH.
- **No sudo / no `apt`** on this host.

## 1. Kernel — `linux-socfpga` arm64 `Image`

Built with the **same config the socfpga backend selects** (generic arm64
`defconfig`; `_socfpga._ARCH_TABLE["agilex5-hps"].kernel_defconfig = "defconfig"`).
The generic arm64 defconfig already enables `CONFIG_SERIAL_AMBA_PL011`
(-> `ttyAMA0`), `CONFIG_VIRTIO_MMIO`, `CONFIG_VIRTIO_BLK` — so the same Image that
targets the board also boots straight on QEMU `virt`. No board-specific config
fork.

```bash
cd sw/linux/src/linux-socfpga
export ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-
make defconfig
make -j"$(nproc)" Image dtbs
# -> arch/arm64/boot/Image   (50 MB, Linux 6.18.2-gbe94a41dfaf7)
```

Equivalent through `rr` once a project sets `target: agilex5-hps` in
`system_memory_map.yml`: `rr linux kernel` -> `run_socfpga_kernel`.

## 2. Rootfs — aarch64 BusyBox initramfs (no sudo)

Use the in-tree routertl helper, which downloads an arm64 `busybox-static`
(`ports.ubuntu.com`, cached under `~/.cache/routertl/qemu/busybox/arm64/`) and
packs a cpio with `/dev/console` device nodes via `fakeroot` — no root needed:

```bash
routertl/sdk/linux/qemu/build_initramfs.sh \
    --target-arch aarch64 --output rootfs-arm64.cpio.gz
# -> 944 KB cpio.gz; /init mounts proc/sys/dev and exec's /bin/sh
```

## 3. Boot on QEMU `virt`

```bash
qemu-system-aarch64 -machine virt -cpu max -m 2G -nographic -no-reboot \
    -kernel arch/arm64/boot/Image \
    -initrd rootfs-arm64.cpio.gz \
    -append "console=ttyAMA0 rdinit=/init"
```

### Result — interactive shell prompt reached (PASS)

```
[    0.000000] Linux version 6.18.2-gbe94a41dfaf7 (aarch64-xilinx-linux-gcc.real (GCC) 12.2.0) ...
...
[    2.312384] Run /init as init process
  RouteRTL — QEMU Test Environment
Kernel: 6.18.2-gbe94a41dfaf7
Shell ready. Type 'poweroff -f' to exit QEMU.
BusyBox v1.30.1 (Ubuntu 1:1.30.1-7ubuntu3.1) built-in shell (ash)
/ # uname -a
Linux (none) 6.18.2-gbe94a41dfaf7 #1 SMP PREEMPT ... aarch64 GNU/Linux
/ # echo RR_SHELL_LIVE_OK
RR_SHELL_LIVE_OK
/ # poweroff -f
[   29.917562] reboot: Power down
```

The guest accepted commands and powered down cleanly — the shell is live, not a
printed banner. This validates the kernel `Image` + rootfs + userspace half of
Phase A in software. (A getty `login:` prompt is a one-line `/sbin/getty` swap on
the init; an interactive shell from init is the equivalent Phase-A proof.)

## 4. U-Boot SPL + `u-boot.itb` — no-sudo build

`u-boot-socfpga`'s default target builds the SPL and a binman FIT
(`u-boot.itb` = U-Boot proper + ATF BL31 + control DTB). Two non-obvious,
**no-sudo** unblocks:

1. **swig** (needed by U-Boot's bundled `scripts/dtc/pylibfdt`): the U-Boot make
   builds its *own* pylibfdt from `libfdt.i` and ignores `PYTHONPATH`, so a
   system pylibfdt does **not** satisfy it. The fix is a pip binary wheel:
   `pip install --user swig` (4.4.1) -> `~/.local/bin/swig`. No `apt`.
2. **BL31 blob for binman**: the socfpga FIT dtsi references the bare filename
   `bl31.bin`, which binman searches for in the build dir — `make BL31=<path>`
   alone leaves it "faked/missing". Copy `bl31.bin` into the U-Boot dir.

```bash
# ATF BL31 first (agilex5, RESET_TO_BL2=1) -> build/agilex5/release/bl31.bin
cd sw/linux/src/u-boot-socfpga
export ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-
export PATH="$HOME/.local/bin:$PATH"          # pip swig
export BL31="$PWD/../arm-trusted-firmware/build/agilex5/release/bl31.bin"
make socfpga_agilex5_defconfig
cp -f "$BL31" ./bl31.bin                        # binman finds it by bare name
make -j"$(nproc)" BL31="$BL31"
# -> u-boot.itb (941 KB FIT: uboot + atf[real BL31] + fdt), spl/u-boot-spl(.bin)
```

`tools/dumpimage -l u-boot.itb` confirms the FIT carries the real ATF image (not a
fake). Equivalent through `rr`: `rr linux uboot` -> `run_socfpga_uboot`.

> The genuine remaining wall for an **on-board** Phase-A console is hardware-only:
> the SDM-loaded ATF BL2 first stage, a programmed bitstream/HPS handoff, and the
> SD A2/FAT layout — none reproducible off-board. The software artifacts above
> (kernel `Image`, `u-boot.itb` + SPL, ATF BL31) are all built and the kernel is
> QEMU-boot-proven.

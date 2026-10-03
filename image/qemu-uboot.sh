#!/bin/sh
# SPDX-License-Identifier: MIT
#
# qemu-uboot.sh - build the mainline U-Boot that image/qemu-test.py boots
# images with: qemu-riscv64_smode, switched to standard boot the way the
# VisionFive 2 defconfig has it (CONFIG_BOOTSTD_DEFAULTS, boot command
# "bootflow scan", no distro boot scripts), so QEMU exercises the same
# path that finds the image on the board.
#
#   image/qemu-uboot.sh [OUTDIR]      (default: ./qemu-uboot)
#
# Prints the path of u-boot.bin. Needs git, make, gcc-riscv64-linux-gnu,
# bison, flex, libssl-dev, python3.

set -eu
TAG=${UBOOT_TAG:-v2026.10-rc5}
OUT=${1:-$PWD/qemu-uboot}
SRC=$OUT/u-boot
CROSS=${CROSS_COMPILE:-riscv64-linux-gnu-}
[ "$(uname -m)" = riscv64 ] && CROSS=

mkdir -p "$OUT"
[ -d "$SRC/.git" ] || git clone -q --depth 1 --branch "$TAG" https://github.com/u-boot/u-boot "$SRC"
make -s -C "$SRC" O="$OUT/build" CROSS_COMPILE="$CROSS" qemu-riscv64_smode_defconfig
"$SRC/scripts/config" --file "$OUT/build/.config" \
	-d DISTRO_DEFAULTS -e BOOTSTD_DEFAULTS -e BOOTSTD_BOOTCOMMAND \
	--set-str BOOTCOMMAND "bootflow scan" -d TOOLS_MKEFICAPSULE
make -s -C "$SRC" O="$OUT/build" CROSS_COMPILE="$CROSS" olddefconfig
make -s -C "$SRC" O="$OUT/build" CROSS_COMPILE="$CROSS" -j"$(nproc)"
grep -q '^bootcmd=bootflow scan$' "$OUT/build/u-boot.cfg" 2>/dev/null ||
	strings "$OUT/build/u-boot.bin" | grep -qx 'bootcmd=bootflow scan'
echo "$OUT/build/u-boot.bin"

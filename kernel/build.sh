#!/bin/sh
# SPDX-License-Identifier: MIT
#
# build.sh - build a VisionFive 2 kernel with the GPU + HDMI patch stack.
#
#   ./kernel/build.sh [LINUX_SRC] [OUT]
#
# LINUX_SRC  existing Linux checkout to patch (default: clone v7.3-rc5 into
#            ./linux). The patches are applied on a new branch "vf2-gpu-hdmi"
#            based on v7.3-rc5; the tree must not have local changes.
# OUT        build directory (default: ./build)
#
# Cross-compiles from x86_64 with riscv64-linux-gnu-gcc, or builds natively
# on a riscv64 host. Produces Debian packages in the parent of OUT
# (linux-image and linux-libc-dev; also linux-headers on native builds),
# plus Image, modules and the VisionFive 2 DTBs under OUT.
#
# Host packages (Debian/Ubuntu):
#   gcc-riscv64-linux-gnu bc bison flex kmod libssl-dev libelf-dev
#   libdw-dev python3 rsync debhelper dpkg-dev

set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
BASE=v7.3-rc5
SRC=${1:-$PWD/linux}
OUT=${2:-$PWD/build}
JOBS=${JOBS:-$(nproc)}

case $(uname -m) in
riscv64) CROSS= ;;
*) CROSS=${CROSS_COMPILE:-riscv64-linux-gnu-} ;;
esac
MAKE="make -C $SRC O=$OUT ARCH=riscv CROSS_COMPILE=$CROSS -j$JOBS"

if ! git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
	git clone --depth 1 --branch "$BASE" https://github.com/torvalds/linux "$SRC"
fi

cd "$SRC"
if ! git diff --quiet || ! git diff --cached --quiet; then
	echo "$SRC has local changes; refusing to apply patches" >&2
	exit 1
fi
if git rev-parse -q --verify vf2-gpu-hdmi >/dev/null; then
	echo "branch vf2-gpu-hdmi already exists in $SRC; reusing it"
	git checkout -q vf2-gpu-hdmi
else
	git checkout -q -b vf2-gpu-hdmi "$BASE"
	git am --3way "$HERE"/patches/*.patch
fi

mkdir -p "$OUT"
$MAKE defconfig
"$SRC"/scripts/kconfig/merge_config.sh -m -O "$OUT" "$OUT/.config" \
	"$HERE/config/vf2-gpu-hdmi.config"
$MAKE olddefconfig

# Fail early if a fragment symbol did not survive (renamed or unmet deps).
missing=$(grep '^CONFIG_' "$HERE/config/vf2-gpu-hdmi.config" | grep -vxF -f "$OUT/.config" || true)
if [ -n "$missing" ]; then
	echo "config fragment lines not applied:" >&2
	echo "$missing" >&2
	exit 1
fi

$MAKE Image modules dtbs
# Cross builds skip linux-headers: it would need the target's libssl/libelf.
if [ -n "$CROSS" ]; then
	DEB_BUILD_PROFILES=pkg.linux-upstream.nokernelheaders $MAKE bindeb-pkg
else
	$MAKE bindeb-pkg
fi

echo
echo "Image: $OUT/arch/riscv/boot/Image"
echo "DTB:   $OUT/arch/riscv/boot/dts/starfive/jh7110-starfive-visionfive-2-v1.3b.dtb"
echo "debs:  $(dirname "$OUT")/linux-*.deb"

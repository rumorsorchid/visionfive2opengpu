#!/bin/sh
# SPDX-License-Identifier: MIT
#
# setup.sh - one-time userspace setup on the VisionFive 2 (Debian/Ubuntu
# riscv64) after booting a kernel built with kernel/build.sh.
#
#   sudo ./setup.sh /path/to/rogue_36.50.54.182_v1.fw

set -eu
FW_SRC=${1:-}
FW_DST=/lib/firmware/powervr/rogue_36.50.54.182_v1.fw

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

if [ -n "$FW_SRC" ]; then
	install -D -m 0644 "$FW_SRC" "$FW_DST"
	echo "installed $FW_DST ($(sha256sum "$FW_DST" | cut -d' ' -f1))"
elif [ ! -f "$FW_DST" ]; then
	echo "no firmware given and $FW_DST missing; see firmware/README.md"
	exit 1
fi

# BXE-4-32 is marked experimental in the driver: opt in explicitly.
cat >/etc/modprobe.d/powervr-jh7110.conf <<'EOF'
# VisionFive 2: BXE-4-32 is "experimental" in drm/imagination
options powervr exp_hw_support=1
EOF
echo "wrote /etc/modprobe.d/powervr-jh7110.conf"

# Mesa only whitelists conformance-tested PowerVR cores.
cat >/etc/profile.d/powervr-jh7110.sh <<'EOF'
# VisionFive 2 / BXE-4-32: Mesa's PowerVR Vulkan driver is not yet
# conformance tested on this core, so it has to be enabled explicitly.
export PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1
export MESA_VK_DEVICE_SELECT=1010:36054182
EOF
echo "wrote /etc/profile.d/powervr-jh7110.sh (log in again to apply)"

if ! grep -qw 'cma=[0-9]*[MG]' /proc/cmdline; then
	echo "note: consider adding cma=256M to the kernel command line"
fi

if command -v apt-get >/dev/null; then
	echo "suggested packages:"
	echo "  apt install mesa-vulkan-drivers vulkan-tools vkmark glmark2-es2-wayland \\"
	echo "              glmark2-es2-drm labwc weston mesa-utils"
fi

update-initramfs -u 2>/dev/null || true
echo "done; reboot or: modprobe -r powervr; modprobe powervr"

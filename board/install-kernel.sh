#!/bin/sh
# SPDX-License-Identifier: MIT
#
# install-kernel.sh - install the kernel packages built by kernel/build.sh on
# the VisionFive 2 and make the bootloader use the matching device tree.
#
#   sudo ./install-kernel.sh linux-image-*.deb [linux-libc-dev_*.deb]
#
# Handles the two common setups:
#  * u-boot-menu (Debian): sets U_BOOT_FDT and runs u-boot-update, which
#    writes /boot/extlinux/extlinux.conf;
#  * EFI boot with U-Boot loading the DTB from the ESP (e.g.
#    /boot/efi/dtb/starfive/): copies the DTB there, keeping a backup.
# Anything else: prints where the DTB is so you can wire it up by hand.

set -eu
DTB_NAME=starfive/jh7110-starfive-visionfive-2-v1.3b.dtb

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
[ $# -ge 1 ] || { echo "usage: $0 linux-image-*.deb [more .debs]"; exit 1; }

dpkg -i "$@"

img=$(dpkg-deb -f "$1" Package)
ver=${img#linux-image-}
dtb=/usr/lib/linux-image-$ver/$DTB_NAME
[ -f "$dtb" ] || { echo "DTB $dtb not found in the package"; exit 1; }
echo "kernel $ver installed; DTB $dtb"

if command -v u-boot-update >/dev/null 2>&1; then
	conf=/etc/default/u-boot
	touch "$conf"
	# u-boot-menu 4.2 looks for device trees in /lib/firmware/<version>/
	# unless told otherwise; kernel packages install them here:
	for kv in "U_BOOT_FDT=\"$DTB_NAME\"" 'U_BOOT_FDT_DIR="/usr/lib/linux-image-"'; do
		key=${kv%%=*}
		if grep -q "^$key=" "$conf"; then
			sed -i "s|^$key=.*|$kv|" "$conf"
		else
			echo "$kv" >>"$conf"
		fi
	done
	u-boot-update
	if grep -q "fdt /usr/lib/linux-image-$ver/$DTB_NAME" /boot/extlinux/extlinux.conf; then
		echo "extlinux.conf regenerated (fdt $DTB_NAME)"
	else
		echo "WARNING: /boot/extlinux/extlinux.conf has no fdt line for $DTB_NAME;"
		echo "U-Boot would boot with its own device tree (no GPU, no HDMI)"
		exit 1
	fi
elif [ -d /boot/efi/dtb/starfive ]; then
	dst=/boot/efi/dtb/$DTB_NAME
	[ -f "$dst" ] && cp "$dst" "$dst.bak-$(date +%Y%m%d%H%M%S)"
	cp "$dtb" "$dst"
	echo "copied DTB to $dst (previous one backed up)"
else
	echo "no u-boot-menu and no /boot/efi/dtb: point your bootloader at"
	echo "  kernel: /boot/vmlinuz-$ver"
	echo "  dtb:    $dtb"
fi

echo "next: sudo board/setup.sh <firmware>; then reboot"

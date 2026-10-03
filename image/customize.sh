#!/bin/sh
# SPDX-License-Identifier: MIT
#
# customize.sh - runs inside the riscv64 root filesystem (chroot, under
# qemu-user on a non-riscv64 build host) while image/build.sh builds the
# VisionFive 2 image. Not meant to be run by hand.
#
# Installs the package lists, the kernel, openfw and the image's own
# integration package, configures boot, users and the desktop, and checks
# the result: the build fails rather than producing an image that would
# not boot or that contains anything outside Debian main.

set -eu
STAGE=/tmp/vf2-stage
# shellcheck source=/dev/null
. "$STAGE/config"
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 LANG=C.UTF-8
APT="apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
say() { echo "customize: $*"; }
die() { echo "customize: ERROR: $*" >&2; exit 1; }

# No daemons start inside the build chroot.
printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d
echo "man-db man-db/auto-update boolean false" | debconf-set-selections

# -- files the package scripts read when they run ------------------------
echo "$VF2_HOSTNAME" >/etc/hostname
cat >/etc/hosts <<EOF
127.0.0.1	localhost
127.0.1.1	$VF2_HOSTNAME
::1		localhost ip6-localhost ip6-loopback
ff02::1		ip6-allnodes
ff02::2		ip6-allrouters
EOF
cat >/etc/fstab <<EOF
# <file system>		<mount point>	<type>	<options>		<dump>	<pass>
UUID=$FSUUID	/		ext4	defaults,noatime	0	1
EOF

# u-boot-menu writes /boot/extlinux/extlinux.conf whenever a kernel is
# installed; mainline U-Boot's standard boot finds it on the partition
# marked bootable and loads kernel, initrd and this device tree.
mkdir -p /etc/default
cat >/etc/default/u-boot <<EOF
# u-boot-update(8) settings of the VisionFive 2 image
U_BOOT_MENU_LABEL="Debian sid (VisionFive 2 open GPU)"
U_BOOT_ROOT="root=UUID=$FSUUID"
U_BOOT_PARAMETERS="rw rootwait console=ttyS0,115200n8 console=tty0"
U_BOOT_FDT="$DTB"
U_BOOT_TIMEOUT="30"
EOF

# initramfs: the NVMe path (PCIe host + PHY + nvme) and the display
# modules, so the console comes back on HDMI early (docs/boot.md).
mkdir -p /etc/initramfs-tools
cat >>/etc/initramfs-tools/modules <<'EOF'
# VisionFive 2: root on NVMe behind the JH7110 PCIe controller
phy-jh7110-pcie
pcie-starfive
nvme
# VisionFive 2: HDMI (VOUT clocks and subsystems first, see docs/boot.md)
clk-starfive-jh7110-vout
jh7110-vout-subsystem
jh7110-hdmi-subsystem
phy-jh7110-inno-hdmi
jh7110-inno-hdmi
verisilicon-dc
EOF

# -- apt sources: Debian main only -----------------------------------------
signed=
for k in "$SIGNED_BY" "${SIGNED_BY%.gpg}.pgp"; do
	[ -f "$k" ] && { signed="Signed-By: $k"; break; }
done
rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list
cat >/etc/apt/sources.list.d/vf2-image.sources <<EOF
Types: deb
URIs: $MIRROR
Suites: $SUITE
Components: $COMPONENTS
$signed
EOF
$APT update

# -- packages ----------------------------------------------------------------
# install_list FILE STRICT: "a|b" picks the first available; STRICT=1 fails
# on a missing package, STRICT=0 records it and goes on.
available() {
	apt-cache policy "$1" </dev/null 2>/dev/null | sed -n 's/^ *Candidate: //p' | grep -qv '(none)'
}
install_list() {
	pkgs=
	missing=
	while read -r line; do
		line=${line%%#*}
		line=$(echo "$line" | tr -d ' \t')
		[ -n "$line" ] || continue
		pick=
		for alt in $(echo "$line" | tr '|' ' '); do
			if available "$alt"; then pick=$alt; break; fi
		done
		if [ -n "$pick" ]; then pkgs="$pkgs $pick"; else missing="$missing $line"; fi
	done <"$1"
	if [ -n "$missing" ]; then
		if [ "$2" = 1 ] && [ "$ALLOW_MISSING" != 1 ]; then
			die "not in the archive for $(dpkg --print-architecture):$missing"
		fi
		say "skipping (not in the archive):$missing"
		echo "$missing" | tr ' ' '\n' | sed '/^$/d' >>"$STAGE/skipped"
	fi
	# shellcheck disable=SC2086
	[ -z "$pkgs" ] || $APT install $pkgs
}
install_list "$STAGE/packages.txt" 1
# Optional packages one at a time: one that fails to install (for
# example a broken dependency in sid) must not take the others down.
grep -v '^[[:space:]]*\(#\|$\)' "$STAGE/packages-optional.txt" | while read -r p; do
	if available "$p"; then
		$APT install "$p" </dev/null || { say "optional $p failed to install"; echo "$p" >>"$STAGE/skipped"; }
	else
		say "optional $p: not in the archive"
		echo "$p" >>"$STAGE/skipped"
	fi
done

# The kernel (kernel/build.sh), openfw and the integration package. The
# kernel's postinst builds the initramfs and runs u-boot-update.
# shellcheck disable=SC2046
$APT install $(ls "$STAGE"/debs/*.deb)

# -- system configuration ----------------------------------------------------
if [ -f /etc/locale.gen ]; then
	sed -i 's/^# *\(en_US.UTF-8 UTF-8\)/\1/' /etc/locale.gen
	locale-gen
else
	locale-gen en_US.UTF-8
fi
update-locale LANG=en_US.UTF-8
ln -sf /usr/share/zoneinfo/Etc/UTC /etc/localtime
echo Etc/UTC >/etc/timezone

# User (password = the user name unless the build set one), in the groups
# a desktop and the GPU tests need; adm reads the kernel log via journalctl.
useradd -m -s /bin/bash -c "VisionFive 2" "$USERNAME"
for g in sudo video render audio input plugdev netdev adm users; do
	getent group "$g" >/dev/null && usermod -aG "$g" "$USERNAME"
done
echo "$USERNAME:$PASSWORD" | chpasswd
mkdir -p /var/lib/vf2
if [ "$USERNAME" = vf2 ] && [ "$PASSWORD" = vf2 ]; then
	getent shadow vf2 | cut -d: -f2 >/var/lib/vf2/default-password
	chmod 600 /var/lib/vf2/default-password
fi
passwd -l root >/dev/null

# greetd: log the user straight into the desktop once at boot; after a
# logout tuigreet asks for a login.
greeter=_greetd
getent passwd "$greeter" >/dev/null || greeter=greeter
cat >/etc/greetd/config.toml <<EOF
# greetd settings of the VisionFive 2 image
[terminal]
vt = 7

[default_session]
command = "tuigreet --time --remember --asterisks --cmd vf2-session"
user = "$greeter"

[initial_session]
command = "vf2-session"
user = "$USERNAME"
EOF
command -v tuigreet >/dev/null ||
	sed -i "s|^command = \"tuigreet.*|command = \"agreety --cmd vf2-session\"|" /etc/greetd/config.toml

# Firefox: composite with WebRender and run WebGL on the GPU through Zink
# (GLES 3; Zink's desktop GL stops below 3.2 on this GPU). The menu has a
# "software rendering" entry for comparison.
for d in /usr/lib/firefox-esr/defaults/pref /usr/lib/firefox/defaults/pref; do
	[ -d "$d" ] || continue
	cat >"$d/vf2-gpu.js" <<'EOF'
// VisionFive 2 image: GPU rendering through Zink on the PowerVR BXE-4-32
pref("gfx.webrender.all", true);
pref("gfx.egl.prefer-gles.enabled", true);
pref("layers.acceleration.force-enabled", true);
pref("webgl.force-enabled", true);
pref("widget.dmabuf.force-enabled", true);
// the JH7110's video decoder needs closed firmware: not used
pref("media.hardware-video-decoding.enabled", false);
EOF
done

systemctl enable greetd.service NetworkManager.service ssh.service systemd-timesyncd.service \
	vf2-firstboot.service vf2-selftest.service
systemctl set-default graphical.target
mkdir -p /var/log/journal

# Restore man-db's index updates for the running system.
echo "man-db man-db/auto-update boolean true" | debconf-set-selections

# -- checks --------------------------------------------------------------------
kver=$(linux-version list | head -n 1)
[ -n "$kver" ] || die "no kernel installed"
[ -f "/boot/initrd.img-$kver" ] || die "no initramfs for $kver"
[ -f "/usr/lib/linux-image-$kver/$DTB" ] || die "DTB $DTB missing from the kernel package"
ext=/boot/extlinux/extlinux.conf
grep -q "linux /boot/vmlinuz-$kver" "$ext" || die "$ext does not boot $kver"
grep -q "initrd /boot/initrd.img-$kver" "$ext" || die "$ext has no initrd"
grep -q "fdt /usr/lib/linux-image-$kver/$DTB" "$ext" || die "$ext has no fdt line for $DTB"
grep -q "root=UUID=$FSUUID" "$ext" || die "$ext has the wrong root="
for m in nvme pcie-starfive verisilicon-dc; do
	lsinitramfs "/boot/initrd.img-$kver" | grep -q "/$m.ko" || die "initramfs lacks $m"
done
say "boot: $(grep -c '^label' "$ext") extlinux entries for $kver"

fw=/usr/lib/firmware/powervr/rogue_36.50.54.182_v1.fw
[ "$(sha256sum "$fw" | cut -d' ' -f1)" = "$FW_SHA256" ] || die "$fw is not the openfw build"
# No firmware other than openfw anywhere in the image.
# (wireless-regdb's regulatory database is data, not firmware)
other=$(find /usr/lib/firmware -type f ! -path "$fw" ! -name 'regulatory.db*' 2>/dev/null || true)
[ -z "$other" ] || die "firmware files besides openfw: $other"
# Debian main only: nothing from contrib, non-free or non-free-firmware.
nonfree=$(dpkg-query -W -f '${Package} ${Section}\n' | awk '$2 ~ /^(contrib|non-free)/')
[ -z "$nonfree" ] || die "packages outside main: $nonfree"
say "checks passed: openfw is the only firmware, all packages from main"

# -- record and clean up -------------------------------------------------------
doc=/usr/share/doc/vf2-integration
mkdir -p "$doc"
sort -u "$STAGE/skipped" 2>/dev/null >"$doc/skipped-packages" || true
dpkg-query -W -f '${Package}\t${Version}\n' >"$doc/packages"
cp "$STAGE/build-info" "$doc/build-info"
$APT clean
rm -rf /var/lib/apt/lists/*
rm -f /usr/sbin/policy-rc.d
rm -f /etc/ssh/ssh_host_*
: >/etc/machine-id
rm -f /var/lib/dbus/machine-id
find /var/log -type f -name '*.log' -exec truncate -s 0 {} +
rm -rf /tmp/* /var/tmp/*
say "done"

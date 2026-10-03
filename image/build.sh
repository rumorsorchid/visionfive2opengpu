#!/bin/sh
# SPDX-License-Identifier: MIT
#
# build.sh - build a flashable Debian sid disk image for the VisionFive 2
# with the open GPU stack: the kernel from kernel/ (blob-free), openfw as
# the GPU firmware, Mesa (PowerVR Vulkan + Zink), a labwc desktop, Firefox
# and the board tests. Write the result to an NVMe drive (or SD card) and
# boot it with mainline U-Boot (image/README.md).
#
#   sudo image/build.sh --kernel-debs DIR [options]
#
#   --kernel-debs DIR   directory with the linux-image-*.deb from kernel/build.sh
#   --firmware FILE     GPU firmware (default: openfw/prebuilt/rogue_36.50.54.182_v1.fw)
#   --out DIR           output directory (default: ./out)
#   --name NAME         image name (default: vf2-debian-sid-<date>)
#   --user NAME --password PW --hostname NAME   (default: vf2 / vf2 / vf2)
#   --suite S --mirror URL --components "C..." --keyring FILE
#                       archive (default: Debian sid main from deb.debian.org)
#   --packages FILE --packages-optional FILE
#                       package lists (default: image/packages*.txt)
#   --allow-missing     do not fail on packages the archive lacks (trial
#                       builds against other archives only)
#
# Needs root (mmdebstrap chroot mode), mmdebstrap, sfdisk, mke2fs with -d,
# dpkg-deb, gzip; on a non-riscv64 host also qemu-user-static with binfmt
# for riscv64. The image needs no loop devices: the filesystem is created
# from a directory (mke2fs -d) and copied into the partition.
#
# Output in OUT: NAME.img.gz (write it with: gunzip -c NAME.img.gz | dd ...),
# NAME.img.gz.sha256, NAME.packages (installed packages), NAME.build-info.

set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
TOP=$(dirname "$HERE")

KDEBS=
FW=$TOP/openfw/prebuilt/rogue_36.50.54.182_v1.fw
OUT=$PWD/out
NAME=vf2-debian-sid-$(date -u +%Y%m%d)
USERNAME=vf2
PASSWORD=vf2
VF2_HOSTNAME=vf2
SUITE=sid
MIRROR=http://deb.debian.org/debian
COMPONENTS=main
KEYRING=/usr/share/keyrings/debian-archive-keyring.gpg
ALLOW_MISSING=0
PACKAGES=$HERE/packages.txt
PACKAGES_OPT=$HERE/packages-optional.txt
DTB=starfive/jh7110-starfive-visionfive-2-v1.3b.dtb
PART_START_MIB=16

usage() { sed -n '3,32p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
	case $1 in
	--kernel-debs) KDEBS=$2; shift ;;
	--firmware) FW=$2; shift ;;
	--out) OUT=$2; shift ;;
	--name) NAME=$2; shift ;;
	--user) USERNAME=$2; shift ;;
	--password) PASSWORD=$2; shift ;;
	--hostname) VF2_HOSTNAME=$2; shift ;;
	--suite) SUITE=$2; shift ;;
	--mirror) MIRROR=$2; shift ;;
	--components) COMPONENTS=$2; shift ;;
	--keyring) KEYRING=$2; shift ;;
	--packages) PACKAGES=$2; shift ;;
	--packages-optional) PACKAGES_OPT=$2; shift ;;
	--allow-missing) ALLOW_MISSING=1 ;;
	-h|--help) usage 0 ;;
	*) echo "unknown option $1" >&2; usage 1 ;;
	esac
	shift
done

die() { echo "build.sh: $*" >&2; exit 1; }
say() { echo "build.sh: $*"; }
[ "$(id -u)" = 0 ] || die "run as root (mmdebstrap needs chroot)"
[ -n "$KDEBS" ] || die "--kernel-debs DIR is required"
KDEB=
for f in "$KDEBS"/linux-image-*.deb; do
	case $f in *-dbg_*) continue ;; esac
	[ -f "$f" ] || continue
	[ -z "$KDEB" ] || die "more than one linux-image-*.deb in $KDEBS"
	KDEB=$f
done
[ -n "$KDEB" ] || die "no linux-image-*.deb in $KDEBS (build one with kernel/build.sh)"
[ -f "$FW" ] || die "firmware $FW not found"
# debian-archive-keyring ships .gpg files, newer versions .pgp ones
[ -f "$KEYRING" ] || [ ! -f "${KEYRING%.gpg}.pgp" ] || KEYRING=${KEYRING%.gpg}.pgp
[ -f "$KEYRING" ] || die "keyring $KEYRING not found (install debian-archive-keyring)"
for t in mmdebstrap sfdisk mke2fs e2fsck dpkg-deb gzip sha256sum truncate; do
	command -v "$t" >/dev/null || die "$t not found"
done
if [ "$(uname -m)" != riscv64 ]; then
	# In a container binfmt_misc is often not mounted although the host's
	# riscv64 handler (registered with the F flag) works: ask arch-test.
	if command -v arch-test >/dev/null; then
		arch-test riscv64 >/dev/null 2>&1 ||
			die "cannot run riscv64 binaries: install qemu-user-static (binfmt for riscv64)"
	elif ! grep -qs enabled /proc/sys/fs/binfmt_misc/qemu-riscv64; then
		say "warning: cannot verify riscv64 emulation (install arch-test); trying anyway"
	fi
fi
dpkg-deb -c "$KDEB" | grep -q "/$DTB\$" || die "$KDEB has no $DTB"

WORK=$(mktemp -d "${TMPDIR:-/var/tmp}/vf2-image.XXXXXX")
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM
STAGE=$WORK/vf2-stage
ROOT=$WORK/rootfs
mkdir -p "$STAGE/debs" "$OUT" "$WORK/doc"

GITREV=$(git -C "$TOP" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)
VERSION=$(date -u +%Y%m%d)+git$GITREV
FW_SHA256=$(sha256sum "$FW" | cut -d' ' -f1)
FSUUID=$(cat /proc/sys/kernel/random/uuid)
PARTUUID=$(cat /proc/sys/kernel/random/uuid)
case $KEYRING in
*ubuntu*) SIGNED_BY=/usr/share/keyrings/ubuntu-archive-keyring.gpg ;;
*) SIGNED_BY=/usr/share/keyrings/debian-archive-keyring.gpg ;;
esac

# -- openfw-firmware: the GPU firmware with its source ----------------------
pkg=$WORK/openfw-firmware
fwdir=$pkg/usr/lib/firmware/powervr
doc=$pkg/usr/share/doc/openfw-firmware
mkdir -p "$fwdir" "$doc" "$pkg/DEBIAN"
install -m 0644 "$FW" "$fwdir/rogue_36.50.54.182_v1.fw"
echo "$FW_SHA256  rogue_36.50.54.182_v1.fw" >"$doc/SHA256SUMS"
if git -C "$TOP" rev-parse >/dev/null 2>&1; then
	git -C "$TOP" archive --format=tar --prefix=openfw-src/ HEAD openfw tools/pvrfw.py |
		gzip -9n >"$doc/openfw-src.tar.gz"
else
	tar -C "$TOP" --exclude='*.o' --exclude=__pycache__ -czf "$doc/openfw-src.tar.gz" openfw tools/pvrfw.py
fi
cat >"$doc/copyright" <<'EOF'
openfw: open firmware for the PowerVR BXE-4-32 GPU of the StarFive JH7110.
License: MIT. Source: openfw-src.tar.gz in this directory (rebuild with
"make -C openfw" and a mipsel-linux-gnu GCC; the result is byte-identical).

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
EOF
cat >"$pkg/DEBIAN/control" <<EOF
Package: openfw-firmware
Version: $VERSION
Architecture: all
Section: kernel
Priority: optional
Maintainer: VisionFive 2 open GPU image <root@localhost>
Description: open firmware for the PowerVR BXE-4-32 GPU (JH7110)
 openfw replaces Imagination's closed firmware for the GPU of the StarFive
 JH7110 (VisionFive 2). It is MIT licensed and built with GCC; its source
 is in /usr/share/doc/openfw-firmware.
EOF
# Any other package's copy of this file (Imagination's, in Debian's
# non-free firmware) is diverted aside and never loaded.
cat >"$pkg/DEBIAN/preinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = install ] || [ "$1" = upgrade ]; then
	dpkg-divert --package openfw-firmware --add --rename \
		--divert /usr/lib/firmware/powervr/rogue_36.50.54.182_v1.fw.non-openfw \
		/usr/lib/firmware/powervr/rogue_36.50.54.182_v1.fw
fi
EOF
cat >"$pkg/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = purge ]; then
	dpkg-divert --package openfw-firmware --remove --rename \
		--divert /usr/lib/firmware/powervr/rogue_36.50.54.182_v1.fw.non-openfw \
		/usr/lib/firmware/powervr/rogue_36.50.54.182_v1.fw
fi
EOF
echo "activate-noawait update-initramfs" >"$pkg/DEBIAN/triggers"
chmod 755 "$pkg/DEBIAN/preinst" "$pkg/DEBIAN/postrm"
dpkg-deb --root-owner-group -Zxz --build "$pkg" "$STAGE/debs/openfw-firmware_${VERSION}_all.deb" >/dev/null

# -- vf2-integration: session, first boot, self-test, board tools ----------
pkg=$WORK/vf2-integration
mkdir -p "$pkg"
cp -a "$HERE/overlay/." "$pkg/"
mkdir -p "$pkg/usr/lib/vf2/board" "$pkg/usr/share/doc/vf2-integration" "$pkg/DEBIAN"
for f in vf2-gpu-check.sh openfw-test.sh bench.sh setup.sh install-kernel.sh vf2-regs.py power-ab-test.sh; do
	install -m 0755 "$TOP/board/$f" "$pkg/usr/lib/vf2/board/$f"
done
install -m 0644 "$TOP/board/cts.md" "$pkg/usr/lib/vf2/board/cts.md"
install -m 0644 "$HERE/README.md" "$pkg/usr/share/doc/vf2-integration/README.md"
install -m 0644 "$TOP/docs/boot.md" "$pkg/usr/share/doc/vf2-integration/boot.md"
(cd "$pkg" && find etc -type f | sed 's|^|/|' | sort) >"$pkg/DEBIAN/conffiles"
cat >"$pkg/DEBIAN/control" <<EOF
Package: vf2-integration
Version: $VERSION
Architecture: all
Section: misc
Priority: optional
Maintainer: VisionFive 2 open GPU image <root@localhost>
Depends: systemd, cloud-guest-utils, fdisk, e2fsprogs, lsof, openfw-firmware
Description: VisionFive 2 open GPU stack: desktop session, first boot, self-test
 The labwc session with GPU compositing on the PowerVR BXE-4-32 (through
 Zink), the first-boot service that grows the root filesystem, the GPU
 self-test, desktop defaults and the board test scripts.
EOF
cat >"$pkg/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
	systemctl enable vf2-firstboot.service vf2-selftest.service >/dev/null 2>&1 || true
fi
EOF
chmod 755 "$pkg/DEBIAN/postinst"
dpkg-deb --root-owner-group -Zxz --build "$pkg" "$STAGE/debs/vf2-integration_${VERSION}_all.deb" >/dev/null

cp "$KDEB" "$STAGE/debs/"
cp "$HERE/customize.sh" "$STAGE/"
cp "$PACKAGES" "$STAGE/packages.txt"
cp "$PACKAGES_OPT" "$STAGE/packages-optional.txt"
: >"$STAGE/skipped"
cat >"$STAGE/build-info" <<EOF
image:      $NAME
built:      $(date -u '+%F %T') UTC
repository: $GITREV
kernel:     $(basename "$KDEB")
firmware:   openfw, sha256 $FW_SHA256
archive:    $MIRROR $SUITE $COMPONENTS
EOF
cat >"$STAGE/config" <<EOF
VF2_HOSTNAME='$VF2_HOSTNAME'
USERNAME='$USERNAME'
PASSWORD='$PASSWORD'
FSUUID='$FSUUID'
DTB='$DTB'
FW_SHA256='$FW_SHA256'
SUITE='$SUITE'
MIRROR='$MIRROR'
COMPONENTS='$COMPONENTS'
SIGNED_BY='$SIGNED_BY'
ALLOW_MISSING='$ALLOW_MISSING'
EOF

# -- root filesystem ---------------------------------------------------------
say "building the $SUITE root filesystem (riscv64)"
# shellcheck disable=SC2016  # "$1" is expanded by mmdebstrap (the chroot)
mmdebstrap --mode=root --architectures=riscv64 --variant=important \
	--components="$COMPONENTS" --keyring="$KEYRING" \
	--include=ca-certificates,apt-utils \
	--customize-hook="copy-in $STAGE /tmp" \
	--customize-hook='chroot "$1" /bin/sh /tmp/vf2-stage/customize.sh' \
	--customize-hook="sync-out /usr/share/doc/vf2-integration $WORK/doc" \
	"$SUITE" "$ROOT" "deb $MIRROR $SUITE $COMPONENTS"

# -- disk image: GPT, one ext4 partition marked bootable ---------------------
used=$(du -s -x -B1M "$ROOT" | cut -f1)
fs_mib=$(((used * 125 / 100 + 1024 + 63) / 64 * 64))
say "root filesystem: ${used} MiB used, ${fs_mib} MiB partition"
# U-Boot's ext4 driver predates some newer mke2fs defaults: leave out
# metadata_csum_seed and orphan_file so any mainline U-Boot can read it.
mkfs() {
	mke2fs -q -F -t ext4 -L vf2-root -U "$FSUUID" -m 1 -E root_owner=0:0 "$@" \
		-d "$ROOT" "$WORK/root.ext4" "${fs_mib}M"
}
mkfs -O ^metadata_csum_seed,^orphan_file 2>/dev/null || mkfs -O ^metadata_csum_seed
e2fsck -fn "$WORK/root.ext4" >/dev/null || die "the new filesystem does not check clean"

img=$WORK/$NAME.img
truncate -s $(((PART_START_MIB + fs_mib + 1) * 1024 * 1024)) "$img"
sfdisk -q "$img" <<EOF
label: gpt
start=${PART_START_MIB}MiB, size=${fs_mib}MiB, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, uuid=$PARTUUID, name=vf2-root, attrs=LegacyBIOSBootable
EOF
dd if="$WORK/root.ext4" of="$img" bs=1M seek="$PART_START_MIB" conv=notrunc,sparse status=none
rm -f "$WORK/root.ext4"
sfdisk -d "$img"

say "compressing"
if command -v pigz >/dev/null; then pigz -9 -c "$img"; else gzip -9 -c "$img"; fi >"$OUT/$NAME.img.gz"
# BSD format ("SHA256 (file) = ..."): OpenBSD's sha256 -C and GNU
# sha256sum -c both read it.
(cd "$OUT" && sha256sum --tag "$NAME.img.gz" >"$NAME.img.gz.sha256")
cp "$WORK/doc/packages" "$OUT/$NAME.packages"
# The packages the image adds to Debian, for installing on an existing system
mkdir -p "$OUT/debs"
cp "$STAGE/debs/"*.deb "$OUT/debs/"
{
	cat "$STAGE/build-info"
	echo "size:       $(stat -c %s "$img") bytes uncompressed (grows to fill the disk on first boot)"
	echo "skipped optional packages: $(tr '\n' ' ' <"$WORK/doc/skipped-packages")"
} >"$OUT/$NAME.build-info"
cat "$OUT/$NAME.build-info"
say "image: $OUT/$NAME.img.gz"

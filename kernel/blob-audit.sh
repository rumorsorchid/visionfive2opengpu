#!/bin/sh
# SPDX-License-Identifier: MIT
#
# blob-audit.sh - check that a kernel built by build.sh can load no
# firmware except openfw, the open GPU firmware.
#
#   ./kernel/blob-audit.sh OUT          (OUT: the build directory)
#
# Mainline Linux ships no firmware files, but drivers ask userspace for
# them at runtime. linux-libre patches those requests out; it would also
# block the PowerVR firmware by name, which here is openfw (MIT). So the
# kernel is kept blob-free by configuration instead
# (config/vf2-libre-desktop.config), and this script proves it on what
# was actually linked: every member of vmlinux.a and every module in
# modules.order that calls a firmware-loading function, plus every
# firmware name a module or built-in driver declares.
#
# Allowed:
#   powervr           loads powervr/rogue_<BVNC>_v1.fw; on the JH7110 only
#                     rogue_36.50.54.182_v1.fw, which the image fills
#                     with openfw (other names in its table belong to
#                     other SoCs' GPUs)
#   devlink, ethtool  flash a file to a device only when a user runs
#                     "devlink dev flash" / "ethtool --flash-module-firmware"
#                     and names the file; they never fetch one on their own
#
# Exit status 0 when clean; 1 and a list otherwise.

set -eu
OUT=${1:?usage: $0 OUT}
case $(uname -m) in
riscv64) NM=${NM:-nm} ;;
*) NM=${NM:-${CROSS_COMPILE:-riscv64-linux-gnu-}nm} ;;
esac

FWAPI=' (request_firmware|request_firmware_direct|request_firmware_nowait|request_firmware_into_buf|request_partial_firmware_into_buf|firmware_request_nowarn|firmware_request_platform|firmware_request_cache|firmware_upload_register)$'
ALLOWED_OBJS='^(drivers/gpu/drm/imagination/|net/devlink/dev\.o$|net/ethtool/module\.o$)'
bad=0

# Built-in code: members of vmlinux.a that call a loader.
OUTABS=$(cd "$OUT" && pwd)
for o in $("$NM" -u -A "$OUT/vmlinux.a" 2>/dev/null | grep -E "$FWAPI" |
		sed 's/^.*vmlinux\.a://; s/:.*//' | sed "s|^$OUTABS/||; s|^\./||" | sort -u); do
	if echo "$o" | grep -qE "$ALLOWED_OBJS"; then
		echo "allowed  built-in $o"
	else
		echo "BLOB     built-in $o requests firmware"
		bad=1
	fi
done

# Modules that are part of this configuration.
while read -r m; do
	ko=$OUT/${m%.o}.ko
	[ -f "$ko" ] || continue
	"$NM" -u "$ko" | grep -qE "$FWAPI" || continue
	case $m in
	drivers/gpu/drm/imagination/*) echo "allowed  module   $m" ;;
	*) echo "BLOB     module   $m requests firmware"; bad=1 ;;
	esac
done <"$OUT/modules.order"

# Declared firmware names (MODULE_FIRMWARE).
if [ -f "$OUT/modules.builtin.modinfo" ]; then
	decl=$(tr '\0' '\n' <"$OUT/modules.builtin.modinfo" | grep '\.firmware=' || true)
	if [ -n "$decl" ]; then
		echo "BLOB     built-in drivers declare firmware:"
		echo "$decl" | sed 's/^/           /'
		bad=1
	fi
fi

if [ "$bad" = 0 ]; then
	echo "blob audit: clean (only openfw can be loaded)"
else
	echo "blob audit: FAILED; disable the drivers above in config/vf2-libre-desktop.config" >&2
fi
exit "$bad"

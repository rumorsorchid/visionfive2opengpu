#!/bin/sh
# SPDX-License-Identifier: MIT
#
# bench.sh - reproducible benchmark run on the VisionFive 2.
#
#   ./bench.sh headless          vkmark offscreen (no root needed)
#   sudo ./bench.sh kms          vkmark direct to HDMI (stop your DM first)
#   ./bench.sh wayland           vkmark + glmark2 (Zink) inside a compositor
#
# Results go to bench-<mode>-<date>.txt together with the stack versions so
# runs from different kernels, firmware and Mesa versions can be compared.

set -u
MODE=${1:-headless}
SIZE=${SIZE:-1920x1080}
DUR=${DUR:-20}
OUT=bench-$MODE-$(date +%Y%m%d-%H%M%S).txt
export PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1 MESA_VK_DEVICE_SELECT=1010:36054182

{
	echo "kernel:   $(uname -r)"
	echo "firmware: $(dmesg 2>/dev/null | grep -o 'FW version v[0-9.]* (build [0-9]*' | tail -1)"
	echo "mesa:     $(vulkaninfo --summary 2>/dev/null | awk -F= '/driverInfo/ {print $2; exit}')"
	echo "gpu clk:  $(cat /sys/kernel/debug/clk/gpu_core/clk_rate 2>/dev/null || echo unknown) Hz"
	echo "mode:     $MODE  size: $SIZE  duration: ${DUR}s"
	echo
} >"$OUT"

SCENES=""
for s in clear cube shading desktop effect2d texture vertex; do
	SCENES="$SCENES -b $s:duration=$DUR"
done

case $MODE in
headless)
	# shellcheck disable=SC2086
	vkmark --winsys headless -s "$SIZE" $SCENES 2>&1 | tee -a "$OUT" ;;
kms)
	# shellcheck disable=SC2086
	vkmark --winsys kms --winsys-options kms-tty=/dev/tty1 -s "$SIZE" $SCENES 2>&1 | tee -a "$OUT" ;;
wayland)
	[ -n "${WAYLAND_DISPLAY:-}" ] || { echo "run inside a Wayland session"; exit 1; }
	# shellcheck disable=SC2086
	vkmark --winsys wayland -s "$SIZE" $SCENES 2>&1 | tee -a "$OUT"
	glmark2-es2-wayland -s "$SIZE" 2>&1 | tee -a "$OUT" ;;
*)
	echo "unknown mode $MODE"; exit 1 ;;
esac

echo "dmesg timeouts/resets during run: $(dmesg 2>/dev/null | grep -c 'Job timeout\|FW hard reset')" | tee -a "$OUT"
echo "results: $OUT"

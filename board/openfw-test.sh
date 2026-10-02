#!/bin/sh
# SPDX-License-Identifier: MIT
#
# openfw-test.sh - try the open firmware (openfw/) on the board, then put
# the previous firmware back.
#
#   sudo ./openfw-test.sh path/to/openfw/rogue_36.50.54.182_v1.fw [--no-jobs]
#
# Stage 1: the driver probes ("FW version v1.0 (build 0 OS)"), the
# firmware answers health checks, MMU flushes and power requests, and the
# GPU runtime-suspends and resumes (each resume boots the firmware again).
# Stage 2 (unless --no-jobs): real work through Mesa's Vulkan driver -
# vulkaninfo, short vkmark scenes (clears, geometry + fragment, texture
# uploads through transfer jobs, blending, a 1080p scene that grows the
# parameter buffer) and, when installed, a dEQP-VK smoke/compute subset.
# The GPU is allowed to suspend between workloads. Each step checks its
# exit status and the kernel log for job timeouts and firmware resets.
# Close anything using the GPU (compositor, vkcube, ...) before running.
#
# Log: openfw-test-<date>.txt. Exit status: number of FAILed checks.

set -u
NEW=${1:-}
JOBS=1
[ "${2:-}" = --no-jobs ] && JOBS=0
FWDIR=/lib/firmware/powervr
FW=$FWDIR/rogue_36.50.54.182_v1.fw
SAVED=$FWDIR/rogue_36.50.54.182_v1.fw.before-openfw
GPU=/sys/devices/platform/soc/18000000.gpu
LOG=openfw-test-$(date +%Y%m%d-%H%M%S).txt
FAILS=0

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
[ -f "$NEW" ] || { echo "usage: $0 openfw/rogue_36.50.54.182_v1.fw [--no-jobs]"; exit 1; }

exec 3>"$LOG"
log()  { echo "$*"; echo "$*" >&3; }
pass() { log "PASS  $*"; }
fail() { log "FAIL  $*"; FAILS=$((FAILS + 1)); }

# shellcheck disable=SC2317  # invoked via trap
restore() {
	log ""
	log "== restoring the previous firmware"
	modprobe -r powervr 2>/dev/null
	if [ -f "$SAVED" ]; then
		mv -f "$SAVED" "$FW"
	else
		rm -f "$FW"
	fi
	modprobe powervr && log "powervr reloaded with $(sha256sum "$FW" 2>/dev/null | cut -c1-16)..."
}
trap restore EXIT INT TERM

if lsof /dev/dri/renderD* >/dev/null 2>&1; then
	log "something has a render node open:"
	lsof /dev/dri/renderD* | tee -a "$LOG"
	trap - EXIT
	exit 1
fi

log "== installing $NEW"
[ -f "$FW" ] && cp -p "$FW" "$SAVED"
install -m 0644 "$NEW" "$FW"
log "sha256 $(sha256sum "$FW" | cut -d' ' -f1)"

modprobe -r powervr 2>/dev/null
mark="openfw-test-$$"
echo "$mark" > /dev/kmsg
# Trace groups MAIN | MTS | POW | HWR | DBG (ROGUE_FWIF_LOG_TYPE_GROUP_*)
modprobe powervr exp_hw_support=1 init_fw_trace_mask=0x80000606 || fail "modprobe powervr"
sleep 2

kmsg() { dmesg | sed -n "/$mark/,\$p"; }

log ""
log "== probe"
kmsg | grep -i powervr >&3
if kmsg | grep -q "FW version v1.0 (build 0 OS)"; then
	pass "driver accepted openfw"
else
	fail "driver did not report the openfw version (see log)"
fi
if kmsg | grep -qi "Firmware failed to boot\|failed to boot"; then
	fail "firmware failed to boot"
else
	pass "firmware booted (firmware_started handshake)"
fi

dri=""
for d in /sys/kernel/debug/dri/*; do
	[ -d "$d/pvr_fw" ] && dri=$d
done

trace() {
	[ -n "$dri" ] && cat "$dri/pvr_fw/trace_0" 2>/dev/null
}

log ""
log "== runtime PM (each resume boots the firmware, each suspend is"
log "   a FORCED_IDLE + OFF request handshake)"
for i in 1 2 3; do
	echo on > "$GPU/power/control"
	sleep 1
	st=$(cat "$GPU/power/runtime_status")
	if [ "$st" = active ]; then pass "cycle $i: resumed"; else fail "cycle $i: runtime_status=$st after resume"; fi
	echo auto > "$GPU/power/control"
	sleep 2
	st=$(cat "$GPU/power/runtime_status")
	if [ "$st" = suspended ]; then pass "cycle $i: suspended"; else fail "cycle $i: runtime_status=$st (expected suspended)"; fi
done

# -- stage 2: jobs ---------------------------------------------------------------
resets() { kmsg | grep -ciE 'job timeout|timed out|FW hard reset|device lost'; }

# run NAME TIMEOUT COMMAND...: one workload, then let the GPU suspend
run() {
	name=$1; secs=$2; shift 2
	before=$(resets)
	log ""
	log "-- $name: $*"
	if timeout "$secs" "$@" >>"$LOG" 2>&1; then rc=0; else rc=$?; fi
	after=$(resets)
	if [ "$rc" = 0 ] && [ "$after" = "$before" ]; then
		pass "$name"
	else
		fail "$name (exit $rc, $((after - before)) timeout/reset messages)"
		log "   firmware trace (last 40 lines):"
		echo on > "$GPU/power/control"; sleep 1
		trace | tail -n 40 >&3
		echo auto > "$GPU/power/control"
	fi
	sleep 3		# runtime suspend between workloads
}

if [ "$JOBS" = 1 ]; then
	log ""
	log "== jobs"
	export PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1 MESA_VK_DEVICE_SELECT=1010:36054182
	if command -v vulkaninfo >/dev/null; then
		run "vulkaninfo" 60 vulkaninfo --summary
	else
		log "SKIP  vulkaninfo not installed (apt install vulkan-tools)"
	fi
	if command -v vkmark >/dev/null; then
		for scene in clear cube texture shading desktop effect2d; do
			run "vkmark $scene 640x480" 120 vkmark --winsys headless -s 640x480 \
				-b "$scene:duration=5"
		done
		run "vkmark cube 1920x1080 (parameter buffer growth)" 180 \
			vkmark --winsys headless -s 1920x1080 -b cube:duration=10 -b desktop:duration=10
	else
		log "SKIP  vkmark not installed"
	fi
	if command -v deqp-vk >/dev/null; then
		for cases in 'dEQP-VK.api.smoke.*' 'dEQP-VK.compute.pipeline.basic.*'; do
			run "$cases" 900 deqp-vk --deqp-log-images=disable \
				--deqp-log-filename=/tmp/openfw-deqp.qpa --deqp-case="$cases"
		done
	fi
fi

log ""
log "== kernel messages"
kmsg >&3
if kmsg | grep -qi "device lost\|FW hard reset\|timed out\|KCCB.*stall"; then
	fail "driver reported a firmware problem (see log)"
else
	pass "no watchdog/lost-device messages"
fi

log ""
log "== firmware trace"
echo on > "$GPU/power/control"; sleep 1
trace >&3 || log "(no debugfs trace: mount debugfs and enable CONFIG_DEBUG_FS)"
trace | tail -n 20
echo auto > "$GPU/power/control"

log ""
log "$FAILS check(s) failed; log in $LOG"
exit "$FAILS"

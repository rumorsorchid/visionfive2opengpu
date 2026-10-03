#!/bin/sh
# SPDX-License-Identifier: MIT
#
# vf2-gpu-check.sh - verify the VisionFive 2 GPU + HDMI stack and write a
# report you can attach to a bug report.
#
#   sudo ./vf2-gpu-check.sh            checks only
#   sudo ./vf2-gpu-check.sh --run      also runs short vulkaninfo/vkmark tests
#
# Exit status: number of FAILed checks.

RUN_TESTS=0
[ "$1" = "--run" ] && RUN_TESTS=1

FW=/lib/firmware/powervr/rogue_36.50.54.182_v1.fw
FW_V11_SHA="" # fill in once you have verified the v1.1 build 6976702 image
GPU_DEV=/sys/devices/platform/soc/18000000.gpu
REPORT=vf2-gpu-report-$(date +%Y%m%d-%H%M%S).txt
FAILS=0

exec 3>"$REPORT"
log()  { echo "$*"; echo "$*" >&3; }
pass() { log "PASS  $*"; }
warn() { log "WARN  $*"; }
fail() { log "FAIL  $*"; FAILS=$((FAILS + 1)); }
section() { log ""; log "== $*"; }
capture() { echo "\$ $*" >&3; "$@" >&3 2>&1; }

section "system"
log "kernel: $(uname -r)"
log "model:  $(tr -d '\0' < /proc/device-tree/model 2>/dev/null)"
log "cmdline: $(cat /proc/cmdline)"
grep -q 'starfive,jh7110' /proc/device-tree/compatible 2>/dev/null \
	&& pass "JH7110 device tree" || fail "not a JH7110 device tree"

section "cache coherency (XPbmtUC)"
if dmesg | grep -q 'Using XPbmtUC bit 32'; then
	pass "XPbmtUC active (uncached alias via PTE bit 32)"
else
	fail "XPbmtUC not active: CONFIG_ERRATA_SIFIVE_XPBMTUC missing? GPU completions will be lost"
fi
cma=$(grep -i '^CmaTotal' /proc/meminfo | awk '{print $2}')
if [ "${cma:-0}" -ge 131072 ]; then
	pass "CMA ${cma} kB"
else
	warn "CMA ${cma:-0} kB; display buffers want >= 128 MiB (add cma=256M)"
fi

section "firmware"
if [ -f "$FW" ]; then
	sha=$(sha256sum "$FW" | cut -d' ' -f1)
	log "sha256 $sha"
	# openfw's published build: next to this script in the repository,
	# or from the openfw-firmware package on the image
	openfw_sha=$(cat "$(dirname "$0")/../openfw/prebuilt/SHA256SUMS" \
		/usr/share/doc/openfw-firmware/SHA256SUMS 2>/dev/null |
		awk '/rogue_36.50.54.182_v1.fw/ { print $1; exit }')
	case "$sha" in
	"$openfw_sha")
		pass "FW is openfw, the open firmware (published build)" ;;
	b5232ac64c0c708ee66400f40033ba4da8895a04ae688bc14821f59c5d4a6326)
		warn "FW is v1.0 build 6503725; the tested build is v1.1 build 6976702" ;;
	"$FW_V11_SHA")
		pass "FW is the tested v1.1 build" ;;
	*)
		log "unknown FW image; run tools/pvrfw.py info on it" ;;
	esac
else
	fail "$FW missing"
fi
fwline=$(dmesg | grep -o 'FW version v[0-9.]* (build [0-9]* OS)' | tail -1)
[ -n "$fwline" ] && pass "driver loaded firmware: $fwline" || warn "no 'FW version' line in dmesg yet"
case $fwline in *"(build 0 OS)"*) log "(build 0 is openfw)" ;; esac

section "modules"
builtin=/lib/modules/$(uname -r)/modules.builtin
for m in powervr verisilicon_dc jh7110_inno_hdmi phy_jh7110_inno_hdmi jh7110_vout_subsystem jh7110_hdmi_subsystem; do
	if lsmod | grep -q "^${m} "; then
		pass "module $m loaded"
	elif [ -r "$builtin" ] && sed 's,.*/,,; s,\.ko$,,; s,-,_,g' "$builtin" | grep -qx "$m"; then
		pass "$m built into the kernel"
	else
		warn "module $m not loaded"
	fi
done
log "powervr params:"
for p in exp_hw_support jh7110_power_event kernel_heap_guards; do
	[ -r /sys/module/powervr/parameters/$p ] && log "  $p=$(cat /sys/module/powervr/parameters/$p)"
done

section "DRM devices"
capture ls -l /dev/dri
for c in /sys/class/drm/card*; do
	[ -e "$c/device/driver" ] || continue
	log "$(basename "$c"): $(basename "$(readlink -f "$c/device/driver")")"
done
render_found=0
for r in /sys/class/drm/renderD*; do [ -e "$r" ] && render_found=1; done
[ $render_found = 1 ] && pass "render node present" || fail "no render node"
hdmi=
for h in /sys/class/drm/card*-HDMI-A-1; do [ -e "$h" ] && hdmi=$h; done
if [ -n "$hdmi" ]; then
	st=$(cat "$hdmi/status")
	[ "$st" = connected ] && pass "HDMI connected" || warn "HDMI status: $st"
	log "modes: $(tr '\n' ' ' < "$hdmi/modes")"
else
	fail "no HDMI connector"
fi

section "GPU power and clocks"
if [ -d "$GPU_DEV" ]; then
	log "runtime_status: $(cat $GPU_DEV/power/runtime_status)"
	for c in gpu_core gpu_root pll2_out pll1_out; do
		f=/sys/kernel/debug/clk/$c/clk_rate
		[ -r "$f" ] && log "clk $c: $(cat $f) Hz"
	done
else
	fail "no GPU device at $GPU_DEV (DT node missing?)"
fi
if command -v python3 >/dev/null && [ -x "$(dirname "$0")/vf2-regs.py" ]; then
	echo "--- PMU registers" >&3
	python3 "$(dirname "$0")/vf2-regs.py" pmu >&3 2>&1
fi

if [ $RUN_TESTS = 1 ]; then
	section "tests"
	export PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1 MESA_VK_DEVICE_SELECT=1010:36054182
	if command -v vulkaninfo >/dev/null; then
		vulkaninfo --summary >&3 2>&1
		vulkaninfo --summary 2>/dev/null | grep -q 'BXE-4-32' \
			&& pass "Vulkan device BXE-4-32 enumerated" || fail "Vulkan device not enumerated"
	else
		warn "vulkaninfo not installed (apt install vulkan-tools)"
	fi
	if command -v vkmark >/dev/null; then
		before=$(dmesg | grep -c 'Job timeout\|FW hard reset')
		vkmark --winsys headless -s 640x480 -b cube:duration=10 >&3 2>&1 \
			&& pass "vkmark headless cube completed" || fail "vkmark failed"
		after=$(dmesg | grep -c 'Job timeout\|FW hard reset')
		[ "$after" = "$before" ] && pass "no job timeouts / FW resets during vkmark" \
			|| fail "$((after - before)) job timeouts / FW resets during vkmark"
	else
		warn "vkmark not installed"
	fi
fi

section "kernel log (powervr, verisilicon, hdmi)"
dmesg | grep -iE 'powervr|pvr|verisilicon|dc8200|inno|hdmi|xpbmt' >&3
errs=$(dmesg | grep -ciE 'powervr.*(fault|timeout|hard reset|error)')
[ "$errs" = 0 ] && pass "no powervr faults/timeouts in dmesg" || warn "$errs powervr fault/timeout lines in dmesg"

log ""
log "report written to $REPORT ($FAILS failures)"
exit $FAILS

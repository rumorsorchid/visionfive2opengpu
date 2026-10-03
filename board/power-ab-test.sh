#!/bin/sh
# SPDX-License-Identifier: MIT
#
# power-ab-test.sh - find out which mechanism really ungates the BXE-4-32
# rascal/dust power island on the JH7110. See docs/power.md.
#
#   A  jh7110_power_event=1   firmware-derived host sequence
#   L  jh7110_power_event=2   community single write (known good, default)
#   B  jh7110_power_event=0   no host power-up (expected to fail)
#   C  as B + PMU HW-event GPU bit unmasked in 0x04/0x08 (DDK glue)
#   D  as B + rd_power_island=1 (firmware manages the island, like the DDK)
#   E  as D + PMU HW-event GPU bit unmasked
#
# Each case reloads powervr, runs a 5 s headless vkmark cube and counts job
# timeouts / FW hard resets. Run as root with no compositor using the GPU.
# A failing case can wedge the GPU until reboot; results so far are kept in
# the log, so reboot and re-run the remaining cases, e.g. "$0 C D E".

set -u
HERE=$(dirname "$0")
REGS="python3 $HERE/vf2-regs.py"
LOG=power-ab-$(date +%Y%m%d-%H%M%S).log
CASES=${*:-A L D E C B}
export PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1 MESA_VK_DEVICE_SELECT=1010:36054182

say() { echo "$*" | tee -a "$LOG"; }

reload() {
	modprobe -r powervr 2>/dev/null
	sleep 1
	modprobe powervr exp_hw_support=1 jh7110_power_event="$1" \
		rd_power_island="${2:-0}" || return 1
	sleep 2
}

run_case() {
	name=$1 pe=$2 pmu=$3 rd=$4
	say ""
	say "=== case $name: jh7110_power_event=$pe rd_power_island=$rd, PMU GPU event unmasked=$pmu"
	if [ "$pmu" = 1 ]; then
		on=$($REGS pmu | awk '/HW_EVENT_TURN_ON_MASK/ {print $3}')
		off=$($REGS pmu | awk '/HW_EVENT_TURN_OFF_MASK/ {print $3}')
		$REGS pmu-set 0x04 $((on & ~0x80)) | tee -a "$LOG"
		$REGS pmu-set 0x08 $((off & ~0x80)) | tee -a "$LOG"
	fi
	$REGS pmu >>"$LOG" 2>&1
	dmesg -C
	if ! reload "$pe" "$rd"; then
		say "case $name: modprobe failed"
		return
	fi
	timeout 60 vkmark --winsys headless -s 320x240 -b cube:duration=5 >>"$LOG" 2>&1
	rc=$?
	bad=$(dmesg | grep -c 'Job timeout\|FW hard reset\|POWER_EVENT timed out')
	dmesg | grep -i powervr >>"$LOG"
	$REGS pmu >>"$LOG" 2>&1
	if [ $rc = 0 ] && [ "$bad" = 0 ]; then
		say "case $name: PASS"
	else
		say "case $name: FAIL (vkmark rc=$rc, $bad timeout/reset lines)"
	fi
	if [ "$pmu" = 1 ]; then
		$REGS pmu-set 0x04 "$on" >>"$LOG"
		$REGS pmu-set 0x08 "$off" >>"$LOG"
	fi
}

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
for c in $CASES; do
	case $c in
	A) run_case A 1 0 0 ;;
	L) run_case L 2 0 0 ;;
	B) run_case B 0 0 0 ;;
	C) run_case C 0 1 0 ;;
	D) run_case D 0 0 1 ;;
	E) run_case E 0 1 1 ;;
	*) echo "unknown case $c" ;;
	esac
done
reload 1
say ""
say "log: $LOG  - please share it (e.g. in an issue on this repository)"

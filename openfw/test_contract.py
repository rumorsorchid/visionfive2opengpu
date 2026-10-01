#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
test_contract.py - check a firmware image against what the Linux powervr
driver needs from it, in tools/fwemu.

The driver (v7.3) only ever looks at a handful of things the firmware
writes: sysinit.firmware_started after boot, the kernel CCB read offset,
the CMD_EXECUTED bit of the return slot for commands it waits on (MMU
cache, log type, cleanup), osdata.kccb_cmds_executed (watchdog),
sysdata.pow_state == IDLE (runtime suspend), power_sync for power
requests, and, after a power-off request, an idle MIPS core; on runtime
resume it boots the same image again. This test drives the sequence the
driver does at probe, runtime suspend and resume and checks each step.

    test_contract.py FW.fw --kernel LINUX [FW2.fw ...]

Run it on openfw and on Imagination's image to compare.
"""
import argparse
import io
import os
import sys
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, os.path.join(HERE, "..", "tools", "fwemu"))
import fwemu  # noqa: E402
import fwtrace  # noqa: E402
from fwregs import load_cr_names  # noqa: E402
from pvrfw import Firmware, Tables  # noqa: E402

POW_OFF, POW_ON, POW_FORCED_IDLE, POW_IDLE = range(4)


def run(path, kernel):
    args = argparse.Namespace(config_flags=0, trace_mask=0x80007FFF, max_insns=2_000_000,
                              trace_regs=False, trace_exc=False, complete_polls=True,
                              kccb=[], watch=None)
    fw = Firmware(path, Tables(kernel))
    with redirect_stdout(io.StringIO()):
        emu = fwemu.Emu(fw, fwemu.Layout(os.path.join(HERE, "..", "tools", "fwemu",
                                                      "layout.json")),
                        load_cr_names(kernel), args)
    emu.sf_table = fwtrace.load_sf_table(kernel)
    results = []

    def check(cond, what):
        results.append((bool(cond), what))

    def quiet(fn, *a):
        with redirect_stdout(io.StringIO()):
            return fn(*a)

    started = quiet(emu.run)
    Y, C = "rogue_fwif_sysdata", "rogue_fwif_ccb_ctl"
    sysdata = emu.objects["sysdata"][0]
    osdata = emu.objects["osdata"][0]
    power_sync = emu.objects["power_sync"][0]

    def pow_state():
        return emu.get(Y, sysdata, "pow_state")

    def executed():
        return emu.get("rogue_fwif_osdata", osdata, "kccb_cmds_executed")

    def kccb(name):
        """Submit like pvr_kccb_send_cmd + MTS kick; return (slot, rtn)."""
        before = executed()
        quiet(emu.kccb_scenario, name)
        slot = (emu.get(C, emu.kccb_ctl, "read_offset") - 1) & ((1 << emu.kccb_n) - 1)
        check(emu.get(C, emu.kccb_ctl, "read_offset") == emu.get(C, emu.kccb_ctl, "write_offset"),
              "%s: kernel CCB drained" % name)
        check(executed() == before + 1, "%s: kccb_cmds_executed advanced" % name)
        return emu.r32(emu.kccb_rtn + 4 * slot)

    check(started == 1, "boot: firmware_started = 1")
    check(pow_state() == POW_ON, "boot: pow_state = ON")

    # Watchdog health check; afterwards the firmware should report idle
    # so runtime PM can suspend the GPU.
    kccb("health")
    check(pow_state() == POW_IDLE, "health: pow_state = IDLE")
    # Commands the driver waits on (pvr_kccb_wait_for_completion)
    for name in ("mmucache", "logtype"):
        check(kccb(name) & 1, "%s: return slot CMD_EXECUTED" % name)
    # Runtime suspend: pvr_power_fw_disable()
    emu.w32(power_sync, 0)
    kccb("pow-idle")
    check(emu.r32(power_sync) != 0, "forced idle: power_sync set")
    check(pow_state() == POW_FORCED_IDLE, "forced idle: pow_state = FORCED_IDLE")
    emu.w32(power_sync, 0)
    kccb("pow-off")
    check(emu.r32(power_sync) != 0, "power off: power_sync set")
    check(pow_state() == POW_OFF, "power off: pow_state = OFF")
    check(emu.parked, "power off: core parked in wait, interrupts off")
    # Runtime resume: pvr_power_fw_enable() restarts the same image; the
    # kernel CCB continues where it was.
    quiet(emu.kccb_scenario, "reboot")
    check(emu.get("rogue_fwif_sysinit", fwemu.SYSINIT_VA, "firmware_started") == 1,
          "resume: firmware boots again")
    check(pow_state() == POW_ON, "resume: pow_state = ON")
    kccb("health")
    check(pow_state() == POW_IDLE, "resume: health check answered, IDLE")
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fw", nargs="+")
    ap.add_argument("--kernel", required=True)
    args = ap.parse_args()
    table = {}
    for path in args.fw:
        table[path] = run(path, args.kernel)
    names = [w for _, w in table[args.fw[0]]]
    width = max(len(n) for n in names)
    print("%-*s  %s" % (width, "", "  ".join(os.path.basename(p)[:24] for p in args.fw)))
    failed = 0
    for i, name in enumerate(names):
        cells = []
        for path in args.fw:
            ok = table[path][i][0]
            failed += not ok
            cells.append(("PASS" if ok else "FAIL").ljust(min(24, len(os.path.basename(path)))))
        print("%-*s  %s" % (width, name, "  ".join(cells)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

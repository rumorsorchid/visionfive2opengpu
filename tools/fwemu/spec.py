#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
spec.py - functional register sequences of a firmware for each job
scenario, with bookkeeping writes (MTS task acknowledgements, firmware
TLB/remap maintenance, scratch registers) filtered out.

    spec.py FW --kernel LINUX compute transfer render > spec.txt
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jobs  # noqa: E402

NOISE = {
    0x0B00,            # MTS_SCHEDULE (task scheduling)
    0x0B08,            # MTS task done
    0x1A78,            # SCRATCH15 (firmware's own bookkeeping)
    0x0878, 0x087C,    # MIPS_ADDR_REMAP_RANGE_CONFIG (firmware MMU)
    # bank selects for the lockup checks' signature register reads (the
    # reads are not compared; how often a firmware re-reads is its own)
    0x8000, 0x8238, 0x83E0,
}


# Reads the firmware makes on every task entry/exit (idle checks, timers).
READ_NOISE = NOISE | {
    0x0160, 0x0164,    # TIMER
    0x0890,            # XPU_BROADCAST
    0x0130,            # EVENT_STATUS
    0x1290,            # BIF_MMU_ENTRY
    0x08F0,            # (per-task status poll)
    0x0020, 0x0024,    # CORE_ID
    0xF308,            # MULTICORE_SYSTEM
}


def functional(accesses):
    out = []
    for k, off, v in accesses:
        if k == "W" and off in NOISE:
            continue
        if k == "R" and off in READ_NOISE:
            continue
        if k == "W" and off == 0x0138 and v == 0x00080000:  # EVENT_CLEAR(SLAVE_REQ) per task
            continue
        out.append((k, off, v))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fw")
    ap.add_argument("scenario", nargs="+", choices=sorted(jobs.SCENARIOS))
    ap.add_argument("--kernel", required=True)
    ap.add_argument("--set", action="append", default=[], help="scenario input name=value")
    args = ap.parse_args()
    params = {k: int(v, 0) for k, _, v in (x.partition("=") for x in args.set)}
    for sc in args.scenario:
        r = jobs.Runner(args.fw, args.kernel, params=params)
        r.boot()
        res = jobs.SCENARIOS[sc](r)
        print("##### %s: %s" % (sc, res))
        for st in r.steps[1:]:
            st = dict(st, accesses=functional(st["accesses"]))
            print(r.describe(st, reads=True))


if __name__ == "__main__":
    main()

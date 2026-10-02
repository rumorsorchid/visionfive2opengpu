#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
sweep.py - which command fields change what the firmware does?

For each userspace field of a job command, runs the scenario with the
field set to a few values and reports every functional register access
that differs from the tagged baseline, other than the field's own value
being written somewhere (a plain copy). Fields that only get copied are
listed as "copied to"; the others are the ones the firmware interprets.

    sweep.py FW --kernel LINUX render rogue_fwif_cmd_frag [--values 0,0xffffffff]
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import host as H  # noqa: E402
import jobs  # noqa: E402
import spec  # noqa: E402


def trace(fw, kernel, sc, params):
    r = jobs.Runner(fw, kernel, params=params)
    r.boot()
    res = jobs.SCENARIOS[sc](r)
    acc = []
    for st in r.steps[1:]:
        acc += [(st["step"],) + a for a in spec.functional(st["accesses"])]
    return r, res, acc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fw")
    ap.add_argument("scenario", choices=sorted(jobs.SCENARIOS))
    ap.add_argument("struct")
    ap.add_argument("--kernel", required=True)
    ap.add_argument("--values", default="0,0xffffffffffffffff,0x0123456789abcdef")
    ap.add_argument("--field", action="append", help="only these fields")
    args = ap.parse_args()
    values = [int(v, 0) for v in args.values.split(",")]
    r0, res0, base = trace(args.fw, args.kernel, args.scenario, {})
    fields = args.field or [f for f, _ in H.STREAM_FIELDS.get(args.struct, [])]
    for f in fields:
        bits = dict(H.STREAM_FIELDS.get(args.struct, [])).get(f, 32)
        for v in values:
            v &= (1 << bits) - 1
            r, res, acc = trace(args.fw, args.kernel, args.scenario,
                                {"override": {args.struct: {f: v}}})
            halves = {v & 0xFFFFFFFF, v >> 32}
            if res != res0:
                print("%-40s = 0x%x: RESULT %s -> %s" % (f, v, res0, res))
            if len(acc) != len(base) or any(a[:3] != b[:3] for a, b in zip(acc, base)):
                print("%-40s = 0x%x: ACCESS SEQUENCE CHANGES (%d -> %d)" % (f, v, len(base),
                                                                         len(acc)))
                import difflib
                la = ["%s %s %05x" % (x[0], x[1], x[2]) for x in base]
                lb = ["%s %s %05x" % (x[0], x[1], x[2]) for x in acc]
                for line in list(difflib.unified_diff(la, lb, lineterm="", n=0))[2:30]:
                    print("      " + line)
                continue
            for a, b in zip(base, acc):
                if a[3] == b[3]:
                    continue
                if b[1] in ("W", "R") and b[3] in halves:
                    continue
                print("%-40s = 0x%x: %s %s %-28s +0x%05x 0x%08x -> 0x%08x" % (
                    f, v, b[0], b[1], r.reg_name(b[2]), b[2],
                    a[3] if not isinstance(a[3], tuple) else a[3][0],
                    b[3] if not isinstance(b[3], tuple) else b[3][0]))


if __name__ == "__main__":
    main()

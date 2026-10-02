#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
vary.py - which inputs does each register value depend on?

Runs a jobs.py scenario with default inputs and again with one input
changed, and lists the functional register accesses whose values differ
(and any difference in the access sequence itself).

    vary.py FW --kernel LINUX render shift=0x10000 pc=0x91234000 width=640
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jobs  # noqa: E402
import spec  # noqa: E402


def run(fw, kernel, sc, params):
    r = jobs.Runner(fw, kernel, params=params)
    r.boot()
    res = jobs.SCENARIOS[sc](r)
    steps = [(st["step"], spec.functional(st["accesses"]), st["mem"]) for st in r.steps[1:]]
    return r, res, steps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fw")
    ap.add_argument("scenario", choices=sorted(jobs.SCENARIOS))
    ap.add_argument("change", nargs="+", help="name=value (one run per change)")
    ap.add_argument("--kernel", required=True)
    args = ap.parse_args()
    ra, resa, base = run(args.fw, args.kernel, args.scenario, {})
    for ch in args.change:
        k, _, v = ch.partition("=")
        rb, resb, other = run(args.fw, args.kernel, args.scenario, {k: int(v, 0)})
        print("##### %s: %s -> %s" % (ch, resa, resb))
        for (sa, acc_a, mem_a), (sb, acc_b, mem_b) in zip(base, other):
            if len(acc_a) != len(acc_b) or any(x[:2] != y[:2] for x, y in zip(acc_a, acc_b)):
                print("== %s: access sequence differs (%d vs %d)" % (sa, len(acc_a), len(acc_b)))
                import difflib
                la = ["%s %05x" % (k_, o) for k_, o, _ in acc_a]
                lb = ["%s %05x" % (k_, o) for k_, o, _ in acc_b]
                for line in list(difflib.unified_diff(la, lb, lineterm="", n=1))[:40]:
                    print("   " + line)
                continue
            diffs = [(x, y) for x, y in zip(acc_a, acc_b) if x[2] != y[2]]
            if diffs:
                print("== %s" % sa)
            for (k_, off, va), (_, _, vb) in diffs:
                fmt = (lambda v: "0x%08x" % v) if not isinstance(va, tuple) else str
                print("  %s %-34s +0x%05x  0x%08x -> 0x%08x" % (
                    k_, ra.reg_name(off), off, va if not isinstance(va, tuple) else va[0],
                    vb if not isinstance(vb, tuple) else vb[0]))
                del fmt


if __name__ == "__main__":
    main()

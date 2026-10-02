#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
test_jobs.py - run openfw and Imagination's firmware through the same GPU
job scenarios in tools/fwemu and compare, step by step, what they do:

  * the register writes and polls (bookkeeping such as MTS task
    acknowledgements and firmware MMU maintenance filtered out, see
    tools/fwemu/spec.py; reads are not compared, the reference firmware
    reads extra state for its logs),
  * the host-visible memory they change (client CCB control, timeline
    UFOs, kernel CCB return slots, HWRT data, free lists, ...),
  * the scenario results (jobs completed, fence values, offsets).

    test_jobs.py openfw.fw REFERENCE.fw --kernel LINUX [-k NAME] [-v]

Exit status 0 when every case matches.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools", "fwemu"))
import jobs  # noqa: E402
import spec  # noqa: E402

# (name, scenario, scenario inputs)
CASES = [
    ("compute", "compute", {}),
    ("compute-chained", "compute2", {}),
    ("transfer", "transfer", {}),
    ("render", "render", {}),
    ("render-status", "render", {"status_tags": 1}),
    ("render-msaa4", "render", {"samples": 4}),
    ("render-small", "render", {"width": 64, "height": 64}),
    ("geom-only", "geom", {}),
    ("cleanup", "cleanup", {}),
]


def run(fw, kernel, scenario, params):
    r = jobs.Runner(fw, kernel, params=params)
    r.boot()
    res = jobs.SCENARIOS[scenario](r)
    steps = []
    for st in r.steps[1:]:
        acc = [a for a in spec.functional(st["accesses"]) if a[0] != "R"]
        steps.append((st["step"], acc, st["mem"]))
    return res, steps, r


def fmt(r, a):
    k, off, v = a
    if k == "P":
        return "P %-28s +0x%05x & 0x%08x == 0x%08x" % (r.reg_name(off), off, v[1], v[0])
    return "W %-28s +0x%05x = 0x%08x" % (r.reg_name(off), off, v)


def compare(name, a, b, verbose):
    (ra, sa, runa), (rb, sb, runb) = a, b
    errors = []
    if ra != rb:
        errors.append("result: openfw %s, reference %s" % (ra, rb))
    for i in range(max(len(sa), len(sb))):
        if i >= len(sa) or i >= len(sb):
            errors.append("step count differs: %d vs %d" % (len(sa), len(sb)))
            break
        (na, acc_a, mem_a), (nb, acc_b, mem_b) = sa[i], sb[i]
        if acc_a != acc_b:
            n = next((j for j in range(min(len(acc_a), len(acc_b))) if acc_a[j] != acc_b[j]),
                     min(len(acc_a), len(acc_b)))
            errors.append("step '%s': register access %d differs" % (na, n))
            for j in range(max(0, n - 3), min(max(len(acc_a), len(acc_b)), n + 4)):
                fa = fmt(runa, acc_a[j]) if j < len(acc_a) else "-"
                fb = fmt(runb, acc_b[j]) if j < len(acc_b) else "-"
                errors.append("  %s %-62s | %s" % ("!" if fa != fb else " ", fa, fb))
        for obj in sorted(set(mem_a) | set(mem_b)):
            if mem_a.get(obj) != mem_b.get(obj):
                errors.append("step '%s': memory %s differs" % (na, obj))
                if verbose:
                    errors.append("  openfw    %s" % (mem_a.get(obj, b"").hex()))
                    errors.append("  reference %s" % (mem_b.get(obj, b"").hex()))
    print("%-18s %s" % (name, "ok" if not errors else "FAIL"))
    for e in errors:
        print("    " + e)
    return not errors


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("openfw")
    ap.add_argument("reference")
    ap.add_argument("--kernel", required=True)
    ap.add_argument("-k", dest="only", action="append", help="run only cases containing NAME")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    ok = True
    for name, sc, params in CASES:
        if args.only and not any(s in name for s in args.only):
            continue
        a = run(args.openfw, args.kernel, sc, params)
        b = run(args.reference, args.kernel, sc, params)
        ok &= compare(name, a, b, args.verbose)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

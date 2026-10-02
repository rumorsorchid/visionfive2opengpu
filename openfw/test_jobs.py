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
import host as H  # noqa: E402
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
    ("frames", "frames", {}),
    ("frames-status", "frames", {"status_tags": 1}),
    ("frames-pipelined", "frames-pipelined", {}),
    ("frames-pipe-status", "frames-pipelined", {"status_tags": 1}),
    ("multivm", "multivm", {}),
    ("multivm-concurrent", "multivm-concurrent", {}),
    ("blocked", "blocked", {}),
    ("cleanup-busy", "cleanup-busy", {}),
    ("teardown", "teardown", {}),
    ("mixed", "mixed", {}),
    ("wrap", "wrap", {}),
    ("oom", "oom", {"oom": 1}),
    ("oom-status", "oom", {"oom": 1, "oom_regs": {0x20c8: 5, 0x20d8: 7, 0x20e0: 9,
                                                  0x348: 11, 0x3a0: 13, 0x2000: 15}}),
    ("multikick", "multikick", {}),
    ("multikick-status", "multikick", {"status_tags": 1}),
    ("oom-live", "oom-live", {"oom": 1}),
    ("oom-frames", "oom-frames", {}),
    ("oom-frames-status", "oom-frames", {"status_tags": 1}),
]

# Cases where openfw deliberately takes another route than the reference
# (a TA out of memory with no ready pages waits for the grow instead of
# being stored for a possible partial render): only the results and the
# memory both leave behind at the end are compared.
OUTCOME_CASES = [
    ("oom-twice", "oom", {"oom": 2}),
    ("oom-noready", "oom", {"oom": 1, "fl_threshold": 0}),
]


# --sweep: every userspace command field (pvr_stream_defs.c) and the
# kernel-set flags word, set to patterns that between them flip every bit.
SWEEP_SCENARIO = {"rogue_fwif_cmd_compute": "compute", "rogue_fwif_cmd_transfer": "transfer",
                  "rogue_fwif_cmd_geom": "render", "rogue_fwif_cmd_frag": "render"}
SWEEP_VALUES = (0, 0xFFFFFFFF, 0x55555555, 0xAAAAAAAA)
# flags bits pvr_job.c can set (convert_*_flags); the single-kick render
# scenario keeps FIRSTKICK | LASTKICK (multi-kick has its own scenario)
KERNEL_FLAGS = {"rogue_fwif_cmd_geom": (0x8, 0x3), "rogue_fwif_cmd_frag": (0x040881A8, 0),
                "rogue_fwif_cmd_compute": (0xFFFFFFFF, 0),
                "rogue_fwif_cmd_transfer": (0xFFFFFFFF, 0)}


def sweep_cases():
    for sname, sc in SWEEP_SCENARIO.items():
        for field, bits in H.STREAM_FIELDS[sname] + [("flags", 32)]:
            for v in SWEEP_VALUES:
                if bits == 64:
                    v |= v << 32
                if field == "regs.isp_render" and (v & 3) < 2:
                    continue        # Mesa only submits FAST_2D / FAST_SCALE transfers
                if field == "flags":
                    mask, fixed = KERNEL_FLAGS[sname]
                    v = (v & mask) | fixed
                yield ("%s.%s=%x" % (sname[11:], field, v), sc,
                       {"override": {sname: {field: v}}})


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


def compare_outcome(name, a, b):
    (ra, sa, _), (rb, sb, _) = a, b
    errors = []
    if ra != rb:
        errors.append("result: openfw %s, reference %s" % (ra, rb))
    final_a, final_b = {}, {}
    for _, _, mem in sa:
        final_a.update(mem)
    for _, _, mem in sb:
        final_b.update(mem)
    for obj in sorted(set(final_a) | set(final_b)):
        da, db = final_a.get(obj, b""), final_b.get(obj, b"")
        for off in range(0, max(len(da), len(db)), 4):
            if da[off:off + 4] != db[off:off + 4]:
                errors.append("final %s +0x%03x: openfw %s, reference %s" % (
                    obj, off, da[off:off + 4][::-1].hex(), db[off:off + 4][::-1].hex()))
    print("%-18s %s" % (name, "ok (outcome)" if not errors else "FAIL"))
    for e in errors:
        print("    " + e)
    return not errors


def compare(name, a, b, verbose):
    (ra, sa, runa), (rb, sb, runb) = a, b
    errors = []
    if ra != rb:
        errors.append("result: openfw %s, reference %s" % (ra, rb))
    for i in range(max(len(sa), len(sb))):
        if i >= len(sa) or i >= len(sb):
            errors.append("step count differs: %d vs %d" % (len(sa), len(sb)))
            break
        (na, acc_a, mem_a), (_, acc_b, mem_b) = sa[i], sb[i]
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
                da, db = mem_a.get(obj, b""), mem_b.get(obj, b"")
                for off in range(0, max(len(da), len(db)), 4):
                    wa, wb = da[off:off + 4], db[off:off + 4]
                    if wa != wb:
                        errors.append("  +0x%03x openfw %-10s reference %s" % (
                            off, wa[::-1].hex() or "-", wb[::-1].hex() or "-"))
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
    ap.add_argument("--sweep", action="store_true",
                    help="also sweep every command field over bit patterns")
    args = ap.parse_args()
    ok = True
    for name, sc, params in CASES:
        if args.only and not any(s in name for s in args.only):
            continue
        a = run(args.openfw, args.kernel, sc, params)
        b = run(args.reference, args.kernel, sc, params)
        ok &= compare(name, a, b, args.verbose)
    if args.sweep:
        n = bad = 0
        for name, sc, params in sweep_cases():
            if args.only and not any(s in name for s in args.only):
                continue
            n += 1
            if not compare(name, run(args.openfw, args.kernel, sc, params),
                           run(args.reference, args.kernel, sc, params), args.verbose):
                bad += 1
        print("sweep: %d of %d field values match" % (n - bad, n))
        ok &= not bad
    for name, sc, params in OUTCOME_CASES:
        if args.only and not any(s in name for s in args.only):
            continue
        ok &= compare_outcome(name, run(args.openfw, args.kernel, sc, params),
                              run(args.reference, args.kernel, sc, params))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

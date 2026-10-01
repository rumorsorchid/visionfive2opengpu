#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
fwregs.py - static census of GPU register accesses in a PowerVR MIPS
firmware image.

The MIPS firmware reaches the GPU's control registers through a fixed
virtual window (ROGUE_MIPSFW_REGISTERS_VIRTUAL_BASE = 0xCF800000). This
tool disassembles the microMIPS code sections with GNU objdump, tracks
registers loaded with that base (lui + addiu/ori/addu-immediate), and
records every load/store whose effective address falls inside the window.

    fwregs.py FW.fw --kernel LINUX_SRC [--reg 0x38] [--csv out.csv]

It is a heuristic: register values are tracked linearly within a function
and forgotten at labels that are branch targets, so accesses through
computed offsets (loops over register banks) are reported as "dynamic".
"""

import argparse
import collections
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pvrfw import Firmware, Tables  # noqa: E402

REG_WINDOW = 0xCF800000
WINDOW_SIZE = 0x00800000
LOADS = {"lw", "lhu", "lh", "lbu", "lb", "lwp", "lwl", "lwr", "lwxs", "lw16", "lwgp", "lwsp"}
STORES = {"sw", "sh", "sb", "swp", "swl", "swr", "sw16", "swsp"}
INSN_RE = re.compile(r"^\s*([0-9a-f]+):\s+(?:[0-9a-f]{4}\s?){1,2}\s+(\S+)\s*(.*)$")
MEM_RE = re.compile(r"(-?\d+|-?0x[0-9a-f]+)\((\w+)\)")


def load_cr_names(kernel):
    names = {}
    path = os.path.join(kernel, "drivers/gpu/drm/imagination/pvr_rogue_cr_defs.h")
    for m in re.finditer(r"#define ROGUE_CR_(\w+) (0x[0-9A-Fa-f]+)U\b", open(path).read()):
        name, val = m.group(1), int(m.group(2), 16)
        if re.search(r"_(SHIFT|CLRMSK|EN|MASKFULL|ALIGNSHIFT|ALIGNSIZE)$", name):
            continue
        if val % 4 == 0 and val < 0x100000:
            names.setdefault(val, name)
    names.setdefault(0x0038, "POWER_EVENT (not in Rogue defs)")
    return names


def disassemble(fw, tmpdir):
    out = []
    for e in fw.layout:
        if e["type"] != 1:  # FW_CODE
            continue
        blob = bytearray(e["alloc_size"])
        for p in fw.elf()["phdrs"]:
            if p["type"] == 1 and e["base"] <= p["vaddr"] < e["base"] + e["max_size"]:
                o = p["vaddr"] - e["base"]
                blob[o:o + p["filesz"]] = fw.data[p["offset"]:p["offset"] + p["filesz"]]
        path = os.path.join(tmpdir, "%08x.bin" % e["base"])
        open(path, "wb").write(blob)
        dis = subprocess.run(
            ["mipsel-linux-gnu-objdump", "-D", "-b", "binary", "-m", "mips:micromips", "-EL",
             "--adjust-vma=0x%x" % e["base"], path],
            check=True, capture_output=True, text=True).stdout
        out.extend(dis.splitlines())
    return out


def branch_targets(lines):
    targets = set()
    for line in lines:
        for t in re.findall(r"\b0x([0-9a-f]{8})\b", line):
            targets.add(int(t, 16) & ~1)
    return targets


def census(lines):
    targets = branch_targets(lines)
    known = {}  # reg -> value
    accesses = []
    for line in lines:
        m = INSN_RE.match(line)
        if not m:
            continue
        addr, mnem, ops = int(m.group(1), 16), m.group(2), m.group(3)
        if addr in targets:
            known.clear()
        args = [a.strip() for a in ops.split(",")]
        base_mnem = mnem.split(".")[0]
        if base_mnem in LOADS | STORES:
            mm = MEM_RE.search(ops)
            if mm and mm.group(2) in known:
                ea = (known[mm.group(2)] + int(mm.group(1), 0)) & 0xffffffff
                if REG_WINDOW <= ea < REG_WINDOW + WINDOW_SIZE:
                    kind = "W" if base_mnem in STORES else "R"
                    accesses.append((addr, kind, ea - REG_WINDOW, mnem))
            if base_mnem in LOADS and args:
                known.pop(args[0], None)
            continue
        if mnem == "lui" and len(args) == 2:
            known[args[0]] = (int(args[1], 0) << 16) & 0xffffffff
            continue
        if mnem in ("addiu", "addi", "ori") and len(args) == 3 and args[1] in known:
            try:
                imm = int(args[2], 0)
            except ValueError:
                known.pop(args[0], None)
                continue
            v = known[args[1]]
            known[args[0]] = (v | imm) if mnem == "ori" else (v + imm) & 0xffffffff
            continue
        if mnem.startswith(("j", "b")) and "link" not in mnem:
            # Calls clobber caller-saved registers; keep it simple.
            if mnem.startswith(("jal", "bal")):
                for r in list(known):
                    if not r.startswith("s"):
                        known.pop(r)
            continue
        if args and re.match(r"^[a-z]+\d*$", args[0]):
            known.pop(args[0], None)
    return accesses


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fw")
    ap.add_argument("--kernel", required=True)
    ap.add_argument("--reg", type=lambda x: int(x, 0), action="append",
                    help="show every access site for this register offset")
    ap.add_argument("--csv")
    args = ap.parse_args()

    fw = Firmware(args.fw, Tables(args.kernel))
    names = load_cr_names(args.kernel)
    with tempfile.TemporaryDirectory() as tmp:
        lines = disassemble(fw, tmp)
    acc = census(lines)

    per_reg = collections.defaultdict(lambda: collections.Counter())
    for addr, kind, off, _ in acc:
        per_reg[off & ~3][kind] += 1
    print("%d register accesses at %d distinct offsets (%s v%d.%d b%d)\n" % (
        len(acc), len(per_reg), fw.bvnc_str, fw.ver_major, fw.ver_minor, fw.ver_build))
    print("%-8s %-5s %-5s %s" % ("offset", "reads", "writes", "name"))
    for off in sorted(per_reg):
        name = names.get(off) or (names.get(off - 4, "") + " [hi]" if off - 4 in names else "?")
        print("0x%05x  %5d %5d  %s" % (off, per_reg[off]["R"], per_reg[off]["W"], name))
    for reg in args.reg or []:
        print("\naccess sites for 0x%x (%s):" % (reg, names.get(reg, "?")))
        for addr, kind, off, mnem in acc:
            if off & ~3 == reg:
                print("  %08x %s %s" % (addr, kind, mnem))
    if args.csv:
        with open(args.csv, "w") as f:
            f.write("pc,kind,offset,name\n")
            for addr, kind, off, mnem in acc:
                f.write("0x%08x,%s,0x%05x,%s\n" % (addr, kind, off, names.get(off & ~3, "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

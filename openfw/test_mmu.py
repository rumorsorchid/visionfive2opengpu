#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
test_mmu.py - check openfw's address translation on a TLB-equipped core.

tools/fwemu runs firmware on Unicorn's fixed-mapping M14K model, because
Unicorn never delivers TLB refill exceptions to the guest. That leaves the
code most likely to fail on hardware untested: the boot-time wired
mappings, the TLB refill handler and the TLB fix-up/flush routines. This
harness runs exactly that code on Unicorn's M14Kc model (R4000-style TLB):

  1. boot_setup() from the reset vector: wired TLB entries 0-4, Wired = 5,
     the rest invalid; every MIPS_ADDR_REMAP_RANGE_CONFIG write checked;
  2. the refill handler, entered with EntryHi set as the hardware would,
     for pages across the heap: TLB entry contents, the two remap ranges
     it programs, and a load through the new mapping;
  3. a non-heap address takes the refill handler to the general exception
     vector;
  4. tlb_fixup() reloads an entry whose odd page became valid later;
  5. fw_tlb_flush() leaves only the wired entries.

    test_mmu.py openfw.elf
"""
import ctypes
import os
import struct
import subprocess
import sys
import tempfile

from unicorn import UC_ARCH_MIPS, UC_HOOK_INTR, UC_MODE_LITTLE_ENDIAN, \
    UC_MODE_MIPS32, UC_PROT_ALL, Uc
from unicorn import mips_const as MC

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from pvrfw import Firmware  # noqa: E402

CROSS = os.environ.get("CROSS", "mipsel-linux-gnu-")
PAGE = 0x1000
HEAP = 0xC0000000
PT = 0xCF000000
STACK = 0xCF600000
REGS = 0xCF800000
SYS_BASE = 0x8_8000_0000      # fake 36-bit system address of heap page 0
REG_BASE = 0x1_1800_0000      # fake system address of the GPU registers
PT_SYS = 0x8_7000_0000
STACK_SYS = 0x8_7100_0000
STUB = 0xBFC03000             # scratch page for test stubs (kseg1)
DONE = STUB + 0xFF0           # stubs end by branching here

fails = 0


def check(cond, what):
    global fails
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails += 1


def asm(src, base):
    """Assemble a microMIPS stub for address @base."""
    with tempfile.TemporaryDirectory() as d:
        s, o, b = (os.path.join(d, x) for x in ("s.S", "s.o", "s.bin"))
        open(s, "w").write("\t.set micromips\n\t.set noreorder\n\t.set noat\n" + src + "\n")
        subprocess.run([CROSS + "gcc", "-c", "-march=mips32r2", "-mmicromips", "-EL", "-o", o, s],
                       check=True)
        subprocess.run([CROSS + "objcopy", "-O", "binary", "-j", ".text", o, b], check=True)
        return open(b, "rb").read()


def symbols(elf):
    out = subprocess.run([CROSS + "nm", elf], check=True, capture_output=True, text=True).stdout
    return {f[2]: int(f[0], 16) for f in (line.split() for line in out.splitlines()) if len(f) == 3}


def eret_addrs(elf):
    dis = subprocess.run([CROSS + "objdump", "-d", "-M", "micromips", elf], check=True,
                         capture_output=True, text=True).stdout
    return {int(line.split(":")[0], 16) for line in dis.splitlines() if line.rstrip().endswith("eret")}


class Machine:
    def __init__(self, elf):
        self.syms = symbols(elf)
        self.erets = eret_addrs(elf)
        self.uc = uc = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
        uc.ctl_set_cpu_model(MC.UC_CPU_MIPS32_M14KC)
        # kseg0/1 pages: boot code, boot data, exceptions, stubs (+ the
        # alias QEMU's microMIPS jump-region bug can produce)
        self.low = ctypes.create_string_buffer(4 * PAGE)
        for base in (0x1FC00000, 0x17C00000):
            uc.mem_map_ptr(base, 4 * PAGE, UC_PROT_ALL, self.low)
        # identity-mapped MIPS physical memory behind the TLB
        uc.mem_map(HEAP, 0x01000000, UC_PROT_ALL)
        uc.mem_map(PT, 4 * PAGE, UC_PROT_ALL)
        uc.mem_map(STACK, PAGE, UC_PROT_ALL)
        self.reg_writes = []
        self.regs = {}
        uc.mmio_map(REGS, 0x400000, self.reg_read, None, self.reg_write, None)
        # kseg0 addresses used by the cache-initialisation index ops
        uc.mem_map(0, 0x100000, UC_PROT_ALL)
        self.exceptions = []
        uc.hook_add(UC_HOOK_INTR, lambda u, n, _: self.exceptions.append(
            (n, u.reg_read(MC.UC_MIPS_REG_PC))))
        self.load(elf)

    def reg_read(self, uc, off, size, _):
        return self.regs.get(off, 0)

    def reg_write(self, uc, off, size, value, _):
        self.regs[off] = value
        self.reg_writes.append((off, value))

    def remaps(self):
        """Decode the remap writes since the last call: [(entry, enabled, in, out)]."""
        out, lo = [], None
        for off, v in self.reg_writes:
            if off == 0x878:
                lo = v
            elif off == 0x87C and lo is not None:
                out.append(((lo >> 1) & 31, lo & 1, lo & 0xFFFFF000, (v >> 4) << 12,
                            (lo >> 7) & 0xF))
                lo = None
        self.reg_writes = []
        return out

    def load(self, elf):
        data = open(elf, "rb").read()
        fw = Firmware.__new__(Firmware)
        fw.data = data
        for p in fw.elf()["phdrs"]:
            if p["type"] != 1 or not p["filesz"]:
                continue
            seg = data[p["offset"]:p["offset"] + p["filesz"]]
            va = p["vaddr"]
            if va >= 0xC0000000:
                self.uc.mem_write(va, seg)
            else:
                self.uc.mem_write(va & 0x1FFFFFFF, seg)

    def setup_host(self):
        uc = self.uc
        # boot data (rogue_mipsfw_boot_data)
        bd = struct.pack("<QQ4QII", STACK_SYS, REG_BASE,
                         *(PT_SYS + i * PAGE for i in range(4)), 12, 4)
        uc.mem_write(0x1FC01000, bd)
        # page table: every heap page valid except a hole for test 4
        pt = bytearray(4 * PAGE)
        for page in range(0x1000):
            sys_pa = SYS_BASE + page * PAGE
            pte = ((sys_pa >> 12) << 6) & 0x3FFFFFC0 | (2 << 3) | 0x7   # uncached, D V G
            if page == 0x201:
                pte = 0
            struct.pack_into("<I", pt, page * 4, pte)
        uc.mem_write(PT, bytes(pt))
        # a recognisable word in every heap page
        for page in range(0x1000):
            uc.mem_write(HEAP + page * PAGE + 0x10, struct.pack("<I", 0xA5000000 | page))

    def stub(self, src, regs=None, until=None):
        code = asm(src + "\n\tb\t.\n\tnop\n", STUB)
        self.uc.mem_write(STUB & 0x1FFFFFFF, code)
        self.uc.ctl_remove_cache(STUB, STUB + PAGE)     # drop stale translations
        end = STUB + len(code) - 6
        for r, v in (regs or {}).items():
            self.uc.reg_write(r, v)
        # (stopping from a code hook upsets Unicorn's M14Kc model: use until)
        self.uc.emu_start(STUB | 1, until or end, count=100000)

    def tlb(self, index):
        self.stub("""
	mtc0	$a0, $0
	ehb
	tlbr
	ehb
	mfc0	$v0, $10
	mfc0	$v1, $2
	mfc0	$a1, $3
	mfc0	$a2, $5""", {MC.UC_MIPS_REG_A0: index})
        r = self.uc.reg_read
        return (r(MC.UC_MIPS_REG_V0), r(MC.UC_MIPS_REG_V1), r(MC.UC_MIPS_REG_A1),
                r(MC.UC_MIPS_REG_A2))

    def cp0(self, reg, sel=0):
        self.stub("\tmfc0\t$v0, $%d, %d" % (reg, sel))
        return self.uc.reg_read(MC.UC_MIPS_REG_V0)

    def load_word(self, va):
        self.stub("\tlw\t$v0, 0($a0)", {MC.UC_MIPS_REG_A0: va})
        return self.uc.reg_read(MC.UC_MIPS_REG_V0)


def identity_lo(va, flags):
    return ((va >> 6) & 0x3FFFFFC0) | flags


def main():
    elf = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "openfw.elf")
    m = Machine(elf)
    m.setup_host()
    uc, S = m.uc, m.syms
    # QEMU's M14Kc has no 1 KiB pages, so PageMask's MaskX bits read 0.
    pm4k = 0

    print("1. boot_setup: wired mappings")
    after_call = S["_reset"] + 0x24      # instruction after `jalr boot_setup`
    uc.emu_start(S["_reset"] | 1, after_call, count=200000)
    pc = uc.reg_read(MC.UC_MIPS_REG_PC)
    check(pc == after_call and not m.exceptions,
          "returns to _reset (pc=0x%08x, exceptions=%s)" % (pc, m.exceptions))
    check(m.cp0(6) == 5, "Wired = 5")
    check(m.cp0(15, 1) & 0xFFFFF000 == 0x9FC02000, "EBase = 0x9FC02000")
    rm = {e[0]: e for e in m.remaps() if e[1]}
    exp = {
        0: (REGS, 0x007FE000, identity_lo(REGS, 0x40000017), 1, REG_BASE, 5),
        1: (PT, pm4k, identity_lo(PT, 0x17), identity_lo(PT + PAGE, 0x17), PT_SYS, 0),
        2: (PT + 0x2000, pm4k, identity_lo(PT + 0x2000, 0x17), identity_lo(PT + 0x3000, 0x17),
            PT_SYS + 2 * PAGE, 0),
        3: (STACK, pm4k, identity_lo(STACK, 0x1F), 1, STACK_SYS, 0),
        4: (0xC0032000, pm4k, identity_lo(0xC0032000, 0x1F), identity_lo(0xC0033000, 0x1F),
            SYS_BASE + 0x32 * PAGE, 0),
    }
    for i, (va, mask, lo0, lo1, sys_pa, region) in exp.items():
        hi, l0, l1, pmask = m.tlb(i)
        # QEMU drops EntryLo RI/XI without PageGrain.RIE/XIE: compare 29:0
        check((hi, l0 & 0x3FFFFFFF, l1 & 0x3FFFFFFF, pmask) ==
              (va, lo0 & 0x3FFFFFFF, lo1 & 0x3FFFFFFF, mask),
              "TLB%d: hi=%08x lo0=%08x lo1=%08x mask=%08x" % (i, hi, l0, l1, pmask))
        e = rm.get(i)
        check(e is not None and e[2:] == (va, sys_pa, region),
              "remap%-2d %s" % (i, "%08x -> %09x (size code %d)" % e[2:] if e else "missing"))
    for i, sys_pa in ((17, PT_SYS + PAGE), (18, PT_SYS + 3 * PAGE), (20, SYS_BASE + 0x33 * PAGE)):
        e = rm.get(i)
        check(e is not None and e[3] == sys_pa, "remap%d (odd page) -> %09x" % (i, sys_pa))
    check(not ({16, 19} & set(rm)) and not any(5 <= i < 16 or i >= 21 for i in rm),
          "only remap ranges 0-4, 17, 18, 20 enabled")
    for i in range(5, 16):
        hi, l0, l1, _ = m.tlb(i)
        if l0 & 2 or l1 & 2:
            check(False, "TLB%d invalid" % i)
            break
    else:
        check(True, "TLB5-15 invalid")
    check(m.load_word(REGS + 0x20) == 0 and m.reg_writes == [], "register window reachable")
    check(m.load_word(PT + 4 * 0x32) == ((SYS_BASE + 0x32 * PAGE) >> 6) & 0x3FFFFFC0 | 0x17,
          "page table readable through the wired mapping")

    print("2. TLB refill handler")
    refill_eret = min(a for a in m.erets if a > S["tlb_refill"])
    uc.reg_write(MC.UC_MIPS_REG_SP, STACK + PAGE - 16)
    for va in (0xC0000000, 0xC0001ABC, 0xC0123450, 0xC0FFF010, 0xC0FD0010):
        even = va & ~0x1FFF
        uc.reg_write(MC.UC_MIPS_REG_T0, 0x11111111)
        uc.reg_write(MC.UC_MIPS_REG_T1, 0x22222222)
        m.stub("""
	mtc0	$a0, $10
	ehb
	li	$t9, 0x%x
	jr	$t9
	nop""" % (S["tlb_refill"] | 1), {MC.UC_MIPS_REG_A0: even}, until=refill_eret)
        pc = uc.reg_read(MC.UC_MIPS_REG_PC)
        # Unicorn reports the PC one instruction early when stopping on a
        # microMIPS `until` address after a 32-bit load
        check(pc in (refill_eret, refill_eret - 4) and not m.exceptions, "refill 0x%08x reaches eret (pc=0x%08x)" % (va, pc))
        check(uc.reg_read(MC.UC_MIPS_REG_T0) == 0x11111111 and
              uc.reg_read(MC.UC_MIPS_REG_T1) == 0x22222222, "  t0/t1 preserved")
        index = m.cp0(0)
        hi, l0, l1, _ = m.tlb(index)
        p0 = (even - HEAP) >> 12
        def pte(p):
            return (((SYS_BASE + p * PAGE) >> 12) << 6) & 0x3FFFFFC0 | 0x17
        check(5 <= index < 16 and hi == even and l0 == identity_lo(even, 0x17) and
              l1 == identity_lo(even + PAGE, 0x17),
              "  TLB%d: hi=%08x lo0=%08x lo1=%08x (identity, PTE flags)" % (index, hi, l0, l1))
        rm = m.remaps()
        want = [(index, 1, even, (pte(p0) >> 6) << 12, 0),
                (index + 16, 1, even + PAGE, (pte(p0 + 1) >> 6) << 12, 0)]
        check(rm == want, "  remap %s" % ", ".join("%d: %08x -> %09x" % (e[0], e[2], e[3])
                                                   for e in rm))
        check(m.load_word((va & ~3) & ~0xFFF | 0x10) == 0xA5000000 | ((va - HEAP) >> 12),
              "  load through the new mapping")

    print("3. non-heap address")
    m.stub("""
	mtc0	$a0, $10
	ehb
	li	$t9, 0x%x
	jr	$t9
	nop""" % (S["tlb_refill"] | 1), {MC.UC_MIPS_REG_A0: 0x00400000},
           until=S["general_exception"])
    check(uc.reg_read(MC.UC_MIPS_REG_PC) == S["general_exception"],
          "refill for 0x00400000 goes to the general exception vector")

    print("4. tlb_fixup: odd page mapped after the pair was loaded")
    hole = HEAP + 0x201 * PAGE
    m.stub("""
	mtc0	$a0, $10
	ehb
	li	$t9, 0x%x
	jr	$t9
	nop""" % (S["tlb_refill"] | 1), {MC.UC_MIPS_REG_A0: hole - PAGE}, until=refill_eret)
    m.remaps()
    index = m.cp0(0)
    hi, l0, l1, _ = m.tlb(index)
    check(hi == hole - PAGE and l0 & 2 and not l1 & 2, "pair loaded with the odd page invalid")
    newpte = (((SYS_BASE + 0x201 * PAGE) >> 12) << 6) & 0x3FFFFFC0 | 0x17
    uc.mem_write(PT + 0x201 * 4, struct.pack("<I", newpte))
    m.stub("""
	li	$t9, 0x%x
	jalr	$t9
	nop""" % (S["tlb_fixup"] | 1), {MC.UC_MIPS_REG_A0: 2, MC.UC_MIPS_REG_A1: hole + 8})
    check(uc.reg_read(MC.UC_MIPS_REG_V0) == 1, "tlb_fixup returns 1")
    hi, l0, l1, _ = m.tlb(index)
    check(hi == hole - PAGE and l1 == identity_lo(hole, 0x17),
          "same TLB entry (%d) now maps the odd page" % index)
    rm = m.remaps()
    check(any(e[0] == index + 16 and e[2] == hole and e[3] == SYS_BASE + 0x201 * PAGE
              for e in rm), "remap%d updated" % (index + 16))
    uc.mem_write(PT + 0x300 * 4, b"\0\0\0\0")
    m.stub("""
	li	$t9, 0x%x
	jalr	$t9
	nop""" % (S["tlb_fixup"] | 1), {MC.UC_MIPS_REG_A0: 2, MC.UC_MIPS_REG_A1: HEAP + 0x300 * PAGE})
    check(uc.reg_read(MC.UC_MIPS_REG_V0) == 0, "tlb_fixup refuses an invalid PTE (real fault)")

    print("5. fw_tlb_flush")
    m.stub("""
	li	$t9, 0x%x
	jalr	$t9
	nop""" % (S["fw_tlb_flush"] | 1))
    bad = [i for i in range(5, 16) if m.tlb(i)[1] & 2 or m.tlb(i)[2] & 2]
    check(not bad, "TLB5-15 invalid after flush")
    check(all(m.tlb(i)[0] == v[0] for i, v in exp.items()), "wired entries untouched")
    rm = m.remaps()
    check(sorted(e[0] for e in rm) == sorted(list(range(5, 16)) + list(range(21, 32))) and
          not any(e[1] for e in rm), "remap ranges 5-15 and 21-31 disabled")
    check(not m.exceptions, "no unexpected exceptions (%s)" % m.exceptions)

    print("\n%s" % ("all checks passed" if not fails else "%d check(s) FAILED" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

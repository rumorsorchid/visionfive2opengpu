#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
fwemu.py - boot Imagination's PowerVR MIPS firmware in an emulator.

Recreates what the upstream Linux drm/imagination driver does before it
releases the firmware processor from reset (pvr_fw.c, pvr_fw_mips.c,
pvr_vm_mips.c), runs the firmware's microMIPS code under Unicorn and logs
every GPU register access, every write to host-visible firmware structures
and the firmware's own trace buffer.

    fwemu.py FW.fw --kernel LINUX_SRC [--layout layout.json]
             [--config-flags 0x10] [--max-insns N] [--trace-regs]

Physical memory model (MIPS physical addresses):

  HEAP_PA + off          the 16 MiB firmware heap, FW virtual 0xC0000000 + off
  0x1FC00000/01000/02000 boot code, boot data, exception code (wrapper remap)
  0x17C00000...          alias of the above, working around QEMU's microMIPS
                         JAL region bug (masks PC[31:28] instead of PC[31:27])
  0x00000000             boot code again (BRN 63553 remap)
  PT_PA                  the 4-page MIPS page table
  0xCF800000             the GPU register file as decoded by the MIPS wrapper
                         (MIPS_WRAPPER_CONFIG.REGBANK = 0xCF80); +0x200000 is
                         a write-only alias the firmware also uses
"""

import argparse
import ctypes
import json
import os
import struct
import sys

from unicorn import (UC_ARCH_MIPS, UC_HOOK_CODE, UC_HOOK_INTR, UC_HOOK_MEM_UNMAPPED,
                     UC_HOOK_MEM_WRITE, UC_MODE_LITTLE_ENDIAN, UC_MODE_MIPS32,
                     UC_PROT_ALL, Uc, UcError)
from unicorn import mips_const as MC
from unicorn.mips_const import UC_CPU_MIPS32_M14K, UC_MIPS_REG_PC, UC_MIPS_REG_RA

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from pvrfw import Firmware, Tables  # noqa: E402
from fwregs import disassemble, load_cr_names  # noqa: E402
import fwtrace  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402

FW_HEAP_VA = 0xC0000000
FW_HEAP_LOG2 = 24
FW_HEAP_SIZE = 1 << FW_HEAP_LOG2
CONFIG_HEAP_SIZE = 3 * 0x10000
CONFIG_OFFSET = FW_HEAP_SIZE - CONFIG_HEAP_SIZE
CONN_CTL_VA = FW_HEAP_VA + CONFIG_OFFSET
OSINIT_VA = CONN_CTL_VA + 0x10000
SYSINIT_VA = OSINIT_VA + 0x10000

HEAP_PA = 0x80000000
PT_PA = 0x7F000000
REG_PA = 0x18000000          # JH7110 GPU register base (boot_data->reg_base)
REG_BANK_PA = 0xCF800000     # where the MIPS wrapper decodes registers (REGBANK)
PT_VIRT = 0xCF000000         # ROGUE_MIPSFW_PT_VIRTUAL_BASE
STACK_VIRT = 0xCF600000      # ROGUE_MIPSFW_STACK_VIRTUAL_BASE
REG_SIZE = 0x400000

BOOT_REMAP_PA = 0x1FC00000
JAL_BUG_ALIAS_PA = 0x17C00000

PAGE = 0x1000

# Imagination's register poll routine, poll(reg_offset, value, mask, ...)
# (it logs "HW poll ... failed" on timeout). The model completes every
# polled hardware operation by presenting the awaited value.
# poll_mc(core_mask, reg_offset, value, mask) is the per-core variant.
# Keyed by "BVNC/build"; values: (reg, value, mask) argument registers.
# poll_fast(reg, value, mask) spins 2000 times before falling back to poll(),
# poll_fast_mc(core_mask, reg, value, mask) likewise to poll_mc().
POLL_FUNCS = {"36.50.54.182/6503725": {0xC0008C7C: ("a0", "a1", "a2"),
                                       0xC0008D8C: ("a1", "a2", "a3"),
                                       0xC0008EE4: ("a0", "a1", "a2"),
                                       0xC0008F20: ("a1", "a2", "a3")}}



def openfw_poll_funcs(fw_path):
    """openfw's poll_reg(off, mask, value), from the link map built next to the image."""
    m = os.path.join(os.path.dirname(os.path.abspath(fw_path)), "openfw.map")
    if not os.path.exists(m):
        return {}
    for line in open(m):
        f = line.split()
        if len(f) == 2 and f[1] == "poll_reg" and f[0].startswith("0x"):
            return {int(f[0], 16) & ~1: ("a0", "a2", "a1")}
    return {}


# Reset values the firmware relies on. CLK_CTRL comes out of reset with
# every unit in automatic clock gating (DDK rgxdefs_km.h: CLK_CTRL_ALL_AUTO
# masked with MASKFULL); reading it as 0 makes the firmware think the
# freshly powered rascal/dust clocks are off.
RESET_VALUES = {
    0x0000: 0x2A2AAAAA,   # CLK_CTRL lo
    0x0004: 0xAAAAAA00,   # CLK_CTRL hi
}

# Registers whose request bits the hardware clears when the operation is
# done; the model completes them immediately.
SELF_CLEARING = {
    0x12A0,  # BIF_CTRL_INVAL (MMU cache invalidate)
}
ENTRYLO_PFN_SHIFT = 6
ENTRYLO_DVG = 0x4 | 0x2 | 0x1
CACHE_POLICY_ABOVE_32BIT = 1   # write-through, as the kernel uses on 36-bit cores
UNCACHED_POLICY = 2


GPR = {"s%d" % i: getattr(MC, "UC_MIPS_REG_S%d" % i) for i in range(8)}
GPR.update({"s8": MC.UC_MIPS_REG_FP, "fp": MC.UC_MIPS_REG_FP, "ra": MC.UC_MIPS_REG_RA,
            "sp": MC.UC_MIPS_REG_SP})


def virt_to_phys(va):
    """MIPS32 segments with the M14K fixed-mapping MMU (kernel mode)."""
    if 0x80000000 <= va < 0xC0000000:     # kseg0/kseg1: unmapped
        return va & 0x1FFFFFFF
    return va                              # kseg2/3 identity (FMT)


def parse_swm(ops):
    """'s0-s3,ra,20(sp)' -> (['s0','s1','s2','s3','ra'], 20, 'sp')"""
    parts = ops.split(",")
    mem = re.match(r"(-?\d+)\((\w+)\)", parts[-1])
    regs = []
    for p in parts[:-1]:
        if "-" in p:
            a, b = p.split("-")
            regs += ["s%d" % i for i in range(int(a[1:]), int(b[1:]) + 1)]
        else:
            regs.append(p)
    return regs, int(mem.group(1)), mem.group(2)


class Layout:
    def __init__(self, path):
        d = json.load(open(path))
        self.structs, self.anon = d["structs"], d["anon"]

    def size(self, name):
        return self.structs[name]["size"]

    def field(self, name, path):
        """Return (offset, size) of a dotted member path, e.g. 'a.b[2].c'."""
        st = self.structs[name]
        off = 0
        t = None
        for part in path.split("."):
            idxs = []
            if "[" in part:
                part, rest = part.split("[", 1)
                idxs = [int(x, 0) for x in rest.rstrip("]").split("][")]
            m = next((m for m in st["members"] if m["name"] == part), None)
            if m is None:
                raise KeyError("%s has no member %s" % (name, part))
            off += m["offset"]
            t = m["type"]
            if idxs:
                if t["kind"] != "array":
                    raise KeyError("%s.%s is not an array" % (name, part))
                dims = t.get("dims") or [t["size"] // t["elem"]["size"]]
                if len(idxs) != len(dims):
                    raise KeyError("%s.%s needs %d indices" % (name, part, len(dims)))
                flat = 0
                for k, idx in enumerate(idxs):
                    flat = flat * dims[k] + idx
                t = t["elem"]
                off += flat * t["size"]
            if t["kind"] in ("struct", "union"):
                st = self.structs.get(t.get("name")) or self.anon[str(t["die_offset"])]
                name = t.get("name") or name
        return off, t["size"]


class Emu:
    def __init__(self, fw, layout, cr_names, args):
        self.fw, self.L, self.names, self.args = fw, layout, cr_names, args
        self.uc = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
        # The firmware's TLB refill handler maps every heap page identity
        # (VA == MIPS PA) and programs a wrapper remap range from the page
        # table. Unicorn does not deliver TLB refills to the guest, so use
        # the fixed-mapping M14K MMU (kseg2 identity) and back MIPS PA
        # 0xC0000000+ with the heap directly; the remap model below handles
        # the rest.
        self.uc.ctl_set_cpu_model(UC_CPU_MIPS32_M14K)
        self.regs = dict(RESET_VALUES)
        self.remaps = {}
        self.remap_log = []
        self.pending_tasks = []
        self.parked = False
        self.event_status = 0
        self.power_events = []
        self.polls = []
        self.exceptions = 0
        self.reg_log = []
        self.insns = 0
        self.alloc_next = FW_HEAP_VA + 0x100000
        self.objects = {}
        self.trace_lines = []

        # One host buffer for the whole heap so that aliases share storage.
        self.heap = ctypes.create_string_buffer(FW_HEAP_SIZE)
        self.uc.mem_map_ptr(HEAP_PA, FW_HEAP_SIZE, UC_PROT_ALL, self.heap)
        self.uc.mem_map_ptr(FW_HEAP_VA, FW_HEAP_SIZE, UC_PROT_ALL, self.heap)
        self.pt = ctypes.create_string_buffer(4 * PAGE)
        self.uc.mem_map_ptr(PT_PA, 4 * PAGE, UC_PROT_ALL, self.pt)

        self.load_image()
        self.map_remaps()
        self.map_static_windows()
        self.build_page_table()
        self.uc.mmio_map(REG_BANK_PA, REG_SIZE, self.reg_read, None, self.reg_write, None)

    # -- memory helpers ------------------------------------------------------
    def va2off(self, va):
        assert FW_HEAP_VA <= va < FW_HEAP_VA + FW_HEAP_SIZE, hex(va)
        return va - FW_HEAP_VA

    def write(self, va, data):
        o = self.va2off(va)
        ctypes.memmove(ctypes.addressof(self.heap) + o, bytes(data), len(data))

    def read(self, va, n):
        o = self.va2off(va)
        return self.heap.raw[o:o + n]

    def w32(self, va, v):
        self.write(va, struct.pack("<I", v & 0xffffffff))

    def w64(self, va, v):
        self.write(va, struct.pack("<Q", v))

    def r32(self, va):
        return struct.unpack("<I", self.read(va, 4))[0]

    def set(self, sname, base_va, path, value):
        off, size = self.L.field(sname, path)
        fmt = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}[size]
        self.write(base_va + off, struct.pack(fmt, value))

    def get(self, sname, base_va, path):
        off, size = self.L.field(sname, path)
        fmt = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}[size]
        return struct.unpack(fmt, self.read(base_va + off, size))[0]

    def alloc(self, name, size, align=PAGE):
        va = (self.alloc_next + align - 1) & ~(align - 1)
        self.alloc_next = (va + size + PAGE - 1) & ~(PAGE - 1)
        self.objects[name] = (va, size)
        return va

    # -- image ---------------------------------------------------------------
    def section(self, sid_name):
        from pvrfw import SECTION_IDS
        sid = SECTION_IDS.index(sid_name)
        return next(e for e in self.fw.layout if e["id"] == sid)

    def load_image(self):
        """Mirror pvr_fw_process(): code object at heap offset 0, data
        object at the fixed private-data address."""
        self.code_va = FW_HEAP_VA
        data_sec = self.section("MIPS_PRIVATE_DATA")
        self.data_va = data_sec["base"]
        for p in self.fw.elf()["phdrs"]:
            if p["type"] != 1 or not p["filesz"]:
                continue
            for s in self.fw.layout:
                if s["base"] <= p["vaddr"] < s["base"] + s["max_size"]:
                    obj_va = self.code_va if s["type"] == 1 else self.data_va
                    dst = obj_va + s["alloc_offset"] + (p["vaddr"] - s["base"])
                    self.write(dst, self.fw.data[p["offset"]:p["offset"] + p["filesz"]])
                    break
        self.patch_tlb_ops()
        self.boot_code_va = self.code_va + self.section("MIPS_BOOT_CODE")["alloc_offset"]
        self.exc_code_va = self.code_va + self.section("MIPS_EXCEPTIONS_CODE")["alloc_offset"]
        self.boot_data_va = self.data_va + self.section("MIPS_BOOT_DATA")["alloc_offset"]
        self.stack_va = self.data_va + self.section("MIPS_STACK")["alloc_offset"]

    def patch_tlb_ops(self):
        """The emulated MMU is fixed-mapping (identity), so TLB maintenance
        is meaningless and would raise Reserved Instruction. Replace
        tlbwi/tlbwr/tlbr/tlbp with a 32-bit nop, in emulator memory only."""
        self.tlb_patches = []
        self.swm_sites = {}
        self.eret_addrs = set()
        with tempfile.TemporaryDirectory() as tmp:
            lines = disassemble(self.fw, tmp)
        for line in lines:
            m = re.match(r"^\s*([0-9a-f]+):\s+0000 f37c\s+eret", line)
            if m:
                self.eret_addrs.add(int(m.group(1), 16))
                continue
            m = re.match(r"^\s*([0-9a-f]+):\s+(?:[0-9a-f]{4}\s?){1,2}\s+swm\s+(\S+)", line)
            if m:
                self.swm_sites[int(m.group(1), 16)] = parse_swm(m.group(2))
                continue
            m = re.match(r"^\s*([0-9a-f]+):\s+0000 [0-3]37c\s+(tlb\w+)", line)
            if not m:
                continue
            addr = int(m.group(1), 16)
            for s in self.fw.layout:
                if s["type"] == 1 and s["base"] <= addr < s["base"] + s["max_size"]:
                    dst = self.code_va + s["alloc_offset"] + addr - s["base"]
                    self.write(dst, b"\x00\x00\x00\x00")
                    self.tlb_patches.append((addr, m.group(2)))
        print("found %d swm sites (Unicorn SWM workaround)" % len(self.swm_sites))
        print("patched %d TLB instructions: %s" % (
            len(self.tlb_patches), ", ".join("%s@%08x" % (n, a) for a, n in self.tlb_patches)))

    def pa(self, va):
        return HEAP_PA + self.va2off(va)

    def map_remaps(self):
        """The MIPS wrapper's REMAP1/2/3 (and REMAP5 for BRN 63553)."""
        def alias(pa, va):
            ptr = ctypes.cast(ctypes.addressof(self.heap) + self.va2off(va), ctypes.c_void_p)
            self.uc.mem_map_ptr(pa, PAGE, UC_PROT_ALL, ptr)
        for base in (BOOT_REMAP_PA, JAL_BUG_ALIAS_PA):
            alias(base + 0x0000, self.boot_code_va)
            alias(base + 0x1000, self.boot_data_va)
            alias(base + 0x2000, self.exc_code_va)
        alias(0x00000000, self.boot_code_va)

    def map_static_windows(self):
        """Windows the boot loader remaps from boot data (PT pages, stack).
        Mapping them while emulation runs does not take effect in Unicorn,
        so map them up front and only verify the firmware's remap writes."""
        self.expected_remaps = {}
        for i in range(4):
            va = PT_VIRT + i * PAGE
            self.uc.mem_map_ptr(va, PAGE, UC_PROT_ALL,
                                ctypes.c_void_p(ctypes.addressof(self.pt) + i * PAGE))
            self.expected_remaps[va] = PT_PA + i * PAGE
        stack_off = self.va2off(self.stack_va)
        self.uc.mem_map_ptr(STACK_VIRT, PAGE, UC_PROT_ALL,
                            ctypes.c_void_p(ctypes.addressof(self.heap) + stack_off))
        self.expected_remaps[STACK_VIRT] = self.pa(self.stack_va)

    def build_page_table(self):
        """pvr_vm_mips_map() for every heap page we use. Uncached for
        everything the host shares, cached for code."""
        self.pt_entries = {}

    def map_pages(self, va, size, uncached=True):
        policy = UNCACHED_POLICY if uncached else CACHE_POLICY_ABOVE_32BIT
        for off in range(0, size, PAGE):
            pfn = (self.va2off(va + off)) >> 12
            pa = self.pa(va + off)
            pte = (((pa >> 12) << ENTRYLO_PFN_SHIFT) & 0x3FFFFFC0) | (policy << 3) | ENTRYLO_DVG
            struct.pack_into("<I", self.pt, pfn * 4, pte)

    # -- registers -------------------------------------------------------------
    def reg_name(self, off):
        base = off & ~3
        alias = ""
        if base >= 0x200000:
            base -= 0x200000
            alias = "@alias"
        n = self.names.get(base) or (self.names.get(base - 4, "?") + "[hi]"
                                     if base - 4 in self.names else "?")
        return n + alias

    def reg_read(self, uc, off, size, user_data):
        v = self.reg_model_read(off)
        if self.args.trace_regs:
            print("  R %-34s +0x%06x = 0x%08x  pc=%08x" % (
                self.reg_name(off), off, v, uc.reg_read(UC_MIPS_REG_PC)))
        self.reg_log.append(("R", off, v))
        return v

    def reg_write(self, uc, off, size, value, user_data):
        if self.args.trace_regs:
            print("  W %-34s +0x%06x = 0x%08x  pc=%08x" % (
                self.reg_name(off), off, value, uc.reg_read(UC_MIPS_REG_PC)))
        self.reg_log.append(("W", off, value))
        if off & ~0x200000 == 0x0B00:
            # Firmware kicking its own MTS: on hardware this schedules the
            # interrupt task (CONTEXT = INTCTX, 0x20) or the background
            # task once the current handler returns.
            self.pending_tasks.append("irq" if value & 0x20 else "bg")
        self.reg_model_write(off, value)

    def reg_model_read(self, off):
        bvnc = self.fw.bvnc
        fixed = {
            0x0020: bvnc & 0xffffffff,            # CORE_ID__PBVNC lo
            0x0024: bvnc >> 32,                   # CORE_ID__PBVNC hi
            0xF308: 1,                            # MULTICORE_SYSTEM: 1 GPU
            # MULTICORE_GPU of the only core: primary, with fragment,
            # geometry and compute capability, ID 0. The firmware registers
            # geometry/fragment cores from these bits at boot.
            0xF300: 0x78,
        }
        if off in fixed:
            return fixed[off]
        if off in (0x0160, 0x0164):               # TIMER: advance with time
            t = self.insns // 16
            return t & 0xffffffff if off == 0x160 else t >> 32
        if off == 0x0130:                         # EVENT_STATUS
            return self.event_status
        if off in SELF_CLEARING and off not in self.regs_set_by_poll():
            return 0
        return self.regs.get(off, 0)

    def regs_set_by_poll(self):
        return {r for r, v, m in self.polls[-1:] if v}

    def reg_model_write(self, off, value):
        off &= ~0x200000
        self.regs[off] = value
        if off == 0x0138:                         # EVENT_CLEAR
            self.event_status &= ~value
        if off == 0x0038 and value:               # POWER_EVENT
            self.power_events.append(value)
            print("  ** POWER_EVENT 0x%08x: %s, domains 0x%x, gpu mask 0x%02x%s  pc=0x%08x" % (
                value, "power UP" if value & 1 else "power DOWN", (value >> 8) & 0x7,
                value >> 24, ", REQ_EN" if value & 2 else "", self.uc.reg_read(UC_MIPS_REG_PC)))
            if value & 2:
                self.event_status |= 0x400        # POWER_COMPLETE
        if off == 0x087C:  # MIPS_ADDR_REMAP_RANGE_CONFIG, written lo then hi
            self.remap_update(self.regs.get(0x0878, 0) | value << 32)

    # -- MIPS wrapper address remap unit -----------------------------------------
    def host_ptr(self, pa, size):
        for base, buf, length in ((HEAP_PA, self.heap, FW_HEAP_SIZE),
                                  (PT_PA, self.pt, 4 * PAGE)):
            if base <= pa and pa + size <= base + length:
                return ctypes.c_void_p(ctypes.addressof(buf) + pa - base)
        return None

    def remap_update(self, val):
        entry = (val >> 1) & 0x1f
        self.remap_log.append(val)
        if not val & 1:
            return
        base_in = val & 0xFFFFF000
        size = PAGE << (2 * ((val >> 7) & 0xf))
        out = (val >> 36) << 12
        if self.args.trace_regs:
            print("  remap[%2d] 0x%08x+0x%x -> 0x%09x" % (entry, base_in, size, out))
        if base_in == REG_BANK_PA:
            return  # register bank, decoded by the wrapper before remapping
        if FW_HEAP_VA <= base_in < FW_HEAP_VA + FW_HEAP_SIZE:
            # Identity-mapped heap page: already backed; check consistency.
            if out != self.pa(base_in):
                print("!! remap[%d] heap 0x%08x -> 0x%x, expected 0x%x" % (
                    entry, base_in, out, self.pa(base_in)))
            return
        exp = self.expected_remaps.get(base_in)
        if exp is None:
            print("!! remap[%d] 0x%08x+0x%x -> 0x%x: unexpected window" % (
                entry, base_in, size, out))
        elif exp != out:
            print("!! remap[%d] 0x%08x -> 0x%x, expected 0x%x" % (entry, base_in, out, exp))

    # -- host structures (pvr_fw.c) ---------------------------------------------
    def setup_host(self):
        L, fw = self.L, self.fw
        feats = fw.features()
        # The kernel maps the code and data objects (sizes from the layout
        # table) uncached; the firmware's wired mappings choose their own.
        code_size = sum(e["alloc_size"] for e in fw.layout if e["type"] == 1)
        data_size = sum(e["alloc_size"] for e in fw.layout if e["type"] == 2)
        self.map_pages(self.code_va, code_size)
        self.map_pages(self.data_va, data_size)

        # Boot data (pvr_mips_init + boot_data at BOOTLDR_CONF_OFFSET 0).
        bd = self.boot_data_va
        self.set("rogue_mipsfw_boot_data", bd, "stack_phys_addr", self.pa(self.stack_va))
        self.set("rogue_mipsfw_boot_data", bd, "reg_base", REG_PA)
        for i in range(4):
            self.set("rogue_mipsfw_boot_data", bd, "pt_phys_addr[%d]" % i, PT_PA + i * PAGE)
        self.set("rogue_mipsfw_boot_data", bd, "pt_log2_page_size", 12)
        self.set("rogue_mipsfw_boot_data", bd, "pt_num_pages", 4)

        def obj(name, sname=None, size=None, uncached=True):
            sz = size if size is not None else L.size(sname)
            va = self.alloc(name, sz)
            self.map_pages(va, (sz + PAGE - 1) & ~(PAGE - 1), uncached)
            return va

        # Config heap objects at fixed offsets.
        for va, sname in ((CONN_CTL_VA, "rogue_fwif_connection_ctl"),
                          (OSINIT_VA, "rogue_fwif_osinit"),
                          (SYSINIT_VA, "rogue_fwif_sysinit")):
            self.map_pages(va, (L.size(sname) + PAGE - 1) & ~(PAGE - 1))
            self.objects[sname] = (va, L.size(sname))

        # Kernel CCB / firmware CCB (pvr_ccb.c): 2^n slots.
        kccb_n, fwccb_n = 7, 7
        kccb_ctl = obj("kccb_ctl", "rogue_fwif_ccb_ctl")
        kccb = obj("kccb", size=(1 << kccb_n) * L.size("rogue_fwif_kccb_cmd"))
        kccb_rtn = obj("kccb_rtn", size=(1 << kccb_n) * 4)
        fwccb_ctl = obj("fwccb_ctl", "rogue_fwif_ccb_ctl")
        fwccb = obj("fwccb", size=(1 << fwccb_n) * L.size("rogue_fwif_fwccb_cmd"))
        self.kccb_ctl, self.kccb, self.kccb_rtn, self.kccb_n = kccb_ctl, kccb, kccb_rtn, kccb_n
        self.fwccb_ctl, self.fwccb = fwccb_ctl, fwccb
        for ctl, n, cmd in ((kccb_ctl, kccb_n, "rogue_fwif_kccb_cmd"),
                            (fwccb_ctl, fwccb_n, "rogue_fwif_fwccb_cmd")):
            self.set("rogue_fwif_ccb_ctl", ctl, "wrap_mask", (1 << n) - 1)
            self.set("rogue_fwif_ccb_ctl", ctl, "cmd_size", L.size(cmd))

        power_sync = obj("power_sync", size=4)
        hwrinfobuf = obj("hwrinfobuf", "rogue_fwif_hwrinfobuf")
        obj("mmucache_sync", size=4)
        sysdata = obj("sysdata", "rogue_fwif_sysdata")
        fault_page = obj("fault_page", size=PAGE)
        gpu_util = obj("gpu_util_fwcb", "rogue_fwif_gpu_util_fwcb")
        runtime_cfg = obj("runtime_cfg", "rogue_fwif_runtime_cfg")
        tracebuf_ctl = obj("tracebuf_ctl", "rogue_fwif_tracebuf")
        trace_dwords = 12000
        tracebuf = obj("tracebuf0", size=trace_dwords * 4)
        osdata = obj("osdata", "rogue_fwif_osdata")

        # Values (fw_*_init in pvr_fw.c).
        core_clock = 409600000
        S = "rogue_fwif_sysinit"
        self.set(S, SYSINIT_VA, "fault_phys_addr", self.pa(fault_page))
        self.set(S, SYSINIT_VA, "pds_exec_base", 0xDA00000000)
        self.set(S, SYSINIT_VA, "usc_exec_base", 0xE000000000)
        self.set(S, SYSINIT_VA, "runtime_cfg_fw_addr", runtime_cfg)
        self.set(S, SYSINIT_VA, "trace_buf_ctl_fw_addr", tracebuf_ctl)
        self.set(S, SYSINIT_VA, "fw_sys_data_fw_addr", sysdata)
        self.set(S, SYSINIT_VA, "gpu_util_fw_cb_ctl_fw_addr", gpu_util)
        self.set(S, SYSINIT_VA, "initial_core_clock_speed", core_clock)
        self.set(S, SYSINIT_VA, "marker_val", 1)
        self.set("rogue_fwif_sysdata", sysdata, "config_flags", self.args.config_flags)
        self.set("rogue_fwif_runtime_cfg", runtime_cfg, "core_clock_speed", core_clock)
        self.set("rogue_fwif_runtime_cfg", runtime_cfg, "active_pm_latency_persistant", 1)
        self.set("rogue_fwif_runtime_cfg", runtime_cfg, "default_dusts_num_init",
                 feats.get("NUM_CLUSTERS") or 1)
        self.set("rogue_fwif_gpu_util_fwcb", gpu_util, "last_word", 0)  # IDLE state

        O = "rogue_fwif_osinit"
        self.set(O, OSINIT_VA, "kernel_ccbctl_fw_addr", kccb_ctl)
        self.set(O, OSINIT_VA, "kernel_ccb_fw_addr", kccb)
        self.set(O, OSINIT_VA, "kernel_ccb_rtn_slots_fw_addr", kccb_rtn)
        self.set(O, OSINIT_VA, "firmware_ccbctl_fw_addr", fwccb_ctl)
        self.set(O, OSINIT_VA, "firmware_ccb_fw_addr", fwccb)
        self.set(O, OSINIT_VA, "rogue_fwif_hwr_info_buf_ctl_fw_addr", hwrinfobuf)
        self.set(O, OSINIT_VA, "fw_os_data_fw_addr", osdata)
        self.set("rogue_fwif_osdata", osdata, "power_sync_fw_addr", power_sync)

        T = "rogue_fwif_tracebuf"
        self.set(T, tracebuf_ctl, "log_type", self.args.trace_mask)
        self.set(T, tracebuf_ctl, "tracebuf_size_in_dwords", trace_dwords)
        self.set(T, tracebuf_ctl, "tracebuf[0].trace_buffer_fw_addr", tracebuf)
        self.tracebuf_va, self.trace_dwords = tracebuf, trace_dwords
        self.tracebuf_ctl = tracebuf_ctl

    # -- run -------------------------------------------------------------------------
    def run(self):
        self.setup_host()
        uc = self.uc
        last_pcs = []

        pending = []
        poll_funcs = {}
        if self.args.complete_polls:
            poll_funcs.update(POLL_FUNCS.get("%s/%d" % (self.fw.bvnc_str, self.fw.ver_build), {}))
            poll_funcs.update(openfw_poll_funcs(self.fw.path))
        argreg = {"a0": MC.UC_MIPS_REG_A0, "a1": MC.UC_MIPS_REG_A1,
                  "a2": MC.UC_MIPS_REG_A2, "a3": MC.UC_MIPS_REG_A3}

        def on_code(uc, addr, size, _):
            if addr in poll_funcs:
                r_reg, r_val, r_mask = poll_funcs[addr]
                reg = uc.reg_read(argreg[r_reg])
                val = uc.reg_read(argreg[r_val])
                mask = uc.reg_read(argreg[r_mask])
                self.polls.append((reg, val, mask))
                self.reg_log.append(("P", reg, (val, mask)))
                if self.args.trace_regs:
                    print("  P %-34s +0x%06x & 0x%08x == 0x%08x" % (
                        self.reg_name(reg), reg, mask, val))
                if reg == 0x0130:
                    self.event_status = (self.event_status & ~mask) | (val & mask)
                else:
                    self.regs[reg] = (self.regs.get(reg, 0) & ~mask) | (val & mask)
            # Unicorn's microMIPS SWM stores only 16 bits per register; it
            # does not modify registers, so redo the stores in full once it
            # has executed (i.e. before the next instruction).
            while pending:
                a, v = pending.pop()
                uc.mem_write(virt_to_phys(a), struct.pack("<I", v))
            site = self.swm_sites.get(addr)
            if site:
                regs, off, base = site
                ea = (uc.reg_read(GPR[base]) + off) & 0xffffffff
                for i, r in enumerate(regs):
                    pending.append((ea + 4 * i, uc.reg_read(GPR[r])))
            if self.stop_at_eret and addr in self.eret_addrs:
                uc.emu_stop()
                return
            self.insns += 1
            last_pcs.append(addr)
            if len(last_pcs) > 64:
                last_pcs.pop(0)
            if self.insns >= self.insn_limit:
                uc.emu_stop()

        def on_unmapped(uc, access, addr, size, value, _):
            print("!! unmapped access type=%d addr=0x%08x size=%d value=0x%x pc=0x%08x" % (
                access, addr, size, value, uc.reg_read(UC_MIPS_REG_PC)))
            return False

        def on_intr(uc, intno, _):
            self.exceptions += 1
            if self.args.trace_exc or self.exceptions <= 5:
                print("!! exception %d at pc=0x%08x ra=0x%08x" % (
                    intno, uc.reg_read(UC_MIPS_REG_PC), uc.reg_read(UC_MIPS_REG_RA)))

        uc.hook_add(UC_HOOK_CODE, on_code)

        def on_watch(uc, access, addr, size, value, _):
            print("  watch: %s 0x%08x size %d value 0x%x pc=0x%08x" % (
                "W" if access == 17 else "R", addr, size, value, uc.reg_read(UC_MIPS_REG_PC)))
        for w in self.args.watch or []:
            lo, _, hi = w.partition("-")
            lo = int(lo, 0)
            hi = int(hi, 0) if hi else lo + 4
            uc.hook_add(UC_HOOK_MEM_WRITE, on_watch, begin=lo, end=hi - 1)
        uc.hook_add(UC_HOOK_MEM_UNMAPPED, on_unmapped)
        uc.hook_add(UC_HOOK_INTR, on_intr)

        # Reset: the MIPS core starts at 0xBFC00000 in microMIPS mode
        # (MIPS_WRAPPER_CONFIG.BOOT_ISA_MODE). Unicorn cannot set ISA mode
        # from emu_start, so enter through a 4-instruction MIPS32 trampoline.
        tramp_pa = 0x1FC03000
        uc.mem_map(tramp_pa, PAGE)
        uc.mem_write(tramp_pa, struct.pack("<4I", 0x3c19bfc0, 0x37390001, 0x03200008, 0))
        self.last_pcs = last_pcs
        self.trace_seen = 0
        started = self.boot()
        for cmd in self.args.kccb or []:
            self.kccb_scenario(cmd)
        return started

    def boot(self):
        """Release the core from reset (pvr_fw_start) and run it until it
        idles. Also used for the reboot after a runtime resume: firmware
        memory, page table and the kernel's structures are kept."""
        uc = self.uc
        self.insn_limit = self.insns + self.args.max_insns
        self.stop_at_eret = False
        self.parked = False
        self.pending_tasks = []
        uc.reg_write(MC.UC_MIPS_REG_CP0_STATUS, 0x00400004)    # reset: BEV | ERL
        before = self.insns
        try:
            uc.emu_start(0xBFC03000, 0xFFFFFFFF, count=0)
        except UcError as e:
            print("!! emulation stopped: %s at pc=0x%08x" % (e, uc.reg_read(UC_MIPS_REG_PC)))
        started = self.get("rogue_fwif_sysinit", SYSINIT_VA, "firmware_started")
        print("\nexecuted %d instructions; firmware_started=%d" % (self.insns - before, started))
        print("last PCs:", " ".join("%08x" % p for p in self.last_pcs[-16:]))
        self.report()
        return started

    # -- kernel CCB + interrupts ------------------------------------------------------
    KCCB_CMDS = {"health": 115, "pow-units": 107, "pow-idle": 107, "pow-cancel-idle": 107,
                 "pow-off": 107, "logtype": 206, "mmucache": 102}
    # Vectored interrupts (Cause.IV, IntCtl.VS = 0x100) at EBase 0x9FC02000:
    # IP2 CP0 timer, IP3 MTS background task, IP4 MTS interrupt task. A
    # handler runs from its vector until it reaches an eret.
    IRQ_VECTORS = {"timer": 0x9FC02400, "bg": 0x9FC02500, "irq": 0x9FC02600}

    def send_kccb(self, cmd_type, fields=()):
        K = "rogue_fwif_kccb_cmd"
        size = self.L.size(K)
        wo = self.get("rogue_fwif_ccb_ctl", self.kccb_ctl, "write_offset")
        slot = self.kccb + wo * size
        self.write(slot, b"\0" * size)
        self.set(K, slot, "cmd_type", cmd_type | 0x2ABC0000)
        for path, value in fields:
            self.set(K, slot, path, value)
        self.set(K, slot, "kccb_flags", 0)
        self.w32(self.kccb_rtn + wo * 4, 0)
        self.set("rogue_fwif_ccb_ctl", self.kccb_ctl, "write_offset",
                 (wo + 1) & ((1 << self.kccb_n) - 1))
        # pvr_fw_mts_schedule(PVR_FWIF_DM_GP): host write to MTS_SCHEDULE
        self.reg_model_write(0x0B00, 0)
        return wo

    def is_parked(self):
        """Stopped right after a `wait` with interrupts disabled: the
        firmware has halted itself (e.g. after a power-off request)."""
        uc = self.uc
        pc = uc.reg_read(UC_MIPS_REG_PC)
        prev = bytes(uc.mem_read(virt_to_phys(pc - 4), 4))
        return prev == b"\x00\x00\x7c\x93" and not uc.reg_read(MC.UC_MIPS_REG_CP0_STATUS) & 1

    def inject(self, name):
        if self.parked:
            print("  %s interrupt not delivered: core parked" % name)
            return
        start = self.IRQ_VECTORS[name]
        uc = self.uc
        st = uc.reg_read(MC.UC_MIPS_REG_CP0_STATUS)
        uc.reg_write(MC.UC_MIPS_REG_CP0_STATUS, st | 0x2)  # EXL
        before = self.insns
        self.insn_limit = self.insns + self.args.max_insns
        self.stop_at_eret = True
        try:
            uc.emu_start(start | 1, 0xFFFFFFFF, count=0)
        except UcError as e:
            print("!! %s irq: %s at pc=0x%08x" % (name, e, uc.reg_read(UC_MIPS_REG_PC)))
            self.report()
            self.dump_hot_polls()
        if self.is_parked():
            self.parked = True
            print("  %s irq: core parked in `wait` with interrupts disabled at 0x%08x "
                  "after %d instructions" % (name, uc.reg_read(UC_MIPS_REG_PC),
                                             self.insns - before))
            return
        uc.reg_write(MC.UC_MIPS_REG_CP0_STATUS, st)
        print("  %s irq handled in %d instructions, stopped at 0x%08x" % (
            name, self.insns - before, uc.reg_read(UC_MIPS_REG_PC)))

    def compute_kick(self):
        """Mirror what the kernel does for a Vulkan compute dispatch
        (pvr_context.c, pvr_queue.c, pvr_cccb.c): a FW memory context, a
        compute context whose common context points at a client CCB, a CDM
        command in that CCB, and a kernel CCB KICK."""
        L = self.L
        if not hasattr(self, "cctx"):
            memctx = self.alloc("fwmemctx", L.size("rogue_fwif_fwmemcontext"))
            self.map_pages(memctx, PAGE)
            self.set("rogue_fwif_fwmemcontext", memctx, "pc_dev_paddr", 0x90000000)
            self.set("rogue_fwif_fwmemcontext", memctx, "page_cat_base_reg_set", 0xFFFFFFFF)
            state = self.alloc("compute_ctx_state", PAGE)
            self.map_pages(state, PAGE)
            cccb_ctl = self.alloc("cccb_ctl", PAGE)
            self.map_pages(cccb_ctl, PAGE)
            cccb = self.alloc("cccb", 4 * PAGE)
            self.map_pages(cccb, 4 * PAGE)
            self.set("rogue_fwif_cccb_ctl", cccb_ctl, "wrap_mask", 4 * PAGE - 1)
            cctx = self.alloc("fwcomputectx", PAGE)
            self.map_pages(cctx, PAGE)
            C = "rogue_fwif_fwcomputecontext"
            for path, v in (("cdm_context.ccbctl_fw_addr", cccb_ctl),
                            ("cdm_context.ccb_fw_addr", cccb),
                            ("cdm_context.dm", 4),               # PVR_FWIF_DM_CDM
                            ("cdm_context.max_deadline_ms", 30000),
                            ("cdm_context.pid", 1),
                            ("cdm_context.server_common_context_id", 1),
                            ("cdm_context.fw_mem_context_fw_addr", memctx),
                            ("cdm_context.context_state_addr", state)):
                self.set(C, cctx, path, v)
            self.cctx, self.cccb, self.cccb_ctl, self.cccb_woff = cctx, cccb, cccb_ctl, 0
        H, X = "rogue_fwif_ccb_cmd_header", "rogue_fwif_cmd_compute"
        hdr = self.cccb + self.cccb_woff
        payload = L.size(X)
        self.set(H, hdr, "cmd_type", 205 | 0x2ABC0000 | 0x8000)   # CCB_CMD_TYPE_CDM
        self.set(H, hdr, "cmd_size", payload)
        self.set(H, hdr, "ext_job_ref", 1)
        self.set(H, hdr, "int_job_ref", 1)
        body = hdr + L.size(H)
        self.set(X, body, "regs.cdm_ctrl_stream_base", 0xE000000000 + 0x1000)
        self.set(X, body, "regs.cdm_context_state_base_addr", 0xE000000000 + 0x2000)
        self.cccb_woff += L.size(H) + payload
        self.set("rogue_fwif_cccb_ctl", self.cccb_ctl, "write_offset", self.cccb_woff)
        slot = self.send_kccb(101, [("cmd_data.cmd_kick_data.context_fw_addr", self.cctx),
                                    ("cmd_data.cmd_kick_data.client_woff_update", self.cccb_woff),
                                    ("cmd_data.cmd_kick_data.client_wrap_mask_update",
                                     4 * PAGE - 1)])
        print("\n== compute kick: KCCB slot %d, cCCB woff %d" % (slot, self.cccb_woff))
        self.inject("bg")
        self.drain_irqs()
        print("  cCCB read_offset=%d" % self.get("rogue_fwif_cccb_ctl", self.cccb_ctl, "read_offset"))
        self.report()
        print("\n== CDM finished (EVENT_STATUS.COMPUTE_FINISHED)")
        self.event_status |= 0x4
        self.inject("irq")
        self.drain_irqs()
        print("  cCCB read_offset=%d" % self.get("rogue_fwif_cccb_ctl", self.cccb_ctl, "read_offset"))
        self.report()

    def dump_hot_polls(self, n=8):
        """Most-read registers in the recent log: what the firmware was
        polling when it gave up."""
        import collections
        recent = self.reg_log[-20000:]
        c = collections.Counter(o for k, o, _ in recent if k == "R")
        print("  most-polled registers recently:")
        for off, cnt in c.most_common(n):
            print("    %-36s +0x%06x  %6d reads, last value 0x%08x" % (
                self.reg_name(off), off, cnt,
                next(v for k, o, v in reversed(recent) if o == off and k == "R")))

    def drain_irqs(self):
        for _ in range(16):
            if not self.pending_tasks:
                break
            self.inject(self.pending_tasks.pop(0))

    def kccb_scenario(self, spec):
        name, _, arg = spec.partition("=")
        fields = []
        if name == "compute":
            self.compute_kick()
            return
        if name == "reboot":
            # Runtime resume: the kernel restarts the firmware without
            # reloading it or touching sysinit.firmware_started; clear the
            # flag here so the new boot is observable.
            print("\n== reboot (runtime resume)")
            self.set("rogue_fwif_sysinit", SYSINIT_VA, "firmware_started", 0)
            self.boot()
            return
        if name == "timer":
            print("\n== CP0 timer interrupt")
            self.inject("timer")
            self.report()
            return
        if name == "pow-idle":
            fields = [("cmd_data.pow_data.pow_type", 2),  # FORCED_IDLE_REQ
                      ("cmd_data.pow_data.power_req_data.pow_request_type", 1)]
        elif name == "pow-cancel-idle":
            fields = [("cmd_data.pow_data.pow_type", 2),
                      ("cmd_data.pow_data.power_req_data.pow_request_type", 2)]
        elif name == "pow-off":
            fields = [("cmd_data.pow_data.pow_type", 1),  # OFF_REQ
                      ("cmd_data.pow_data.power_req_data.forced", 1)]
        elif name == "mmucache":
            # pvr_mmu_flush_exec() with PVR_MMU_SYNC_LEVEL_2_FLAGS
            sync = self.objects["mmucache_sync"][0]
            fields = [("cmd_data.mmu_cache_data.cache_flags", int(arg or "0x400001f", 0)),
                      ("cmd_data.mmu_cache_data.mmu_cache_sync_fw_addr", sync),
                      ("cmd_data.mmu_cache_data.mmu_cache_sync_update_value", 0)]
        elif name == "pow-units":
            fields = [("cmd_data.pow_data.pow_type", 3),  # NUM_UNITS_CHANGE
                      ("cmd_data.pow_data.power_req_data.num_of_dusts", int(arg or "1", 0))]
        slot = self.send_kccb(self.KCCB_CMDS[name], fields)
        print("\n== KCCB %s -> slot %d, MTS kick, background-task interrupt" % (spec, slot))
        self.inject("bg")
        self.drain_irqs()
        print("  kccb read_offset=%d rtn[%d]=0x%x" % (
            self.get("rogue_fwif_ccb_ctl", self.kccb_ctl, "read_offset"), slot,
            self.r32(self.kccb_rtn + slot * 4)))
        self.report()

    def trace(self):
        tp = self.get("rogue_fwif_tracebuf", self.tracebuf_ctl, "tracebuf[0].trace_pointer")
        raw = self.read(self.tracebuf_va, self.trace_dwords * 4)
        dwords = struct.unpack("<%dI" % self.trace_dwords, raw)
        return fwtrace.decode(dwords, self.sf_table, 0, tp)

    def report(self):
        C = "rogue_fwif_connection_ctl"
        print("connection: fw_state=%d os_state=%d alive_fw_token=%d" % (
            self.get(C, CONN_CTL_VA, "connection_fw_state"),
            self.get(C, CONN_CTL_VA, "connection_os_state"),
            self.get(C, CONN_CTL_VA, "alive_fw_token")))
        sysdata = self.objects["sysdata"][0]
        osdata = self.objects["osdata"][0]
        print("sysdata: pow_state=%d  osdata: kccb_cmds_executed=%d  power_sync=%d" % (
            self.get("rogue_fwif_sysdata", sysdata, "pow_state"),
            self.get("rogue_fwif_osdata", osdata, "kccb_cmds_executed"),
            self.r32(self.objects["power_sync"][0])))
        writes = [(o, v) for k, o, v in self.reg_log if k == "W"]
        print("register writes: %d (%d distinct registers), reads: %d" % (
            len(writes), len({o for o, _ in writes}),
            sum(1 for k, _, _ in self.reg_log if k == "R")))
        if self.args.trace_mask:
            entries = self.trace()
            print("firmware trace:")
            for ts, msg in entries[self.trace_seen:]:
                print("  [%d] %s" % (ts, msg))
            self.trace_seen = len(entries)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fw")
    ap.add_argument("--kernel", required=True)
    ap.add_argument("--layout", default=os.path.join(HERE, "layout.json"))
    ap.add_argument("--config-flags", type=lambda x: int(x, 0), default=0)
    ap.add_argument("--trace-mask", type=lambda x: int(x, 0), default=0x80007FFF,
                    help="firmware log_type (default: all groups); 0 disables")
    ap.add_argument("--max-insns", type=int, default=2_000_000)
    ap.add_argument("--trace-regs", action="store_true")
    ap.add_argument("--trace-exc", action="store_true")
    ap.add_argument("--no-complete-polls", dest="complete_polls", action="store_false",
                    help="do not satisfy the firmware's register polls automatically")
    ap.add_argument("--kccb", action="append",
                    help="after boot, in order: health, pow-idle, pow-cancel-idle, pow-units=N, "
                         "pow-off, logtype, mmucache[=FLAGS], reboot (runtime resume), compute (a CDM kick + completion), "
                         "or timer (CP0 timer interrupt)")
    ap.add_argument("--watch", action="append",
                    help="log writes to ADDR or LO-HI (virtual addresses)")
    args = ap.parse_args()
    fw = Firmware(args.fw, Tables(args.kernel))
    emu = Emu(fw, Layout(args.layout), load_cr_names(args.kernel), args)
    emu.sf_table = fwtrace.load_sf_table(args.kernel)
    emu.run()


if __name__ == "__main__":
    main()

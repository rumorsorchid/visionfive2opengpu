#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
pvrfw.py - inspect, compare and verify PowerVR Rogue firmware images in the
format loaded by the upstream Linux drm/imagination ("powervr") driver.

Container layout (drivers/gpu/drm/imagination/pvr_fw_info.h):

    +-----------------------+ 0
    | original ELF / .ldr   |
    +-----------------------+ FILE_SIZE - 4K - device_info_size
    | device info           |   (BRN mask, ERN mask, feature mask, params)
    +-----------------------+ FILE_SIZE - 4K
    | pvr_fw_info_header    |
    | layout table          |
    +-----------------------+ FILE_SIZE

Sub-commands:
    info FW                       decode header, layout, device info, ELF
    diff FW_A FW_B                compare two images
    check-ddk FW --ddk DIR        cross-check device info against the
                                  Imagination DDK per-core headers (hwdefs)
    extract FW OUTDIR             dump each layout section to its own file
    devinfo FW                    print the device info as JSON (pack's input)
    pack --elf E --device J -o FW wrap a MIPS firmware ELF into the container,
                                  with device info encoded from JSON

Bit -> name orderings are read from the kernel's pvr_rogue_fwif_dev_info.h
when --kernel points at a Linux tree; a built-in copy (from v7.3-rc5) is
used otherwise.
"""

import argparse
import hashlib
import os
import re
import struct
import sys

FW_BLOCK = 4096
INFO_HDR = struct.Struct("<IIIIQIIHHIII")
LAYOUT_ENTRY = struct.Struct("<IIIIII")
DEVINFO_HDR = struct.Struct("<QQQQ")

SECTION_IDS = [
    "META_CODE", "META_PRIVATE_DATA", "META_COREMEM_CODE", "META_COREMEM_DATA",
    "MIPS_CODE", "MIPS_EXCEPTIONS_CODE", "MIPS_BOOT_CODE", "MIPS_PRIVATE_DATA",
    "MIPS_BOOT_DATA", "MIPS_STACK", "RISCV_UNCACHED_CODE", "RISCV_CACHED_CODE",
    "RISCV_PRIVATE_DATA", "RISCV_COREMEM_CODE", "RISCV_COREMEM_DATA",
]
SECTION_TYPES = ["NONE", "FW_CODE", "FW_DATA", "FW_COREMEM_CODE", "FW_COREMEM_DATA"]

# Snapshot of pvr_rogue_fwif_dev_info.h (Linux v7.3-rc5).
BUILTIN_BRNS = [44079, 47217, 48492, 48545, 49927, 50767, 51764, 62269, 63142,
                63553, 66011, 71242]
BUILTIN_ERNS = [35421, 38020, 38748, 42064, 42290, 42606, 47025, 57596]
BUILTIN_FEATURES = """
AXI_ACELITE CDM_CONTROL_STREAM_FORMAT CLUSTER_GROUPING COMMON_STORE_SIZE_IN_DWORDS
COMPUTE COMPUTE_MORTON_CAPABLE COMPUTE_OVERLAP COREID_PER_OS DYNAMIC_DUST_POWER
ECC_RAMS FBCDC FBCDC_ALGORITHM FBCDC_ARCHITECTURE FBC_MAX_DEFAULT_DESCRIPTORS
FBC_MAX_LARGE_DESCRIPTORS FB_CDC_V4 GPU_MULTICORE_SUPPORT GPU_VIRTUALISATION
GS_RTA_SUPPORT IRQ_PER_OS ISP_MAX_TILES_IN_FLIGHT ISP_SAMPLES_PER_PIXEL
ISP_ZLS_D24_S8_PACKING_OGL_MODE LAYOUT_MARS MAX_PARTITIONS META META_COREMEM_SIZE
MIPS NUM_CLUSTERS NUM_ISP_IPP_PIPES NUM_OSIDS NUM_RASTER_PIPES PBE2_IN_XE
PBVNC_COREID_REG PERFBUS PERF_COUNTER_BATCH PHYS_BUS_WIDTH RISCV_FW_PROCESSOR
ROGUEXE S7_TOP_INFRASTRUCTURE SIMPLE_INTERNAL_PARAMETER_FORMAT
SIMPLE_INTERNAL_PARAMETER_FORMAT_V2 SIMPLE_PARAMETER_FORMAT_VERSION SLC_BANKS
SLC_CACHE_LINE_SIZE_BITS SLC_SIZE_CONFIGURABLE SLC_SIZE_IN_KILOBYTES SOC_TIMER
SYS_BUS_SECURE_RESET TESSELLATION TILE_REGION_PROTECTION TILE_SIZE_X TILE_SIZE_Y
TLA TPU_CEM_DATAMASTER_GLOBAL_REGISTERS TPU_DM_GLOBAL_REGISTERS
TPU_FILTERING_MODE_CONTROL USC_MIN_OUTPUT_REGISTERS_PER_PIX VDM_DRAWINDIRECT
VDM_OBJECT_LEVEL_LLS VIRTUAL_ADDRESS_SPACE_BITS WATCHDOG_TIMER WORKGROUP_PROTECTION
XE_ARCHITECTURE XE_MEMORY_HIERARCHY XE_TPU2 XPU_MAX_REGBANKS_ADDR_WIDTH
XPU_MAX_SLAVES XPU_REGISTER_BROADCAST XT_TOP_INFRASTRUCTURE ZLS_SUBTILE
""".split()

# Features whose presence carries a parameter value (FEATURE_MAPPING_VALUE in
# pvr_device_info.c). Snapshot of v7.3-rc5.
BUILTIN_VALUED = set("""
CDM_CONTROL_STREAM_FORMAT COMMON_STORE_SIZE_IN_DWORDS ECC_RAMS FBCDC
FBCDC_ALGORITHM FBCDC_ARCHITECTURE FBC_MAX_DEFAULT_DESCRIPTORS
FBC_MAX_LARGE_DESCRIPTORS ISP_MAX_TILES_IN_FLIGHT ISP_SAMPLES_PER_PIXEL LAYOUT_MARS
MAX_PARTITIONS META META_COREMEM_SIZE NUM_CLUSTERS NUM_ISP_IPP_PIPES NUM_OSIDS
NUM_RASTER_PIPES PHYS_BUS_WIDTH SIMPLE_PARAMETER_FORMAT_VERSION SLC_BANKS
SLC_CACHE_LINE_SIZE_BITS SLC_SIZE_IN_KILOBYTES TILE_SIZE_X TILE_SIZE_Y
USC_MIN_OUTPUT_REGISTERS_PER_PIX VIRTUAL_ADDRESS_SPACE_BITS XE_ARCHITECTURE
XPU_MAX_REGBANKS_ADDR_WIDTH XPU_MAX_SLAVES XPU_REGISTER_BROADCAST
""".split())


class Tables:
    def __init__(self, kernel=None):
        self.brns, self.erns = BUILTIN_BRNS, BUILTIN_ERNS
        self.features, self.valued = BUILTIN_FEATURES, BUILTIN_VALUED
        self.source = "built-in (v7.3-rc5)"
        if kernel:
            self._load(kernel)

    def _load(self, kernel):
        base = os.path.join(kernel, "drivers/gpu/drm/imagination")
        hdr = open(os.path.join(base, "pvr_rogue_fwif_dev_info.h")).read()
        self.brns = [int(x) for x in re.findall(r"PVR_FW_HAS_BRN_(\d+)", hdr)]
        self.erns = [int(x) for x in re.findall(r"PVR_FW_HAS_ERN_(\d+)", hdr)]
        self.features = [x for x in re.findall(r"PVR_FW_HAS_FEATURE_(\w+)", hdr)
                         if x != "MAX"]
        src = open(os.path.join(base, "pvr_device_info.c")).read()
        self.valued = set(re.findall(r"FEATURE_MAPPING_VALUE\((\w+),", src))
        self.source = base


def bits(masks):
    for i, m in enumerate(masks):
        for b in range(64):
            if m >> b & 1:
                yield i * 64 + b


class Firmware:
    def __init__(self, path, tables):
        self.path = path
        self.data = open(path, "rb").read()
        self.t = tables
        d = self.data
        if len(d) < FW_BLOCK or len(d) % FW_BLOCK:
            raise ValueError("size %d is not a non-zero multiple of 4K" % len(d))
        off = len(d) - FW_BLOCK
        (self.info_version, self.header_len, self.layout_num, self.layout_size,
         self.bvnc, self.fw_page_size, self.flags, self.ver_major, self.ver_minor,
         self.ver_build, self.devinfo_size, _pad) = INFO_HDR.unpack_from(d, off)
        if self.info_version != 3:
            raise ValueError("unsupported info_version %d" % self.info_version)
        self.layout = []
        for i in range(self.layout_num):
            e = LAYOUT_ENTRY.unpack_from(d, off + self.header_len + i * self.layout_size)
            self.layout.append(dict(id=e[0], type=e[1], base=e[2], max_size=e[3],
                                    alloc_size=e[4], alloc_offset=e[5]))
        doff = off - self.devinfo_size
        brn_n, ern_n, feat_n, param_n = DEVINFO_HDR.unpack_from(d, doff)
        q = doff + DEVINFO_HDR.size

        def take(n):
            nonlocal q
            v = list(struct.unpack_from("<%dQ" % n, d, q))
            q += 8 * n
            return v

        self.brn_mask, self.ern_mask = take(brn_n), take(ern_n)
        self.feat_mask, self.params = take(feat_n), take(param_n)
        self.image_end = doff

    # -- decoded views -------------------------------------------------------
    @property
    def bvnc_str(self):
        b = self.bvnc
        return "%d.%d.%d.%d" % (b >> 48, b >> 32 & 0xffff, b >> 16 & 0xffff, b & 0xffff)

    def quirks(self):
        out = []
        for b in bits(self.brn_mask):
            out.append(self.t.brns[b] if b < len(self.t.brns) else "unknown-bit-%d" % b)
        return out

    def enhancements(self):
        out = []
        for b in bits(self.ern_mask):
            out.append(self.t.erns[b] if b < len(self.t.erns) else "unknown-bit-%d" % b)
        return out

    def features(self):
        """Return ordered dict name -> value (None for boolean features).
        Mirrors pvr_device_info_set_features(): params are consumed in bit
        order for features listed as FEATURE_MAPPING_VALUE."""
        out, p = {}, 0
        for b in bits(self.feat_mask):
            name = self.t.features[b] if b < len(self.t.features) else "unknown-bit-%d" % b
            if name in self.t.valued:
                out[name] = self.params[p] if p < len(self.params) else "MISSING"
                p += 1
            else:
                out[name] = None
        self.unused_params = self.params[p:]
        return out

    def elf(self):
        d = self.data
        if d[:4] != b"\x7fELF":
            return None
        cls, endian = d[4], d[5]
        fmt = "<" if endian == 1 else ">"
        if cls != 1:
            return dict(note="64-bit ELF not decoded")
        (e_type, e_machine, _v, e_entry, e_phoff, e_shoff, e_flags, _eh, phentsize,
         phnum, _shentsize, _shnum, _shstrndx) = struct.unpack_from(fmt + "HHIIIIIHHHHHH", d, 16)
        phdrs = []
        for i in range(phnum):
            p = struct.unpack_from(fmt + "IIIIIIII", d, e_phoff + i * phentsize)
            phdrs.append(dict(type=p[0], offset=p[1], vaddr=p[2], paddr=p[3],
                              filesz=p[4], memsz=p[5], flags=p[6], align=p[7]))
        machines = {8: "MIPS", 243: "RISC-V", 174: "META"}
        isa = []
        if e_machine == 8:
            arch = (e_flags >> 28) & 0xf
            isa.append({0: "mips1", 5: "mips32", 6: "mips64", 7: "mips32r2",
                        8: "mips64r2", 9: "mips32r6"}.get(arch, "arch%d" % arch))
            if e_flags & 0x02000000:
                isa.append("microMIPS")
        return dict(machine=machines.get(e_machine, str(e_machine)), entry=e_entry,
                    flags=e_flags, isa=isa, phdrs=phdrs,
                    endian="little" if endian == 1 else "big")

    def strings(self, minlen=6):
        return set(m.group().decode() for m in
                   re.finditer(rb"[\x20-\x7e]{%d,}" % minlen, self.data[:self.image_end]))

    def source_files(self):
        return sorted(s for s in self.strings() if re.match(r"^[\w/]+\.c$", s))


def fmt_val(v):
    return "" if v is None else " = %s" % v


def cmd_info(fw, args):
    print("file          %s" % fw.path)
    print("size          %d bytes (%d x 4K)" % (len(fw.data), len(fw.data) // FW_BLOCK))
    print("sha256        %s" % hashlib.sha256(fw.data).hexdigest())
    print("bvnc          %s" % fw.bvnc_str)
    print("fw version    v%d.%d build %d%s" % (fw.ver_major, fw.ver_minor, fw.ver_build,
                                              " OS" if fw.flags & 1 else ""))
    print("fw page size  %d" % fw.fw_page_size)
    print("tables from   %s" % fw.t.source)
    print("\nlayout table:")
    print("  %-22s %-9s %-10s %9s %9s %9s" % ("section", "type", "fw-addr", "max", "alloc",
                                             "offset"))
    for e in fw.layout:
        print("  %-22s %-9s 0x%08x %9d %9d %9d" % (
            SECTION_IDS[e["id"]] if e["id"] < len(SECTION_IDS) else e["id"],
            SECTION_TYPES[e["type"]] if e["type"] < len(SECTION_TYPES) else e["type"],
            e["base"], e["max_size"], e["alloc_size"], e["alloc_offset"]))
    print("\nquirks (BRN):       %s" % ", ".join(map(str, fw.quirks())))
    print("enhancements (ERN): %s" % ", ".join(map(str, fw.enhancements())))
    print("features:")
    for k, v in fw.features().items():
        print("  %s%s" % (k, fmt_val(v)))
    if fw.unused_params:
        print("  WARNING: %d unconsumed feature params: %s" % (len(fw.unused_params),
                                                           fw.unused_params))
    e = fw.elf()
    if e:
        print("\nELF: %s %s-endian %s entry=0x%08x flags=0x%08x" % (
            e["machine"], e["endian"], "+".join(e["isa"]), e["entry"], e["flags"]))
        for p in e["phdrs"]:
            print("  PT %-2d off=0x%06x vaddr=0x%08x filesz=%7d memsz=%7d flags=%s" % (
                p["type"], p["offset"], p["vaddr"], p["filesz"], p["memsz"],
                "".join(c for c, m in (("R", 4), ("W", 2), ("X", 1)) if p["flags"] & m)))
    if args.verbose:
        print("\nFW source files referenced by asserts:")
        for s in fw.source_files():
            print("  " + s)
    return 0


def cmd_diff(a, b, args):
    rc = 0

    def row(label, va, vb):
        nonlocal rc
        mark = "  " if va == vb else "!="
        if va != vb:
            rc = 1
        print("%s %-16s %-36s %s" % (mark, label, va, vb))

    print("   %-16s %-36s %s" % ("", os.path.basename(a.path), os.path.basename(b.path)))
    row("bvnc", a.bvnc_str, b.bvnc_str)
    row("version", "v%d.%d b%d" % (a.ver_major, a.ver_minor, a.ver_build),
        "v%d.%d b%d" % (b.ver_major, b.ver_minor, b.ver_build))
    row("size", len(a.data), len(b.data))
    row("layout", [(SECTION_IDS[e["id"]], hex(e["base"]), e["alloc_size"]) for e in a.layout] ==
        [(SECTION_IDS[e["id"]], hex(e["base"]), e["alloc_size"]) for e in b.layout], True)
    row("BRNs", a.quirks(), b.quirks())
    row("ERNs", a.enhancements(), b.enhancements())
    fa, fb = a.features(), b.features()
    for k in sorted(set(fa) | set(fb)):
        va = fa.get(k, "-") if k in fa else "absent"
        vb = fb.get(k, "-") if k in fb else "absent"
        if va != vb:
            row(k, "present" if va is None else va, "present" if vb is None else vb)
    ea, eb = a.elf() or {}, b.elf() or {}
    for i, (pa, pb) in enumerate(zip(ea.get("phdrs", []), eb.get("phdrs", []))):
        row("PT%d vaddr/memsz" % i, "0x%08x/%d" % (pa["vaddr"], pa["memsz"]),
            "0x%08x/%d" % (pb["vaddr"], pb["memsz"]))
    sa, sb = set(a.source_files()), set(b.source_files())
    if sa != sb:
        print("!= FW source files only in A: %s" % sorted(sa - sb))
        print("!= FW source files only in B: %s" % sorted(sb - sa))
    if args.verbose:
        ta, tb = a.strings(12), b.strings(12)
        print("\nstrings only in A (%d):" % len(ta - tb))
        for s in sorted(ta - tb):
            print("  < " + s)
        print("strings only in B (%d):" % len(tb - ta))
        for s in sorted(tb - ta):
            print("  > " + s)
    return rc


def parse_ddk_core(ddk, bvnc):
    """Return (features dict, brns set, erns set) from the DDK hwdefs."""
    b, v, n, c = bvnc.split(".")
    km = os.path.join(ddk, "hwdefs/rogue/km")
    cfg = open(os.path.join(km, "configs/rgxconfig_km_%s.V.%s.%s.h" % (b, n, c))).read()
    core = open(os.path.join(km, "cores/rgxcore_km_%s.h" % bvnc)).read()
    feats = {}
    for m in re.finditer(r"#define RGX_FEATURE_(\w+)(?:[ \t]+\((\d+)U?\))?", cfg):
        feats[m.group(1)] = int(m.group(2)) if m.group(2) else None
    brns = set(int(x) for x in re.findall(r"#define FIX_HW_BRN_(\d+)", core))
    erns = set(int(x) for x in re.findall(r"#define HW_ERN_(\d+)", core))
    return feats, brns, erns


def cmd_check_ddk(fw, args):
    feats, brns, erns = parse_ddk_core(args.ddk, fw.bvnc_str)
    rc = 0
    known_brns, known_erns = set(fw.t.brns), set(fw.t.erns)
    fq, fe = set(fw.quirks()), set(fw.enhancements())
    print("BVNC %s: firmware v%d.%d b%d vs DDK hwdefs in %s\n" % (
        fw.bvnc_str, fw.ver_major, fw.ver_minor, fw.ver_build, args.ddk))

    print("BRNs   DDK: %s" % sorted(brns))
    print("       FW : %s" % sorted(fq))
    for x in sorted(brns - fq):
        tag = "MISMATCH" if x in known_brns else "info (driver has no handling for it)"
        rc |= x in known_brns
        print("   BRN %-6d in DDK, not in FW image   -> %s" % (x, tag))
    for x in sorted(fq - brns):
        print("   BRN %-6s in FW image, not in DDK   -> review (newer core DB?)" % x)

    print("ERNs   DDK: %s" % sorted(erns))
    print("       FW : %s" % sorted(fe))
    for x in sorted(erns - fe):
        tag = "MISMATCH" if x in known_erns else "info (driver has no handling for it)"
        rc |= x in known_erns
        print("   ERN %-6d in DDK, not in FW image   -> %s" % (x, tag))
    for x in sorted(fe - erns):
        # The FW device info is generated from a newer Imagination core
        # database than a given DDK release; an extra ERN means "newer DB
        # knows this enhancement exists", not a contradiction.
        print("   ERN %-6s in FW image, not in DDK   -> review (newer core DB?)" % x)

    ff = fw.features()
    # The DDK's hwdefs/rogue/km headers only carry features the *kernel* side
    # needs; user-mode-only features (common store size, ISP tiles in flight,
    # XE_TPU2, ...) are absent there by design. A feature present in the FW
    # image but absent from the KM header is therefore informational. A
    # feature the DDK KM header has but the FW lacks, or a value mismatch,
    # is a real discrepancy.
    print("\nfeatures (only names the upstream driver knows are compared):")
    for name in fw.t.features:
        in_ddk, in_fw = name in feats, name in ff
        if not in_ddk and not in_fw:
            continue
        if in_ddk and in_fw and feats[name] == ff[name]:
            print("   ok        %s%s" % (name, fmt_val(ff[name])))
        elif in_fw and not in_ddk:
            print("   fw-only   %-40s (not in DDK KM hwdefs)%s" % (name, fmt_val(ff[name])))
        else:
            rc = 1
            print("   MISMATCH  %-40s DDK:%s FW:%s" % (
                name, ("yes" + fmt_val(feats[name])) if in_ddk else "no",
                ("yes" + fmt_val(ff[name])) if in_fw else "no"))
    extra = sorted(set(feats) - set(fw.t.features))
    print("\nDDK-only features (no upstream mapping, informational): %s" % ", ".join(extra))
    print("\nresult: %s" % ("MISMATCHES FOUND" if rc else "device info matches DDK"))
    return rc


def cmd_extract(fw, args):
    os.makedirs(args.outdir, exist_ok=True)
    e = fw.elf()
    if not e:
        print("not an ELF image; nothing to extract")
        return 1
    # Reproduce what pvr_fw_process_elf_command_stream() does: copy each
    # PT_LOAD segment into the layout section whose FW address range covers it.
    out = {}
    for p in e["phdrs"]:
        if p["type"] != 1 or not p["memsz"]:
            continue
        for s in fw.layout:
            if s["base"] <= p["vaddr"] < s["base"] + s["max_size"]:
                buf = out.setdefault(s["id"], bytearray(s["alloc_size"]))
                o = p["vaddr"] - s["base"]
                seg = fw.data[p["offset"]:p["offset"] + p["filesz"]]
                buf[o:o + len(seg)] = seg
                break
        else:
            print("warning: segment at 0x%08x not covered by layout" % p["vaddr"])
    for sid, buf in out.items():
        name = SECTION_IDS[sid].lower()
        base = next(s["base"] for s in fw.layout if s["id"] == sid)
        path = os.path.join(args.outdir, "%s@%08x.bin" % (name, base))
        open(path, "wb").write(buf)
        print("wrote %s (%d bytes)" % (path, len(buf)))
    return 0


# -- packing -------------------------------------------------------------------
PVR_FW_FLAGS_OPEN_SOURCE = 1
# MIPS layout (pvr_rogue_mips.h / the Imagination build): id, type, base,
# max size. Allocation sizes follow the ELF; boot data and stack are one
# page each and carry no ELF content.
MIPS_LAYOUT = [
    ("MIPS_CODE", 1, 0xC0000000, 192512),
    ("MIPS_EXCEPTIONS_CODE", 1, 0x9FC02000, 8192),
    ("MIPS_BOOT_CODE", 1, 0xBFC00000, 4096),
    ("MIPS_PRIVATE_DATA", 2, 0xC0032000, 32768),
    ("MIPS_BOOT_DATA", 2, 0xBFC01000, 4096),
    ("MIPS_STACK", 2, 0xCF600000, 4096),
]
MIPS_MIN_ALLOC = {"MIPS_PRIVATE_DATA": 3 * FW_BLOCK}


def roundup(x, a=FW_BLOCK):
    return (x + a - 1) // a * a


def devinfo_json(fw):
    feats = fw.features()
    return {"bvnc": fw.bvnc_str, "brns": fw.quirks(), "erns": fw.enhancements(),
            "features": feats}


def encode_devinfo(t, dev):
    """Inverse of Firmware's device-info parsing (pvr_device_info.c)."""
    def mask(names, table, what):
        bitset = 0
        for n in names:
            if n not in table:
                raise ValueError("%s %s unknown to the kernel tables" % (what, n))
            bitset |= 1 << table.index(n)
        nq = max(1, (bitset.bit_length() + 63) // 64)
        return [(bitset >> (64 * i)) & (2**64 - 1) for i in range(nq)]

    brn = mask(dev["brns"], t.brns, "BRN")
    ern = mask(dev["erns"], t.erns, "ERN")
    feat = mask(dev["features"], t.features, "feature")
    params = []
    for name in t.features:          # params in feature-bit order
        if name in dev["features"] and name in t.valued:
            v = dev["features"][name]
            if v is None:
                raise ValueError("feature %s needs a value" % name)
            params.append(int(v))
        elif name in dev["features"] and dev["features"][name] is not None:
            raise ValueError("feature %s takes no value" % name)
    blob = DEVINFO_HDR.pack(len(brn), len(ern), len(feat), len(params))
    blob += struct.pack("<%dQ" % (len(brn) + len(ern) + len(feat) + len(params)),
                        *(brn + ern + feat + params))
    if len(blob) > FW_BLOCK:
        raise ValueError("device info exceeds 4 KiB")
    return blob + b"\0" * (FW_BLOCK - len(blob))


def mips_layout(elf_info):
    """Layout table for a MIPS ELF: same sections, bases and limits as
    Imagination's images, allocation sizes from the ELF's segments."""
    need = {}
    for p in elf_info["phdrs"]:
        if p["type"] != 1 or not p["memsz"]:
            continue
        for name, _typ, base, maxsz in MIPS_LAYOUT:
            if base <= p["vaddr"] < base + maxsz:
                end = p["vaddr"] + p["memsz"] - base
                if end > maxsz:
                    raise ValueError("segment at 0x%08x exceeds %s (%d > %d bytes)" % (
                        p["vaddr"], name, end, maxsz))
                need[name] = max(need.get(name, 0), end)
                break
        else:
            raise ValueError("segment at 0x%08x is in no MIPS layout section" % p["vaddr"])
    entries, offset = [], {1: 0, 2: 0}
    for name, typ, base, maxsz in MIPS_LAYOUT:
        alloc = max(roundup(need.get(name, 0)) or FW_BLOCK, MIPS_MIN_ALLOC.get(name, 0))
        entries.append(dict(id=SECTION_IDS.index(name), type=typ, base=base, max_size=maxsz,
                            alloc_size=alloc, alloc_offset=offset[typ]))
        offset[typ] += alloc
    code_end = 0xC0000000 + offset[1]
    if code_end > MIPS_LAYOUT[3][2]:
        raise ValueError("code allocation (0x%x) overlaps private data at 0x%08x" % (
            offset[1], MIPS_LAYOUT[3][2]))
    return entries


def pack(elf_bytes, layout, devinfo, bvnc, version, flags=PVR_FW_FLAGS_OPEN_SOURCE):
    major, minor, build = version
    hdr = INFO_HDR.pack(3, INFO_HDR.size, len(layout), LAYOUT_ENTRY.size, bvnc, FW_BLOCK,
                        flags, major, minor, build, len(devinfo), 0)
    for e in layout:
        hdr += LAYOUT_ENTRY.pack(e["id"], e["type"], e["base"], e["max_size"],
                                 e["alloc_size"], e["alloc_offset"])
    hdr += b"\0" * (FW_BLOCK - len(hdr))
    body = elf_bytes + b"\0" * (roundup(len(elf_bytes)) - len(elf_bytes))
    return body + devinfo + hdr


def parse_bvnc(s):
    b, v, n, c = (int(x) for x in s.split("."))
    return b << 48 | v << 32 | n << 16 | c


def cmd_devinfo(fw, args):
    import json
    print(json.dumps(devinfo_json(fw), indent=2))
    return 0


def cmd_pack(args, t):
    import json
    dev = json.load(open(args.device))
    elf_bytes = open(args.elf, "rb").read()
    tmp = Firmware.__new__(Firmware)
    tmp.data = elf_bytes
    info = Firmware.elf(tmp)
    if not info or info.get("machine") != "MIPS":
        print("%s: not a MIPS ELF" % args.elf)
        return 1
    if args.layout_from:
        layout = Firmware(args.layout_from, t).layout
    else:
        layout = mips_layout(info)
    version = tuple(int(x) for x in args.version.split("."))
    out = pack(elf_bytes, layout, encode_devinfo(t, dev), parse_bvnc(dev["bvnc"]), version)
    # Round-trip through the parser the other sub-commands (and fwemu) use.
    open(args.output, "wb").write(out)
    fw = Firmware(args.output, t)
    if devinfo_json(fw) != {"bvnc": dev["bvnc"], "brns": sorted(dev["brns"], key=t.brns.index),
                            "erns": sorted(dev["erns"], key=t.erns.index),
                            "features": {k: dev["features"][k] for k in t.features
                                         if k in dev["features"]}}:
        print("internal error: device info does not round-trip")
        return 1
    print("wrote %s: %d bytes, %s v%d.%d build %d, %d layout entries" % (
        args.output, len(out), dev["bvnc"], version[0], version[1], version[2], len(layout)))
    if args.compare:
        ref = Firmware(args.compare, t)
        a = out[-2 * FW_BLOCK:-FW_BLOCK]
        b = ref.data[-2 * FW_BLOCK:-FW_BLOCK]
        print("device info vs %s: %s" % (args.compare, "identical" if a == b else "DIFFERENT"))
        if a != b:
            return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kernel", help="Linux source tree for enum tables")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("info")
    s.add_argument("fw")
    s = sub.add_parser("diff")
    s.add_argument("a")
    s.add_argument("b")
    s = sub.add_parser("check-ddk")
    s.add_argument("fw")
    s.add_argument("--ddk", required=True, help="img-rogue DDK source directory")
    s = sub.add_parser("extract")
    s.add_argument("fw")
    s.add_argument("outdir")
    s = sub.add_parser("devinfo")
    s.add_argument("fw")
    s = sub.add_parser("pack")
    s.add_argument("--elf", required=True)
    s.add_argument("--device", required=True, help="device description JSON")
    s.add_argument("--version", default="1.0.0", help="MAJOR.MINOR.BUILD")
    s.add_argument("--layout-from", help="copy the layout table from this image")
    s.add_argument("--compare", help="require identical device info to this image")
    s.add_argument("-o", "--output", required=True)
    args = ap.parse_args()
    t = Tables(args.kernel)
    if args.cmd == "pack":
        return cmd_pack(args, t)
    if args.cmd == "diff":
        return cmd_diff(Firmware(args.a, t), Firmware(args.b, t), args)
    fw = Firmware(args.fw, t)
    return {"info": cmd_info, "check-ddk": cmd_check_ddk, "extract": cmd_extract,
            "devinfo": cmd_devinfo}[args.cmd](fw, args)


if __name__ == "__main__":
    sys.exit(main())

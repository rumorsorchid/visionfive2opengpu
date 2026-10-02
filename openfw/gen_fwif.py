#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
gen_fwif.py - generate fwif.h for openfw: byte offsets of the firmware
interface structures as the (64-bit) Linux driver lays them out, plus
trace string-format IDs.

The driver's pvr_rogue_fwif*.h headers are written for an LP64 host; a
few structures carry host pointers, so compiling them for the 32-bit
firmware would shift fields. Offsets therefore come from layout.json
(see tools/fwemu/extract_layout.py), which is the host's view.

    gen_fwif.py --layout ../tools/fwemu/layout.json --kernel LINUX -o fwif.h
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "fwemu"))
import fwtrace  # noqa: E402
from fwemu import Layout  # noqa: E402

FIELDS = {
    "rogue_fwif_sysinit": ["firmware_started", "fault_phys_addr", "fw_sys_data_fw_addr",
                           "runtime_cfg_fw_addr", "trace_buf_ctl_fw_addr",
                           "initial_core_clock_speed"],
    "rogue_fwif_osinit": ["kernel_ccbctl_fw_addr", "kernel_ccb_fw_addr",
                          "kernel_ccb_rtn_slots_fw_addr", "firmware_ccbctl_fw_addr",
                          "firmware_ccb_fw_addr", "fw_os_data_fw_addr"],
    "rogue_fwif_osdata": ["power_sync_fw_addr", "kccb_cmds_executed", "fw_os_config_flags"],
    "rogue_fwif_sysdata": ["config_flags", "pow_state"],
    "rogue_fwif_ccb_ctl": ["write_offset", "read_offset", "wrap_mask", "cmd_size"],
    "rogue_fwif_kccb_cmd": ["cmd_type", "kccb_flags", "cmd_data",
                            "cmd_data.pow_data.pow_type",
                            "cmd_data.pow_data.power_req_data.num_of_dusts",
                            "cmd_data.pow_data.power_req_data.forced",
                            "cmd_data.pow_data.power_req_data.pow_request_type",
                            "cmd_data.mmu_cache_data.cache_flags",
                            "cmd_data.mmu_cache_data.mmu_cache_sync_fw_addr",
                            "cmd_data.mmu_cache_data.mmu_cache_sync_update_value"],
    "rogue_fwif_connection_ctl": ["connection_fw_state", "connection_os_state",
                                  "alive_fw_token"],
    "rogue_fwif_tracebuf": ["log_type", "tracebuf_size_in_dwords",
                            "tracebuf[0].trace_pointer", "tracebuf[0].trace_buffer_fw_addr"],
    "rogue_mipsfw_boot_data": ["stack_phys_addr", "reg_base", "pt_phys_addr",
                               "pt_log2_page_size", "pt_num_pages"],
}

# Structures emitted in full: every scalar member, flattened, as
# OFF_<STRUCT>_<PATH>; arrays of scalars longer than ARRAY_FLATTEN as their
# base offset plus STRIDE_<STRUCT>_<PATH>.
FULL = [
    "rogue_fwif_fwcommoncontext", "rogue_fwif_cccb_ctl", "rogue_fwif_ccb_cmd_header",
    "rogue_fwif_ufo", "rogue_fwif_cleanup_ctl", "rogue_fwif_fwmemcontext",
    "rogue_fwif_kccb_cmd_kick_data", "rogue_fwif_cleanup_request", "rogue_fwif_freelist_gs_data",
    "rogue_fwif_cmd_compute", "rogue_fwif_cmd_transfer", "rogue_fwif_cmd_geom",
    "rogue_fwif_cmd_frag", "rogue_fwif_fwcomputecontext", "rogue_fwif_fwrendercontext",
    "rogue_fwif_fwtransfercontext", "rogue_fwif_geom_ctx_state", "rogue_fwif_frag_ctx_state",
    "rogue_fwif_hwrtdata", "rogue_fwif_hwrtdata_common", "rogue_fwif_freelist",
    "rogue_fwif_fwccb_cmd", "rogue_fwif_fwccb_cmd_freelist_gs_data",
    "rogue_fwif_fwccb_cmd_context_reset_data",
]
ARRAY_FLATTEN = 8
EXTRA_FIELDS = {
    "rogue_fwif_sysinit": ["pds_exec_base", "usc_exec_base"],
    "rogue_fwif_kccb_cmd": ["cmd_data.cmd_kick_data", "cmd_data.combined_geom_frag_cmd_kick_data",
                            "cmd_data.combined_geom_frag_cmd_kick_data.geom_cmd_kick_data",
                            "cmd_data.combined_geom_frag_cmd_kick_data.frag_cmd_kick_data",
                            "cmd_data.cleanup_data", "cmd_data.free_list_gs_data"],
}


def leaves(L, sname, prefix="", base=0):
    """(path, offset, size, count, stride) for every scalar member."""
    st = L.structs[sname] if sname in L.structs else L.anon[sname]
    for m in st["members"]:
        t = m["type"]
        off = base + m["offset"]
        name = prefix + m["name"]
        if t["kind"] in ("struct", "union"):
            sub = t.get("name") if t.get("name") in L.structs else str(t.get("die_offset"))
            yield (name, off, t["size"], 0, 0)
            yield from leaves(L, sub, name + ".", off)
        elif t["kind"] == "array":
            e = t["elem"]
            n = t["size"] // e["size"]
            if e["kind"] in ("struct", "union"):
                sub = e.get("name") if e.get("name") in L.structs else str(e.get("die_offset"))
                yield (name, off, t["size"], n, e["size"])
                for i in range(n):
                    yield from leaves(L, sub, "%s[%d]." % (name, i), off + i * e["size"])
            elif n > ARRAY_FLATTEN:
                yield (name, off, t["size"], n, e["size"])
            else:
                yield (name, off, t["size"], n, e["size"])
                for i in range(n):
                    yield ("%s[%d]" % (name, i), off + i * e["size"], e["size"], 0, 0)
        else:
            yield (name, off, t["size"], 0, 0)


def cname(sname, path):
    return (sname.replace("rogue_fwif_", "").replace("rogue_", "") + "_" +
            path.replace(".", "_").replace("[", "").replace("]", "")).upper()


# Trace formats openfw emits, by name. The firmware writes only the ID;
# the kernel (pvr_fw_trace.c) and tools/fwemu/fwtrace.py hold the strings.
# A (format, n) tuple picks the n-th ID when the same string is listed
# more than once.
TRACE = {
    "OPENFW_BOOT": "Initialised Firmware with config flags 0x%08x and extended config flags 0x%08x",
    "OPENFW_OS_INIT": "Initialised OS %d with config flags 0x%08x",
    "OPENFW_CLOCK": "Core clock set to %d Hz",
    "OPENFW_GPU_INIT": "GPU init",
    "OPENFW_GPU_DEINIT": "GPU deinit",
    "OPENFW_KCCB": "KCCB Slot %u: Cmd=0x%08x, OSid=%u",
    "OPENFW_KCCB_RTN": "KCCB Slot %u: Return value %u",
    "OPENFW_KCCB_UNKNOWN": "Unknown KCCB Command: KCCBCtl=0x%08x, KCCB=0x%08x, Roff=%u, "
                           "Woff=%u, Wrap=%u, Cmd=0x%08x, CmdType=0x%08x",
    "OPENFW_BG": "Bg Task OSid = %u",
    "OPENFW_IRQ": "Irq Task (EVENT_STATUS=0x%08x)",
    "OPENFW_POW_IDLE": "OS requested forced IDLE, pow flags: 0x%x",
    "OPENFW_POW_CANCEL_IDLE": "OS cancelled forced IDLE, pow flags: 0x%x",
    "OPENFW_POW_OFF": "OS requested pow off (forced = %d), pow flags: 0x%x",
    "OPENFW_POW_DUSTS": "Changing number of dusts from %d to %d",
    "OPENFW_PAGE_FAULT": "Mips page fault detected (BadVAddr: 0x%08x, EntryLo0: 0x%08x, "
                         "EntryLo1: 0x%08x)",
    "OPENFW_DBG_HEX4": "0x%08x 0x%08x 0x%08x 0x%08x",
    "OPENFW_KICK_CDM": "Kick Compute: FWCtx 0x%08.8x @ %d. (PID:%d, prio:%d, ext:0x%08x, int:0x%08x)",
    "OPENFW_KICK_TQ": "Kick 3D TQ: FWCtx 0x%08.8x @ %d, CSW resume:%d. (PID:%d, prio:%d, "
                      "frame:%d, ext:0x%08x, int:0x%08x)",
    "OPENFW_KICK_TA": "Kick TA: FWCtx 0x%08.8x @ %d, RTD 0x%08x, First kick:%d, Last kick:%d, "
                      "CSW resume:%d. (PID:%d, prio:%d, frame:%d, ext:0x%08x, int:0x%08x)",
    "OPENFW_KICK_3D": "Kick 3D: FWCtx 0x%08.8x @ %d, RTD 0x%08x, Partial render:%d, CSW resume:%d. "
                      "(PID:%d, prio:%d, frame:%d, ext:0x%08x, int:0x%08x)",
    "OPENFW_CDM_DONE": "Compute finished",
    "OPENFW_TA_DONE": "TA finished",
    "OPENFW_3D_DONE": "3D finished, HWRTData0State=%x, HWRTData1State=%x",
    "OPENFW_UFO_CHECK": "UFO PR-Check: [0x%08.8x] is 0x%08.8x requires >= 0x%08.8x",
    "OPENFW_UFO_UPDATE": ("UFO Update: [0x%08.8x] = 0x%08.8x", 0),
    "OPENFW_MEMCTX": "Activate MemCtx=0x%08x DM=%d secure=%d",
    "OPENFW_FL_GROW": "Freelist grow completed [0x%08x]: added pages 0x%08x, total pages 0x%08x, "
                      "new DevVirtAddr 0x%08x%08x",
}


def macro(sname, path):
    return ("OFF_%s_%s" % (sname.replace("rogue_fwif_", "").replace("rogue_", ""),
                           path.replace(".", "_").replace("[", "").replace("]", ""))).upper()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layout", required=True)
    ap.add_argument("--kernel", required=True)
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()
    L = Layout(args.layout)
    sf = fwtrace.load_sf_table(args.kernel)
    out = ["/* SPDX-License-Identifier: MIT */",
           "/* Generated by gen_fwif.py from the Linux drm/imagination FWIF headers. */",
           "#ifndef OPENFW_FWIF_H", "#define OPENFW_FWIF_H", ""]
    for sname, fields in FIELDS.items():
        out.append("/* struct %s: %d bytes */" % (sname, L.size(sname)))
        out.append("#define SIZEOF_%s %d" % (sname.replace("rogue_fwif_", "").replace(
            "rogue_", "").upper(), L.size(sname)))
        for f in fields:
            off, _ = L.field(sname, f)
            out.append("#define %s %d" % (macro(sname, f), off))
        out.append("")
    for sname, paths in EXTRA_FIELDS.items():
        for f in paths:
            off, _ = L.field(sname, f)
            out.append("#define %s %d" % (macro(sname, f), off))
        out.append("")
    for sname in FULL:
        out.append("/* struct %s: %d bytes */" % (sname, L.size(sname)))
        out.append("#define SIZEOF_%s %d" % (cname(sname, "")[:-1], L.size(sname)))
        seen = set()
        for path, off, size, n, stride in leaves(L, sname):
            m = "OFF_" + cname(sname, path)
            if m in seen:
                continue
            seen.add(m)
            out.append("#define %s %d" % (m, off))
            if n:
                out.append("#define STRIDE_%s %d" % (cname(sname, path), stride))
        out.append("")
    for name, fmt in TRACE.items():
        nth = None
        if isinstance(fmt, tuple):
            fmt, nth = fmt
        ids = sorted(k for k, (v, _) in sf.items() if v == fmt)
        if not ids or (nth is None and len(ids) != 1):
            sys.exit("trace format not unique/found: %s" % fmt)
        out.append("#define SF_%s 0x%08xU /* \"%s\" */" % (name, ids[nth or 0], fmt))
    out += ["", "#endif", ""]
    open(args.output, "w").write("\n".join(out))
    print("wrote %s" % args.output)


if __name__ == "__main__":
    main()

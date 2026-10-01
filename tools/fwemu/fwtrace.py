#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
fwtrace.py - decode a PowerVR firmware trace buffer using the string
format table from the kernel's pvr_rogue_fwif_sf.h.

Entry layout (pvr_fw_trace.c): [sf id][timestamp hi][timestamp lo][params...]
where the sf id encodes its parameter count in bits 19:16.
"""

import re
import struct

IDMARKER = 0x70000000
IDMASK = 0xFFF00000
TIMESTAMP_TIME_MASK = 0x0000FFFFFFFFFFFF


def load_sf_table(kernel):
    path = kernel + "/drivers/gpu/drm/imagination/pvr_rogue_fwif_sf.h"
    src = open(path).read()
    groups = re.search(r"enum rogue_fw_log_sfgroups \{(.*?)\};", src, re.S).group(1)
    gid = {g.strip(): i for i, g in enumerate(x for x in groups.replace("\n", "").split(",")
                                               if x.strip())}
    table = {}
    for m in re.finditer(r'ROGUE_FW_LOG_CREATESFID\((\d+),\s*(ROGUE_FW_GROUP_\w+),\s*(\d+)\),'
                         r'\s*((?:"(?:[^"\\]|\\.)*"\s*)+)', src):
        num, grp, nparams = int(m.group(1)), gid[m.group(2)], int(m.group(3))
        fmt = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(4)))
        sfid = num | grp << 12 | nparams << 16 | IDMARKER
        table[sfid] = (fmt.encode().decode("unicode_escape"), nparams)
    return table


_CONV = re.compile(r"%([-+ #0]*)(\d+)?(?:\.(\d+))?(hh|h|ll|l|z)?([diuxXcsp%])")


def c_format(fmt, args):
    """Minimal printf for the firmware's format strings (integers only)."""
    it = iter(args)

    def conv(m):
        flags, width, prec, _len, kind = m.groups()
        if kind == "%":
            return "%"
        v = next(it, 0)
        if kind in "di":
            v = struct.unpack("<i", struct.pack("<I", v & 0xffffffff))[0]
            kind = "d"
        elif kind in "us":
            kind = "d"
        elif kind == "p":
            kind = "x"
        elif kind == "c":
            return chr(v & 0x7f)
        s = ("%" + kind) % v
        if prec:
            s = s.rjust(int(prec), "0")
        if width:
            s = s.rjust(int(width), "0" if "0" in flags and "-" not in flags else " ")
        return s
    return _CONV.sub(conv, fmt)


def decode(dwords, table, start=0, end=None):
    end = len(dwords) if end is None else end
    out = []
    i = start
    while i + 3 <= end:
        sfid = dwords[i]
        if (sfid & IDMASK) != IDMARKER or sfid not in table:
            i += 1
            continue
        fmt, n = table[sfid]
        ts = (dwords[i + 1] << 32 | dwords[i + 2]) & TIMESTAMP_TIME_MASK
        params = dwords[i + 3:i + 3 + n]
        out.append((ts, c_format(fmt, params)))
        i += 3 + n
    return out

#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
extract_layout.py - build layout.c against a Linux tree and dump every
struct of the PowerVR firmware interface (offsets, sizes, member types) to
JSON for fwemu.

    extract_layout.py --kernel LINUX_SRC -o layout.json
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

from elftools.elf.elffile import ELFFile

HERE = os.path.dirname(os.path.abspath(__file__))


def build(kernel, obj):
    inc = os.path.join(kernel, "drivers/gpu/drm/imagination")
    subprocess.run(["gcc", "-std=gnu11", "-g", "-fno-eliminate-unused-debug-types",
                    "-c", "-o", obj, "-I", os.path.join(HERE, "shim"), "-I", inc,
                    os.path.join(HERE, "layout.c")], check=True)


def die_type(die):
    """Resolve a type DIE into a dict: kind, name, size, plus element info."""
    while die.tag in ("DW_TAG_typedef", "DW_TAG_volatile_type", "DW_TAG_const_type"):
        if die.tag == "DW_TAG_typedef":
            name = die.attributes["DW_AT_name"].value.decode()
            inner = die.get_DIE_from_attribute("DW_AT_type")
            t = die_type(inner)
            t.setdefault("typedef", name)
            return t
        die = die.get_DIE_from_attribute("DW_AT_type")
    name = die.attributes.get("DW_AT_name")
    name = name.value.decode() if name else None
    if die.tag == "DW_TAG_base_type":
        return {"kind": "base", "name": name, "size": die.attributes["DW_AT_byte_size"].value}
    if die.tag in ("DW_TAG_structure_type", "DW_TAG_union_type"):
        return {"kind": "struct" if die.tag == "DW_TAG_structure_type" else "union",
                "name": name, "size": die.attributes.get("DW_AT_byte_size").value
                if "DW_AT_byte_size" in die.attributes else 0,
                "die_offset": die.offset}
    if die.tag == "DW_TAG_enumeration_type":
        return {"kind": "enum", "name": name, "size": die.attributes["DW_AT_byte_size"].value}
    if die.tag == "DW_TAG_array_type":
        elem = die_type(die.get_DIE_from_attribute("DW_AT_type"))
        dims = []
        for c in die.iter_children():
            if c.tag == "DW_TAG_subrange_type":
                if "DW_AT_upper_bound" in c.attributes:
                    dims.append(c.attributes["DW_AT_upper_bound"].value + 1)
                elif "DW_AT_count" in c.attributes:
                    dims.append(c.attributes["DW_AT_count"].value)
                else:
                    dims.append(0)
        count = 1
        for d in dims:
            count *= d
        return {"kind": "array", "elem": elem, "dims": dims, "size": elem["size"] * count}
    if die.tag == "DW_TAG_pointer_type":
        return {"kind": "pointer", "size": die.attributes["DW_AT_byte_size"].value}
    return {"kind": die.tag, "size": 0}


def members(die):
    out = []
    for c in die.iter_children():
        if c.tag != "DW_TAG_member":
            continue
        name = c.attributes.get("DW_AT_name")
        loc = c.attributes.get("DW_AT_data_member_location")
        out.append({"name": name.value.decode() if name else None,
                    "offset": loc.value if loc else 0,
                    "type": die_type(c.get_DIE_from_attribute("DW_AT_type"))})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", required=True)
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "layout.json"))
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        obj = os.path.join(tmp, "layout.o")
        build(args.kernel, obj)
        structs = {}
        with open(obj, "rb") as f:
            elf = ELFFile(f)
            dw = elf.get_dwarf_info()
            by_offset = {}
            for cu in dw.iter_CUs():
                for die in cu.iter_DIEs():
                    if die.tag not in ("DW_TAG_structure_type", "DW_TAG_union_type"):
                        continue
                    if "DW_AT_declaration" in die.attributes:
                        continue
                    by_offset[die.offset] = die
                    name = die.attributes.get("DW_AT_name")
                    if name:
                        structs[name.value.decode()] = {
                            "kind": "struct" if die.tag == "DW_TAG_structure_type" else "union",
                            "size": die.attributes["DW_AT_byte_size"].value,
                            "members": members(die)}
            # anonymous structs/unions referenced by members
            anon = {}
            for off, die in by_offset.items():
                if "DW_AT_name" not in die.attributes:
                    anon[str(off)] = {"kind": "struct" if die.tag == "DW_TAG_structure_type"
                                      else "union",
                                      "size": die.attributes["DW_AT_byte_size"].value,
                                      "members": members(die)}
    json.dump({"structs": structs, "anon": anon}, open(args.output, "w"), indent=1)
    print("%d named structs, %d anonymous -> %s" % (len(structs), len(anon), args.output))


if __name__ == "__main__":
    sys.exit(main())

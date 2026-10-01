#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
vf2-regs.py - read (and carefully write) JH7110 PMU and GPU registers via
/dev/mem, for the GPU power-up experiments described in docs/power.md.

  sudo ./vf2-regs.py pmu                 dump PMU registers
  sudo ./vf2-regs.py gpu                 dump key GPU registers (GPU must be
                                         runtime-active, see below)
  sudo ./vf2-regs.py pmu-set 0x08 0x7f   write a PMU register (prints old/new)

Reading GPU registers while the GPUA power domain is off or its clocks are
gated can hang the AXI bus and lock up the board. The 'gpu' command refuses
unless the powervr device reports runtime_status == active; keep a GPU job
running (e.g. vkmark) in another terminal while dumping.

Needs a kernel without CONFIG_STRICT_DEVMEM, or booted with iomem=relaxed.
"""

import mmap
import os
import struct
import sys

PMU_BASE = 0x17030000
GPU_BASE = 0x18000000
GPU_DEV = "/sys/devices/platform/soc/18000000.gpu"

PMU_DOMAINS = ["SYSTOP", "CPU", "GPUA", "VDEC", "VOUT", "ISP", "VENC"]
HW_EVENTS = ["RTC", "GMAC", "RFU", "RGPIO0", "RGPIO1", "RGPIO2", "RGPIO3", "GPU"]

PMU_REGS = [
    (0x04, "HW_EVENT_TURN_ON_MASK", "1 = event masked; bit 7 = GPU event"),
    (0x08, "HW_EVENT_TURN_OFF_MASK", "1 = event masked; vendor DDK writes 0 on GPU resume, ~0 on suspend"),
    (0x0C, "SW_TURN_ON_POWER", "domain bits"),
    (0x10, "SW_TURN_OFF_POWER", "domain bits"),
    (0x44, "SW_ENCOURAGE", ""),
    (0x48, "TIMER_INT_MASK", ""),
    (0x80, "CURR_POWER_MODE", "domain bits, 1 = on"),
    (0x88, "EVENT_STATUS", ""),
    (0x8C, "INT_STATUS", "bit0 seq done, bit1 hw req, 3:2 sw fail, 5:4 hw fail, 8:6 pch fail"),
]

GPU_REGS = [
    (0x0000, 8, "CLK_CTRL"),
    (0x0008, 8, "CLK_STATUS"),
    (0x0018, 4, "CORE_ID (legacy)"),
    (0x0020, 8, "CORE_ID__PBVNC"),
    (0x0038, 4, "POWER_EVENT (undocumented on Rogue)"),
    (0x0100, 8, "SOFT_RESET"),
    (0x0130, 4, "EVENT_STATUS"),
    (0x0160, 8, "TIMER"),
    (0x03C8, 4, "SIDEKICK_IDLE"),
    (0x0810, 8, "MIPS_WRAPPER_CONFIG"),
    (0x08A8, 4, "MIPS_WRAPPER_IRQ_STATUS"),
    (0x08D0, 4, "MIPS_EXCEPTION_STATUS"),
    (0x0BD8, 4, "IRQ_OS0_EVENT_STATUS"),
    (0x12B0, 8, "BIF_FAULT_BANK0_MMU_STATUS"),
    (0x12B8, 8, "BIF_FAULT_BANK0_REQ_STATUS"),
    (0x3820, 4, "SLC_STATUS0"),
    (0x8328, 4, "JONES_IDLE"),
]


class Window:
    def __init__(self, base, size=0x10000, write=False):
        flags = os.O_RDWR if write else os.O_RDONLY
        self.fd = os.open("/dev/mem", flags | os.O_SYNC)
        prot = mmap.PROT_READ | (mmap.PROT_WRITE if write else 0)
        self.m = mmap.mmap(self.fd, size, mmap.MAP_SHARED, prot, offset=base)

    def r32(self, off):
        return struct.unpack_from("<I", self.m, off)[0]

    def r64(self, off):
        return self.r32(off) | self.r32(off + 4) << 32

    def w32(self, off, val):
        struct.pack_into("<I", self.m, off, val)


def names(val, table):
    return ",".join(n for i, n in enumerate(table) if val >> i & 1) or "-"


def cmd_pmu():
    w = Window(PMU_BASE)
    for off, name, note in PMU_REGS:
        v = w.r32(off)
        decoded = ""
        if off in (0x04, 0x08):
            decoded = " masked=[%s]" % names(v, HW_EVENTS)
        elif off in (0x0C, 0x10, 0x80):
            decoded = " [%s]" % names(v, PMU_DOMAINS)
        print("PMU+0x%02x %-24s 0x%08x%s  %s" % (off, name, v, decoded, note))


def gpu_active():
    try:
        return open(os.path.join(GPU_DEV, "power/runtime_status")).read().strip() == "active"
    except OSError:
        return False


def cmd_gpu(force=False):
    if not gpu_active() and not force:
        sys.exit("GPU is not runtime-active; refusing to touch its registers "
                 "(run a GPU workload concurrently, or pass --force at your own risk)")
    w = Window(GPU_BASE, 0x10000)
    for off, width, name in GPU_REGS:
        v = w.r64(off) if width == 8 else w.r32(off)
        print("GPU+0x%04x %-36s 0x%0*x" % (off, name, width * 2, v))
    core_id = w.r64(0x20)
    print("decoded PBVNC: %d.%d.%d.%d (expect 36.50.54.182)" % (
        core_id >> 48 & 0xffff, core_id >> 32 & 0xffff, core_id >> 16 & 0xffff, core_id & 0xffff))


def cmd_pmu_set(off, val):
    allowed = {0x04, 0x08}
    if off not in allowed:
        sys.exit("only the HW event mask registers (0x04, 0x08) may be written by this tool")
    w = Window(PMU_BASE, write=True)
    old = w.r32(off)
    w.w32(off, val)
    print("PMU+0x%02x: 0x%08x -> 0x%08x (readback 0x%08x)" % (off, old, val, w.r32(off)))


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    if argv[1] == "pmu":
        cmd_pmu()
    elif argv[1] == "gpu":
        cmd_gpu("--force" in argv)
    elif argv[1] == "pmu-set" and len(argv) == 4:
        cmd_pmu_set(int(argv[2], 0), int(argv[3], 0))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)

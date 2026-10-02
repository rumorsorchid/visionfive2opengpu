#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
jobs.py - run GPU jobs through a firmware image in fwemu.

Submits jobs the way the Linux powervr driver does (host.py) and plays the
GPU: when the firmware writes a data master's start register, the matching
completion event is raised in EVENT_STATUS and the MTS interrupt task is
delivered, as the hardware would. Every register write, every firmware
write to host-visible memory and the firmware trace are logged per step,
with tagged command fields resolved to their names.

    jobs.py FW.fw --kernel LINUX compute [transfer render fence ...]
    jobs.py FW.fw --kernel LINUX render --json out.json

Scenarios:
  compute      one compute job (CDM)
  compute2     two compute jobs, the second waiting on the first's fence
  transfer     one transfer job (TQ on the fragment data master)
  render       geometry + partial-render fragment (combined kick) + fragment
  geom         geometry + partial-render fragment only
  cleanup      compute job, then context cleanup
  frames       three frames on one render target (HWRT data 0, 1, 0)
  frames-pipelined  four frames, next geometry kicked while the fragment runs
  multivm      compute/transfer contexts in two VMs, sequential
  multivm-concurrent  the same with the jobs in flight together
  blocked      a job waiting on another context's later job
  wrap         200 chained compute jobs: the client CCB wraps (PADDING)
  cleanup-busy context cleanup while its job runs, then when idle
  teardown     render, then context / HWRT data / free list cleanups
  mixed        compute, transfer and a render in flight together
"""
import argparse
import hashlib
import io
import json
import os
import sys
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import fwemu  # noqa: E402
import fwtrace  # noqa: E402
import host as H  # noqa: E402
from fwregs import load_cr_names  # noqa: E402
from pvrfw import Firmware, Tables  # noqa: E402

PAGE_SIZE = 0x1000

# Start registers: (offset, value) written by the firmware to launch work
# on a data master, and the EVENT_STATUS bits the hardware raises when that
# work is done. Learnt from Imagination's firmware (docs/firmware.md).
# Registers the hardware updates while work runs (parameter-manager stack
# pointers, page counts, catalogue bases). With "status_tags", completing
# work sets each to 0x7Exxxxxx (xxxxxx = offset) so the firmware's copies
# of them into memory can be traced.
STATUS_REGS = {
    "TA": [0x2000, 0x20c8, 0x20d8, 0x20e0, 0x348, 0x3a0, 0x274, 0x270, 0x2c0,
           0x1248, 0x124c, 0x1250, 0x1254, 0x1260, 0x1264, 0x20b8, 0x20bc,
           0x210, 0x214],
    # (0xd20, the TE's count of active render target array layers, stays
    # 0: a single-layer render; layered renders are not modelled)
    "3D": [0x2008, 0x2088, 0x2098, 0x20a0, 0x350, 0x3a8, 0x284, 0x280, 0x2d0,
           0x1268, 0x126c, 0x1270, 0x1274, 0x1280, 0x1284, 0x2078, 0x207c, 0x218, 0x21c],
}

KICKS = {
    0x0478: ("CDM", 1 << 2),          # COMPUTE_FINISHED
    0x0F00: ("3D", 1 << 3),           # PIXELBE_END_RENDER (fragment, transfer)
    0x0400: ("TA", 1 << 5),           # TA_FINISHED
}


class Runner:
    def __init__(self, fw_path, kernel, quiet=True, trace_regs=False, max_insns=3_000_000,
                 params=None):
        self.kernel = kernel
        self.p = dict(DEFAULT_PARAMS)
        self.p.update(params or {})
        args = argparse.Namespace(config_flags=0, trace_mask=0x80007FFF, max_insns=max_insns,
                                  trace_regs=trace_regs, trace_exc=False, complete_polls=True,
                                  kccb=[], watch=None)
        fw = Firmware(fw_path, Tables(kernel))
        self.quiet = quiet
        with self.out():
            self.emu = fwemu.Emu(fw, fwemu.Layout(os.path.join(HERE, "layout.json")),
                                 load_cr_names(kernel), args)
        self.emu.sf_table = fwtrace.load_sf_table(kernel)
        # power-on register values (e.g. read-modify-write targets)
        for reg, v in self.p["reg_init"].items():
            self.emu.regs[reg] = v
        self.names = self.emu.names
        self.tags = {}
        self.steps = []
        self.watch = {}
        self.watch_times = {}
        self.watch_gpu_mem = {}
        self.kicks = []          # kicks in the current step (reports)
        self._kick_hist = []     # every kick, completed in order by the model
        self.oom_left = self.p["oom"]
        self.ta_stalled = False
        self.ta_hold = False     # scenario keeps the TA running
        self.hang = set(self.p["hang"])  # data masters whose work never finishes
        self.time_offset = 0     # added to the GPU timer (scenario time jumps)
        self.reg_values = {}     # register reads overridden by the scenario
        orig_read = self.emu.reg_model_read

        def model_read(off):
            if off in (0x0160, 0x0164):
                t = self.emu.insns // 16 + self.time_offset
                return t & 0xffffffff if off == 0x0160 else t >> 32
            if off in self.reg_values:
                return self.reg_values[off]
            return orig_read(off)
        self.emu.reg_model_read = model_read
        orig = self.emu.reg_model_write

        def model_write(off, value):
            orig(off, value)
            o = off & ~0x200000
            if o in KICKS and value & 1:
                self.kicks.append(o)
                self._kick_hist.append(o)
            if o == 0x0328 and value & 1:
                self.ta_stalled = False      # TA resumed after out-of-memory
            if o == 0x0100 and value:
                # soft reset: work still running on the GPU is lost
                done = getattr(self, "_done_kicks", 0)
                del self._kick_hist[done:]
                self.ta_stalled = False
        self.emu.reg_model_write = model_write

    def out(self):
        return redirect_stdout(io.StringIO()) if self.quiet else _Null()

    def boot(self):
        with self.out():
            started = self.emu.run()
        self.host = H.Host(self.emu)
        if self.p["shift"]:
            # move every following FW object: exposes FW-address dependencies
            self.emu.alloc("shift", self.p["shift"])
        self.mark("boot")
        return started

    # -- bookkeeping -----------------------------------------------------------------
    def tag_fields(self, sname, values):
        for path, v in values.items():
            if isinstance(v, int) and (v & 0xFF000000) == 0x5A000000:
                self.tags[v & 0xFFFFFFFF] = "%s.%s" % (sname.replace("rogue_fwif_", ""), path)
            if isinstance(v, int) and (v & 0xFFFFFFFF) & 0xFF000000 == 0x5A000000 and v >> 32:
                self.tags[v & 0xFFFFFFFF] = "%s.%s" % (sname.replace("rogue_fwif_", ""), path)

    def watch_gpu(self, name, vm, va, size):
        """GPU memory (through the VM's page tables) to compare."""
        self.watch_gpu_mem[name] = (self.host.gpu_vms[vm], va, size)

    def watch_obj(self, name, va, size, times=()):
        """times: offsets of 64-bit timestamps inside the object; they
        depend on instruction counts, so only whether they are set is
        compared."""
        self.watch[name] = (va, size)
        self.watch_times[name] = times

    def snapshot(self):
        snap = {}
        for n, (va, sz) in self.watch.items():
            data = bytearray(self.emu.read(va, sz))
            for off in self.watch_times.get(n, ()):
                if any(data[off:off + 8]):
                    data[off:off + 8] = (1).to_bytes(8, "little")
            snap[n] = bytes(data)
        for n, (vm, va, sz) in getattr(self, "watch_gpu_mem", {}).items():
            snap[n] = vm.read(va, sz)
        # every write the firmware makes to system memory (GPU page
        # tables, buffers) shows up here, watched or not
        pages = self.emu.sysmem.pages
        snap["sysmem"] = hashlib.sha256(b"".join(
            k.to_bytes(8, "little") + bytes(pages[k]) for k in sorted(pages))).digest()[:8]
        return snap

    def mark(self, name):
        """Close the current step: collect register writes, memory changes
        and new trace lines since the previous mark."""
        e = self.emu
        log = e.reg_log
        start = getattr(self, "_log_pos", 0)
        writes = [(o & ~0x200000, v) for k, o, v in log[start:] if k == "W"]
        accesses = [(k, o & ~0x200000, v) for k, o, v in log[start:]]
        self._log_pos = len(log)
        trace = e.trace()
        new_trace = [m for _, m in trace[getattr(self, "_trace_pos", 0):]]
        self._trace_pos = len(trace)
        snap = self.snapshot()
        prev = getattr(self, "_snap", {})
        mem = {}
        for n, data in snap.items():
            if prev.get(n) != data:
                mem[n] = data
        self._snap = snap
        step = {"step": name, "writes": writes, "accesses": accesses, "mem": mem,
                "trace": new_trace, "kicks": list(self.kicks)}
        self.kicks = []
        self.steps.append(step)
        return step

    # -- driving -------------------------------------------------------------------------
    def settle(self, label, complete=True, rounds=24):
        """Deliver queued MTS tasks; complete started work like the GPU."""
        e = self.emu
        with self.out():
            budget = rounds          # per completed kick
            while budget > 0:
                budget -= 1
                if e.pending_tasks:
                    e.inject(e.pending_tasks.pop(0))
                    continue
                if complete and self.kicks_pending():
                    budget = rounds
                    continue
                break
        return self.mark(label)

    def kicks_pending(self):
        done = getattr(self, "_done_kicks", 0)
        if done >= len(self.all_kicks()):
            return False
        off = self.all_kicks()[done]
        def held(o):
            dm = KICKS[o][0]
            return dm in self.hang or (dm == "TA" and (self.ta_stalled or self.ta_hold))
        if held(off):
            # a stalled TA only completes after the firmware resumes it, a
            # hung data master never; complete the next other kick instead
            for i in range(done + 1, len(self._kick_hist)):
                if not held(self._kick_hist[i]):
                    self._kick_hist.insert(done, self._kick_hist.pop(i))
                    off = self._kick_hist[done]
                    break
            else:
                return False
        self._done_kicks = done + 1
        dm, bits = KICKS[off]
        if dm == "TA" and self.oom_left:
            # the PM runs out of free list pages: the TA stalls (kick not
            # complete; it goes to the back of the queue)
            self.oom_left -= 1
            self._done_kicks = done
            self.ta_stalled = True
            for reg, v in self.p["oom_regs"].items():
                self.emu.regs[reg] = v
            self.emu.event_status |= 1 << 7          # PM_OUT_OF_MEMORY
            self.emu.pending_tasks.append("irq")
            return True
        if self.p.get("status_tags"):
            for reg in STATUS_REGS.get(dm, ()):
                self.emu.regs[reg] = 0x7E000000 | reg
        if dm == "TA":
            for reg, v in self.p["ta_regs"].items():
                self.emu.regs[reg] = v
        self.emu.event_status |= bits
        self.emu.pending_tasks.append("irq")
        return True

    def all_kicks(self):
        return self._kick_hist

    def power_cycle(self):
        """Runtime suspend and resume as pvr_power does it: FORCED_IDLE
        and OFF requests, then the firmware is restarted (not reloaded)."""
        e = self.emu
        e.send_kccb(107, [("cmd_data.pow_data.pow_type", 2),
                          ("cmd_data.pow_data.power_req_data.pow_request_type", 1)])
        self.kccb_bg()
        self.settle("forced idle request")
        e.send_kccb(107, [("cmd_data.pow_data.pow_type", 1),
                          ("cmd_data.pow_data.power_req_data.forced", 1)])
        self.kccb_bg()
        self.settle("power off request")
        e.set("rogue_fwif_sysinit", fwemu.SYSINIT_VA, "firmware_started", 0)
        with self.out():
            e.boot()
        self.mark("resume (firmware restarted)")

    def kccb_bg(self):
        self.emu.pending_tasks.append("bg")

    def page_fault(self, label, cat_base=1, addr=0xA000123450, write=False, tag=0x3):
        """The GPU MMU faults on an access through page catalogue set
        cat_base (BIF_CAT_BASE<n>): fault status in bank 0, MMU_PAGE_FAULT
        event, interrupt task."""
        e = self.emu
        e.regs[0x12B0] = (cat_base << 12) | 1                 # FAULT
        e.regs[0x12B4] = 0
        req = (addr & 0xFFFFFFFFF0) | (tag << 40) | (0 if write else 1 << 50)
        e.regs[0x12B8] = req & 0xFFFFFFFF
        e.regs[0x12BC] = req >> 32
        e.event_status |= 1 << 9
        e.pending_tasks.append("irq")
        return self.settle(label)

    def complete_dm(self, dm, label):
        """The GPU finishes the oldest running work of one data master
        ("CDM", "TA", "3D") first, whatever was started before it."""
        done = getattr(self, "_done_kicks", 0)
        for i in range(done, len(self._kick_hist)):
            if KICKS[self._kick_hist[i]][0] == dm:
                self._kick_hist.insert(done, self._kick_hist.pop(i))
                with self.out():
                    self.kicks_pending()
                break
        return self.settle(label, complete=False)

    def tick(self, label, dt=0x10000, regs=None):
        """Let dt GPU timer ticks pass and deliver the firmware's timer
        interrupt (its periodic work: lockup checks, safety nets)."""
        self.time_offset += dt
        self.reg_values.update(regs or {})
        self.emu.pending_tasks.append("timer")
        return self.settle(label)

    # -- reports ---------------------------------------------------------------------------
    def reg_name(self, off):
        return self.names.get(off) or (self.names.get(off - 4, "?") + "[hi]"
                                       if off - 4 in self.names else "?")

    def describe(self, step, show_writes=True, reads=False):
        out = ["== %s" % step["step"]]
        if show_writes:
            seq = step["accesses"] if reads else [("W", o, v) for o, v in step["writes"]]
            for k, off, v in seq:
                if k == "P":
                    out.append("  P %-34s +0x%05x & 0x%08x == 0x%08x" % (
                        self.reg_name(off), off, v[1], v[0]))
                    continue
                t = self.tags.get(v)
                out.append("  %s %-34s +0x%05x = 0x%08x%s" % (
                    k, self.reg_name(off), off, v, "  <- " + t if t else ""))
        for n, data in step["mem"].items():
            out.append("  M %-20s %s" % (n, data.hex()))
        for m in step["trace"]:
            out.append("  T %s" % m)
        return "\n".join(out)


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# Scenario inputs; tools/fwemu/vary.py changes them one at a time to find
# out which register values depend on which input.
DEFAULT_PARAMS = {
    "shift": 0,                       # extra FW heap allocation before the scenario
    "pc": 0x90000000,                 # page catalogue physical address of the VM
    "fl_addr": 0xE200000000, "fl_initial": 256, "fl_max": 4096, "fl_grow": 64,
    "gfl_addr": 0xE210000000, "fl_threshold": 13,
    "width": 1920, "height": 1080, "samples": 1,
    "callstack": 0xE300000000,
    "geom": {}, "rt": None,
    "status_tags": 0,
    "override": {},                  # {struct name: {field path: value}}
    "reg_init": {},                  # {register offset: power-on value}
    "oom": 0,                        # PM out-of-memory events raised during TAs
    "grow_fail": 0,                  # the kernel cannot grow free lists
    "oom_regs": {},                  # {register: value} set with each OOM event
    "hang": (),                      # data masters ("CDM", "TA", "3D") that never finish
    "ta_regs": {},                   # {register: value} set when a TA finishes
}


# -- scenarios ---------------------------------------------------------------------------
def fields(r, sname, base, extra=None):
    vals = H.tagged(sname, base, extra)
    vals.update(r.p["override"].get(sname, {}))
    return vals


def compute_job(r, ctx, deps=(), base=0):
    vals = fields(r, "rogue_fwif_cmd_compute", base)
    r.tag_fields("rogue_fwif_cmd_compute", vals)
    payload = H.cmd(r.host.L, "rogue_fwif_cmd_compute", vals)
    return r.host.job(ctx.queues["compute"], H.CCB_CDM, payload, deps)


def watch_queue(r, q, prefix):
    r.watch_obj(prefix + ".cccb_ctl", q.ctrl, 32)
    r.watch_obj(prefix + ".ufo", q.ufo, 4)


def sc_compute(r, n=1, chained=False):
    h = r.host
    vm = h.vm_context(r.p["pc"])
    ctx = h.compute_context(vm)
    r.ctx = ctx
    q = ctx.queues["compute"]
    watch_queue(r, q, "compute")
    r.watch_obj("kccb_rtn", r.emu.kccb_rtn, 16)
    r.watch_obj("fwmemctx", vm, 16)
    r.mark("setup")
    prev = None
    jobs = []
    for i in range(n):
        deps = [prev.fence()] if (chained and prev) else []
        j = compute_job(r, ctx, deps, base=0x10 * i)
        h.submit(j)
        r.kccb_bg()
        r.settle("compute job %d" % (i + 1))
        jobs.append(j)
        prev = j
    return {"jobs_done": [j.done() for j in jobs], "ufo": q.ufo_value(),
            "read_offset": h.get("rogue_fwif_cccb_ctl", q.ctrl, "read_offset"),
            "write_offset": q.write_offset}


def sc_transfer(r):
    h = r.host
    vm = h.vm_context(r.p["pc"])
    ctx = h.transfer_context(vm)
    q = ctx.queues["transfer"]
    watch_queue(r, q, "transfer")
    r.mark("setup")
    # Mesa only submits transfers in FAST_2D / FAST_SCALE ISP mode
    vals = fields(r, "rogue_fwif_cmd_transfer", 0x40, {"regs.isp_render": 0x5A05A000 | 2})
    r.tag_fields("rogue_fwif_cmd_transfer", vals)
    j = h.job(q, H.CCB_TQ_3D, H.cmd(h.L, "rogue_fwif_cmd_transfer", vals))
    h.submit(j)
    r.kccb_bg()
    r.settle("transfer job")
    return {"jobs_done": [j.done()], "ufo": q.ufo_value(),
            "read_offset": h.get("rogue_fwif_cccb_ctl", q.ctrl, "read_offset"),
            "write_offset": q.write_offset}


def sc_render(r, with_frag=True):
    h = r.host
    p = r.p
    vm = h.vm_context(p["pc"])
    ctx = h.render_context(vm, callstack_addr=p["callstack"])
    gq, fq = ctx.queues["geometry"], ctx.queues["fragment"]
    fl = h.free_list(vm, gpu_addr=p["fl_addr"], initial=p["fl_initial"], max_pages=p["fl_max"],
                     grow=p["fl_grow"])
    gfl = h.free_list(vm, gpu_addr=p["gfl_addr"], initial=p["fl_initial"],
                      max_pages=p["fl_max"], grow=p["fl_grow"])
    rt = h.hwrt([fl, gfl], width=p["width"], height=p["height"], samples=p["samples"],
                geom=p["geom"], rt=p["rt"])
    r.rt, r.fl, r.gfl = rt, fl, gfl
    watch_queue(r, gq, "geom")
    watch_queue(r, fq, "frag")
    r.watch_obj("hwrtdata0", rt.data[0], h.L.size("rogue_fwif_hwrtdata"))
    r.watch_obj("freelist", fl.fw, h.L.size("rogue_fwif_freelist"))
    r.watch_obj("fwccb_ctl", r.emu.fwccb_ctl, 4)
    r.mark("setup")
    gvals = fields(r, "rogue_fwif_cmd_geom", 0x80, {"flags": 0x3})   # FIRSTKICK | LASTKICK
    r.tag_fields("rogue_fwif_cmd_geom", gvals)
    gvals["cmd_shared.hwrt_data_fw_addr"] = rt.data[0]
    geom = h.job(gq, H.CCB_GEOM, H.cmd(h.L, "rogue_fwif_cmd_geom", gvals), hwrt=rt.data[0])
    pvals = fields(r, "rogue_fwif_cmd_frag", 0x100)
    r.tag_fields("rogue_fwif_cmd_frag", pvals)
    pvals["cmd_shared.hwrt_data_fw_addr"] = rt.data[0]
    pr = h.job(fq, H.CCB_FRAG_PR, H.cmd(h.L, "rogue_fwif_cmd_frag", pvals), hwrt=rt.data[0])
    h.submit_combined(geom, pr)
    r.kccb_bg()
    r.settle("geometry + PR kick")
    res = {"geom_done": geom.done(), "pr_done": pr.done()}
    if with_frag:
        fvals = fields(r, "rogue_fwif_cmd_frag", 0x180)
        r.tag_fields("rogue_fwif_cmd_frag", fvals)
        fvals["cmd_shared.hwrt_data_fw_addr"] = rt.data[0]
        frag = h.job(fq, H.CCB_FRAG, H.cmd(h.L, "rogue_fwif_cmd_frag", fvals),
                     deps=[geom.fence()], hwrt=rt.data[0])
        h.submit(frag)
        r.kccb_bg()
        r.settle("fragment kick")
        res["frag_done"] = frag.done()
    res["geom_ufo"], res["frag_ufo"] = gq.ufo_value(), fq.ufo_value()
    return res


def sc_cleanup(r):
    res = sc_compute(r)
    h = r.host
    slot = h.cleanup(H.CLEANUP_FWCOMMONCONTEXT, r.ctx.fw_addr(r.ctx.queues["compute"]))
    r.kccb_bg()
    r.settle("context cleanup")
    res["cleanup_rtn"] = r.emu.r32(r.emu.kccb_rtn + 4 * slot)
    return res


def new_render(r, vm=None, data_sets=1):
    """Render context with local + global free lists and HWRT data sets."""
    h, p = r.host, r.p
    vm = vm or h.vm_context(p["pc"])
    ctx = h.render_context(vm, callstack_addr=p["callstack"])
    fl = h.free_list(vm, gpu_addr=p["fl_addr"], initial=p["fl_initial"], max_pages=p["fl_max"],
                     grow=p["fl_grow"], threshold=p["fl_threshold"])
    gfl = h.free_list(vm, gpu_addr=p["gfl_addr"], initial=p["fl_initial"],
                      max_pages=p["fl_max"], grow=p["fl_grow"], threshold=p["fl_threshold"])
    fl.grow_fails = gfl.grow_fails = bool(p["grow_fail"])     # host out of memory
    rts = [h.hwrt([fl, gfl], width=p["width"], height=p["height"], samples=p["samples"],
                  geom=p["geom"], rt=p["rt"]) for _ in range(data_sets)]
    # tail pointer cache and render target cache in GPU memory, left dirty
    # (the firmware zeroes them when a geometry phase was cut short)
    g = p["geom"] or {}
    tpc, tpc_size = g.get("tpc_dev_addr", 0xE100000000), g.get("tpc_size", 0x4000)
    rtc = g.get("rtc_dev_addr", 0xE100200000)
    gvm = h.gpu_vms[vm]
    if gvm.translate(tpc) is None:
        gvm.map(tpc, tpc_size, fill=0xA5)
        gvm.map(rtc, PAGE_SIZE, fill=0x5A)
    r.watch_gpu("tpc", vm, tpc, tpc_size)
    r.watch_gpu("rtc", vm, rtc, 0x400)
    return ctx, fl, gfl, rts


def render_jobs(r, ctx, hwrt, base, deps=(), geom_flags=0x3):
    """Geometry + partial-render fragment (combined kick) and the fragment
    job, as pvr_queue builds them for one DRM_PVR_JOB_TYPE_GEOMETRY +
    FRAGMENT submission."""
    h = r.host
    gq, fq = ctx.queues["geometry"], ctx.queues["fragment"]
    gvals = fields(r, "rogue_fwif_cmd_geom", base, {"flags": geom_flags})
    r.tag_fields("rogue_fwif_cmd_geom", gvals)
    gvals["cmd_shared.hwrt_data_fw_addr"] = hwrt
    geom = h.job(gq, H.CCB_GEOM, H.cmd(h.L, "rogue_fwif_cmd_geom", gvals), deps=deps, hwrt=hwrt)
    pvals = fields(r, "rogue_fwif_cmd_frag", base + 0x80)
    pvals["cmd_shared.hwrt_data_fw_addr"] = hwrt
    pr = h.job(fq, H.CCB_FRAG_PR, H.cmd(h.L, "rogue_fwif_cmd_frag", pvals), hwrt=hwrt)
    fvals = fields(r, "rogue_fwif_cmd_frag", base + 0x100)
    r.tag_fields("rogue_fwif_cmd_frag", fvals)
    fvals["cmd_shared.hwrt_data_fw_addr"] = hwrt
    frag = h.job(fq, H.CCB_FRAG, H.cmd(h.L, "rogue_fwif_cmd_frag", fvals),
                 deps=[geom.fence()], hwrt=hwrt)
    return geom, pr, frag


def sc_frames(r, n=3, pipelined=False):
    """n frames on one render target; frame i uses HWRT data i % 2 like
    Mesa. Pipelined: frame i+1's geometry is kicked while frame i's
    fragment job is still running."""
    h = r.host
    ctx, fl, gfl, (rt,) = new_render(r)
    gq, fq = ctx.queues["geometry"], ctx.queues["fragment"]
    watch_queue(r, gq, "geom")
    watch_queue(r, fq, "frag")
    for i in range(2):
        r.watch_obj("hwrtdata%d" % i, rt.data[i], h.L.size("rogue_fwif_hwrtdata"))
    r.watch_obj("freelist", fl.fw, h.L.size("rogue_fwif_freelist"))
    r.watch_obj("gfreelist", gfl.fw, h.L.size("rogue_fwif_freelist"))
    r.mark("setup")
    frames = []
    for i in range(n):
        geom, pr, frag = render_jobs(r, ctx, rt.data[i % 2], 0x10 * i)
        h.submit_combined(geom, pr)
        r.kccb_bg()
        r.settle("frame %d geometry" % i, complete=not pipelined or i == 0)
        h.submit(frag)
        r.kccb_bg()
        r.settle("frame %d fragment" % i, complete=not pipelined)
        frames.append((geom, pr, frag))
    if pipelined:
        r.settle("drain")
    return {"done": [[j.done() for j in f] for f in frames],
            "geom_ufo": gq.ufo_value(), "frag_ufo": fq.ufo_value()}


def sc_multivm(r, concurrent=False):
    """Compute and transfer contexts in two VMs (two page catalogues)."""
    h = r.host
    vms = [h.vm_context(r.p["pc"]), h.vm_context(r.p["pc"] + 0x100000)]
    cctx = [h.compute_context(vm) for vm in vms]
    tctx = h.transfer_context(vms[1])
    for i, c in enumerate(cctx):
        watch_queue(r, c.queues["compute"], "compute%d" % i)
        r.watch_obj("fwmemctx%d" % i, vms[i], 16)
    watch_queue(r, tctx.queues["transfer"], "transfer")
    r.mark("setup")
    jobs_ = []
    for rnd in range(2):
        for i, c in enumerate(cctx):
            j = compute_job(r, c, base=0x10 * (2 * rnd + i))
            h.submit(j)
            r.kccb_bg()
            r.settle("round %d compute vm%d" % (rnd, i), complete=not concurrent)
            jobs_.append(j)
        vals = fields(r, "rogue_fwif_cmd_transfer", 0x40 + rnd, {"regs.isp_render": 0x5A05A000 | 2})
        r.tag_fields("rogue_fwif_cmd_transfer", vals)
        t = h.job(tctx.queues["transfer"], H.CCB_TQ_3D, H.cmd(h.L, "rogue_fwif_cmd_transfer", vals))
        h.submit(t)
        r.kccb_bg()
        r.settle("round %d transfer vm1" % rnd, complete=not concurrent)
        jobs_.append(t)
        if concurrent:
            r.settle("round %d drain" % rnd)
    return {"done": [j.done() for j in jobs_]}


def sc_blocked(r):
    """A job waiting on another context's job that is submitted later."""
    h = r.host
    vm = h.vm_context(r.p["pc"])
    a, b = h.compute_context(vm), h.compute_context(vm)
    qa, qb = a.queues["compute"], b.queues["compute"]
    watch_queue(r, qa, "computeA")
    watch_queue(r, qb, "computeB")
    r.mark("setup")
    # B's job depends on A's next fence value, which A has not reached
    jb = compute_job(r, b, deps=[(qa.ufo, qa.seqno + 1)], base=0x20)
    h.submit(jb)
    r.kccb_bg()
    r.settle("B blocked")
    ja = compute_job(r, a, base=0x10)
    h.submit(ja)
    r.kccb_bg()
    r.settle("A runs, then B")
    return {"done": [ja.done(), jb.done()], "a": qa.ufo_value(), "b": qb.ufo_value()}


def sc_wrap(r, n=200):
    """Enough compute jobs to wrap the 32 KiB client CCB (PADDING)."""
    h = r.host
    vm = h.vm_context(r.p["pc"])
    ctx = h.compute_context(vm)
    q = ctx.queues["compute"]
    watch_queue(r, q, "compute")
    r.mark("setup")
    js = []
    for i in range(n):
        j = compute_job(r, ctx, deps=[js[-1].fence()] if js else [], base=0x10 * (i % 8))
        h.submit(j)
        r.kccb_bg()
        r.settle("job %d" % i)
        js.append(j)
    return {"done": all(j.done() for j in js), "ufo": q.ufo_value(),
            "read_offset": h.get("rogue_fwif_cccb_ctl", q.ctrl, "read_offset"),
            "write_offset": q.write_offset}


def sc_cleanup_busy(r):
    """CLEANUP of a context while its job runs: busy, then done."""
    h = r.host
    vm = h.vm_context(r.p["pc"])
    ctx = h.compute_context(vm)
    q = ctx.queues["compute"]
    watch_queue(r, q, "compute")
    r.watch_obj("kccb_rtn", r.emu.kccb_rtn, 16)
    r.mark("setup")
    j = compute_job(r, ctx)
    h.submit(j)
    r.kccb_bg()
    r.settle("job running", complete=False)
    s1 = h.cleanup(H.CLEANUP_FWCOMMONCONTEXT, ctx.fw_addr(q))
    r.kccb_bg()
    r.settle("cleanup while busy", complete=False)
    rtn1 = r.emu.r32(r.emu.kccb_rtn + 4 * s1)
    r.settle("job completes")
    s2 = h.cleanup(H.CLEANUP_FWCOMMONCONTEXT, ctx.fw_addr(q))
    r.kccb_bg()
    r.settle("cleanup when idle")
    return {"done": j.done(), "busy_rtn": rtn1, "idle_rtn": r.emu.r32(r.emu.kccb_rtn + 4 * s2)}


def sc_teardown(r):
    """Render, then the kernel's teardown order: contexts, HWRT data, free lists."""
    h = r.host
    ctx, fl, gfl, (rt,) = new_render(r)
    r.watch_obj("kccb_rtn", r.emu.kccb_rtn, 32)
    r.mark("setup")
    geom, pr, frag = render_jobs(r, ctx, rt.data[0], 0)
    h.submit_combined(geom, pr)
    r.kccb_bg()
    r.settle("geometry")
    h.submit(frag)
    r.kccb_bg()
    r.settle("fragment")
    slots = []
    for kind, addr in ((H.CLEANUP_FWCOMMONCONTEXT, ctx.fw_addr(ctx.queues["geometry"])),
                       (H.CLEANUP_FWCOMMONCONTEXT, ctx.fw_addr(ctx.queues["fragment"])),
                       (H.CLEANUP_HWRTDATA, rt.data[0]), (H.CLEANUP_HWRTDATA, rt.data[1]),
                       (H.CLEANUP_FREELIST, fl.fw), (H.CLEANUP_FREELIST, gfl.fw)):
        slots.append(h.cleanup(kind, addr))
        r.kccb_bg()
        r.settle("cleanup %d" % kind)
    return {"done": frag.done(), "rtn": [r.emu.r32(r.emu.kccb_rtn + 4 * s) for s in slots]}


def sc_mixed(r):
    """Compute, transfer and a render in flight at the same time."""
    h = r.host
    vm = h.vm_context(r.p["pc"])
    cctx, tctx = h.compute_context(vm), h.transfer_context(vm)
    ctx, fl, gfl, (rt,) = new_render(r, vm)
    for n, q in (("compute", cctx.queues["compute"]), ("transfer", tctx.queues["transfer"]),
                 ("geom", ctx.queues["geometry"]), ("frag", ctx.queues["fragment"])):
        watch_queue(r, q, n)
    r.mark("setup")
    cj = compute_job(r, cctx)
    h.submit(cj)
    geom, pr, frag = render_jobs(r, ctx, rt.data[0], 0)
    h.submit_combined(geom, pr)
    vals = fields(r, "rogue_fwif_cmd_transfer", 0x40, {"regs.isp_render": 0x5A05A000 | 2})
    t = h.job(tctx.queues["transfer"], H.CCB_TQ_3D, H.cmd(h.L, "rogue_fwif_cmd_transfer", vals))
    h.submit(t)
    h.submit(frag)
    r.kccb_bg()
    r.settle("all kicked", complete=False)
    r.settle("drain")
    return {"done": [cj.done(), geom.done(), pr.done(), t.done(), frag.done()]}


def sc_oom(r):
    """A render whose TA runs out of parameter memory: the kernel answers
    the firmware's free list grow requests like pvr_free_list_process_grow_req."""
    h = r.host
    ctx, fl, gfl, (rt,) = new_render(r)
    gq, fq = ctx.queues["geometry"], ctx.queues["fragment"]
    watch_queue(r, gq, "geom")
    watch_queue(r, fq, "frag")
    r.watch_obj("hwrtdata0", rt.data[0], h.L.size("rogue_fwif_hwrtdata"))
    r.watch_obj("freelist", fl.fw, h.L.size("rogue_fwif_freelist"))
    r.watch_obj("gfreelist", gfl.fw, h.L.size("rogue_fwif_freelist"))
    r.watch_obj("fwccb_ctl", r.emu.fwccb_ctl, 16)
    r.watch_obj("kccb_rtn", r.emu.kccb_rtn, 16)
    r.mark("setup")
    geom, pr, frag = render_jobs(r, ctx, rt.data[0], 0)
    h.submit_combined(geom, pr)
    r.kccb_bg()
    fw_cmds = []
    for i in range(8):
        r.settle("geometry %d" % i, complete=True)
        cmds = h.fwccb_process()
        fw_cmds += cmds
        if not cmds:
            break
        r.kccb_bg()
    h.submit(frag)
    r.kccb_bg()
    r.settle("fragment")
    return {"done": [geom.done(), pr.done(), frag.done()],
            "fwccb": [(hex(t), sorted(i.items())) for t, i in fw_cmds]}


def sc_oom_live(r):
    """OOM with ready pages; the kernel's grow update arrives while the
    TA is still running."""
    h = r.host
    ctx, fl, gfl, (rt,) = new_render(r)
    for n, q in (("geom", ctx.queues["geometry"]), ("frag", ctx.queues["fragment"])):
        watch_queue(r, q, n)
    r.watch_obj("hwrtdata0", rt.data[0], h.L.size("rogue_fwif_hwrtdata"))
    r.watch_obj("gfreelist", gfl.fw, h.L.size("rogue_fwif_freelist"))
    r.watch_obj("fwccb_ctl", r.emu.fwccb_ctl, 16)
    r.mark("setup")
    geom, pr, frag = render_jobs(r, ctx, rt.data[0], 0)
    r.ta_hold = True
    h.submit_combined(geom, pr)
    r.kccb_bg()
    r.settle("geometry, out of memory")
    cmds = h.fwccb_process()
    r.kccb_bg()
    r.settle("grow update during the TA")
    r.ta_hold = False
    r.settle("TA finishes")
    h.submit(frag)
    r.kccb_bg()
    r.settle("fragment")
    return {"done": [geom.done(), pr.done(), frag.done()],
            "fwccb": [(hex(t), sorted(i.items())) for t, i in cmds]}


def sc_oom_frames(r, n=3):
    """Frames that each run out of memory once; grow updates in between."""
    h = r.host
    ctx, fl, gfl, (rt,) = new_render(r)
    for nm, q in (("geom", ctx.queues["geometry"]), ("frag", ctx.queues["fragment"])):
        watch_queue(r, q, nm)
    for i in range(2):
        r.watch_obj("hwrtdata%d" % i, rt.data[i], h.L.size("rogue_fwif_hwrtdata"))
    r.watch_obj("freelist", fl.fw, h.L.size("rogue_fwif_freelist"))
    r.watch_obj("gfreelist", gfl.fw, h.L.size("rogue_fwif_freelist"))
    r.watch_obj("fwccb_ctl", r.emu.fwccb_ctl, 16)
    r.mark("setup")
    out, cmds = [], []
    for i in range(n):
        r.oom_left = 1
        geom, pr, frag = render_jobs(r, ctx, rt.data[i % 2], 0x10 * i)
        h.submit_combined(geom, pr)
        r.kccb_bg()
        r.settle("frame %d geometry" % i)
        cmds += h.fwccb_process()
        r.kccb_bg()
        r.settle("frame %d grow update" % i)
        h.submit(frag)
        r.kccb_bg()
        r.settle("frame %d fragment" % i)
        out.append([geom.done(), pr.done(), frag.done()])
    return {"done": out, "fwccb": [(hex(t), sorted(i.items())) for t, i in cmds]}


def sc_multikick(r, kicks=3):
    """A render whose geometry comes in several kicks (Mesa splits a render
    when its control stream fills up): FIRSTKICK, middle kicks, LASTKICK,
    each paired with a partial-render fragment job; the fragment job
    follows the last kick."""
    h = r.host
    ctx, fl, gfl, (rt,) = new_render(r)
    gq, fq = ctx.queues["geometry"], ctx.queues["fragment"]
    watch_queue(r, gq, "geom")
    watch_queue(r, fq, "frag")
    r.watch_obj("hwrtdata0", rt.data[0], h.L.size("rogue_fwif_hwrtdata"))
    r.watch_obj("freelist", fl.fw, h.L.size("rogue_fwif_freelist"))
    r.watch_obj("gfreelist", gfl.fw, h.L.size("rogue_fwif_freelist"))
    r.mark("setup")
    done = []
    for k in range(kicks):
        flags = (0x1 if k == 0 else 0) | (0x2 if k == kicks - 1 else 0)
        geom, pr, frag = render_jobs(r, ctx, rt.data[0], 0x10 * k, geom_flags=flags)
        h.submit_combined(geom, pr)
        r.kccb_bg()
        r.settle("geometry kick %d (flags %d)" % (k, flags))
        done += [geom.done(), pr.done()]
    h.submit(frag)
    r.kccb_bg()
    r.settle("fragment")
    return {"done": done + [frag.done()], "geom_ufo": gq.ufo_value(), "frag_ufo": fq.ufo_value()}


def sc_suspend(r, n=3):
    """Frames with a runtime suspend/resume of the GPU between them: the
    parameter manager loses its state; free lists must carry on."""
    h = r.host
    ctx, fl, gfl, (rt,) = new_render(r)
    gq, fq = ctx.queues["geometry"], ctx.queues["fragment"]
    watch_queue(r, gq, "geom")
    watch_queue(r, fq, "frag")
    for i in range(2):
        r.watch_obj("hwrtdata%d" % i, rt.data[i], h.L.size("rogue_fwif_hwrtdata"))
    r.watch_obj("freelist", fl.fw, h.L.size("rogue_fwif_freelist"))
    r.watch_obj("gfreelist", gfl.fw, h.L.size("rogue_fwif_freelist"))
    r.mark("setup")
    out = []
    for i in range(n):
        geom, pr, frag = render_jobs(r, ctx, rt.data[i % 2], 0x10 * i)
        h.submit_combined(geom, pr)
        r.kccb_bg()
        r.settle("frame %d geometry" % i)
        h.submit(frag)
        r.kccb_bg()
        r.settle("frame %d fragment" % i)
        out.append([geom.done(), pr.done(), frag.done()])
        if i < n - 1:
            r.power_cycle()
    return {"done": out, "geom_ufo": gq.ufo_value(), "frag_ufo": fq.ufo_value()}


HWRINFO_TIMES = (0x20, 0x28, 0x58, 0x60, 0x68, 0x70)


def watch_hwr(r):
    """What a hardware recovery leaves for the host: context reset
    notifications in the firmware CCB, the HWR info buffer, power state."""
    e, L = r.emu, r.host.L
    r.watch_obj("fwccb_ctl", e.fwccb_ctl, 16)
    r.watch_obj("fwccb", e.fwccb, 2 * L.size("rogue_fwif_fwccb_cmd"))
    hib = e.objects["hwrinfobuf"][0]
    r.watch_obj("hwrinfo0", hib, 0x88, HWRINFO_TIMES)
    r.watch_obj("hwrinfo1", hib + 0x88, 0x88, HWRINFO_TIMES)
    r.watch_obj("hwrcounters", hib + 0x880, L.size("rogue_fwif_hwrinfobuf") - 0x880)
    r.watch_obj("pow_state", e.objects["sysdata"][0] + 8, 4)


def wait_recovery(r, jobs_, ticks, regs=None):
    """Timer ticks until the hung jobs are finished (by recovery)."""
    n = 0
    while n < ticks and not all(j.done() for j in jobs_):
        r.tick("timer %d" % n, regs=regs(n) if regs else None)
        n += 1
    return n


def sc_hang_compute(r, ticks=24, progress=0):
    """A compute job that never finishes: the firmware's lockup detection
    and recovery, then the next job on the same context. progress: for
    that many timer ticks the CDM signature register keeps changing."""
    h = r.host
    vm = h.vm_context(r.p["pc"])
    ctx = h.compute_context(vm)
    q = ctx.queues["compute"]
    watch_queue(r, q, "compute")
    watch_hwr(r)
    r.mark("setup")
    r.hang.add("CDM")
    j = compute_job(r, ctx)
    h.submit(j)
    r.kccb_bg()
    r.settle("compute job (hangs)")
    n = wait_recovery(r, [j], ticks,
                      lambda i: {0x40F8: 0x1000 + i} if i < progress else None)
    fw = h.fwccb_process()
    r.hang.discard("CDM")
    j2 = compute_job(r, ctx, base=0x10)
    h.submit(j2)
    r.kccb_bg()
    r.settle("next compute job")
    return {"ticks": n, "done": [j.done(), j2.done()], "ufo": q.ufo_value(),
            "fwccb": [(hex(t), sorted(i.items())) for t, i in fw]}


def sc_hang_render(r, dm="TA", ticks=24):
    """A render whose geometry (dm="TA") or fragment ("3D") work never
    finishes; then the next frame."""
    h = r.host
    ctx, fl, gfl, (rt,) = new_render(r)
    gq, fq = ctx.queues["geometry"], ctx.queues["fragment"]
    watch_queue(r, gq, "geom")
    watch_queue(r, fq, "frag")
    r.watch_obj("hwrtdata0", rt.data[0], h.L.size("rogue_fwif_hwrtdata"))
    r.watch_obj("freelist", fl.fw, h.L.size("rogue_fwif_freelist"))
    r.watch_obj("gfreelist", gfl.fw, h.L.size("rogue_fwif_freelist"))
    watch_hwr(r)
    r.mark("setup")
    r.hang.add(dm)
    geom, pr, frag = render_jobs(r, ctx, rt.data[0], 0)
    h.submit_combined(geom, pr)
    r.kccb_bg()
    r.settle("geometry + PR kick")
    h.submit(frag)
    r.kccb_bg()
    r.settle("fragment kick")
    n = wait_recovery(r, [geom, frag], ticks)
    fw = h.fwccb_process()
    if fw:
        r.kccb_bg()
        r.settle("host answers")
    r.hang.discard(dm)
    geom2, pr2, frag2 = render_jobs(r, ctx, rt.data[0], 0x200, deps=[frag.fence()])
    h.submit_combined(geom2, pr2)
    r.kccb_bg()
    r.settle("next geometry")
    h.submit(frag2)
    r.kccb_bg()
    r.settle("next fragment")
    return {"ticks": n, "done": [geom.done(), pr.done(), frag.done(), geom2.done(),
                                 frag2.done()],
            "ufo": [gq.ufo_value(), fq.ufo_value()],
            "fwccb": [(hex(t), sorted(i.items())) for t, i in fw]}


def sc_hang_transfer(r, ticks=12, next_transfer=False):
    """A transfer job (3D pipe, no render target) that never finishes;
    then a compute job, or (next_transfer) another transfer job: the
    reference firmware faults in that one's completion (a stale pointer in
    its transfer bookkeeping after the recovery), so only openfw runs it."""
    h = r.host
    vm = h.vm_context(r.p["pc"])
    cctx = h.compute_context(vm)
    ctx = h.transfer_context(vm)
    q = ctx.queues["transfer"]
    watch_queue(r, q, "transfer")
    watch_hwr(r)
    r.mark("setup")
    r.hang.add("3D")
    vals = fields(r, "rogue_fwif_cmd_transfer", 0)
    r.tag_fields("rogue_fwif_cmd_transfer", vals)
    j = h.job(q, H.CCB_TQ_3D, H.cmd(h.L, "rogue_fwif_cmd_transfer", vals))
    h.submit(j)
    r.kccb_bg()
    r.settle("transfer job (hangs)")
    n = wait_recovery(r, [j], ticks)
    fw = h.fwccb_process()
    r.hang.discard("3D")
    if next_transfer:
        j2 = h.job(q, H.CCB_TQ_3D, H.cmd(h.L, "rogue_fwif_cmd_transfer", vals))
    else:
        j2 = compute_job(r, cctx)
    h.submit(j2)
    r.kccb_bg()
    r.settle("next job")
    return {"ticks": n, "done": [j.done(), j2.done()], "ufo": q.ufo_value(),
            "fwccb": [(hex(t), sorted(i.items())) for t, i in fw]}


def sc_hang_compute_usc(r, ticks=40, progress=10, twice=False):
    """Compute work holding USC slots whose state changes for `progress`
    ticks (the signature registers stay put), then stops; with twice, a
    second job hangs the same way after the recovery."""
    h = r.host
    vm = h.vm_context(r.p["pc"])
    ctx = h.compute_context(vm)
    q = ctx.queues["compute"]
    watch_queue(r, q, "compute")
    watch_hwr(r)
    r.mark("setup")
    r.hang.add("CDM")
    r.reg_values.update({0x4178: 0x22222222, 0x417C: 0x00000022})
    res = {"ticks": [], "done": []}
    for k in range(2 if twice else 1):
        j = compute_job(r, ctx, base=0x10 * k)
        h.submit(j)
        r.kccb_bg()
        r.settle("compute job %d (hangs)" % k)
        n = wait_recovery(r, [j], ticks, lambda i: {0x41E0: 0x100 + i + 64 * k}
                          if i < progress else None)
        res["ticks"].append(n)
        res["done"].append(j.done())
    res["fwccb"] = [(hex(t), sorted(i.items())) for t, i in h.fwccb_process()]
    res["ufo"] = q.ufo_value()
    return res


def sc_overrun(r, ticks=20, dt=0x400000):
    """A compute job that keeps making progress but runs past its
    context's deadline (30 s)."""
    h = r.host
    vm = h.vm_context(r.p["pc"])
    ctx = h.compute_context(vm)
    q = ctx.queues["compute"]
    watch_queue(r, q, "compute")
    watch_hwr(r)
    r.mark("setup")
    r.hang.add("CDM")
    j = compute_job(r, ctx)
    h.submit(j)
    r.kccb_bg()
    r.settle("compute job (runs too long)")
    n = 0
    while n < ticks and not j.done():
        r.tick("timer %d" % n, dt=dt, regs={0x40F8: 0x1000 + n})
        n += 1
    fw = h.fwccb_process()
    return {"ticks": n, "done": j.done(), "ufo": q.ufo_value(),
            "fwccb": [(hex(t), sorted(i.items())) for t, i in fw]}


def sc_hang_ta_compute_ok(r, ticks=24):
    """Geometry hangs while a compute job makes progress and then finishes
    normally: the geometry waits for it, then is recovered alone."""
    h = r.host
    vm = h.vm_context(r.p["pc"])
    cctx = h.compute_context(vm)
    cq = cctx.queues["compute"]
    ctx, fl, gfl, (rt,) = new_render(r, vm)
    gq = ctx.queues["geometry"]
    watch_queue(r, cq, "compute")
    watch_queue(r, gq, "geom")
    watch_hwr(r)
    r.mark("setup")
    r.hang |= {"CDM", "TA"}
    cj = compute_job(r, cctx)
    h.submit(cj)
    r.kccb_bg()
    r.settle("compute job (busy)")
    geom, pr, frag = render_jobs(r, ctx, rt.data[0], 0)
    h.submit_combined(geom, pr)
    r.kccb_bg()
    r.settle("geometry (hangs)")
    n = 0
    while n < ticks and not geom.done():
        if n == 8:
            r.hang.discard("CDM")       # the compute job finishes
            r.settle("compute finishes")
        r.tick("timer %d" % n, regs={0x40F8: 0x1000 + n} if n < 8 else None)
        n += 1
    fw = h.fwccb_process()
    return {"ticks": n, "done": [cj.done(), geom.done()],
            "fwccb": [(hex(t), sorted(i.items())) for t, i in fw]}


def sc_fault_compute(r, ticks=24, kind="compute"):
    """A compute (or geometry) job whose memory access faults in the GPU
    MMU; then the next job."""
    h = r.host
    vm = h.vm_context(r.p["pc"])
    if kind == "compute":
        ctx = h.compute_context(vm)
        q = ctx.queues["compute"]
        watch_queue(r, q, "compute")
    else:
        ctx, fl, gfl, (rt,) = new_render(r, vm)
        q = ctx.queues["geometry"]
        watch_queue(r, q, "geom")
        watch_queue(r, ctx.queues["fragment"], "frag")
        r.watch_obj("hwrtdata0", rt.data[0], h.L.size("rogue_fwif_hwrtdata"))
    watch_hwr(r)
    r.mark("setup")
    dm = "CDM" if kind == "compute" else "TA"
    r.hang.add(dm)
    if kind == "compute":
        jobs_ = [compute_job(r, ctx)]
        h.submit(jobs_[0])
    else:
        geom, pr, frag = render_jobs(r, ctx, rt.data[0], 0)
        jobs_ = [geom]
        h.submit_combined(geom, pr)
    r.kccb_bg()
    r.settle("job")
    r.page_fault("page fault")
    n = wait_recovery(r, jobs_, ticks)
    fw = h.fwccb_process()
    if fw:
        r.kccb_bg()
        r.settle("host answers")
    r.hang.discard(dm)
    if kind == "compute":
        j2 = compute_job(r, ctx, base=0x10)
        h.submit(j2)
        r.kccb_bg()
        r.settle("next job")
        jobs_.append(j2)
    return {"ticks": n, "done": [j.done() for j in jobs_],
            "fwccb": [(hex(t), sorted(i.items())) for t, i in fw]}


def sc_fault_two(r, ticks=24, which=1):
    """Compute (VM A) and geometry (VM B) busy; the MMU faults through
    page catalogue set `which` (1: VM A's, 2: VM B's)."""
    h = r.host
    vma = h.vm_context(r.p["pc"])
    vmb = h.vm_context(r.p["pc"] + 0x1000000)
    cctx = h.compute_context(vma)
    cq = cctx.queues["compute"]
    ctx, fl, gfl, (rt,) = new_render(r, vmb)
    gq = ctx.queues["geometry"]
    watch_queue(r, cq, "compute")
    watch_queue(r, gq, "geom")
    watch_hwr(r)
    r.mark("setup")
    r.hang |= {"CDM", "TA"}
    cj = compute_job(r, cctx)
    h.submit(cj)
    r.kccb_bg()
    r.settle("compute job")
    geom, pr, frag = render_jobs(r, ctx, rt.data[0], 0)
    h.submit_combined(geom, pr)
    r.kccb_bg()
    r.settle("geometry")
    r.page_fault("page fault", cat_base=which)
    n = wait_recovery(r, [cj, geom], ticks)
    fw = h.fwccb_process()
    if fw:
        r.kccb_bg()
        r.settle("host answers")
    return {"ticks": n, "done": [cj.done(), geom.done()],
            "fwccb": [(hex(t), sorted(i.items())) for t, i in fw]}


def sc_oom_wait(r, ticks=12, pr_ticks=0):
    """Geometry out of parameter memory, waiting for the kernel's grow
    (fl_threshold 0: no ready pages) while timer ticks pass: the kernel
    answers late. With pr_ticks and a free list at its maximum, the
    partial render on the 3D pipe takes that many ticks instead."""
    h = r.host
    ctx, fl, gfl, (rt,) = new_render(r)
    gq, fq = ctx.queues["geometry"], ctx.queues["fragment"]
    watch_queue(r, gq, "geom")
    watch_queue(r, fq, "frag")
    r.watch_obj("hwrtdata0", rt.data[0], h.L.size("rogue_fwif_hwrtdata"))
    r.watch_obj("gfreelist", gfl.fw, h.L.size("rogue_fwif_freelist"))
    watch_hwr(r)
    r.mark("setup")
    geom, pr, frag = render_jobs(r, ctx, rt.data[0], 0)
    if pr_ticks:
        r.hang.add("3D")
    h.submit_combined(geom, pr)
    r.kccb_bg()
    r.settle("geometry, out of memory")
    for i in range(pr_ticks or ticks):
        r.tick("timer %d" % i, regs={0x5038: 0x100 + i} if pr_ticks else None)
    r.hang.discard("3D")
    r.settle("partial render finishes")
    cmds = h.fwccb_process()
    r.kccb_bg()
    r.settle("grow update")
    h.submit(frag)
    r.kccb_bg()
    r.settle("fragment")
    return {"done": [geom.done(), pr.done(), frag.done()],
            "fwccb": [(hex(t), sorted(i.items())) for t, i in cmds]}


def sc_oom_wait_hang(r, wait=3, ticks=12):
    """Geometry waits `wait` ticks for a free list grow, resumes when it
    arrives and then never finishes."""
    h = r.host
    ctx, fl, gfl, (rt,) = new_render(r)
    gq, fq = ctx.queues["geometry"], ctx.queues["fragment"]
    watch_queue(r, gq, "geom")
    watch_queue(r, fq, "frag")
    r.watch_obj("hwrtdata0", rt.data[0], h.L.size("rogue_fwif_hwrtdata"))
    r.watch_obj("gfreelist", gfl.fw, h.L.size("rogue_fwif_freelist"))
    watch_hwr(r)
    r.mark("setup")
    geom, pr, frag = render_jobs(r, ctx, rt.data[0], 0)
    h.submit_combined(geom, pr)
    r.kccb_bg()
    r.settle("geometry, out of memory")
    for i in range(wait):
        r.tick("waiting %d" % i)
    r.hang.add("TA")
    cmds = h.fwccb_process()
    r.kccb_bg()
    r.settle("grow update, TA resumes")
    n = wait_recovery(r, [geom], ticks)
    cmds += h.fwccb_process()
    return {"ticks": n, "done": [geom.done(), pr.done()],
            "fwccb": [(hex(t), sorted(i.items())) for t, i in cmds]}


def sc_stress(r, seed, ops=40):
    """Randomised desktop-like traffic, the same for every firmware (seeded):
    a compositor-like render context drawing frames on two HWRT data sets,
    applications' compute and transfer work in one or two VMs, priorities,
    cross-queue fences, completions in any order, memory pressure, timer
    ticks with progress, suspend/resume when idle, and teardown."""
    import random
    rnd = random.Random(seed)
    h, p = r.host, r.p
    vms = [h.vm_context(p["pc"] + 0x100000 * i) for i in range(rnd.choice((1, 2)))]
    comp = [h.compute_context(rnd.choice(vms), priority=rnd.choice((0, 0, 1, 2)))
            for _ in range(rnd.choice((1, 2)))]
    xfer = [h.transfer_context(rnd.choice(vms), priority=rnd.choice((0, 1)))
            for _ in range(rnd.choice((0, 1, 1)))]
    renders = []
    for _ in range(rnd.choice((1, 1, 2))):
        ctx, fl, gfl, rts = new_render(r, rnd.choice(vms), data_sets=2)
        renders.append({"ctx": ctx, "rts": rts, "frame": 0, "fls": (fl, gfl)})
    queues = [c.queues["compute"] for c in comp] + [c.queues["transfer"] for c in xfer]
    for i, q in enumerate(queues):
        watch_queue(r, q, "q%d" % i)
    for i, rd in enumerate(renders):
        watch_queue(r, rd["ctx"].queues["geometry"], "r%d.geom" % i)
        watch_queue(r, rd["ctx"].queues["fragment"], "r%d.frag" % i)
        for k, rt in enumerate(rd["rts"]):
            r.watch_obj("r%d.hwrt%d" % (i, k), rt.data[0], h.L.size("rogue_fwif_hwrtdata"))
        for k, fl in enumerate(rd["fls"]):
            r.watch_obj("r%d.fl%d" % (i, k), fl.fw, h.L.size("rogue_fwif_freelist"))
    r.watch_obj("kccb_rtn", r.emu.kccb_rtn, 64)
    r.watch_obj("fwccb_ctl", r.emu.fwccb_ctl, 16)
    r.mark("setup")
    jobs_, fences, n, ticks = [], [], 0, 0
    fw_cmds = []

    def deps():
        return [rnd.choice(fences)] if fences and rnd.random() < 0.4 else []

    def pump(label, complete):
        r.settle(label, complete=complete)
        for _ in range(4):
            cmds = h.fwccb_process()
            if not cmds:
                break
            fw_cmds.extend(cmds)
            r.kccb_bg()
            r.settle(label + " (host answers)", complete=complete)

    for step in range(ops):
        op = rnd.choice(("compute", "compute", "transfer", "frame", "frame", "frame",
                         "complete", "complete", "settle", "tick", "oom", "power"))
        label = "%d %s" % (step, op)
        if op == "compute" or (op == "transfer" and not xfer):
            c = rnd.choice(comp)
            j = compute_job(r, c, deps(), base=0x10 * (n % 8))
            h.submit(j)
        elif op == "transfer":
            c = rnd.choice(xfer)
            q = c.queues["transfer"]
            vals = fields(r, "rogue_fwif_cmd_transfer", 0x10 * (n % 8))
            j = h.job(q, H.CCB_TQ_3D, H.cmd(h.L, "rogue_fwif_cmd_transfer", vals), deps())
            h.submit(j)
        elif op == "frame":
            rd = rnd.choice(renders)
            rt = rd["rts"][rd["frame"] % 2]
            rd["frame"] += 1
            geom, pr, frag = render_jobs(r, rd["ctx"], rt.data[0], 0x200 * (n % 4), deps())
            h.submit_combined(geom, pr)
            r.kccb_bg()
            pump(label + " geometry", rnd.random() < 0.5)
            h.submit(frag)
            jobs_ += [geom, pr]
            j = frag
        elif op == "complete":
            r.complete_dm(rnd.choice(("CDM", "TA", "3D")), label)
            continue
        elif op == "settle":
            pump(label, True)
            continue
        elif op == "tick":
            ticks += 1
            r.tick(label, regs={0x4600: 0x1000 + ticks})     # everything progresses
            continue
        elif op == "oom":
            r.oom_left += 1
            continue
        else:
            pump(label + " (drain)", True)
            if all(x.done() for x in jobs_):
                r.power_cycle()
            continue
        n += 1
        jobs_.append(j)
        fences.append(j.fence())
        r.kccb_bg()
        pump(label, rnd.random() < 0.6)
    pump("drain", True)
    pump("drain again", True)
    return {"done": [j.done() for j in jobs_],
            "ufo": [q.ufo_value() for q in queues],
            "fwccb": [(hex(t), sorted(i.items())) for t, i in fw_cmds]}


def sc_hang_two(r, ticks=24, ta_progress=0):
    """Compute and geometry hung together (innocent / guilty work);
    ta_progress: ticks for which the geometry signature keeps changing."""
    h = r.host
    vm = h.vm_context(r.p["pc"])
    cctx = h.compute_context(vm)
    cq = cctx.queues["compute"]
    ctx, fl, gfl, (rt,) = new_render(r, vm)
    gq, fq = ctx.queues["geometry"], ctx.queues["fragment"]
    watch_queue(r, cq, "compute")
    watch_queue(r, gq, "geom")
    watch_queue(r, fq, "frag")
    r.watch_obj("hwrtdata0", rt.data[0], h.L.size("rogue_fwif_hwrtdata"))
    watch_hwr(r)
    r.mark("setup")
    r.hang |= {"CDM", "TA"}
    cj = compute_job(r, cctx)
    h.submit(cj)
    r.kccb_bg()
    r.settle("compute job (hangs)")
    geom, pr, frag = render_jobs(r, ctx, rt.data[0], 0)
    h.submit_combined(geom, pr)
    r.kccb_bg()
    r.settle("geometry (hangs)")
    n = wait_recovery(r, [cj, geom], ticks,
                      lambda i: {0x5000: 0x1000 + i} if i < ta_progress else None)
    fw = h.fwccb_process()
    if fw:
        r.kccb_bg()
        r.settle("host answers")
    r.hang -= {"CDM", "TA"}
    r.settle("after recovery")
    h.submit(frag)
    r.kccb_bg()
    r.settle("fragment")
    return {"ticks": n, "done": [cj.done(), geom.done(), pr.done(), frag.done()],
            "ufo": [cq.ufo_value(), gq.ufo_value(), fq.ufo_value()],
            "fwccb": [(hex(t), sorted(i.items())) for t, i in fw]}


def sc_power(r):
    """Kernel CCB maintenance and power commands without jobs: health
    check, forced idle and cancel, MMU cache flush, log type, number of
    units, power off and firmware restart."""
    e = r.emu
    r.watch_obj("kccb_rtn", e.kccb_rtn, 32)
    r.mark("setup")
    sync = e.objects["mmucache_sync"][0]
    cmds = [("health", 115, []),
            ("forced idle", 107, [("cmd_data.pow_data.pow_type", 2),
                                  ("cmd_data.pow_data.power_req_data.pow_request_type", 1)]),
            ("cancel forced idle", 107, [("cmd_data.pow_data.pow_type", 2),
                                         ("cmd_data.pow_data.power_req_data.pow_request_type", 2)]),
            ("mmu cache", 102, [("cmd_data.mmu_cache_data.cache_flags", 0x400001F),
                                ("cmd_data.mmu_cache_data.mmu_cache_sync_fw_addr", sync),
                                ("cmd_data.mmu_cache_data.mmu_cache_sync_update_value", 1)]),
            ("log type", 206, []),
            ("units", 107, [("cmd_data.pow_data.pow_type", 3),
                            ("cmd_data.pow_data.power_req_data.num_of_dusts", 1)])]
    for name, t, fields in cmds:
        e.send_kccb(t, fields)
        r.kccb_bg()
        r.settle(name)
    r.power_cycle()
    e.send_kccb(115, [])
    r.kccb_bg()
    r.settle("health after restart")
    return {"rtn": [e.r32(e.kccb_rtn + 4 * i) for i in range(8)], "sync": e.r32(sync)}


def sc_priority(r, prios=(0, 0, 2, 1)):
    """Contexts of different priority waiting for a busy data master:
    compute contexts (CDM) and transfer contexts (3D pipe)."""
    h = r.host
    vm = h.vm_context(r.p["pc"])
    cctx = [h.compute_context(vm, priority=p) for p in prios]
    tctx = [h.transfer_context(vm, priority=p) for p in prios]
    for i, c in enumerate(cctx):
        watch_queue(r, c.queues["compute"], "compute%d" % i)
    for i, c in enumerate(tctx):
        watch_queue(r, c.queues["transfer"], "transfer%d" % i)
    r.mark("setup")
    jobs_ = []
    for i, c in enumerate(cctx):        # the first runs, the others wait
        j = compute_job(r, c, base=0x10 * i)
        h.submit(j)
        jobs_.append(j)
    for i, c in enumerate(tctx):
        vals = fields(r, "rogue_fwif_cmd_transfer", 0x40 + i, {"regs.isp_render": 0x5A05A000 | 2})
        t = h.job(c.queues["transfer"], H.CCB_TQ_3D, H.cmd(h.L, "rogue_fwif_cmd_transfer", vals))
        h.submit(t)
        jobs_.append(t)
    r.kccb_bg()
    r.settle("all submitted", complete=False)
    r.settle("drain")
    return {"done": [j.done() for j in jobs_]}


SCENARIOS = {
    "compute": lambda r: sc_compute(r),
    "compute2": lambda r: sc_compute(r, n=2, chained=True),
    "transfer": sc_transfer,
    "render": sc_render,
    "geom": lambda r: sc_render(r, with_frag=False),
    "cleanup": sc_cleanup,
    "frames": sc_frames,
    "frames-pipelined": lambda r: sc_frames(r, n=4, pipelined=True),
    "multivm": sc_multivm,
    "multivm-concurrent": lambda r: sc_multivm(r, concurrent=True),
    "blocked": sc_blocked,
    "wrap": sc_wrap,
    "cleanup-busy": sc_cleanup_busy,
    "teardown": sc_teardown,
    "mixed": sc_mixed,
    "oom": sc_oom,
    "oom-live": sc_oom_live,
    "oom-frames": sc_oom_frames,
    "multikick": sc_multikick,
    "suspend": sc_suspend,
    "power": sc_power,
    "priority": sc_priority,
    "priority2": lambda r: sc_priority(r, prios=(2, 1, 0, 2)),
    "hang-compute": sc_hang_compute,
    "hang-compute-progress": lambda r: sc_hang_compute(r, ticks=40, progress=12),
    "hang-ta": lambda r: sc_hang_render(r, "TA"),
    "hang-two": sc_hang_two,
    "hang-transfer": sc_hang_transfer,
    **{"stress%d" % i: (lambda i: lambda r: sc_stress(r, i))(i) for i in range(64)},
    "hang-transfer-next": lambda r: sc_hang_transfer(r, next_transfer=True),
    "hang-compute-usc": sc_hang_compute_usc,
    "hang-compute-twice": lambda r: sc_hang_compute_usc(r, ticks=30, progress=4, twice=True),
    "overrun": sc_overrun,
    "fault-compute": sc_fault_compute,
    "fault-geom": lambda r: sc_fault_compute(r, kind="geom"),
    "fault-two": sc_fault_two,
    "oom-wait": sc_oom_wait,
    "oom-wait-hang": sc_oom_wait_hang,
    "pr-slow": lambda r: sc_oom_wait(r, pr_ticks=10),
    "fault-two-b": lambda r: sc_fault_two(r, which=2),
    "hang-ta-compute-ok": sc_hang_ta_compute_ok,
    "hang-two-progress": lambda r: sc_hang_two(r, ticks=30, ta_progress=20),
    "hang-3d": lambda r: sc_hang_render(r, "3D"),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fw")
    ap.add_argument("scenario", nargs="+", choices=sorted(SCENARIOS))
    ap.add_argument("--kernel", required=True)
    ap.add_argument("--json")
    ap.add_argument("--no-writes", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="show emulator output")
    args = ap.parse_args()
    results = {}
    for sc in args.scenario:
        r = Runner(args.fw, args.kernel, quiet=not args.verbose)
        r.boot()
        res = SCENARIOS[sc](r)
        print("##### %s: %s" % (sc, res))
        for st in r.steps[1:]:
            print(r.describe(st, not args.no_writes))
        results[sc] = {"result": res, "steps": [
            {"step": s["step"], "writes": s["writes"], "trace": s["trace"],
             "mem": {k: v.hex() for k, v in s["mem"].items()}} for s in r.steps]}
    if args.json:
        json.dump(results, open(args.json, "w"), indent=1)


if __name__ == "__main__":
    main()

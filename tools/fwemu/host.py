# SPDX-License-Identifier: MIT
"""
host.py - the Linux powervr driver's job-submission side, for fwemu.

Builds the firmware-visible objects exactly as drm/imagination does
(v7.3: pvr_vm.c, pvr_context.c, pvr_queue.c, pvr_cccb.c, pvr_free_list.c,
pvr_hwrt.c, pvr_job.c) and submits jobs the way pvr_queue_run_job() does:

  client CCB:  [FENCE_PR {ufo, seqno}...]  <job command>  UPDATE {timeline, seqno}
  kernel CCB:  KICK {context, client_woff, wrap_mask, cleanup ctl}
               or COMBINED_GEOM_FRAG_KICK for a geometry job paired with
               its partial-render fragment job (Mesa always submits that pair)

Command payloads are built with cmd() from field values; tag() produces
recognisable values so a register trace shows where each field went.
"""
import struct

MAGIC = 0x2ABC0000
TASK = 0x8000
CCB_GEOM = 201 | MAGIC | TASK
CCB_TQ_3D = 202 | MAGIC | TASK
CCB_FRAG = 203 | MAGIC | TASK
CCB_FRAG_PR = 204 | MAGIC | TASK
CCB_CDM = 205 | MAGIC | TASK
CCB_NULL = 210 | MAGIC | TASK
CCB_FENCE = 212 | MAGIC
CCB_UPDATE = 213 | MAGIC
CCB_FENCE_PR = 215 | MAGIC
CCB_PADDING = 221 | MAGIC

KCCB_KICK = 101
KCCB_MMUCACHE = 102
KCCB_CLEANUP = 106
KCCB_FREELIST_GROW_UPDATE = 110
KCCB_FREELISTS_RECONSTRUCTION_UPDATE = 112
KCCB_COMBINED_KICK = 117
FWCCB_FREELIST_GROW = 103 | MAGIC
FWCCB_UPDATE_STATS = 107 | MAGIC
FWCCB_FREELISTS_RECONSTRUCTION = 104 | MAGIC
FWCCB_CONTEXT_RESET_NOTIFICATION = 105 | MAGIC
RTDATA_STATE_HWR = 9
HWRTDATA_HAS_LAST_GEOM = 1 << 2

CLEANUP_FWCOMMONCONTEXT, CLEANUP_HWRTDATA, CLEANUP_FREELIST = 0, 1, 2

DM_GP, DM_TDM, DM_GEOM, DM_FRAG, DM_CDM = 0, 1, 2, 3, 4

PAGE = 0x1000
CCCB_SIZE_LOG2 = 15
MAX_DEADLINE_MS = 30000
ROGUE_FW_BIF_INVALID_PCSET = 0xFFFFFFFF
FREE_LIST_ENTRY_SIZE = 4
FREE_LIST_ALIGNMENT = 16 // FREE_LIST_ENTRY_SIZE - 1


def align(x, a):
    return (x + a - 1) & ~(a - 1)


def tag(index, bits=32):
    """A value that identifies stream field @index in a register trace:
    0x5Axxx000 with the field number in bits 23:12 of the low word, so
    the low control bits of the register stay clear."""
    lo = 0x5A000000 | (index & 0xFFF) << 12
    if bits == 32:
        return lo
    return (0xA0 | (index & 0x1F)) << 32 | lo


class Queue:
    """pvr_queue: client CCB, timeline UFO, register state, FW context slot."""

    def __init__(self, host, ctx, name, dm, ctx_offset, state_size):
        self.host, self.ctx, self.name, self.dm = host, ctx, name, dm
        self.ctx_offset = ctx_offset
        h = host
        self.size = 1 << CCCB_SIZE_LOG2
        self.ctrl = h.fw_obj("%s.%s.cccb_ctl" % (ctx.name, name), h.L.size("rogue_fwif_cccb_ctl"))
        h.set("rogue_fwif_cccb_ctl", self.ctrl, "wrap_mask", self.size - 1)
        self.cccb = h.fw_obj("%s.%s.cccb" % (ctx.name, name), self.size)
        self.reg_state = h.fw_obj("%s.%s.reg_state" % (ctx.name, name), state_size)
        self.ufo = h.fw_obj("%s.%s.timeline_ufo" % (ctx.name, name), 4)
        self.write_offset = 0
        self.seqno = 0
        self.kicks_sent = 0

    # pvr_cccb_write_command_with_header()
    def write_cmd(self, cmd_type, data, ext_job_ref=0, int_job_ref=0):
        h = self.host
        H = "rogue_fwif_ccb_cmd_header"
        hdr_size = h.L.size(H)
        size = align(len(data), 8)
        total = hdr_size + size
        remaining = self.size - self.write_offset
        if remaining < total + hdr_size:
            pad = self.cccb + self.write_offset
            h.write(pad, b"\0" * hdr_size)
            h.set(H, pad, "cmd_type", CCB_PADDING)
            h.set(H, pad, "cmd_size", remaining - hdr_size)
            self.write_offset = 0
        at = self.cccb + self.write_offset
        h.write(at, b"\0" * total)
        h.set(H, at, "cmd_type", cmd_type)
        h.set(H, at, "cmd_size", size)
        h.set(H, at, "ext_job_ref", ext_job_ref)
        h.set(H, at, "int_job_ref", int_job_ref)
        h.write(at + hdr_size, data)
        self.write_offset += total
        return at

    def ufo_value(self):
        return self.host.r32(self.ufo)


class Context:
    def __init__(self, host, name, kind, vm, size):
        self.host, self.name, self.kind, self.vm = host, name, kind, vm
        self.data = bytearray(size)       # ctx->data, copied to the FW object
        self.queues = {}
        self.fw = None

    def fw_addr(self, queue):
        return self.fw + queue.ctx_offset


class GpuVM:
    """A GPU virtual address space in system memory, as pvr_mmu.c builds
    it with 4 KiB device pages: a 1024-entry page catalogue (32-bit entries,
    PD address >> 12 in bits 31:4), 512-entry page directories and tables
    (64-bit entries, next-level / page address in bits 39:12), valid = 1."""

    def __init__(self, sysmem, pc_pa):
        self.mem, self.pc = sysmem, pc_pa
        self.mem.write(pc_pa, bytes(PAGE))

    def _pte_addr(self, va, create):
        m = self.mem
        pce = self.pc + 4 * ((va >> 30) & 0x3FF)
        e = m.r32(pce)
        if not e & 1:
            if not create:
                return None
            pd = m.alloc()
            m.write(pd, bytes(PAGE))
            e = (pd >> 8) | 1
            m.w32(pce, e)
        pd = (e & ~0xF) << 8
        pde = pd + 8 * ((va >> 21) & 0x1FF)
        e = m.r64(pde)
        if not e & 1:
            if not create:
                return None
            pt = m.alloc()
            m.write(pt, bytes(PAGE))
            e = pt | 1                        # page size 4 KiB (0), valid
            m.w64(pde, e)
        return (e & 0xFFFFFFF000) + 8 * ((va >> 12) & 0x1FF)

    def map(self, va, size, fill=0xA5, read_only=False):
        for off in range(0, size, PAGE):
            pte = self._pte_addr(va + off, True)
            page = self.mem.alloc()
            self.mem.write(page, bytes([fill]) * PAGE)
            self.mem.w64(pte, page | (2 if read_only else 0) | 1)

    def translate(self, va):
        pte = self._pte_addr(va, False)
        if pte is None:
            return None
        e = self.mem.r64(pte)
        return (e & 0xFFFFFFF000) + (va & (PAGE - 1)) if e & 1 else None

    def read(self, va, n):
        out = bytearray()
        while n:
            k = min(n, PAGE - (va & (PAGE - 1)))
            pa = self.translate(va)
            out += self.mem.read(pa, k) if pa is not None else bytes(k)
            va += k
            n -= k
        return bytes(out)


class FreeList:
    def __init__(self, host, vm, gpu_addr, initial, max_pages, grow, threshold, fw_id):
        self.host = host
        self.max_pages, self.grow_pages, self.grow_threshold = max_pages, grow, threshold
        self.fw_id = fw_id
        self.gpu_addr = gpu_addr
        ready = self.ready_pages_for(initial)
        F = "rogue_fwif_freelist"
        self.fw = host.fw_obj("freelist%d" % fw_id, host.L.size(F))
        current = initial - ready
        cur_dev = (gpu_addr + (max_pages - current) * FREE_LIST_ENTRY_SIZE) & ~(16 - 1)
        for path, v in (("max_pages", max_pages), ("current_pages", current),
                        ("grow_pages", grow), ("ready_pages", ready), ("freelist_id", fw_id),
                        ("grow_pending", 0), ("current_stack_top", current - 1),
                        ("freelist_dev_addr", gpu_addr), ("current_dev_addr", cur_dev)):
            host.set(F, self.fw, path, v)
        self.current_pages, self.ready_pages = current, ready
        self.hwrts = []          # HWRT data objects using this free list

    def reconstruct(self):
        """pvr_free_list_reconstruct: after a hardware recovery the pages
        are put back on the list; HWRT data using it start over."""
        h, F, D = self.host, "rogue_fwif_freelist", "rogue_fwif_hwrtdata"
        h.set(F, self.fw, "current_stack_top", h.get(F, self.fw, "current_pages") - 1)
        h.set(F, self.fw, "allocated_page_count", 0)
        h.set(F, self.fw, "allocated_mmu_page_count", 0)
        for d in self.hwrts:
            h.set(D, d, "state", RTDATA_STATE_HWR)
            h.set(D, d, "hwrt_data_flags", h.get(D, d, "hwrt_data_flags") & ~HWRTDATA_HAS_LAST_GEOM)

    def process_grow_req(self):
        """pvr_free_list_process_grow_req: the FW used the ready pages;
        grow by grow_pages if allowed and answer with FREELIST_GROW_UPDATE."""
        h = self.host
        self.current_pages += self.ready_pages
        self.ready_pages = 0
        grow = 0
        if (self.grow_pages and self.current_pages + self.grow_pages <= self.max_pages
                and not getattr(self, "grow_fails", False)):
            self.current_pages += self.grow_pages
            grow = self.grow_pages
            self.ready_pages = self.ready_pages_for(self.current_pages)
            self.current_pages -= self.ready_pages
        p = "cmd_data.free_list_gs_data."
        return h.emu.send_kccb(KCCB_FREELIST_GROW_UPDATE, [
            (p + "freelist_fw_addr", self.fw), (p + "delta_pages", grow),
            (p + "new_pages", self.current_pages + self.ready_pages),
            (p + "ready_pages", self.ready_pages)])

    def ready_pages_for(self, pages):
        ready = pages * self.grow_threshold // 100
        ready = min(ready, self.grow_pages)
        return ready & ~FREE_LIST_ALIGNMENT


class HWRT:
    """pvr_hwrt_dataset: one common object, two hwrtdata objects."""

    def __init__(self, host, free_lists, width, height, samples=1, layers=1,
                 geom=None, rt=None, isp_merge=(0, 0, 0, 0, 0, 0), region_header_size=0):
        h = self.host = host
        C = "rogue_fwif_hwrtdata_common"
        tile_x = tile_y = 16
        ntx, nty = -(-width // tile_x), -(-height // tile_y)
        # simple_parameter_format_version 2
        mtx, mty = -(-ntx // 8) * 2, -(-nty // 8) * 2
        tile_max_x, tile_max_y = align(ntx, 2) - 1, align(nty, 2) - 1
        geom = geom or {}
        vals = {
            "geom_caches_need_zeroing": 0,
            "isp_merge_lower_x": isp_merge[0], "isp_merge_lower_y": isp_merge[1],
            "isp_merge_upper_x": isp_merge[2], "isp_merge_upper_y": isp_merge[3],
            "isp_merge_scale_x": isp_merge[4], "isp_merge_scale_y": isp_merge[5],
            "multi_sample_ctl": multisamplectl(samples, False),
            "flipped_multi_sample_ctl": multisamplectl(samples, True),
            "mtile_stride": mtx * mty,
            "teaa": (1 << 1 if samples >= 2 else 0) | (1 << 2 if samples >= 4 else 0),
            "screen_pixel_max": ((width - 1) & 0x7FFF) | ((height - 1) & 0x7FFF) << 16,
            "te_screen": (tile_max_x & 0x1FF) | (tile_max_y & 0x1FF) << 12,
            "te_mtile1": mtx & 0x1FF,
            "te_mtile2": mty & 0x1FF,
            "isp_mtile_size": (mtx * (2 if samples >= 4 else 1) & 0x3FF) |
                              (mty * (2 if samples >= 2 else 1) & 0x3FF) << 16,
            "tpc_stride": geom.get("tpc_stride", 0x400),
            "tpc_size": geom.get("tpc_size", 0x4000),
            "rgn_header_size": region_header_size,
        }
        self.common = h.fw_obj("hwrt_common", h.L.size(C))
        for k, v in vals.items():
            h.set(C, self.common, k, v)
        self.free_lists = free_lists
        self.data = []
        D = "rogue_fwif_hwrtdata"
        rt = rt or [{}, {}]
        for i in range(2):
            d = h.fw_obj("hwrtdata%d" % i, h.L.size(D))
            h.set(D, d, "hwrt_data_common_fw_addr", self.common)
            for j, fl in enumerate(free_lists):
                h.set(D, d, "freelists_fw_addr[%d]" % j, fl.fw)
                fl.hwrts.append(d)
            h.set(D, d, "tail_ptrs_dev_addr", geom.get("tpc_dev_addr", 0xE100000000))
            h.set(D, d, "vheap_table_dev_addr", geom.get("vheap_table_dev_addr", 0xE100100000))
            h.set(D, d, "rtc_dev_addr", geom.get("rtc_dev_addr", 0xE100200000))
            h.set(D, d, "pm_mlist_dev_addr", rt[i].get("pm_mlist_dev_addr", 0xE100300000 + i * 0x100000))
            h.set(D, d, "macrotile_array_dev_addr",
                  rt[i].get("macrotile_array_dev_addr", 0xE100500000 + i * 0x100000))
            h.set(D, d, "rgn_header_dev_addr",
                  rt[i].get("region_header_dev_addr", 0xE100700000 + i * 0x100000))
            h.set(D, d, "rta_ctl.max_rts", layers)
            self.data.append(d)
        self.cleanup_off, _ = h.L.field(D, "cleanup_state")


def multisamplectl(samples, y_flip):
    pos = {1: ([8], [8]), 2: ([12, 4], [12, 4]), 4: ([6, 14, 2, 10], [2, 6, 10, 14]),
           8: ([9, 7, 13, 5, 3, 1, 11, 15], [5, 11, 9, 3, 13, 7, 15, 1])}[samples]
    v = 0
    for i in range(8):
        x = pos[0][i] if i < len(pos[0]) else 0
        y = pos[1][i] if i < len(pos[1]) else 0
        v |= x << (i * 8)
        v |= ((16 - y) & 0xF if y_flip else y) << (i * 8 + 4)
    return v


class Job:
    def __init__(self, queue, cmd_type, payload, deps=(), hwrt=None, job_id=0):
        self.queue, self.cmd_type, self.payload = queue, cmd_type, payload
        self.deps = list(deps)        # [(ufo_fw_addr, value)]
        self.hwrt = hwrt              # hwrtdata FW address or None
        self.id = job_id
        self.seqno = None
        self.paired = None

    def done(self):
        v = self.queue.ufo_value()
        return ((v - self.seqno) & 0xFFFFFFFF) < 0x80000000

    def fence(self):
        return (self.queue.ufo, self.seqno)


class Host:
    def __init__(self, emu):
        self.emu = emu
        self.L = emu.L
        self.set, self.get, self.write, self.r32, self.w32 = (emu.set, emu.get, emu.write,
                                                             emu.r32, emu.w32)
        self.next_ctx_id = 1
        self.next_job_id = 1
        self.next_fl_id = 1
        self.free_lists = {}
        self.vms = []
        self.gpu_vms = {}          # FW memory context -> GpuVM

    def fw_obj(self, name, size):
        """pvr_fw_object_create(..., PVR_BO_FW_FLAGS_DEVICE_UNCACHED)"""
        va = self.emu.alloc(name, max(size, 4))
        self.emu.map_pages(va, align(max(size, 4), PAGE))
        self.write(va, b"\0" * align(max(size, 4), PAGE))
        return va

    # -- pvr_vm.c ----------------------------------------------------------------
    def vm_context(self, pc_paddr=None):
        pc = pc_paddr if pc_paddr is not None else 0x90000000 + len(self.vms) * 0x10000
        M = "rogue_fwif_fwmemcontext"
        va = self.fw_obj("fwmemctx%d" % len(self.vms), self.L.size(M))
        self.set(M, va, "pc_dev_paddr", pc)
        self.set(M, va, "page_cat_base_reg_set", ROGUE_FW_BIF_INVALID_PCSET)
        self.vms.append(va)
        self.gpu_vms[va] = GpuVM(self.emu.sysmem, pc)
        return va

    # -- pvr_context.c / pvr_queue.c -------------------------------------------------
    def _init_fw_context(self, ctx, q, priority=0):
        L = self.L
        F = "rogue_fwif_fwcommoncontext"
        base = q.ctx_offset

        def put(path, v):
            off, size = L.field(F, path)
            fmt = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}[size]
            struct.pack_into(fmt, ctx.data, base + off, v)
        put("ccbctl_fw_addr", q.ctrl)
        put("ccb_fw_addr", q.cccb)
        put("dm", q.dm)
        put("priority", priority)
        put("priority_seq_num", 0)
        put("max_deadline_ms", MAX_DEADLINE_MS)
        put("pid", 1234)
        put("server_common_context_id", ctx.id)
        put("fw_mem_context_fw_addr", ctx.vm)
        put("context_state_addr", q.reg_state)

    def _finish_context(self, ctx):
        ctx.fw = self.fw_obj(ctx.name, len(ctx.data))
        self.write(ctx.fw, bytes(ctx.data))
        return ctx

    def _new_ctx(self, kind, vm, sname):
        ctx = Context(self, "%sctx%d" % (kind, self.next_ctx_id), kind, vm, self.L.size(sname))
        ctx.id = self.next_ctx_id
        self.next_ctx_id += 1
        return ctx

    def compute_context(self, vm, static=None, priority=0):
        S = "rogue_fwif_fwcomputecontext"
        ctx = self._new_ctx("compute", vm, S)
        off, _ = self.L.field(S, "cdm_context")
        q = Queue(self, ctx, "compute", DM_CDM, off, self.L.size("rogue_fwif_compute_ctx_state"))
        ctx.queues["compute"] = q
        self._init_fw_context(ctx, q, priority)
        static = static if static is not None else {
            n: tag(0x100 + i, 64) for i, n in enumerate(
                ("cdmreg_cdm_context_pds0", "cdmreg_cdm_context_pds1", "cdmreg_cdm_terminate_pds",
                 "cdmreg_cdm_terminate_pds1", "cdmreg_cdm_resume_pds0",
                 "cdmreg_cdm_context_pds0_b", "cdmreg_cdm_resume_pds0_b"))}
        self._put_static(ctx, S, "static_compute_context_state.ctxswitch_regs", static)
        return self._finish_context(ctx)

    def render_context(self, vm, static=None, callstack_addr=0):
        S = "rogue_fwif_fwrendercontext"
        ctx = self._new_ctx("render", vm, S)
        goff, _ = self.L.field(S, "geom_context")
        foff, _ = self.L.field(S, "frag_context")
        gq = Queue(self, ctx, "geometry", DM_GEOM, goff, self.L.size("rogue_fwif_geom_ctx_state"))
        # xe_memory_hierarchy: num_raster_pipes * (1 + xpu_max_slaves) ISP store registers
        fq = Queue(self, ctx, "fragment", DM_FRAG, foff,
                   self.L.size("rogue_fwif_frag_ctx_state") + 4 * 1 * (1 + 3))
        if callstack_addr:
            P = "rogue_fwif_geom_ctx_state"
            self.set(P, gq.reg_state, "geom_core[0].geom_reg_vdm_call_stack_pointer_init",
                     callstack_addr)
        ctx.queues["geometry"], ctx.queues["fragment"] = gq, fq
        self._init_fw_context(ctx, gq)
        self._init_fw_context(ctx, fq)
        names = ["geom_reg_vdm_context_state_base_addr", "geom_reg_vdm_context_state_resume_addr",
                 "geom_reg_ta_context_state_base_addr"]
        for i in range(2):
            for t in ("store_task0", "store_task1", "store_task2", "store_task3", "store_task4",
                      "resume_task0", "resume_task1", "resume_task2", "resume_task3",
                      "resume_task4"):
                names.append("geom_state[%d].geom_reg_vdm_context_%s" % (i, t))
        static = static if static is not None else {
            n: tag(0x200 + i, 64) for i, n in enumerate(names)}
        self._put_static(ctx, S, "static_render_context_state.ctxswitch_regs[0]", static)
        return self._finish_context(ctx)

    def transfer_context(self, vm, priority=0):
        S = "rogue_fwif_fwtransfercontext"
        ctx = self._new_ctx("transfer", vm, S)
        off, _ = self.L.field(S, "tq_context")
        q = Queue(self, ctx, "transfer", DM_FRAG, off,
                  self.L.size("rogue_fwif_frag_ctx_state") + 4 * 1)
        ctx.queues["transfer"] = q
        self._init_fw_context(ctx, q, priority)
        return self._finish_context(ctx)

    def _put_static(self, ctx, S, prefix, values):
        for name, v in values.items():
            off, size = self.L.field(S, prefix + "." + name)
            struct.pack_into("<Q" if size == 8 else "<I", ctx.data, off, v)

    # -- pvr_free_list.c / pvr_hwrt.c ------------------------------------------------
    def free_list(self, vm, gpu_addr=0xE200000000, initial=256, max_pages=4096, grow=64,
                  threshold=13):
        fl = FreeList(self, vm, gpu_addr, initial, max_pages, grow, threshold, self.next_fl_id)
        self.free_lists[self.next_fl_id] = fl
        self.next_fl_id += 1
        return fl

    # -- firmware CCB (pvr_fwccb_process) ------------------------------------------------
    def fwccb_process(self):
        """Consume pending FWCCB commands like the kernel; answer free list
        grow requests. Returns the commands as (type, {field: value})."""
        e, C = self.emu, "rogue_fwif_fwccb_cmd"
        size = self.L.size(C)
        out = []
        while True:
            ro = self.get("rogue_fwif_ccb_ctl", e.fwccb_ctl, "read_offset")
            wo = self.get("rogue_fwif_ccb_ctl", e.fwccb_ctl, "write_offset")
            if ro == wo:
                return out
            cmd = e.fwccb + ro * size
            t = self.get(C, cmd, "cmd_type")
            wrap = self.get("rogue_fwif_ccb_ctl", e.fwccb_ctl, "wrap_mask")
            self.set("rogue_fwif_ccb_ctl", e.fwccb_ctl, "read_offset", (ro + 1) & wrap)
            info = {}
            if t == FWCCB_FREELIST_GROW:
                fid = self.get(C, cmd, "cmd_data.cmd_free_list_gs.freelist_id")
                info["freelist_id"] = fid
                if fid in self.free_lists:
                    info["kccb_slot"] = self.free_lists[fid].process_grow_req()
            elif t == FWCCB_UPDATE_STATS:
                for f in ("element_to_update", "pid_owner", "adjustment_value"):
                    info[f] = self.get(C, cmd, "cmd_data.cmd_update_stats_data." + f)
            elif t == FWCCB_FREELISTS_RECONSTRUCTION:
                # pvr_free_list_process_reconstruct_req
                p = "cmd_data.cmd_freelists_reconstruction."
                n = min(self.get(C, cmd, p + "freelist_count"), 16)
                ids = [self.get(C, cmd, p + "freelist_ids[%d]" % i) for i in range(n)]
                info["freelist_ids"] = ids
                for fid in ids:
                    if fid in self.free_lists:
                        self.free_lists[fid].reconstruct()
                q = "cmd_data.free_lists_reconstruction_data."
                info["kccb_slot"] = e.send_kccb(
                    KCCB_FREELISTS_RECONSTRUCTION_UPDATE,
                    [(q + "freelist_count", n)] +
                    [(q + "freelist_ids[%d]" % i, fid) for i, fid in enumerate(ids)])
            elif t == FWCCB_CONTEXT_RESET_NOTIFICATION:
                p = "cmd_data.cmd_context_reset_notification."
                for f in ("server_common_context_id", "reset_reason", "dm", "reset_job_ref",
                          "flags", "fault_address"):
                    info[f] = self.get(C, cmd, p + f)
            out.append((t, info))

    def hwrt(self, free_lists, width=1920, height=1080, **kw):
        return HWRT(self, free_lists, width, height, **kw)

    # -- jobs (pvr_queue_submit_job_to_cccb, pvr_cccb_send_kccb_*) -------------------
    def job(self, queue, cmd_type, payload, deps=(), hwrt=None):
        j = Job(queue, cmd_type, payload, deps, hwrt, self.next_job_id)
        self.next_job_id += 1
        queue.seqno += 1
        j.seqno = queue.seqno
        return j

    def _to_cccb(self, job):
        q = job.queue
        deps = list(job.deps)
        if job.paired is not None and job.cmd_type in (CCB_FRAG, CCB_FRAG_PR):
            deps.append(job.paired.fence())
        for i in range(0, len(deps), 64):
            chunk = deps[i:i + 64]
            q.write_cmd(CCB_FENCE_PR, b"".join(struct.pack("<II", a, v) for a, v in chunk))
        payload = bytearray(job.payload)
        if job.cmd_type == CCB_GEOM and job.paired is not None:
            off, _ = self.L.field("rogue_fwif_cmd_geom", "partial_render_geom_frag_fence")
            struct.pack_into("<II", payload, off, q.ufo, job.seqno - 1)
        job.cmd_at = q.write_cmd(job.cmd_type, bytes(payload), job.id, job.id)
        q.write_cmd(CCB_UPDATE, struct.pack("<II", q.ufo, job.seqno))

    def _kick_data(self, prefix, q, hwrt):
        f = [(prefix + "context_fw_addr", q.ctx.fw_addr(q)),
             (prefix + "client_woff_update", q.write_offset),
             (prefix + "client_wrap_mask_update", q.size - 1)]
        if hwrt:
            f += [(prefix + "num_cleanup_ctl", 1),
                  (prefix + "cleanup_ctl_fw_addr[0]", hwrt)]
        return f

    def submit(self, job):
        self._to_cccb(job)
        q = job.queue
        cleanup = job.hwrt + self.cleanup_off(job) if job.hwrt else None
        q.kicks_sent += 1
        return self.emu.send_kccb(KCCB_KICK, self._kick_data("cmd_data.cmd_kick_data.", q, cleanup))

    def submit_combined(self, geom, frag):
        geom.paired, frag.paired = frag, geom
        self._to_cccb(geom)
        self._to_cccb(frag)
        p = "cmd_data.combined_geom_frag_cmd_kick_data."
        cleanup = geom.hwrt + self.cleanup_off(geom)
        f = self._kick_data(p + "geom_cmd_kick_data.", geom.queue, cleanup)
        f += self._kick_data(p + "frag_cmd_kick_data.", frag.queue,
                             None if frag.cmd_type == CCB_FRAG_PR else cleanup)
        geom.queue.kicks_sent += 1
        frag.queue.kicks_sent += 1
        return self.emu.send_kccb(KCCB_COMBINED_KICK, f)

    def cleanup_off(self, job):
        off, _ = self.L.field("rogue_fwif_hwrtdata", "cleanup_state")
        return off

    def cleanup(self, kind, addr):
        path = {CLEANUP_FWCOMMONCONTEXT: "context_fw_addr", CLEANUP_HWRTDATA: "hwrt_data_fw_addr",
                CLEANUP_FREELIST: "freelist_fw_addr"}[kind]
        return self.emu.send_kccb(KCCB_CLEANUP, [
            ("cmd_data.cleanup_data.cleanup_type", kind),
            ("cmd_data.cleanup_data.cleanup_data." + path, addr)])


def cmd(L, sname, values):
    """Build a command struct from {dotted.path: value}."""
    buf = bytearray(L.size(sname))
    for path, v in values.items():
        off, size = L.field(sname, path)
        struct.pack_into({1: "<B", 2: "<H", 4: "<I", 8: "<Q"}[size], buf, off,
                         v & ((1 << (8 * size)) - 1))
    return bytes(buf)


# Userspace-provided fields (pvr_stream_defs.c) for this core's features
# (GPU_MULTICORE_SUPPORT, TPU_DM_GLOBAL_REGISTERS; no CDM_USER_MODE_QUEUE,
# VDM_DRAWINDIRECT, TESSELLATION, CLUSTER_GROUPING, S7_TOP, ZLS_SUBTILE).
STREAM_FIELDS = {
    "rogue_fwif_cmd_compute": [
        ("regs.tpu_border_colour_table", 64), ("regs.cdm_ctrl_stream_base", 64),
        ("regs.cdm_context_state_base_addr", 64), ("regs.cdm_resume_pds1", 32),
        ("regs.tpu_tag_cdm_ctrl", 32), ("execute_count", 32)],
    "rogue_fwif_cmd_geom": [
        ("regs.vdm_ctrl_stream_base", 64), ("regs.tpu_border_colour_table", 64),
        ("regs.ppp_ctrl", 32), ("regs.te_psg", 32), ("regs.vdm_context_resume_task0_size", 32),
        ("regs.view_idx", 32)],
    "rogue_fwif_cmd_frag": [
        ("regs.isp_scissor_base", 64), ("regs.isp_dbias_base", 64),
        ("regs.isp_oclqry_base", 64), ("regs.isp_zlsctl", 64),
        ("regs.isp_zload_store_base", 64), ("regs.isp_stencil_load_store_base", 64)] +
        [("regs.pbe_word[%d][%d]" % (i, j), 64) for i in range(8) for j in range(3)] +
        [("regs.tpu_border_colour_table", 64)] +
        [("regs.pds_bgnd[%d]" % i, 64) for i in range(3)] +
        [("regs.pds_pr_bgnd[%d]" % i, 64) for i in range(3)] +
        [("regs.usc_clear_register[%d]" % i, 32) for i in range(8)] +
        [("regs.usc_pixel_output_ctrl", 32), ("regs.isp_bgobjdepth", 32),
         ("regs.isp_bgobjvals", 32), ("regs.isp_aa", 32), ("regs.isp_ctl", 32),
         ("regs.event_pixel_pds_info", 32), ("regs.view_idx", 32),
         ("regs.event_pixel_pds_data", 32), ("regs.isp_oclqry_stride", 32),
         ("zls_stride", 32), ("sls_stride", 32), ("execute_count", 32)],
    "rogue_fwif_cmd_transfer": [
        ("regs.pds_bgnd0_base", 64), ("regs.pds_bgnd1_base", 64),
        ("regs.pds_bgnd3_sizeinfo", 64), ("regs.isp_mtile_base", 64)] +
        [("regs.pbe_wordx_mrty[%d]" % i, 64) for i in range(9)] +
        [("regs.isp_bgobjvals", 32), ("regs.usc_pixel_output_ctrl", 32),
         ("regs.usc_clear_register0", 32), ("regs.usc_clear_register1", 32),
         ("regs.usc_clear_register2", 32), ("regs.usc_clear_register3", 32),
         ("regs.isp_mtile_size", 32), ("regs.isp_render_origin", 32), ("regs.isp_ctl", 32),
         ("regs.isp_aa", 32), ("regs.event_pixel_pds_info", 32),
         ("regs.event_pixel_pds_code", 32), ("regs.event_pixel_pds_data", 32),
         ("regs.isp_render", 32), ("regs.isp_rgn", 32), ("regs.frag_screen", 32)],
}


def tagged(sname, base=0, overrides=None):
    """Every userspace field of @sname set to tag(base + i)."""
    vals = {f: tag(base + i, bits) for i, (f, bits) in enumerate(STREAM_FIELDS[sname])}
    vals.update(overrides or {})
    return vals


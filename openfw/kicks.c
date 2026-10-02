// SPDX-License-Identifier: MIT
/*
 * Per-data-master job programming: compute (CDM), transfer (TQ, on the
 * 3D pipe), geometry (TA) and fragment (3D).
 *
 * The register sequences reproduce what Imagination's firmware writes for
 * the same commands under tools/fwemu (jobs.py, spec.py). Most command
 * fields are copied to a register as they are; the rest are listed in
 * docs/firmware.md, "Job execution".
 *
 * The parameter manager (PM) has two contexts: context 0 serves the
 * geometry phase (TA) and context 1 the fragment phase (3D). Each one has
 * its own copy of the local and global free list state, its own PM page
 * catalogues (VCE, TE, ALIST) and stack pointers. Before a render the
 * state the TA left in context 0 is stored to the free lists and
 * HWRT data, then loaded into context 1.
 */
#include "fw.h"

/* struct rogue_fwif_compute_ctx_state */
#define OFF_COMPUTE_CTX_STATE_FLAGS	0
#define COMPUTE_CTX_STATE_PDS0_B	(1u << 0)

/* ISP_RENDER.MODE */
#define ISP_RENDER_MODE_MASK		3u
#define ISP_RENDER_MODE_FAST_2D		2u

#define ISP_AA_MODE_MASK		3u
#define ISP_ZLSCTL_FORCEZSTORE		(1u << 2)

/* rogue_fwif_cmd_frag.flags (pvr_rogue_fwif_client.h) */
#define FRAG_FLAGS_GET_VIS_RESULTS	(1u << 5)
#define FRAG_FLAGS_DISABLE_PIXELMERGE	(1u << 15)

#define CDM_CTX(r) OFF_FWCOMPUTECONTEXT_STATIC_COMPUTE_CONTEXT_STATE_CTXSWITCH_REGS_CDMREG_CDM_##r

static inline void copy32(u32 reg, u32 addr)
{
	reg_write(reg, FW32(addr));
}

static inline void copy64(u32 reg, u32 addr)
{
	reg_write(reg, FW32(addr));
	reg_write(reg + 4, FW32(addr + 4));
}

static u32 ctx_pid(u32 ctx)
{
	return FW32(ctx + OFF_FWCOMMONCONTEXT_PID);
}

static u32 job_ref(struct job *j)
{
	return FW32(j->cmd + OFF_CCB_CMD_HEADER_EXT_JOB_REF);
}

/* Tiles in flight per USC: ISP_CTL bits 15:12 plus one, at most 4. */
static u32 tiles_in_flight(u32 isp_ctl)
{
	u32 n = 1 + ((isp_ctl >> 12) & 0xF);

	return n > 4 ? 4 : n;
}

/* -- compute ------------------------------------------------------------------- */

void kick_cdm(struct job *j)
{
	u32 p = j->payload;
	u32 st = FW32(j->ctx + OFF_FWCOMMONCONTEXT_CONTEXT_STATE_ADDR);
	u32 flags = FW32(st + OFF_COMPUTE_CTX_STATE_FLAGS);

	copy64(0x0480, p + OFF_CMD_COMPUTE_REGS_CDM_CTRL_STREAM_BASE);
	copy64(0x17A8, p + OFF_CMD_COMPUTE_REGS_TPU_BORDER_COLOUR_TABLE);
	copy64(0x0498, p + OFF_CMD_COMPUTE_REGS_CDM_CONTEXT_STATE_BASE_ADDR);
	copy32(0x1858, p + OFF_CMD_COMPUTE_REGS_TPU_TAG_CDM_CTRL);

	/*
	 * Context switch programs, from the static state the kernel put in
	 * the compute context. The two copies of the context-store PDS
	 * program alternate between kicks.
	 */
	copy64(0x04A8, j->ctx + ((flags & COMPUTE_CTX_STATE_PDS0_B) ? CDM_CTX(CONTEXT_PDS0) : CDM_CTX(CONTEXT_PDS0_B)));
	FW32(st + OFF_COMPUTE_CTX_STATE_FLAGS) = flags ^ COMPUTE_CTX_STATE_PDS0_B;
	copy32(0x04B0, j->ctx + CDM_CTX(CONTEXT_PDS1));
	copy64(0x04B8, j->ctx + CDM_CTX(TERMINATE_PDS));
	copy32(0x04C0, j->ctx + CDM_CTX(TERMINATE_PDS1));

	TRACE(SF_OPENFW_KICK_CDM, j->ctx, j->cmd - FW32(j->ctx + OFF_FWCOMMONCONTEXT_CCB_FW_ADDR),
	      ctx_pid(j->ctx), FW32(j->ctx + OFF_FWCOMMONCONTEXT_PRIORITY), job_ref(j), job_ref(j));
	reg_write(0x0478, 1);			/* CDM kick */
}

void finish_cdm(struct job *j)
{
	(void)j;
	TRACE(SF_OPENFW_CDM_DONE);
	reg_write(CR_EVENT_CLEAR, EVENT_COMPUTE_FINISHED);
	gpu_dm_fence(DM_CDM);
}

/* -- transfer ------------------------------------------------------------------ */

/* PBE state words for the transfer's render targets */
static const u16 tq_pbe_regs[6] = { 0x1510, 0x1550, 0x1518, 0x1558, 0x1520, 0x1560 };

void kick_tq(struct job *j)
{
	u32 p = j->payload;
	u32 render = FW32(p + OFF_CMD_TRANSFER_REGS_ISP_RENDER);
	u32 isp_ctl = FW32(p + OFF_CMD_TRANSFER_REGS_ISP_CTL);

	copy32(0x0F88, p + OFF_CMD_TRANSFER_REGS_ISP_BGOBJVALS);
	copy64(0x06A0, p + OFF_CMD_TRANSFER_REGS_PDS_BGND0_BASE);
	copy64(0x06A8, p + OFF_CMD_TRANSFER_REGS_PDS_BGND1_BASE);
	copy64(0x06B8, p + OFF_CMD_TRANSFER_REGS_PDS_BGND3_SIZEINFO);
	if ((render & ISP_RENDER_MODE_MASK) == ISP_RENDER_MODE_FAST_2D) {
		/* fast 2D: region headers in the transfer heap */
		copy64(0x0F20, p + OFF_CMD_TRANSFER_REGS_ISP_MTILE_BASE);
		copy32(0x0F28, p + OFF_CMD_TRANSFER_REGS_ISP_RGN);
		reg_write64(0x0FC8, TRANSFER_FRAG_HEAP_BASE);
	}
	copy32(0x4070, p + OFF_CMD_TRANSFER_REGS_USC_PIXEL_OUTPUT_CTRL);
	copy32(0x4078, p + OFF_CMD_TRANSFER_REGS_USC_CLEAR_REGISTER0);
	copy32(0x4080, p + OFF_CMD_TRANSFER_REGS_USC_CLEAR_REGISTER1);
	copy32(0x4088, p + OFF_CMD_TRANSFER_REGS_USC_CLEAR_REGISTER2);
	copy32(0x4090, p + OFF_CMD_TRANSFER_REGS_USC_CLEAR_REGISTER3);
	copy32(0x3E40, p + OFF_CMD_TRANSFER_REGS_FRAG_SCREEN);
	copy32(0x0F18, p + OFF_CMD_TRANSFER_REGS_ISP_MTILE_SIZE);
	copy32(0x0F10, p + OFF_CMD_TRANSFER_REGS_ISP_RENDER_ORIGIN);
	reg_write(0x0FD8, tiles_in_flight(isp_ctl));
	reg_write(0x0F38, isp_ctl);
	copy32(0x0F30, p + OFF_CMD_TRANSFER_REGS_ISP_AA);
	if (FW32(p + OFF_CMD_TRANSFER_REGS_ISP_AA) & ISP_AA_MODE_MASK)
		reg_write64(0x0FA8, 0x8888888888888888ull);	/* samples at the pixel centre */
	reg_write64(0x0F48, 0);			/* no depth/stencil load/store */
	copy32(0x0628, p + OFF_CMD_TRANSFER_REGS_EVENT_PIXEL_PDS_INFO);
	copy32(0x0618, p + OFF_CMD_TRANSFER_REGS_EVENT_PIXEL_PDS_CODE);
	copy32(0x0620, p + OFF_CMD_TRANSFER_REGS_EVENT_PIXEL_PDS_DATA);
	for (u32 i = 0; i < 6; i++)
		copy64(tq_pbe_regs[i], p + OFF_CMD_TRANSFER_REGS_PBE_WORDX_MRTY + 8 * i);
	reg_write(0x0388, 0);
	reg_write(0x0F08, render);

	TRACE(SF_OPENFW_KICK_TQ, j->ctx, j->cmd - FW32(j->ctx + OFF_FWCOMMONCONTEXT_CCB_FW_ADDR), 0,
	      ctx_pid(j->ctx), FW32(j->ctx + OFF_FWCOMMONCONTEXT_PRIORITY),
	      FW32(p + OFF_CMD_TRANSFER_COMMON_FRAME_NUM), job_ref(j), job_ref(j));
	reg_write(0x0F00, 1);			/* 3D kick */
}

void finish_tq(struct job *j)
{
	(void)j;
	(void)reg_read(0x0F08);
	TRACE(SF_OPENFW_3D_DONE, 0x7FFFFFFFu, 0x7FFFFFFFu);
	reg_write(CR_EVENT_CLEAR, EVENT_PIXELBE_END_RENDER);
	gpu_dm_fence(DM_FRAG);
}

/* -- parameter manager state ----------------------------------------------------- */

/*
 * Free list registers per PM context: the list base, the stack top and
 * the allocated page counts, then a load trigger. Global free lists have
 * their own copy. The local stack top sits at a different bit position in
 * each context's 64-bit register (top_shift); the global one is a 32-bit
 * register.
 */
struct pm_fl_regs {
	u16 base, top;
	u8 top_shift;
	u16 pages, mmu_pages, load;
};

static const struct pm_fl_regs pm_fl[2][2] = {
	[0] = {	/* context 0 (TA) */
		{ 0x0210, 0x0220, 32, 0x0390, 0x0394, 0x0208 },	/* local */
		{ 0x20B8, 0x20C0, 0, 0x20D0, 0x20D4, 0x20B0 },	/* global */
	},
	[1] = {	/* context 1 (3D) */
		{ 0x0218, 0x0230, 22, 0x0398, 0x039C, 0x0200 },
		{ 0x2078, 0x2080, 0, 0x2090, 0x2094, 0x20A8 },
	},
};

/* Where each PM context reports the current free list state. */
static const struct {
	u16 top, pages, mmu_pages;
} pm_fl_status[2][2] = {
	{ { 0x0348, 0x03A0, 0x2000 },	/* TA local */
	  { 0x20C8, 0x20D8, 0x20E0 } },	/* TA global */
	{ { 0x0350, 0x03A8, 0x2008 },	/* 3D local */
	  { 0x2088, 0x2098, 0x20A0 } },	/* 3D global */
};

/* Free lists loaded in each PM context: [context][local/global] */
static u32 pm_loaded_fl[2][2];

/* HWRT data whose geometry finished and whose 3D pass has not started */
static u32 pm_pending_hwrt, pm_pending_stored;

/* HWRT data last loaded on PM context 1 (3D) */
static u32 pm_3d_hwrt;

/* TA stalled out of memory until this free list grows (oom_geom) */
static u32 oom_wait_fl, oom_wait_kind;

/* After the TA: what PM context 0 built for this render target. */
static void pm_store_rtdata(u32 h)
{
	FW32(h + OFF_HWRTDATA_PM_MLIST_STACK_POINTER) = reg_read(0x02C0);
	fw_write64(h + OFF_HWRTDATA_PM_ALIST_STACK_POINTER, reg_read64(0x0270));
	fw_write64(h + OFF_HWRTDATA_VCE_CAT_BASE0, reg_read64(CR_BIF_PM_CAT_BASE_VCE0));
	fw_write64(h + OFF_HWRTDATA_TE_CAT_BASE0, reg_read64(CR_BIF_PM_CAT_BASE_TE0));
	fw_write64(h + OFF_HWRTDATA_ALIST_CAT_BASE, reg_read64(CR_BIF_PM_CAT_BASE_ALIST0));
}

static void pm_load_freelist(u32 pmctx, u32 kind, u32 fl)
{
	const struct pm_fl_regs *r = &pm_fl[pmctx][kind];
	u32 top = FW32(fl + OFF_FREELIST_CURRENT_STACK_TOP);

	copy64(r->base, fl + OFF_FREELIST_CURRENT_DEV_ADDR);
	if (r->top_shift == 32)
		reg_write64(r->top, (u64)top << 32);
	else if (r->top_shift == 22)
		reg_write64(r->top, (u64)top << 22);
	else
		reg_write(r->top, top);
	copy32(r->pages, fl + OFF_FREELIST_ALLOCATED_PAGE_COUNT);
	copy32(r->mmu_pages, fl + OFF_FREELIST_ALLOCATED_MMU_PAGE_COUNT);
	reg_write(r->load, 1);
	poll_reg(r->load, 1, 0);
	pm_loaded_fl[pmctx][kind] = fl;
}

static void pm_store_freelist(u32 pmctx, u32 kind, u32 fl)
{
	FW32(fl + OFF_FREELIST_CURRENT_STACK_TOP) = reg_read(pm_fl_status[pmctx][kind].top);
	FW32(fl + OFF_FREELIST_ALLOCATED_PAGE_COUNT) = reg_read(pm_fl_status[pmctx][kind].pages);
	FW32(fl + OFF_FREELIST_ALLOCATED_MMU_PAGE_COUNT) =
		reg_read(pm_fl_status[pmctx][kind].mmu_pages);
}

/*
 * Before power-off: free lists from PM context 0, the render state of the
 * HWRT data last on PM context 1 (and of a render whose 3D pass has not
 * started), then the free lists from context 1 (the latest copy).
 */
void pm_save(void)
{
	for (u32 k = 0; k < 2; k++)
		if (pm_loaded_fl[0][k])
			pm_store_freelist(0, k, pm_loaded_fl[0][k]);
	if (pm_pending_hwrt && !pm_pending_stored)
		pm_store_rtdata(pm_pending_hwrt);
	if (pm_3d_hwrt) {
		u32 h = pm_3d_hwrt;

		FW32(h + OFF_HWRTDATA_PM_MLIST_STACK_POINTER) = reg_read(0x02D0);
		fw_write64(h + OFF_HWRTDATA_PM_ALIST_STACK_POINTER, reg_read64(0x0280));
		fw_write64(h + OFF_HWRTDATA_VCE_CAT_BASE0, reg_read64(CR_BIF_PM_CAT_BASE_VCE1));
		fw_write64(h + OFF_HWRTDATA_TE_CAT_BASE0, reg_read64(CR_BIF_PM_CAT_BASE_TE1));
		fw_write64(h + OFF_HWRTDATA_ALIST_CAT_BASE, reg_read64(CR_BIF_PM_CAT_BASE_ALIST1));
	}
	for (u32 k = 0; k < 2; k++)
		if (pm_loaded_fl[1][k])
			pm_store_freelist(1, k, pm_loaded_fl[1][k]);
}

/*
 * Before the kernel frees a free list: if the PM holds it, store every
 * free list the PM holds back to memory and forget them all.
 */
void pm_unload_freelists(u32 fl)
{
	int held = 0;

	for (u32 c = 0; c < 2; c++)
		for (u32 k = 0; k < 2; k++)
			held |= pm_loaded_fl[c][k] == fl;
	if (!held)
		return;
	for (u32 c = 0; c < 2; c++) {
		for (u32 k = 0; k < 2; k++) {
			if (pm_loaded_fl[c][k])
				pm_store_freelist(c, k, pm_loaded_fl[c][k]);
			pm_loaded_fl[c][k] = 0;
		}
	}
}

static u32 hwrt_freelist(u32 hwrt, u32 kind)
{
	return FW32(hwrt + OFF_HWRTDATA_FREELISTS_FW_ADDR + 4 * kind);
}

/* CONTEXT_PB_BASE: bit n set while free list n differs between the TA and 3D */
static void pm_set_pb_base(void)
{
	u32 v = 0;

	for (u32 k = 0; k < 2; k++)
		if (pm_loaded_fl[0][k] != pm_loaded_fl[1][k])
			v |= 1u << k;
	reg_write(0x02B0, v);
}

void pm_reset(void)
{
	for (u32 c = 0; c < 2; c++)
		pm_loaded_fl[c][0] = pm_loaded_fl[c][1] = 0;
	pm_pending_hwrt = pm_pending_stored = 0;
	pm_3d_hwrt = 0;
	oom_wait_fl = 0;
}

/* -- parameter memory exhaustion (PM out of memory) ---------------------------- */

/*
 * When the TA's free lists run dry the PM raises PM_OUT_OF_MEMORY and the
 * TA stalls. The growable free list (the global one when there is one)
 * keeps "ready pages" the kernel reserved at its last grow: they are handed
 * to the PM at once and the TA resumes, while a FREELIST_GROW request asks
 * the kernel for more (pvr_free_list_process_grow_req). Without ready
 * pages the TA stays stalled until the kernel's FREELIST_GROW_UPDATE.
 *
 * Not handled: a free list that cannot grow any more with no ready pages
 * left. Imagination's firmware then runs a partial render to free memory;
 * openfw leaves the TA stalled and the kernel's job timeout resets the GPU.
 */

static void pm_pause_ta_alloc(int pause)
{
	u32 v = reg_read(0x02A0);		/* PM_PAGE_MANAGEOP */

	reg_write(0x02A0, pause ? v | 1 : v & ~1u);
	poll_reg(0x02A8, 1, pause ? 1 : 0);
}

/* Load free list @fl into PM context @c with @pages more pages at @base. */
static void pm_fl_add(u32 c, u32 kind, u64 base, u32 pages)
{
	const struct pm_fl_regs *r = &pm_fl[c][kind];
	u32 top = reg_read(pm_fl_status[c][kind].top) + pages;
	u32 alloc = reg_read(pm_fl_status[c][kind].pages);
	u32 mmu = reg_read(pm_fl_status[c][kind].mmu_pages);

	reg_write64(r->base, base);
	if (r->top_shift == 32)
		reg_write64(r->top, (u64)top << 32);
	else if (r->top_shift == 22)
		reg_write64(r->top, (u64)top << 22);
	else
		reg_write(r->top, top);
	reg_write(r->pages, alloc);
	reg_write(r->mmu_pages, mmu);
	reg_write(r->load, 1);
	poll_reg(r->load, 1, 0);
}

/*
 * Add @pages pages below the current base of free list @fl, in PM
 * context 0 and, when the 3D context shares the list, in context 1.
 */
static void pm_grow_ta(u32 kind, u32 fl, u32 pages)
{
	u64 base = fw_read64(fl + OFF_FREELIST_CURRENT_DEV_ADDR) - (u64)pages * 4;
	u32 cur = FW32(fl + OFF_FREELIST_CURRENT_PAGES) + pages;
	u32 top = FW32(fl + OFF_FREELIST_CURRENT_STACK_TOP) + pages;

	pm_pause_ta_alloc(1);
	pm_fl_add(0, kind, base, pages);
	if (pm_loaded_fl[1][kind] == fl)
		pm_fl_add(1, kind, base, pages);
	pm_pause_ta_alloc(0);

	fw_write64(fl + OFF_FREELIST_CURRENT_DEV_ADDR, base);
	FW32(fl + OFF_FREELIST_CURRENT_PAGES) = cur;
	FW32(fl + OFF_FREELIST_CURRENT_STACK_TOP) = top;
	FW32(fl + OFF_FREELIST_READY_PAGES) = 0;
	TRACE(SF_OPENFW_OOM_RESUMED, (u32)(base >> 32), (u32)base, cur, top);
	reg_write(0x0328, 1);			/* resume the TA */
}

void oom_geom(struct job *j)
{
	u32 h = j->hwrt;
	u32 kind = hwrt_freelist(h, 1) ? 1 : 0;
	u32 fl = hwrt_freelist(h, kind);
	u32 ready = FW32(fl + OFF_FREELIST_READY_PAGES);
	u32 cur = FW32(fl + OFF_FREELIST_CURRENT_PAGES);

	reg_write(CR_EVENT_CLEAR, EVENT_PM_OUT_OF_MEMORY);
	TRACE(SF_OPENFW_OOM, j->ctx, h);
	if (ready) {
		pm_grow_ta(kind, fl, ready);
		cur += ready;
		gpu_dm_fence(DM_GEOM);
		gpu_slc_flush(0x2);
		gpu_slc_flush(0x6);
	} else {
		oom_wait_fl = fl;
		oom_wait_kind = kind;
		FW32(h + OFF_HWRTDATA_STATE) = RTDATA_GEOM_OUTOFMEM;
	}
	if (!FW32(fl + OFF_FREELIST_GROW_PENDING) && FW32(fl + OFF_FREELIST_GROW_PAGES) &&
	    cur < FW32(fl + OFF_FREELIST_MAX_PAGES)) {
		FW32(fl + OFF_FREELIST_GROW_PENDING) = 1;
		fwccb_send(FWCCB_FREELIST_GROW, FW32(fl + OFF_FREELIST_FREELIST_ID), 0, 0);
	}
	fwccb_send(FWCCB_UPDATE_STATS, FWCCB_STATS_NUM_OUT_OF_MEMORY, ctx_pid(j->ctx), 1);
}

/* KCCB FREELIST_GROW_UPDATE: the kernel added pages to a free list. */
void freelist_grow_update(u32 d)
{
	u32 fl = FW32(d + OFF_FREELIST_GS_DATA_FREELIST_FW_ADDR);
	u32 newp = FW32(d + OFF_FREELIST_GS_DATA_NEW_PAGES);
	u32 cur = FW32(fl + OFF_FREELIST_CURRENT_PAGES);
	u64 base = fw_read64(fl + OFF_FREELIST_CURRENT_DEV_ADDR);

	TRACE(SF_OPENFW_GROW_UPDATE, (u32)(base >> 32), (u32)base, newp,
	      FW32(d + OFF_FREELIST_GS_DATA_READY_PAGES));
	FW32(fl + OFF_FREELIST_GROW_PENDING) = 0;
	if (newp <= cur)
		return;				/* the grow failed */
	if (oom_wait_fl == fl && sched_dm_busy(DM_GEOM)) {
		/* the stalled TA gets the new pages now, but for the ready
		 * pages the kernel keeps in reserve for the next OOM */
		u32 ready = FW32(d + OFF_FREELIST_GS_DATA_READY_PAGES);
		u32 add = newp - cur > ready ? newp - cur - ready : newp - cur;

		oom_wait_fl = 0;
		pm_grow_ta(oom_wait_kind, fl, add);
		FW32(fl + OFF_FREELIST_READY_PAGES) = newp - cur - add;
		FW32(sched_running_hwrt(DM_GEOM) + OFF_HWRTDATA_STATE) = RTDATA_KICK_GEOM;
	} else {
		FW32(fl + OFF_FREELIST_READY_PAGES) = newp - cur;
	}
}

/* -- geometry ------------------------------------------------------------------ */

void kick_geom(struct job *j)
{
	u32 p = j->payload, h = j->hwrt;
	u32 c = FW32(h + OFF_HWRTDATA_HWRT_DATA_COMMON_FW_ADDR);
	u32 st = FW32(j->ctx + OFF_FWCOMMONCONTEXT_CONTEXT_STATE_ADDR);
	u32 flags = FW32(p + OFF_CMD_GEOM_FLAGS);
	int first = flags & GEOM_FLAGS_FIRSTKICK;
	u64 v;

	/* PM context 0 is about to be reused: keep what it built for a
	 * render whose 3D pass has not started yet */
	if (pm_pending_hwrt && pm_pending_hwrt != h && !pm_pending_stored) {
		pm_store_rtdata(pm_pending_hwrt);
		pm_pending_stored = 1;
	}
	reg_write64(0x03D0, 0x100000100ull);
	for (u32 k = 0; k < 2; k++) {
		u32 fl = hwrt_freelist(h, k);

		if (fl && pm_loaded_fl[0][k] != fl)
			pm_load_freelist(0, k, fl);
	}
	pm_set_pb_base();
	if (first) {
		gpu_slc_mmu_flush(BIF_CTRL_INVAL_PC);
		/* PM context 0: MLIST and page catalogues of this render
		 * target. The catalogues are left alone while another render's
		 * geometry output still waits for its 3D pass (as the
		 * reference firmware does). */
		copy64(0x02D8, h + OFF_HWRTDATA_PM_MLIST_DEV_ADDR);
		if (!pm_pending_hwrt || pm_pending_hwrt == h) {
			copy64(CR_BIF_PM_CAT_BASE_VCE0, h + OFF_HWRTDATA_VCE_CAT_BASE0);
			copy64(CR_BIF_PM_CAT_BASE_TE0, h + OFF_HWRTDATA_TE_CAT_BASE0);
			copy64(CR_BIF_PM_CAT_BASE_ALIST0, h + OFF_HWRTDATA_ALIST_CAT_BASE);
		}
	}
	/* VHEAP table: initialised by a first kick, reloaded by the next ones
	 * (finish_geom stores it after a kick that is not the last) */
	copy64(0x0248, h + OFF_HWRTDATA_VHEAP_TABLE_DEV_ADDR);
	reg_write(first ? 0x0258 : 0x0250, 1);
	poll_reg(first ? 0x0258 : 0x0250, 1, 0);
	if (first) {
		v = reg_read64(0x03D0);
		reg_write64(0x03D0, v);
		reg_write(0x0290, 1);
		reg_write(0x0198, 1);
	}

	/* tiling engine */
	copy32(0x0C88, p + OFF_CMD_GEOM_REGS_PPP_CTRL);
	copy32(0x0C98, c + OFF_HWRTDATA_COMMON_SCREEN_PIXEL_MAX);
	copy64(0x0C80, c + OFF_HWRTDATA_COMMON_MULTI_SAMPLE_CTL);
	copy32(0x0C28, p + OFF_CMD_GEOM_REGS_TE_PSG);
	copy64(0x0C38, h + OFF_HWRTDATA_RGN_HEADER_DEV_ADDR);
	copy64(0x0C40, h + OFF_HWRTDATA_TAIL_PTRS_DEV_ADDR);
	copy32(0x0C00, c + OFF_HWRTDATA_COMMON_TEAA);
	copy32(0x0C08, c + OFF_HWRTDATA_COMMON_TE_MTILE1);
	copy32(0x0C10, c + OFF_HWRTDATA_COMMON_TE_MTILE2);
	copy32(0x0C18, c + OFF_HWRTDATA_COMMON_TE_SCREEN);
	copy32(0x0C20, c + OFF_HWRTDATA_COMMON_MTILE_STRIDE);
	reg_write(0x4098, FW32(p + OFF_CMD_GEOM_REGS_VIEW_IDX) & 0xFF);
	copy64(0x17A0, p + OFF_CMD_GEOM_REGS_TPU_BORDER_COLOUR_TABLE);
	copy64(0x0CB0, h + OFF_HWRTDATA_RTC_DEV_ADDR);
	copy32(0x0C48, c + OFF_HWRTDATA_COMMON_TPC_STRIDE);
	reg_write(0x0CE0, 0);
	reg_write(0x0CB8, 1);
	/* render target array: active layers so far in this render */
	reg_write(0x0D20, first ? 0 : FW32(h + OFF_HWRTDATA_RTA_CTL_ACTIVE_RENDER_TARGETS));
	reg_write(0x40A8, 0);
	reg_write(0x40A8, 0);
	if (first) {
		reg_write(0x0C68, 1);		/* region header init */
		poll_reg(0x0C68, 1, 0);
	}

	/* vertex data master */
	copy64(0x0408, p + OFF_CMD_GEOM_REGS_VDM_CTRL_STREAM_BASE);
	copy64(0x0418, st + OFF_GEOM_CTX_STATE_GEOM_CORE0_GEOM_REG_VDM_CALL_STACK_POINTER_INIT);

	FW32(h + OFF_HWRTDATA_STATE) = (flags & GEOM_FLAGS_FIRSTKICK) ? RTDATA_KICK_GEOM_FIRST
								     : RTDATA_KICK_GEOM;
	FW32(h + OFF_HWRTDATA_GEOM_CACHES_NEED_ZEROING) = 1;
	if (flags & GEOM_FLAGS_LASTKICK)
		FW32(h + OFF_HWRTDATA_HWRT_DATA_FLAGS) |= HWRTDATA_HAS_LAST_GEOM;

	TRACE(SF_OPENFW_KICK_TA, j->ctx, j->cmd - FW32(j->ctx + OFF_FWCOMMONCONTEXT_CCB_FW_ADDR), h,
	      !!(flags & GEOM_FLAGS_FIRSTKICK), !!(flags & GEOM_FLAGS_LASTKICK), 0,
	      ctx_pid(j->ctx), FW32(j->ctx + OFF_FWCOMMONCONTEXT_PRIORITY),
	      FW32(p + OFF_CMD_GEOM_CMD_SHARED_CMN_FRAME_NUM), job_ref(j), job_ref(j));
	reg_write(0x0400, 1);			/* VDM kick */
}

void finish_geom(struct job *j)
{
	u32 h = j->hwrt;
	int last = FW32(j->payload + OFF_CMD_GEOM_FLAGS) & GEOM_FLAGS_LASTKICK;

	TRACE(SF_OPENFW_TA_DONE);
	reg_write(CR_EVENT_CLEAR, EVENT_TA_FINISHED);
	if (!last) {
		/* more geometry kicks follow: keep the VHEAP table and the
		 * render target array state for them */
		reg_write(0x0260, 1);
		poll_reg(0x0260, 1, 0);
		FW32(h + OFF_HWRTDATA_RTA_CTL_ACTIVE_RENDER_TARGETS) = reg_read(0x0D20);
	}
	/* tail pointer cache flush */
	reg_write(0x0C50, 0x40000000);
	reg_write(0x0CB8, 2);
	poll_reg(0x0CB8, 2, 0);
	poll_reg(0x0C50, 0x40000000, 0);
	gpu_dm_fence(DM_GEOM);
	pm_pending_hwrt = h;	/* its PM state is stored when the 3D takes it */
	pm_pending_stored = 0;
	FW32(h + OFF_HWRTDATA_STATE) = RTDATA_GEOM_FINISHED;
	if (last)
		FW32(h + OFF_HWRTDATA_GEOM_CACHES_NEED_ZEROING) = 0;
}

/* -- fragment ------------------------------------------------------------------ */

int frag_pr_needed(struct job *j)
{
	/* A partial render is only needed after the PM ran out of memory. */
	return j->hwrt && FW32(j->hwrt + OFF_HWRTDATA_STATE) == RTDATA_GEOM_OUTOFMEM;
}

void kick_frag(struct job *j)
{
	u32 p = j->payload, h = j->hwrt;
	u32 c = FW32(h + OFF_HWRTDATA_HWRT_DATA_COMMON_FW_ADDR);
	u32 isp_ctl = FW32(p + OFF_CMD_FRAG_REGS_ISP_CTL);
	u32 flags = FW32(p + OFF_CMD_FRAG_FLAGS);

	/*
	 * Hand the geometry output over to the 3D: the HWRT data holds the
	 * PM state the TA ended with (pm_store_rtdata); free lists the 3D
	 * context does not hold yet are stored from the TA context and
	 * loaded into context 1.
	 */
	if (pm_pending_hwrt == h) {
		if (!pm_pending_stored)
			pm_store_rtdata(h);
		pm_pending_hwrt = pm_pending_stored = 0;
	}
	reg_write64(0x03D0, 0x100010100ull);
	for (u32 k = 0; k < 2; k++) {
		u32 fl = hwrt_freelist(h, k);

		if (!fl || pm_loaded_fl[1][k] == fl)
			continue;	/* already shared by both contexts */
		if (pm_loaded_fl[0][k] == fl)
			pm_store_freelist(0, k, fl);
		pm_load_freelist(1, k, fl);
	}
	pm_set_pb_base();
	gpu_slc_mmu_flush(BIF_CTRL_INVAL_PC);
	copy64(0x02E0, h + OFF_HWRTDATA_PM_MLIST_DEV_ADDR);
	copy64(CR_BIF_PM_CAT_BASE_VCE1, h + OFF_HWRTDATA_VCE_CAT_BASE0);
	copy64(CR_BIF_PM_CAT_BASE_TE1, h + OFF_HWRTDATA_TE_CAT_BASE0);
	copy64(CR_BIF_PM_CAT_BASE_ALIST1, h + OFF_HWRTDATA_ALIST_CAT_BASE);
	reg_write64(0x03D0, 0x10100010100ull);
	copy32(0x02C8, h + OFF_HWRTDATA_PM_MLIST_STACK_POINTER);
	copy64(0x0278, h + OFF_HWRTDATA_PM_ALIST_STACK_POINTER);
	reg_write(0x0190, 1);
	poll_reg(0x0190, 1, 0);
	reg_write(0x0288, 1);
	poll_reg(0x0288, 1, 0);

	copy32(0x4070, p + OFF_CMD_FRAG_REGS_USC_PIXEL_OUTPUT_CTRL);
	for (u32 i = 0; i < 4; i++)
		copy32(0x4078 + 8 * i, p + OFF_CMD_FRAG_REGS_USC_CLEAR_REGISTER + 4 * i);
	reg_write(0x40A0, FW32(p + OFF_CMD_FRAG_REGS_VIEW_IDX) & 0xFF);
	copy64(0x0F20, h + OFF_HWRTDATA_RGN_HEADER_DEV_ADDR);
	copy32(0x3E40, c + OFF_HWRTDATA_COMMON_SCREEN_PIXEL_MAX);
	copy32(0x0F18, c + OFF_HWRTDATA_COMMON_ISP_MTILE_SIZE);
	copy64(0x0FA8, c + OFF_HWRTDATA_COMMON_MULTI_SAMPLE_CTL);
	copy64(0x0FB0, p + OFF_CMD_FRAG_REGS_ISP_SCISSOR_BASE);
	copy64(0x0FB8, p + OFF_CMD_FRAG_REGS_ISP_DBIAS_BASE);
	copy32(0x0F80, p + OFF_CMD_FRAG_REGS_ISP_BGOBJDEPTH);
	copy32(0x0F30, p + OFF_CMD_FRAG_REGS_ISP_AA);
	if (flags & FRAG_FLAGS_GET_VIS_RESULTS)
		copy64(0x0FC0, p + OFF_CMD_FRAG_REGS_ISP_OCLQRY_BASE);	/* occlusion queries */
	reg_write(0x0F28, 0);
	reg_write(0x0F08, 0);			/* ISP_RENDER: normal render */
	reg_write(0x0618, 0x80);
	copy32(0x0620, p + OFF_CMD_FRAG_REGS_EVENT_PIXEL_PDS_DATA);
	copy32(0x0628, p + OFF_CMD_FRAG_REGS_EVENT_PIXEL_PDS_INFO);
	reg_write(0x0F10, 0);
	reg_write64(0x0FC8, 0);
	for (u32 i = 0; i < 8; i++) {
		copy64(0x1510 + 8 * i, p + OFF_CMD_FRAG_REGS_PBE_WORD + 24 * i);
		copy64(0x1550 + 8 * i, p + OFF_CMD_FRAG_REGS_PBE_WORD + 24 * i + 8);
	}
	copy64(0x1790, p + OFF_CMD_FRAG_REGS_TPU_BORDER_COLOUR_TABLE);
	reg_write(0x0FD8, tiles_in_flight(isp_ctl));
	copy64(0x0F48, p + OFF_CMD_FRAG_REGS_ISP_ZLSCTL);
	reg_write(0x0F38, isp_ctl);
	copy32(0x0F88, p + OFF_CMD_FRAG_REGS_ISP_BGOBJVALS);
	copy64(0x0F50, p + OFF_CMD_FRAG_REGS_ISP_ZLOAD_STORE_BASE);
	copy64(0x0F58, p + OFF_CMD_FRAG_REGS_ISP_ZLOAD_STORE_BASE);
	copy64(0x0F60, p + OFF_CMD_FRAG_REGS_ISP_STENCIL_LOAD_STORE_BASE);
	copy64(0x0F68, p + OFF_CMD_FRAG_REGS_ISP_STENCIL_LOAD_STORE_BASE);
	reg_write(0x06D0, (flags & FRAG_FLAGS_DISABLE_PIXELMERGE) ? 0x7F : 0x1F);
	copy64(0x06A0, p + OFF_CMD_FRAG_REGS_PDS_BGND0);
	copy64(0x06A8, p + OFF_CMD_FRAG_REGS_PDS_BGND1);
	copy64(0x06B8, p + OFF_CMD_FRAG_REGS_PDS_BGND2);
	reg_write(0x0388, 1);
	reg_write(0x03D8, 0);
	reg_write(0x03E0, 1);
	reg_write(0x01C0, 0);
	reg_write(0x0298, 0);
	reg_write(0x01C8, 0);

	FW32(h + OFF_HWRTDATA_STATE) = RTDATA_KICK_FRAG;
	pm_3d_hwrt = h;
	TRACE(SF_OPENFW_KICK_3D, j->ctx, j->cmd - FW32(j->ctx + OFF_FWCOMMONCONTEXT_CCB_FW_ADDR), h,
	      j->type == CCB_FRAG_PR, 0, ctx_pid(j->ctx), FW32(j->ctx + OFF_FWCOMMONCONTEXT_PRIORITY),
	      FW32(p + OFF_CMD_FRAG_CMD_SHARED_CMN_FRAME_NUM), job_ref(j), job_ref(j));
	reg_write(0x0F00, 1);			/* 3D kick */
}

void finish_frag(struct job *j)
{
	u32 h = j->hwrt;
	int zls_wait = FW32(j->payload + OFF_CMD_FRAG_REGS_ISP_ZLSCTL) & ISP_ZLSCTL_FORCEZSTORE;

	(void)reg_read(0x0F08);
	/* a forced depth store must have reached memory */
	if (zls_wait)
		poll_reg(CR_EVENT_STATUS, EVENT_ZLS_FINISHED, EVENT_ZLS_FINISHED);
	/* the PM must have released the render's memory */
	poll_reg(CR_EVENT_STATUS, EVENT_PM_3D_MEM_FREE, EVENT_PM_3D_MEM_FREE);
	reg_write(CR_EVENT_CLEAR, EVENT_PM_3D_MEM_FREE);
	/* PC cache invalidate, except while a TA runs and another render's
	 * geometry output waits for its 3D pass (as the reference firmware) */
	if (!(sched_dm_busy(DM_GEOM) && pm_pending_hwrt))
		gpu_slc_mmu_flush(BIF_CTRL_INVAL_PC);
	TRACE(SF_OPENFW_3D_DONE, 0x7FFFFFFFu, FW32(h + OFF_HWRTDATA_STATE));
	reg_write(CR_EVENT_CLEAR, EVENT_PIXELBE_END_RENDER | (zls_wait ? EVENT_ZLS_FINISHED : 0));
	gpu_dm_fence(DM_FRAG);
	FW32(h + OFF_HWRTDATA_STATE) = RTDATA_FRAG_FINISHED;
	FW32(h + OFF_HWRTDATA_HWRT_DATA_FLAGS) = 0;
}

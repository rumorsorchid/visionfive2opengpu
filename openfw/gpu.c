// SPDX-License-Identifier: MIT
/*
 * GPU-wide hardware sequences: unit initialisation, page-catalogue
 * (memory context) management, cache maintenance and the end-of-job
 * fence. The register values come from tracing Imagination's firmware in
 * tools/fwemu (docs/firmware.md, "Job execution"); the comments say what
 * each step is for where that is known.
 */
#include "fw.h"

u32 gpu_units_on;

/* -- unit initialisation ------------------------------------------------------ */

enum { OP_W, OP_R, OP_P, OP_OR, OP_SYS64, OP_END };

struct op {
	u8 op;
	u16 reg;
	u32 val;		/* OP_W value, OP_P mask (expects 0), OP_OR bits, OP_SYS64 offset */
};

#define W(r, v)		{ OP_W, (r), (v) }
#define R(r)		{ OP_R, (r), 0 }
#define P0(r, m)	{ OP_P, (r), (m) }
#define OR(r, v)	{ OP_OR, (r), (v) }
#define SYS64(r, o)	{ OP_SYS64, (r), (o) }

/*
 * Executed before the first job after boot (and after a power-down of
 * the units): release the soft resets, configure SLC, PDS/USC execution
 * bases, pipeline and TPU defaults.
 */
static const struct op units_init_ops[] = {
	/* soft reset release, in two steps, each read back */
	W(0x0100, 0xF3FFFC1D), W(0x0104, 0xFFEFFFF8), R(0x0100),
	W(0x0100, 0x13FFFC1D), W(0x0104, 0xFFEFFE00), R(0x0100),
	W(0x0100, 0x00000000), W(0x0104, 0x00000000), R(0x0100),
	W(0x0050, 1),
	/* SLC flush/invalidate of MMU data, MMU cache invalidate */
	W(CR_SLC_CTRL_FLUSH_INVAL, SLC_FLUSH_INVAL_DM_MMU), P0(CR_SLC_STATUS0, 0x4),
	W(CR_BIF_CTRL_INVAL, 0xC), P0(CR_BIF_CTRL_INVAL, 0xC),
	R(0x3970),				/* SLC_SIZE_IN_KB */
	OR(0x3828, 0x01000000), OR(0x382C, 0x00002000),	/* SLC_CTRL_BYPASS */
	W(0x6000, 1), W(0x6000, 0),
	/* PDS / USC code execution bases from the kernel (sysinit) */
	SYS64(0x0610, OFF_SYSINIT_PDS_EXEC_BASE),
	SYS64(0x4028, OFF_SYSINIT_USC_EXEC_BASE),
	SYS64(0x4010, OFF_SYSINIT_USC_EXEC_BASE),
	SYS64(0x4008, OFF_SYSINIT_USC_EXEC_BASE),
	W(0x18C0, 0x007FC000), W(0x18C8, 0x007FC000), W(0x18D0, 0x007FC000),
	W(0x18E0, 0x007FC000),
	W(0x0600, 0x605F5F00), W(0x0604, 0x00800000),
	W(0x4050, 0x00002000), W(0x06D8, 0), W(0x0720, 0x13), W(0x0640, 9), W(0x0698, 0xB),
	W(0x0630, 1), W(0x0658, 0x15), W(0x06C0, 0x17), W(0x0648, 1), W(0x0608, 1),
	W(0x4068, 1), P0(0x4068, 1),
	W(0x0660, 1), W(0x0670, 1),
	W(0x0CD8, 1), P0(0x0CD8, 1),
	W(0x18E8, 0xF), W(0x18F0, 0xF), W(0x18F8, 0xF), W(0x1900, 0xF),
	W(0x1498, 1), W(0x0700, 1), W(0x0358, 5), W(0x20F8, 1), W(0x2010, 0), W(0x2100, 0),
	W(0x13F8, 7),
	W(0x17B8, 8), W(0x1850, 8), W(0x1858, 8),
	OR(0x17B8, 2), OR(0x1850, 2), OR(0x1858, 2),
	W(0x17C8, 4), W(0x17D0, 4), W(0x1800, 0x20), W(0x1810, 0x20), W(0x1780, 0x21),
	W(0x0C90, 0x3089705F), W(0x0C94, 0x3089705F),
	W(0x1500, 0x80),
	W(0x1710, 0x90000000), W(0x1714, 0),
	W(0x15A0, 0), W(0x15A4, 0), W(0x15A8, 0), W(0x15AC, 0), W(0x15B0, 0), W(0x15B4, 0),
	W(0x15B8, 0), W(0x15BC, 0), W(0x15C0, 0), W(0x15C4, 0), W(0x15C8, 0), W(0x15CC, 0),
	W(0x15D0, 0), W(0x15D4, 0), W(0x15D8, 0), W(0x15DC, 0),
	W(0x0C78, 0x88), W(0x0708, 1), W(0x0FA0, 0x88), W(0x06B0, 0), W(0x0470, 0),
	W(0x0240, 0), W(0x0244, 0x80),
	W(0x1788, 0x20), W(0x178C, 0x20), W(0x1870, 0x20), W(0x1874, 0x20), W(0x1878, 0x20),
	W(0x187C, 0x20),
	W(0xF338, 0), W(0xF318, 0),
	W(0x6330, 1), W(0x6300, 1), W(0x6300, 0), R(0x6318),
	{ OP_END, 0, 0 },
};

static void run_ops(const struct op *o)
{
	for (; o->op != OP_END; o++) {
		switch (o->op) {
		case OP_W:
			reg_write(o->reg, o->val);
			break;
		case OP_R:
			(void)reg_read(o->reg);
			break;
		case OP_P:
			poll_reg(o->reg, o->val, 0);
			break;
		case OP_OR:
			reg_write(o->reg, reg_read(o->reg) | o->val);
			break;
		case OP_SYS64:
			reg_write64(o->reg, fw_read64(FW_SYSINIT + o->val));
			break;
		}
	}
}

/*
 * Leave the power-off-pending state: re-enable the GPIO request event to
 * the MTS (cleared while the GPU is idle or powering down).
 */
void gpu_cancel_power_off(void)
{
	reg_write(CR_MTS_DM0_INTERRUPT_ENABLE,
		  reg_read(CR_MTS_DM0_INTERRUPT_ENABLE) | EVENT_GPIO_REQ);
}

void gpu_units_init(void)
{
	run_ops(units_init_ops);
	gpu_units_on = 1;
	gpu_cancel_power_off();
}

/* SLC flush + invalidate of MMU data, BIF invalidate, then 0x1348. */
void gpu_slc_mmu_flush(u32 bif_flags)
{
	reg_write(CR_SLC_CTRL_FLUSH_INVAL, SLC_FLUSH_INVAL_DM_MMU);
	poll_reg(CR_SLC_STATUS0, 0x4, 0);
	reg_write(CR_BIF_CTRL_INVAL, bif_flags);
	poll_reg(CR_BIF_CTRL_INVAL, bif_flags, 0);
	reg_write(0x1348, 1);
	poll_reg(0x1348, 1, 0);
}

/* The same without the 0x1348 step (power-off) */
void gpu_slc_mmu_flush_nofence(u32 bif_flags)
{
	reg_write(CR_SLC_CTRL_FLUSH_INVAL, SLC_FLUSH_INVAL_DM_MMU);
	poll_reg(CR_SLC_STATUS0, 0x4, 0);
	reg_write(CR_BIF_CTRL_INVAL, bif_flags);
	poll_reg(CR_BIF_CTRL_INVAL, bif_flags, 0);
}

void gpu_slc_flush(u32 bits)
{
	reg_write(CR_SLC_CTRL_FLUSH_INVAL, bits);
	poll_reg(CR_SLC_STATUS0, 0x4, 0);
}

/* -- end-of-job fence ---------------------------------------------------------- */

/*
 * After a data master signals completion: drain its outstanding memory
 * traffic (MCU fence to a fixed address, then the DM's SLC flush) so that
 * everything the job wrote is visible before its fences are signalled.
 */
static const struct {
	u8 busy_bit;		/* 0x688 */
	u8 r668;
	u8 mcu_dm;		/* MCU_FENCE.DM */
	u8 fence_kick;		/* 0x1720 */
	u8 slc_flush;		/* SLC_CTRL_FLUSH_INVAL DM bits */
	u32 r1608;
} fence_cfg[DM_COUNT] = {
	[DM_GEOM] = { 0x1, 0x5, 0, 0x27, 0x2, 0x10001 },
	[DM_FRAG] = { 0x2, 0x3, 1, 0x2B, 0x6, 0x38001 },
	[DM_CDM]  = { 0x4, 0x9, 2, 0x33, 0x8, 0x10001 },
};

void gpu_dm_fence(u32 dm)
{
	reg_write(0x4000, 1);
	reg_write(0x0688, fence_cfg[dm].busy_bit);
	poll_reg(0x0688, fence_cfg[dm].busy_bit, 0);
	reg_write(0x0668, fence_cfg[dm].r668);
	poll_reg(0x0668, 1, 0);
	reg_write(0x1608, fence_cfg[dm].r1608);
	poll_reg(0x1608, 1, 0);
	if (dm == DM_FRAG)
		reg_write(0x1480, 1);
	reg_write(0x1488, 1);
	/* MCU_FENCE: DM in bits 42:40, fence address 0x80_0000_0000 */
	reg_write64(0x1740, (u64)(0x80 | fence_cfg[dm].mcu_dm << 8) << 32);
	reg_write(0x1720, fence_cfg[dm].fence_kick);
	poll_reg(0x1728, 1, 0);
	reg_write(CR_SLC_CTRL_FLUSH_INVAL, fence_cfg[dm].slc_flush);
	poll_reg(CR_SLC_STATUS0, 0x4, 0);
}

/* -- memory contexts ------------------------------------------------------------- */

/*
 * Page catalogue register sets BIF_CAT_BASE1..7 (set n uses register
 * n + 1). A memory context keeps its set in fwmemcontext.page_cat_base_reg_set
 * while the register still holds its page catalogue, so a context that
 * runs again needs no MMU invalidation. BIF_CAT_BASE_INDEX selects the set
 * for each data master.
 */
#define PC_SETS 7
static struct {
	u64 pc;
	u32 refs;
	u32 used;
} pcset[PC_SETS];
static u32 pcset_next;

static const u8 cat_index_shift[DM_COUNT] = {
	[DM_GEOM] = 0, [DM_FRAG] = 8, [DM_CDM] = 16,
};

static u32 multicore_ctrl_reg(u32 dm)
{
	return dm == DM_CDM ? 0xF330 : dm == DM_FRAG ? 0xF310 : 0;
}

u32 memctx_activate(u32 memctx, u32 dm)
{
	u64 pc = fw_read64(memctx + OFF_FWMEMCONTEXT_PC_DEV_PADDR);
	u32 set = FW32(memctx + OFF_FWMEMCONTEXT_PAGE_CAT_BASE_REG_SET);
	u32 mc = multicore_ctrl_reg(dm);
	/* still loaded if the set the context last had holds its catalogue
	 * (the set number lives in the FW memory context, which survives a
	 * firmware restart; a powered-down GPU reads back 0) */
	int loaded = set < PC_SETS && (!pcset[set].used || pcset[set].pc == pc) &&
		     reg_read64(CR_BIF_CAT_BASE0 + 8 * (set + 1)) == pc;
	u64 idx;
	u32 lo;

	if (mc) {
		reg_write(mc, 1);
		if (dm == DM_FRAG && !loaded)
			(void)reg_read(mc);
	}

	if (loaded) {
		pcset[set].pc = pc;
		pcset[set].used = 1;
	} else {
		u32 i, n;

		/* a free set, preferring one never used, then round-robin */
		for (i = 0, n = PC_SETS; i < PC_SETS; i++) {
			if (!pcset[i].used) {
				n = i;
				break;
			}
		}
		for (i = 0; n == PC_SETS && i < PC_SETS; i++) {
			u32 c = (pcset_next + i) % PC_SETS;

			if (!pcset[c].refs)
				n = c;
		}
		if (n == PC_SETS)
			return ROGUE_FW_BIF_INVALID_PCSET;	/* all busy: caller retries */
		set = n;
		pcset_next = (set + 1) % PC_SETS;
		gpu_slc_mmu_flush(0xC);
		reg_write64(CR_BIF_CAT_BASE0 + 8 * (set + 1), pc);
		pcset[set].pc = pc;
		pcset[set].used = 1;
		FW32(memctx + OFF_FWMEMCONTEXT_PAGE_CAT_BASE_REG_SET) = set;
	}
	pcset[set].refs++;
	/* the set indices all live in the low word */
	idx = reg_read64(CR_BIF_CAT_BASE_INDEX);
	lo = (u32)idx & ~(7u << cat_index_shift[dm]);
	lo |= (set + 1) << cat_index_shift[dm];
	reg_write64(CR_BIF_CAT_BASE_INDEX, (idx & ~0xFFFFFFFFull) | lo);
	return set;
}

void memctx_deactivate(u32 memctx, u32 dm)
{
	u32 set = FW32(memctx + OFF_FWMEMCONTEXT_PAGE_CAT_BASE_REG_SET);

	(void)dm;
	if (set < PC_SETS && pcset[set].refs)
		pcset[set].refs--;
}

/* Forget page-catalogue sets (after a GPU reset or power-off). */
void memctx_reset(void)
{
	for (u32 i = 0; i < PC_SETS; i++)
		pcset[i].used = pcset[i].refs = 0;
	pcset_next = 0;
}

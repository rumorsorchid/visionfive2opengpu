/* SPDX-License-Identifier: MIT */
/*
 * openfw internal interfaces.
 */
#ifndef OPENFW_FW_H
#define OPENFW_FW_H

#include "fwif.h"
#include "mips.h"
#include "regs.h"

typedef unsigned int u32;
typedef int s32;
typedef unsigned short u16;
typedef unsigned char u8;
typedef unsigned long long u64;

#define FW32(addr) (*(volatile u32 *)(addr))
#define FW64(addr) (*(volatile u64 *)(addr))

static inline __attribute__((always_inline)) u64 fw_read64(u32 addr)
{
	return (u64)FW32(addr + 4) << 32 | FW32(addr);
}

static inline __attribute__((always_inline)) void fw_write64(u32 addr, u64 v)
{
	FW32(addr) = (u32)v;
	FW32(addr + 4) = (u32)(v >> 32);
}

static inline __attribute__((always_inline)) u64 reg_read64(u32 off)
{
	u32 lo = reg_read(off);

	return (u64)reg_read(off + 4) << 32 | lo;
}

/*
 * Code that rewrites TLB entries runs from the exceptions page (kseg0,
 * unmapped), like the reference firmware's: a TLB refill between its
 * EntryHi/EntryLo/Index writes and the tlbwi would clobber them.
 */
#define KSEG0_TEXT __attribute__((section(".exc.text"), noinline))

/* enum rogue_fwif_kccb_cmd_type (pvr_rogue_fwif.h) */
#define CMD_MAGIC		0x2ABC0000u
#define KCCB_KICK		(101u | CMD_MAGIC)
#define KCCB_MMUCACHE		(102u | CMD_MAGIC)
#define KCCB_SLCFLUSHINVAL	(105u | CMD_MAGIC)
#define KCCB_CLEANUP		(106u | CMD_MAGIC)
#define KCCB_POW		(107u | CMD_MAGIC)
#define KCCB_FREELIST_GROW_UPDATE (110u | CMD_MAGIC)
#define KCCB_FREELISTS_RECONSTRUCTION_UPDATE (112u | CMD_MAGIC)
#define KCCB_HEALTH_CHECK	(115u | CMD_MAGIC)
#define KCCB_COMBINED_KICK	(117u | CMD_MAGIC)
#define KCCB_LOGTYPE_UPDATE	(206u | CMD_MAGIC)

/* enum rogue_fwif_fwccb_cmd_type */
#define FWCCB_FREELIST_GROW	(103u | CMD_MAGIC)
#define FWCCB_UPDATE_STATS	(107u | CMD_MAGIC)
#define FWCCB_STATS_NUM_OUT_OF_MEMORY 2u

#define KCCB_RTN_CMD_EXECUTED	(1u << 0)
#define KCCB_RTN_CLEANUP_BUSY	(1u << 1)

/* client CCB command types (ROGUE_FWIF_CCB_CMD_TYPE_*) */
#define CCB_TASK		0x8000u
#define CCB_GEOM		(201u | CMD_MAGIC | CCB_TASK)
#define CCB_TQ_3D		(202u | CMD_MAGIC | CCB_TASK)
#define CCB_FRAG		(203u | CMD_MAGIC | CCB_TASK)
#define CCB_FRAG_PR		(204u | CMD_MAGIC | CCB_TASK)
#define CCB_CDM			(205u | CMD_MAGIC | CCB_TASK)
#define CCB_NULL		(210u | CMD_MAGIC | CCB_TASK)
#define CCB_FENCE		(212u | CMD_MAGIC)
#define CCB_UPDATE		(213u | CMD_MAGIC)
#define CCB_FENCE_PR		(215u | CMD_MAGIC)
#define CCB_UNFENCED_UPDATE	(218u | CMD_MAGIC)
#define CCB_PADDING		(221u | CMD_MAGIC)

/* enum rogue_fwif_cleanup_type */
#define CLEANUP_FWCOMMONCONTEXT	0u
#define CLEANUP_HWRTDATA	1u
#define CLEANUP_FREELIST	2u

/* data masters (PVR_FWIF_DM_*) */
#define DM_GP			0u
#define DM_GEOM			2u
#define DM_FRAG			3u
#define DM_CDM			4u
#define DM_COUNT		5u

/* enum rogue_fwif_pow_state */
#define POW_OFF			0u
#define POW_ON			1u
#define POW_FORCED_IDLE		2u
#define POW_IDLE		3u

/* enum rogue_fwif_rtdata_state */
#define RTDATA_NONE		0u
#define RTDATA_KICK_GEOM	1u
#define RTDATA_KICK_GEOM_FIRST	2u
#define RTDATA_GEOM_FINISHED	3u
#define RTDATA_KICK_FRAG	4u
#define RTDATA_FRAG_FINISHED	5u
#define RTDATA_GEOM_OUTOFMEM	7u

#define HWRTDATA_HAS_LAST_GEOM	(1u << 2)

#define GEOM_FLAGS_FIRSTKICK	(1u << 0)
#define GEOM_FLAGS_LASTKICK	(1u << 1)

#define ROGUE_FW_BIF_INVALID_PCSET 0xFFFFFFFFu
#define UFO_ADDR_IS_SYNC_CHECKPOINT 1u
#define SYNC_CHECKPOINT_SIGNALED 0x519u

/* GPU virtual heaps fixed by the kernel (pvr_rogue_heap_config.h) */
#define TRANSFER_FRAG_HEAP_BASE	0xE400000000ull

/* -- main.c ----------------------------------------------------------------- */
struct fw_globals {
	u32 kccb_ctl, kccb, kccb_rtn;
	u32 fwccb_ctl, fwccb;
	u32 osdata, sysdata, power_sync;
	u32 tracebuf_ctl;
	u32 dusts;
	u32 halt;
	u32 fault_va, fault_count;
};
extern struct fw_globals g;

void trace_n(u32 id, u32 n, const u32 *args, int force);
#define TRACE(id, ...) do { \
	const u32 __a[] = { 0, ##__VA_ARGS__ }; \
	trace_n(id, sizeof(__a) / sizeof(u32) - 1, __a + 1, 0); \
} while (0)
#define TRACE_FORCE(id, ...) do { \
	const u32 __a[] = { 0, ##__VA_ARGS__ }; \
	trace_n(id, sizeof(__a) / sizeof(u32) - 1, __a + 1, 1); \
} while (0)

int poll_reg(u32 off, u32 mask, u32 value);
void host_irq(void);
void set_pow_state(u32 s);
u32 pow_state(void);
void fw_tlb_flush(void);
void mts_schedule(u32 v);
void fwccb_send(u32 type, u32 a0, u32 a1, u32 a2);

/* -- gpu.c ------------------------------------------------------------------ */
extern u32 gpu_units_on;
void gpu_units_init(void);
void gpu_cancel_power_off(void);
void gpu_slc_mmu_flush(u32 bif_flags);
void gpu_slc_flush(u32 bits);
void gpu_dm_fence(u32 dm);
u32 memctx_activate(u32 memctx, u32 dm);
void memctx_deactivate(u32 memctx, u32 dm);
void memctx_reset(void);

/* -- sched.c ---------------------------------------------------------------- */
void sched_kick(u32 kick);
void sched_run(void);
void sched_irq(void);
u32 sched_cleanup(u32 type, u32 addr);
int sched_idle(void);
int sched_dm_busy(u32 dm);
u32 sched_running_hwrt(u32 dm);
void sched_reset(void);

struct job {
	u32 ctx;		/* FW common context */
	u32 cmd;		/* client CCB address of the command header */
	u32 type;
	u32 payload;		/* address of the command payload */
	u32 hwrt;		/* hwrtdata (geometry/fragment) */
	u32 memctx;
	u32 pcset;
	u32 end;		/* client CCB offset after the command */
};

/* -- kicks.c ---------------------------------------------------------------- */
void kick_cdm(struct job *j);
void kick_tq(struct job *j);
void kick_geom(struct job *j);
void kick_frag(struct job *j);
void finish_cdm(struct job *j);
void finish_tq(struct job *j);
void finish_geom(struct job *j);
void finish_frag(struct job *j);
int frag_pr_needed(struct job *j);
void pm_reset(void);
void pm_unload_freelists(u32 fl);
void oom_geom(struct job *j);
void freelist_grow_update(u32 data);

#endif

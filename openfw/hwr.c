// SPDX-License-Identifier: MIT
/*
 * Hardware recovery (HWR): lockup detection and GPU reset.
 *
 * The upstream kernel leaves GPU lockups to the firmware: its job timeout
 * only re-arms, and its watchdog only checks that kernel CCB commands are
 * still executed. A job that never finishes is found here, from the
 * periodic timer, the way Imagination's firmware finds it (learnt by
 * running it in tools/fwemu with work that never completes, signature
 * registers held or changing):
 *
 *  - Every 31250 GPU timer ticks each busy data master is checked for
 *    progress: a hash of its signature registers (the units' checksums of
 *    the data they process) against the previous check; once that hash
 *    repeats, register by register against a snapshot; then the USC slots
 *    the data master holds, by count and per-slot state.
 *  - A check without progress counts down: compute gets 15 checks after
 *    its last progress (16 after the kick), geometry and fragment 3 (4).
 *    At zero the data master has timed out; it gets one more period while
 *    another data master is still running fine (it may be waiting for
 *    that one), otherwise it has locked up. A job running longer than its
 *    context's deadline (max_deadline_ms, 30 s from the kernel) overruns.
 *  - On a lockup every data master with work is recorded in the HWR info
 *    buffer and reported to the kernel (CONTEXT_RESET_NOTIFICATION; the
 *    reason is "guilty" when one data master was busy, "innocent" when
 *    several were). Once all busy data masters have timed out the GPU is
 *    reset and initialised again and their jobs are skipped: the commands
 *    after them signal their fences, so waiters see the jobs finish.
 *  - A reset geometry or fragment job leaves its parameter buffer in an
 *    unknown state: the kernel is asked to rebuild the free lists
 *    (FREELISTS_RECONSTRUCTION, answered by FREELISTS_RECONSTRUCTION_UPDATE,
 *    which also marks the render targets RTDATA_STATE_HWR). Geometry and
 *    fragment work waits for the answer; fragment jobs for those render
 *    targets are then discarded (sched.c).
 */
#include "fw.h"

#define HWR_CHECK_PERIOD	31250u		/* GPU timer ticks */
#define HWR_CHECKS_CDM		15u
#define HWR_CHECKS		3u
#define HWR_CHECKS_INITIAL_CRC	0x7Du		/* the reference firmware's reset value */

#define DM_STATE_GUILTY_LOCKUP		(1u << 5)
#define DM_STATE_INNOCENT_LOCKUP	(1u << 6)
#define DM_STATE_GUILTY_OVERRUNING	(1u << 7)
#define DM_STATE_INNOCENT_OVERRUNING	(1u << 8)

#define RESET_REASON_GUILTY_LOCKUP	1u
#define RESET_REASON_INNOCENT_LOCKUP	2u
#define RESET_REASON_GUILTY_OVERRUNING	3u
#define RESET_REASON_INNOCENT_OVERRUNING 4u

#define HWRTYPE_OVERRUN		1u
#define HWRTYPE_BIF0FAULT	3u

#define USC_SLOT_OWNER		0x4178u		/* 4 x 64 bit: owner nibble per slot */
#define USC_SLOT_STATE		0x41D8u		/* 64 bit per slot */
#define USC_SLOTS		64u

/*
 * Signature registers per data master, read in this order; idx: bank
 * select written to 0 before the read (USC_INDIRECT and two TPU/ISP
 * banks). From the reference firmware's tables for this core.
 */
struct sig {
	u16 reg, idx;
};

static const struct sig sig_geom[] = {
	{ 0x5000, 0 }, { 0x5020, 0 }, { 0x5028, 0 }, { 0x5030, 0 }, { 0x5060, 0 },
	{ 0x40E0, 0 }, { 0x4600, 0x8000 },
};
static const struct sig sig_frag[] = {
	{ 0x5038, 0x8238 }, { 0x5040, 0x8238 }, { 0x5048, 0x8238 }, { 0x5050, 0x8238 },
	{ 0x5068, 0x8238 }, { 0x5058, 0x83E0 }, { 0x40D8, 0 }, { 0x4600, 0x8000 },
};
static const struct sig sig_cdm[] = {
	{ 0x40F8, 0 }, { 0x4600, 0x8000 },
};

#define MAX_SIG 8

static const struct {
	const struct sig *t;
	u8 n;
	u8 usc_owner;		/* USC slot owner ID */
	u32 events;		/* its events, cleared after the reset */
} dm_cfg[DM_COUNT] = {
	[DM_GEOM] = { sig_geom, 7, 0,
		      EVENT_TA_FINISHED | EVENT_TA_TERMINATE | EVENT_PM_OUT_OF_MEMORY },
	[DM_FRAG] = { sig_frag, 8, 1, EVENT_PIXELBE_END_RENDER | EVENT_ISP_END_MACROTILE |
				      EVENT_PM_3D_MEM_FREE | EVENT_ZLS_FINISHED },
	[DM_CDM]  = { sig_cdm, 2, 2, EVENT_COMPUTE_FINISHED },
};

static const u8 hwr_dms[] = { DM_GEOM, DM_FRAG, DM_CDM };

static struct {
	u32 busy;		/* a job is being watched */
	u32 kick_time;		/* low 32 bits of the GPU timer */
	u32 next_check;
	u32 deadline;		/* ticks */
	u32 checks;		/* checks to go; 0: timed out */
	u32 crc;
	u32 full;		/* comparing register by register */
	u64 snap[MAX_SIG];
	u32 usc_slots, usc_hash;
	u32 state;		/* DM_STATE_* once a reset is decided */
} hd[DM_COUNT];

static u32 hwr_pending;		/* lockup found, reset not done yet */
static u32 hwr_overrun;
static u32 hwr_now;		/* reset without waiting for the other data masters */
static struct {
	u32 valid;
	u32 mmu_status;
	u64 req_status, pc;
} fault;
static u32 ticks_per_ms;
u32 hwr_fl_hold;		/* free list reconstruction pending */

static u32 timer32(void)
{
	return reg_read(CR_TIMER);
}

void hwr_init(void)
{
	u32 rt = FW32(FW_SYSINIT + OFF_SYSINIT_RUNTIME_CFG_FW_ADDR);
	u32 hz = rt ? FW32(rt + OFF_RUNTIME_CFG_CORE_CLOCK_SPEED) : 0;

	if (!hz)
		hz = FW32(FW_SYSINIT + OFF_SYSINIT_INITIAL_CORE_CLOCK_SPEED);
	/* the GPU timer counts every 256 core clock cycles */
	ticks_per_ms = hz / 256000u;
	if (!ticks_per_ms)
		ticks_per_ms = 1;
	for (u32 i = 0; i < sizeof(hwr_dms); i++)
		hd[hwr_dms[i]].crc = HWR_CHECKS_INITIAL_CRC;
}

/* A job starts on @dm (sched.c). */
void hwr_kick(u32 dm, u32 ctx)
{
	u32 now = timer32();

	if (dm >= DM_COUNT || !dm_cfg[dm].t)
		return;
	hd[dm].busy = 1;
	hd[dm].kick_time = now;
	hd[dm].next_check = now + HWR_CHECK_PERIOD;
	hd[dm].deadline = FW32(ctx + OFF_FWCOMMONCONTEXT_MAX_DEADLINE_MS) * ticks_per_ms;
	if (!hd[dm].deadline)
		hd[dm].deadline = 0xFFFFFFFFu;	/* no deadline set: none */
	hd[dm].checks = (dm == DM_CDM ? HWR_CHECKS_CDM : HWR_CHECKS) + 1;
	hd[dm].full = 0;
	hd[dm].usc_slots = hd[dm].usc_hash = 0;
	hd[dm].state = 0;
}

/* The job on @dm finished. */
void hwr_done(u32 dm)
{
	if (dm < DM_COUNT)
		hd[dm].busy = 0;
}

/* -- progress ------------------------------------------------------------------ */

static u32 hash64(u32 h, u64 v)
{
	return (((h << 5) ^ (u32)v) << 5) ^ (u32)(v >> 32);
}

static int sig_progress(u32 dm)
{
	u64 v[MAX_SIG];
	u32 crc = 0, n = dm_cfg[dm].n;
	int progress = 0;

	for (u32 i = 0; i < n; i++) {
		const struct sig *s = &dm_cfg[dm].t[i];

		if (s->idx)
			reg_write(s->idx, 0);
		v[i] = reg_read64(s->reg);
		crc = hash64(crc, v[i]);
	}
	if (!hd[dm].full) {
		if (crc != hd[dm].crc) {
			hd[dm].crc = crc;
			return 1;
		}
		/* unchanged: from now on compare register by register */
		hd[dm].full = 1;
		for (u32 i = 0; i < n; i++)
			hd[dm].snap[i] = v[i];
		return 0;
	}
	for (u32 i = 0; i < n; i++) {
		if (v[i] != hd[dm].snap[i])
			progress = 1;
		hd[dm].snap[i] = v[i];
	}
	if (progress)
		hd[dm].full = 0;	/* the hash keeps its old value */
	return progress;
}

static int usc_progress(u32 dm)
{
	u32 owner = dm_cfg[dm].usc_owner, n = 0, h = 0;

	for (u32 r = 0; r < USC_SLOTS / 16; r++) {
		u64 o = reg_read64(USC_SLOT_OWNER + 8 * r);

		for (u32 k = 0; k < 16; k++, o >>= 4) {
			u32 slot = 16 * r + k;

			if ((o & 0xF) != owner)
				continue;
			u64 v = reg_read64(USC_SLOT_STATE + 8 * slot);

			n++;
			/* every slot's state must count: rotate, do not shift */
			h = ((h << 7) | (h >> 25)) ^ (u32)v ^ ((u32)(v >> 32) << 13) ^ slot;
		}
	}
	if (!n)
		return 0;		/* nothing of this data master in the USC */
	if (n != hd[dm].usc_slots || h != hd[dm].usc_hash) {
		hd[dm].usc_slots = n;
		hd[dm].usc_hash = h;
		return 1;
	}
	return 0;
}

/* Another busy data master with at least 2 checks to go, within its deadline. */
static int other_dm_ok(u32 dm, u32 now)
{
	for (u32 i = 0; i < sizeof(hwr_dms); i++) {
		u32 d = hwr_dms[i];

		if (d != dm && hd[d].busy && hd[d].checks >= 2 &&
		    now - hd[d].kick_time < hd[d].deadline)
			return 1;
	}
	return 0;
}

/* -- reporting ------------------------------------------------------------------ */

static u32 hwr_counter(void)
{
	return g.hwrinfobuf ? FW32(g.hwrinfobuf + OFF_HWRINFOBUF_HWR_COUNTER) : 0;
}

static void count_dm(u32 off, u32 dm)
{
	if (g.hwrinfobuf)
		FW32(g.hwrinfobuf + off + 4 * dm) += 1;
}

static u32 hwrinfo_entry[DM_COUNT];	/* HWR info buffer entry of the lockup */
static u32 hwrinfo_fl[2];		/* entries waiting for free list reconstruction */

static void hwr_record(u32 dm, struct job *j)
{
	u32 b = g.hwrinfobuf, wi, e;

	if (!b)
		return;
	wi = FW32(b + OFF_HWRINFOBUF_WRITE_INDEX);
	e = b + OFF_HWRINFOBUF_HWR_INFO + (wi & 15) * SIZEOF_HWRINFO;
	hwrinfo_entry[dm] = e;
	for (u32 i = 0; i < SIZEOF_HWRINFO; i += 4)
		FW32(e + i) = 0;
	fw_write64(e + OFF_HWRINFO_CR_TIMER, timer_read());
	FW32(e + OFF_HWRINFO_PID) = FW32(j->ctx + OFF_FWCOMMONCONTEXT_PID);
	FW32(e + OFF_HWRINFO_ACTIVE_HWRT_DATA) = j->hwrt;
	FW32(e + OFF_HWRINFO_HWR_NUMBER) = hwr_counter() + 1;
	FW32(e + OFF_HWRINFO_EVENT_STATUS) = reg_read(CR_EVENT_STATUS);
	FW32(e + OFF_HWRINFO_HWR_RECOVERY_FLAGS) = hd[dm].state;
	if (hwr_overrun)
		FW32(e + OFF_HWRINFO_HWR_TYPE) = HWRTYPE_OVERRUN;
	if (fault.valid) {
		FW32(e + OFF_HWRINFO_HWR_TYPE) = HWRTYPE_BIF0FAULT;
		fw_write64(e + OFF_HWRINFO_HWR_DATA_BIF_INFO_BIF_REQ_STATUS, fault.req_status);
		fw_write64(e + OFF_HWRINFO_HWR_DATA_BIF_INFO_BIF_MMU_STATUS, fault.mmu_status);
		fw_write64(e + OFF_HWRINFO_HWR_DATA_BIF_INFO_PC_ADDRESS, fault.pc);
	}
	FW32(e + OFF_HWRINFO_DM) = dm;
	fw_write64(e + OFF_HWRINFO_CR_TIME_OF_KICK, hd[dm].kick_time);
	FW32(b + OFF_HWRINFOBUF_WRITE_INDEX) = (wi + 1) & 15;
	count_dm(hd[dm].state & (DM_STATE_GUILTY_OVERRUNING | DM_STATE_INNOCENT_OVERRUNING) ?
		 OFF_HWRINFOBUF_HWR_DM_OVERRAN_COUNT : OFF_HWRINFOBUF_HWR_DM_LOCKED_UP_COUNT, dm);
}

static void notify(u32 dm, struct job *j, u32 reason)
{
	u32 d[8] = { 0 };

	d[0] = FW32(j->ctx + OFF_FWCOMMONCONTEXT_SERVER_COMMON_CONTEXT_ID);
	d[1] = reason;
	d[2] = dm;
	d[3] = FW32(j->cmd + OFF_CCB_CMD_HEADER_INT_JOB_REF);
	if (fault.valid) {
		u64 addr = fault.req_status & 0xFFFFFFFFF0ull;	/* REQ_STATUS.ADDRESS */

		d[4] = 1;				/* CONTEXT_RESET_FLAG_PF */
		d[6] = (u32)fault.pc;
		d[7] = (u32)(fault.pc >> 32);
		fwccb_post(FWCCB_CONTEXT_RESET_NOTIFICATION,
			   (const u32 []){ d[0], d[1], d[2], d[3], d[4], 0, d[6], d[7],
					   (u32)addr, (u32)(addr >> 32) }, 10);
		return;
	}
	fwccb_post(FWCCB_CONTEXT_RESET_NOTIFICATION, d, 8);
}

/* -- lockup ---------------------------------------------------------------------- */

static void hwr_try_reset(void);

/*
 * A data master has locked up (or overrun): record and report every
 * data master with work, then reset as soon as all of them have stopped.
 */
static void hwr_lockup(void)
{
	u32 n = 0;

	hwr_pending = 1;
	for (u32 i = 0; i < sizeof(hwr_dms); i++)
		n += hd[hwr_dms[i]].busy;
	for (u32 i = 0; i < sizeof(hwr_dms); i++) {
		u32 dm = hwr_dms[i], reason;
		struct job *j = sched_running_job(dm);

		if (!hd[dm].busy)
			continue;
		if (hwr_overrun)
			hd[dm].state = hd[dm].state ? DM_STATE_GUILTY_OVERRUNING :
				       DM_STATE_INNOCENT_OVERRUNING;
		else
			hd[dm].state = n > 1 ? DM_STATE_INNOCENT_LOCKUP : DM_STATE_GUILTY_LOCKUP;
		switch (hd[dm].state) {
		case DM_STATE_GUILTY_LOCKUP:
			reason = RESET_REASON_GUILTY_LOCKUP;
			break;
		case DM_STATE_INNOCENT_LOCKUP:
			reason = RESET_REASON_INNOCENT_LOCKUP;
			break;
		case DM_STATE_GUILTY_OVERRUNING:
			reason = RESET_REASON_GUILTY_OVERRUNING;
			break;
		default:
			reason = RESET_REASON_INNOCENT_OVERRUNING;
			break;
		}
		hwr_record(dm, j);
		notify(dm, j, reason);
	}
	hwr_try_reset();
}

static void hwr_check(u32 dm, u32 now)
{
	int progress;

	hd[dm].next_check = now + HWR_CHECK_PERIOD;
	if (now - hd[dm].kick_time > hd[dm].deadline) {
		if (hwr_pending)
			return;
		TRACE(SF_OPENFW_HWR_OVERRUN);
		hwr_overrun = 1;
		hd[dm].state = DM_STATE_GUILTY_OVERRUNING;
		hd[dm].checks = 0;
		hwr_lockup();
		return;
	}
	progress = sig_progress(dm);
	if (!progress)
		progress = usc_progress(dm);
	hd[dm].checks--;
	TRACE(SF_OPENFW_HWR_CHECK, dm, !progress, hd[dm].checks);
	if (progress) {
		hd[dm].checks = dm == DM_CDM ? HWR_CHECKS_CDM : HWR_CHECKS;
		return;
	}
	if (hd[dm].checks)
		return;
	TRACE(SF_OPENFW_HWR_TIMED_OUT, dm);
	if (hwr_pending)
		return;			/* now ready for the reset */
	if (other_dm_ok(dm, now)) {
		TRACE(SF_OPENFW_HWR_CHANCE, dm);
		hd[dm].checks = 1;
		return;
	}
	TRACE(SF_OPENFW_HWR_LOCKED_UP, dm);
	hwr_lockup();
}

/* -- reset and recovery ------------------------------------------------------------- */

static u32 fl_ids[4], fl_addr[4], fl_n;

/* The free lists the reset geometry / fragment jobs were using. */
static void hwr_fl_collect(void)
{
	fl_n = 0;
	hwrinfo_fl[0] = hwrinfo_fl[1] = 0;
	for (u32 i = 0; i < 2; i++) {
		u32 dm = i ? DM_FRAG : DM_GEOM;
		u32 h = sched_running_job(dm)->hwrt;

		if (!hd[dm].busy || !h)
			continue;
		hwrinfo_fl[i] = hwrinfo_entry[dm];
		for (u32 k = 0; k < 2; k++) {
			u32 fl = FW32(h + OFF_HWRTDATA_FREELISTS_FW_ADDR + 4 * k), id, m;

			if (!fl)
				continue;
			id = FW32(fl + OFF_FREELIST_FREELIST_ID);
			for (m = 0; m < fl_n && fl_ids[m] != id; m++)
				;
			if (m == fl_n && fl_n < 4) {
				fl_addr[fl_n] = fl;
				fl_ids[fl_n++] = id;
			}
		}
	}
}

/* Ask the kernel to rebuild them (pvr_free_list_process_reconstruct_req). */
static void hwr_reconstruct(void)
{
	u32 d[2 + 4];

	if (!fl_n)
		return;
	for (u32 i = 0; i < fl_n; i++)
		TRACE(SF_OPENFW_HWR_FL_REQUEST, fl_addr[i], fl_ids[i]);
	d[0] = fl_n;
	d[1] = hwr_counter();
	for (u32 i = 0; i < 4; i++)
		d[2 + i] = fl_ids[i];
	fwccb_post(FWCCB_FREELISTS_RECONSTRUCTION, d, 2 + fl_n);
	hwr_fl_hold = 1;
}

static void hwr_reset(void)
{
	u32 cnt = hwr_counter();

	for (u32 i = 0; i < sizeof(hwr_dms); i++)
		if (hd[hwr_dms[i]].busy)
			TRACE(SF_OPENFW_HWR_READY, hwr_dms[i]);
	TRACE(SF_OPENFW_HWR_BEGIN, cnt);
	for (u32 i = 0; i < sizeof(hwr_dms); i++) {
		u32 dm = hwr_dms[i];

		if (hd[dm].busy && hwrinfo_entry[dm])
			fw_write64(hwrinfo_entry[dm] + OFF_HWRINFO_CR_TIME_HW_RESET_START, timer_read());
	}
	gpu_hwr_reset();
	pm_hwr_reset();
	for (u32 i = 0; i < sizeof(hwr_dms); i++) {
		u32 dm = hwr_dms[i];

		if (hd[dm].busy && hwrinfo_entry[dm])
			fw_write64(hwrinfo_entry[dm] + OFF_HWRINFO_CR_TIME_HW_RESET_FINISH, timer_read());
	}
	TRACE(SF_OPENFW_HWR_END, cnt);
	if (g.hwrinfobuf)
		FW32(g.hwrinfobuf + OFF_HWRINFOBUF_HWR_COUNTER) = cnt + 1;

	/* free lists of the reset render jobs, before the jobs go */
	hwr_fl_collect();
	for (u32 i = 0; i < sizeof(hwr_dms); i++) {
		u32 dm = hwr_dms[i];

		if (hd[dm].busy) {
			struct job *j = sched_running_job(dm);

			TRACE(SF_OPENFW_HWR_SKIPPED, dm, j->ctx, j->cmd, 0, 0);
			sched_skip(dm);
			count_dm(OFF_HWRINFOBUF_HWR_DM_RECOVERED_COUNT, dm);
			hd[dm].busy = 0;
			host_irq();
			reg_write(CR_EVENT_CLEAR, dm_cfg[dm].events);
		}
		TRACE(SF_OPENFW_HWR_RECOVERED, dm);
	}
	hwr_reconstruct();
	for (u32 i = 0; i < sizeof(hwr_dms); i++)
		hwrinfo_entry[hwr_dms[i]] = 0;
	hwr_pending = 0;
	hwr_overrun = 0;
	hwr_now = 0;
	fault.valid = 0;
}

static void hwr_try_reset(void)
{
	for (u32 i = 0; i < sizeof(hwr_dms); i++) {
		u32 dm = hwr_dms[i];

		if (hd[dm].busy && hd[dm].checks && !hwr_now)
			return;		/* still running: wait for it to time out */
	}
	hwr_reset();
	sched_run();
}

/* Periodic timer: check every busy data master that is due. */
void hwr_timer(void)
{
	u32 now = timer32();

	for (u32 i = 0; i < sizeof(hwr_dms); i++) {
		u32 dm = hwr_dms[i];

		if (hd[dm].busy && hd[dm].checks && (s32)(now - hd[dm].next_check) >= 0)
			hwr_check(dm, now);
	}
	if (hwr_pending)
		hwr_try_reset();
}

/*
 * MMU_PAGE_FAULT event (interrupt task): a data master accessed memory
 * its page tables do not map. The requester stays stalled; the GPU is
 * reset at once and the busy data masters' jobs are skipped, the kernel
 * is told the faulting address (pvr_dump_context_reset_notification).
 */
void hwr_page_fault(void)
{
	u32 st = reg_read(CR_BIF_FAULT_BANK0_MMU_STATUS);

	TRACE(SF_OPENFW_MMU_FAULT, EVENT_MMU_PAGE_FAULT);
	if (!(st & 1) || hwr_pending)
		return;
	fault.valid = 1;
	fault.mmu_status = st;
	fault.req_status = reg_read64(CR_BIF_FAULT_BANK0_REQ_STATUS);
	fault.pc = reg_read64(CR_BIF_CAT_BASE0 + 8 * ((st >> 12) & 0xF));
	hwr_now = 1;
	hwr_lockup();
}

int hwr_holds(u32 dm)
{
	return hwr_pending || (hwr_fl_hold && (dm == DM_GEOM || dm == DM_FRAG));
}

/* KCCB FREELISTS_RECONSTRUCTION_UPDATE: the kernel rebuilt the free lists. */
void hwr_reconstruction_done(u32 data)
{
	u32 n = FW32(data + OFF_FREELISTS_RECONSTRUCTION_DATA_FREELIST_COUNT);

	for (u32 i = 0; i < n && i < 16; i++)
		TRACE(SF_OPENFW_HWR_FL_DONE,
		      FW32(data + OFF_FREELISTS_RECONSTRUCTION_DATA_FREELIST_IDS + 4 * i));
	for (u32 i = 0; i < 2; i++) {
		if (hwrinfo_fl[i])
			fw_write64(hwrinfo_fl[i] + OFF_HWRINFO_CR_TIME_FREELIST_READY, timer_read());
		hwrinfo_fl[i] = 0;
	}
	hwr_fl_hold = 0;
}

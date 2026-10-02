// SPDX-License-Identifier: MIT
/*
 * Client CCB scheduling: the firmware side of pvr_queue.c.
 *
 * The kernel writes, per job, into the context's client CCB
 *
 *   [FENCE_PR {ufo, value}...]  <job command>  UPDATE {timeline ufo, seqno}
 *
 * and sends a KICK with the new write offset. A context is "ready" while
 * it has unread commands. Processing a ready context checks its fences
 * (a UFO is satisfied when (s32)(*addr - value) >= 0), applies UPDATEs
 * that are not behind a job, and starts its next job when the context's
 * data master is free. When the data master reports completion, the
 * UPDATEs following the job signal its fence, the read offset moves past
 * them and the host is interrupted; the kernel then signals the job's
 * dma_fence from the timeline UFO value.
 *
 * Client CCB offsets: read_offset is the start of the oldest command not
 * yet completed, dep_offset the next command to examine.
 */
#include "fw.h"

#define MAX_READY 32

static u32 ready[MAX_READY];
static u32 nready;
static struct job running[DM_COUNT];
static u32 busy;		/* a job ran since the GPU last went idle */
static u32 frag_used;		/* ... and one of them on the 3D pipe */

static const u32 hdr_size = SIZEOF_CCB_CMD_HEADER;

static int ctx_is_ready(u32 ctx)
{
	for (u32 i = 0; i < nready; i++)
		if (ready[i] == ctx)
			return 1;
	return 0;
}

static void ready_add(u32 ctx)
{
	if (!ctx_is_ready(ctx) && nready < MAX_READY)
		ready[nready++] = ctx;
}

static void ready_del(u32 ctx)
{
	for (u32 i = 0; i < nready; i++) {
		if (ready[i] == ctx) {
			for (; i + 1 < nready; i++)
				ready[i] = ready[i + 1];
			nready--;
			return;
		}
	}
}

int sched_idle(void)
{
	for (u32 dm = 0; dm < DM_COUNT; dm++)
		if (running[dm].ctx)
			return 0;
	return nready == 0;
}

void sched_reset(void)
{
	nready = busy = frag_used = 0;
	for (u32 dm = 0; dm < DM_COUNT; dm++)
		running[dm].ctx = 0;
}

/* -- UFOs ------------------------------------------------------------------------ */

static int ufo_satisfied(u32 addr, u32 value)
{
	if (addr & UFO_ADDR_IS_SYNC_CHECKPOINT)
		return FW32(addr & ~3u) != 0xAC1u;	/* not ACTIVE: signalled or errored */
	return (s32)(FW32(addr) - value) >= 0;
}

static int fences_satisfied(u32 ctx, u32 payload, u32 size)
{
	for (u32 p = payload; p + 8 <= payload + size; p += 8) {
		u32 addr = FW32(p), value = FW32(p + 4);

		if (!ufo_satisfied(addr, value)) {
			TRACE(SF_OPENFW_UFO_CHECK, addr, FW32(addr & ~3u), value);
			FW32(ctx + OFF_FWCOMMONCONTEXT_LAST_FAILED_UFO_ADDR) = addr;
			FW32(ctx + OFF_FWCOMMONCONTEXT_LAST_FAILED_UFO_VALUE) = value;
			return 0;
		}
	}
	return 1;
}

static void apply_updates(u32 payload, u32 size)
{
	for (u32 p = payload; p + 8 <= payload + size; p += 8) {
		u32 addr = FW32(p), value = FW32(p + 4);

		TRACE(SF_OPENFW_UFO_UPDATE, addr, value);
		if (addr & UFO_ADDR_IS_SYNC_CHECKPOINT)
			FW32(addr & ~3u) = SYNC_CHECKPOINT_SIGNALED;
		else
			FW32(addr) = value;
	}
}

/* -- client CCB walking ------------------------------------------------------------ */

struct cccb {
	u32 ctl, ccb, woff, wrap;
};

static void cccb_open(u32 ctx, struct cccb *c)
{
	c->ctl = FW32(ctx + OFF_FWCOMMONCONTEXT_CCBCTL_FW_ADDR);
	c->ccb = FW32(ctx + OFF_FWCOMMONCONTEXT_CCB_FW_ADDR);
	c->woff = FW32(c->ctl + OFF_CCCB_CTL_WRITE_OFFSET);
	c->wrap = FW32(c->ctl + OFF_CCCB_CTL_WRAP_MASK);
}

static u32 cmd_type(struct cccb *c, u32 off)
{
	return FW32(c->ccb + off + OFF_CCB_CMD_HEADER_CMD_TYPE);
}

static u32 cmd_size(struct cccb *c, u32 off)
{
	return FW32(c->ccb + off + OFF_CCB_CMD_HEADER_CMD_SIZE);
}

static u32 cmd_next(struct cccb *c, u32 off)
{
	return (off + hdr_size + cmd_size(c, off)) & c->wrap;
}

/* Apply the UPDATE commands at @off; return the offset after them. */
static u32 run_updates(struct cccb *c, u32 off)
{
	while (off != c->woff) {
		u32 t = cmd_type(c, off);

		if (t == CCB_PADDING) {
			off = 0;
			continue;
		}
		if (t != CCB_UPDATE && t != CCB_UNFENCED_UPDATE)
			break;
		apply_updates(c->ccb + off + hdr_size, cmd_size(c, off));
		off = cmd_next(c, off);
	}
	return off;
}

static void set_offsets(struct cccb *c, u32 read, u32 dep)
{
	FW32(c->ctl + OFF_CCCB_CTL_READ_OFFSET) = read;
	FW32(c->ctl + OFF_CCCB_CTL_READ_OFFSET2) = read;
	FW32(c->ctl + OFF_CCCB_CTL_DEP_OFFSET) = dep;
}

/* -- jobs ---------------------------------------------------------------------------- */

static u32 job_dm(u32 type)
{
	switch (type) {
	case CCB_CDM:
		return DM_CDM;
	case CCB_GEOM:
		return DM_GEOM;
	case CCB_FRAG:
	case CCB_FRAG_PR:
	case CCB_TQ_3D:
		return DM_FRAG;
	}
	return DM_GP;
}

static u32 cmd_hwrt(u32 type, u32 payload)
{
	if (type == CCB_GEOM || type == CCB_FRAG || type == CCB_FRAG_PR)
		return FW32(payload + OFF_CMD_GEOM_CMD_SHARED_HWRT_DATA_FW_ADDR);
	return 0;
}

static int start_job(u32 ctx, struct cccb *c, u32 off, u32 type)
{
	u32 dm = job_dm(type);
	struct job *j = &running[dm];

	j->ctx = ctx;
	j->cmd = c->ccb + off;
	j->type = type;
	j->payload = j->cmd + hdr_size;
	j->hwrt = cmd_hwrt(type, j->payload);
	j->end = cmd_next(c, off);
	j->memctx = FW32(ctx + OFF_FWCOMMONCONTEXT_FW_MEM_CONTEXT_FW_ADDR);

	if (!gpu_units_on) {
		gpu_units_init();	/* also cancels any power-off */
		set_pow_state(POW_ON);
	}
	j->pcset = memctx_activate(j->memctx, dm);
	if (j->pcset == ROGUE_FW_BIF_INVALID_PCSET) {
		j->ctx = 0;
		return 0;
	}
	if (dm == DM_FRAG)
		frag_used = 1;
	busy = 1;

	switch (type) {
	case CCB_CDM:
		kick_cdm(j);
		break;
	case CCB_TQ_3D:
		kick_tq(j);
		break;
	case CCB_GEOM:
		kick_geom(j);
		break;
	default:
		kick_frag(j);
		break;
	}
	if (pow_state() != POW_ON) {
		set_pow_state(POW_ON);
		gpu_cancel_power_off();
	}
	return 1;
}

/*
 * Advance one context as far as possible. Returns 1 if it made progress.
 */
static int process(u32 ctx)
{
	struct cccb c;
	u32 off, read, dm;
	int progress = 0;

	cccb_open(ctx, &c);
	dm = FW32(ctx + OFF_FWCOMMONCONTEXT_DM);
	if (dm < DM_COUNT && running[dm].ctx == ctx)
		return 0;			/* a job of this context is running */
	read = FW32(c.ctl + OFF_CCCB_CTL_READ_OFFSET);
	off = read;

	while (off != c.woff) {
		u32 t = cmd_type(&c, off), size = cmd_size(&c, off);
		u32 payload = c.ccb + off + hdr_size;

		switch (t) {
		case CCB_PADDING:
			off = 0;
			if (read != 0)
				read = 0;
			continue;
		case CCB_FENCE:
		case CCB_FENCE_PR:
			if (!fences_satisfied(ctx, payload, size)) {
				set_offsets(&c, read, off);
				return progress;
			}
			off = cmd_next(&c, off);
			continue;
		case CCB_UPDATE:
		case CCB_UNFENCED_UPDATE:
			apply_updates(payload, size);
			off = cmd_next(&c, off);
			read = off;
			progress = 1;
			continue;
		case CCB_NULL:
			off = run_updates(&c, cmd_next(&c, off));
			read = off;
			progress = 1;
			host_irq();
			continue;
		case CCB_FRAG_PR:
			if (!frag_pr_needed(&(struct job){ .hwrt = cmd_hwrt(t, payload) })) {
				/* no partial render pending: only signal the fence */
				off = run_updates(&c, cmd_next(&c, off));
				read = off;
				progress = 1;
				host_irq();
				continue;
			}
			/* fall through */
		case CCB_CDM:
		case CCB_TQ_3D:
		case CCB_GEOM:
		case CCB_FRAG:
			dm = job_dm(t);
			if (running[dm].ctx) {
				set_offsets(&c, read, off);
				return progress;
			}
			{
				/* dep_offset: past the job and its UPDATEs */
				u32 dep = cmd_next(&c, off);

				while (dep != c.woff && (cmd_type(&c, dep) == CCB_UPDATE ||
							 cmd_type(&c, dep) == CCB_UNFENCED_UPDATE))
					dep = cmd_next(&c, dep);
				set_offsets(&c, read, dep);
			}
			if (!start_job(ctx, &c, off, t))
				return progress;
			return 1;
		default:
			/* unknown command: skip it */
			off = cmd_next(&c, off);
			read = off;
			continue;
		}
	}
	set_offsets(&c, read, off);
	ready_del(ctx);
	return 1;
}

void sched_run(void)
{
	int progress;

	do {
		u32 list[MAX_READY], n = nready;

		progress = 0;
		for (u32 i = 0; i < n; i++)
			list[i] = ready[i];
		for (u32 i = 0; i < n; i++)
			progress |= process(list[i]);
	} while (progress);

	if (sched_idle() && busy) {
		/* Idle: let the kernel power the GPU down. */
		busy = 0;
		if (frag_used) {
			frag_used = 0;
			reg_write(0x6300, 1);
		}
		set_pow_state(POW_IDLE);
		host_irq();
	}
}

/* KCCB KICK / one half of COMBINED_GEOM_FRAG_KICK */
void sched_kick(u32 k)
{
	u32 ctx = FW32(k + OFF_KCCB_CMD_KICK_DATA_CONTEXT_FW_ADDR);
	u32 ctl = FW32(ctx + OFF_FWCOMMONCONTEXT_CCBCTL_FW_ADDR);
	u32 n = FW32(k + OFF_KCCB_CMD_KICK_DATA_NUM_CLEANUP_CTL);

	FW32(ctl + OFF_CCCB_CTL_WRITE_OFFSET) = FW32(k + OFF_KCCB_CMD_KICK_DATA_CLIENT_WOFF_UPDATE);
	FW32(ctl + OFF_CCCB_CTL_WRAP_MASK) = FW32(k + OFF_KCCB_CMD_KICK_DATA_CLIENT_WRAP_MASK_UPDATE);
	for (u32 i = 0; i < n && i < 4; i++) {
		u32 cl = FW32(k + OFF_KCCB_CMD_KICK_DATA_CLEANUP_CTL_FW_ADDR + 4 * i);

		FW32(cl + OFF_CLEANUP_CTL_SUBMITTED_COMMANDS) += 1;
	}
	ready_add(ctx);
}

/* -- completion ------------------------------------------------------------------------ */

static void complete(u32 dm)
{
	struct job *j = &running[dm];
	struct cccb c;
	u32 off;

	if (!j->ctx)
		return;
	cccb_open(j->ctx, &c);
	off = run_updates(&c, j->end);
	set_offsets(&c, off, FW32(c.ctl + OFF_CCCB_CTL_DEP_OFFSET));
	if (j->hwrt && j->type != CCB_FRAG_PR) {
		u32 cl = j->hwrt + OFF_HWRTDATA_CLEANUP_STATE;

		FW32(cl + OFF_CLEANUP_CTL_EXECUTED_COMMANDS) += 1;
	}
	memctx_deactivate(j->memctx, dm);
	j->ctx = 0;
	host_irq();
}

static const struct {
	u8 dm;
	u32 event;
} dm_events[] = {
	{ DM_GEOM, EVENT_TA_FINISHED },
	{ DM_FRAG, EVENT_PIXELBE_END_RENDER },
	{ DM_CDM, EVENT_COMPUTE_FINISHED },
};

/* MTS interrupt task: GPU events */
void sched_irq(void)
{
	u32 ev = reg_read(CR_EVENT_STATUS);

	for (u32 i = 0; i < sizeof(dm_events) / sizeof(dm_events[0]); i++) {
		u32 dm = dm_events[i].dm;
		struct job *j = &running[dm];

		if (!(ev & dm_events[i].event) || !j->ctx)
			continue;
		switch (j->type) {
		case CCB_CDM:
			finish_cdm(j);
			break;
		case CCB_TQ_3D:
			finish_tq(j);
			break;
		case CCB_GEOM:
			finish_geom(j);
			break;
		default:
			finish_frag(j);
			break;
		}
		complete(dm);
	}
	sched_run();
}

/* -- cleanup ---------------------------------------------------------------------------- */

u32 sched_cleanup(u32 type, u32 addr)
{
	u32 dm;

	switch (type) {
	case CLEANUP_FWCOMMONCONTEXT: {
		struct cccb c;

		for (dm = 0; dm < DM_COUNT; dm++)
			if (running[dm].ctx == addr)
				return KCCB_RTN_CMD_EXECUTED | KCCB_RTN_CLEANUP_BUSY;
		cccb_open(addr, &c);
		if (FW32(c.ctl + OFF_CCCB_CTL_READ_OFFSET) != c.woff)
			return KCCB_RTN_CMD_EXECUTED | KCCB_RTN_CLEANUP_BUSY;
		ready_del(addr);
		break;
	}
	case CLEANUP_HWRTDATA:
		for (dm = 0; dm < DM_COUNT; dm++)
			if (running[dm].ctx && running[dm].hwrt == addr)
				return KCCB_RTN_CMD_EXECUTED | KCCB_RTN_CLEANUP_BUSY;
		break;
	case CLEANUP_FREELIST:
		for (dm = 0; dm < DM_COUNT; dm++) {
			u32 h = running[dm].hwrt;

			if (running[dm].ctx && h &&
			    (FW32(h + OFF_HWRTDATA_FREELISTS_FW_ADDR0) == addr ||
			     FW32(h + OFF_HWRTDATA_FREELISTS_FW_ADDR1) == addr))
				return KCCB_RTN_CMD_EXECUTED | KCCB_RTN_CLEANUP_BUSY;
		}
		break;
	}
	/* the kernel frees the object next: drop any stale TLB entries */
	fw_tlb_flush();
	return KCCB_RTN_CMD_EXECUTED;
}

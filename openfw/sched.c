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

/*
 * Contexts with unprocessed commands, in the order they became runnable:
 * a context stopped at an unsatisfied fence with nothing before it is
 * "blocked" and goes to the back when the fence is satisfied, as in the
 * reference firmware.
 */
static u32 ready[MAX_READY];
static u8 blocked[MAX_READY];
static u32 nready;
static struct job running[DM_COUNT];

/* a partial render to run on the 3D pipe (see spm_try) */
static struct {
	u32 geom_ctx, hwrt, fence_addr, fence_value;
} spm;

static const u32 hdr_size = SIZEOF_CCB_CMD_HEADER;
static u32 pow_query;		/* idle seen, not reported yet (sched_idle_report) */

static int ctx_is_ready(u32 ctx)
{
	for (u32 i = 0; i < nready; i++)
		if (ready[i] == ctx)
			return 1;
	return 0;
}

static void ready_add(u32 ctx)
{
	if (!ctx_is_ready(ctx) && nready < MAX_READY) {
		blocked[nready] = 0;
		ready[nready++] = ctx;
	}
}

static void ready_del(u32 ctx)
{
	for (u32 i = 0; i < nready; i++) {
		if (ready[i] == ctx) {
			for (; i + 1 < nready; i++) {
				ready[i] = ready[i + 1];
				blocked[i] = blocked[i + 1];
			}
			nready--;
			return;
		}
	}
}

/* Track whether @ctx waits on a fence; a context unblocked goes last. */
static void ready_set_blocked(u32 ctx, int b)
{
	for (u32 i = 0; i < nready; i++) {
		if (ready[i] != ctx)
			continue;
		if (b) {
			blocked[i] = 1;
		} else if (blocked[i]) {
			ready_del(ctx);
			ready_add(ctx);
		}
		return;
	}
}

int sched_dm_busy(u32 dm)
{
	return running[dm].ctx != 0;
}

u32 sched_running_hwrt(u32 dm)
{
	return running[dm].hwrt;
}

struct job *sched_running_job(u32 dm)
{
	return &running[dm];
}

/* No job on any data master (contexts blocked on fences do not count). */
int sched_idle(void)
{
	for (u32 dm = 0; dm < DM_COUNT; dm++)
		if (running[dm].ctx)
			return 0;
	return 1;
}

void sched_reset(void)
{
	nready = 0;
	spm.hwrt = 0;
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

/* UFOs signalled by the job completing now (sched_irq wakes their waiters) */
#define MAX_SIGNALLED 8
static u32 signalled[MAX_SIGNALLED], nsignalled;
static int record_signalled;

static void apply_updates(u32 payload, u32 size)
{
	for (u32 p = payload; p + 8 <= payload + size; p += 8) {
		u32 addr = FW32(p), value = FW32(p + 4);

		TRACE(SF_OPENFW_UFO_UPDATE, addr, value);
		if (record_signalled && nsignalled < MAX_SIGNALLED)
			signalled[nsignalled++] = addr;
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

static void set_read(struct cccb *c, u32 read)
{
	FW32(c->ctl + OFF_CCCB_CTL_READ_OFFSET) = read;
	FW32(c->ctl + OFF_CCCB_CTL_READ_OFFSET2) = read;
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

static void complete(u32 dm);

static int start_job(u32 ctx, struct cccb *c, u32 off, u32 type, u32 pr)
{
	u32 dm = job_dm(type);
	struct job *j = &running[dm];

	j->ctx = ctx;
	j->pr = pr;
	j->cmd = c->ccb + off;
	j->type = type;
	j->payload = j->cmd + hdr_size;
	j->hwrt = cmd_hwrt(type, j->payload);
	j->end = cmd_next(c, off);
	j->memctx = FW32(ctx + OFF_FWCOMMONCONTEXT_FW_MEM_CONTEXT_FW_ADDR);

	if (!gpu_units_on) {
		gpu_units_init();	/* also cancels any power-off */
		set_pow_state(POW_ON);
		pow_query = 0;
	}
	j->pcset = memctx_activate(j->memctx, dm);
	if (j->pcset == ROGUE_FW_BIF_INVALID_PCSET) {
		j->ctx = 0;
		return 0;
	}
	if (j->hwrt && type != CCB_FRAG_PR &&
	    FW32(j->hwrt + OFF_HWRTDATA_STATE) == RTDATA_HWR &&
	    (type == CCB_FRAG || !(FW32(j->payload + OFF_CMD_GEOM_FLAGS) & GEOM_FLAGS_FIRSTKICK))) {
		/* render target abandoned by a hardware recovery: the job is
		 * discarded, its fences signalled */
		TRACE(SF_OPENFW_HWR_DISCARD, dm, 1, j->hwrt, RTDATA_HWR, ctx, off);
		complete(dm);
		return 1;
	}
	hwr_kick(dm, ctx);

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
	if (pow_state() != POW_ON || pow_query) {
		set_pow_state(POW_ON);
		pow_query = 0;
		gpu_cancel_power_off();
	}
	return 1;
}

/*
 * dep_offset: how far the context's commands have been checked. It moves
 * over every command whose fences are satisfied, queued jobs included,
 * and stops at the first fence that is not.
 */
static u32 dep_walk(u32 ctx, struct cccb *c)
{
	u32 d = FW32(c->ctl + OFF_CCCB_CTL_DEP_OFFSET);

	while (d != c->woff) {
		u32 t = cmd_type(c, d);

		if (t == CCB_PADDING) {
			d = 0;
			continue;
		}
		if ((t == CCB_FENCE || t == CCB_FENCE_PR) &&
		    !fences_satisfied(ctx, c->ccb + d + hdr_size, cmd_size(c, d)))
			break;
		d = cmd_next(c, d);
	}
	FW32(c->ctl + OFF_CCCB_CTL_DEP_OFFSET) = d;
	return d;
}

/*
 * Run a context's checked commands: apply UPDATEs, signal NULL and
 * unneeded partial-render commands, start its next job when the data
 * master is free. Returns 1 if it made progress.
 */
static int process(u32 ctx)
{
	struct cccb c;
	u32 off, dep, dm;
	int progress = 0;

	cccb_open(ctx, &c);
	dep = dep_walk(ctx, &c);
	ready_set_blocked(ctx, dep != c.woff && FW32(c.ctl + OFF_CCCB_CTL_READ_OFFSET) == dep);
	dm = FW32(ctx + OFF_FWCOMMONCONTEXT_DM);
	if (dm < DM_COUNT && running[dm].ctx)
		return 0;			/* its data master is busy: wait (also
						 * for UPDATE/NULL/partial-render
						 * commands, as the reference does) */
	if (hwr_holds(dm))
		return 0;			/* hardware recovery in progress */
	off = FW32(c.ctl + OFF_CCCB_CTL_READ_OFFSET);

	while (off != dep) {
		u32 t = cmd_type(&c, off);
		u32 payload = c.ccb + off + hdr_size;

		switch (t) {
		case CCB_PADDING:
			off = 0;
			continue;
		case CCB_UPDATE:
		case CCB_UNFENCED_UPDATE:
			apply_updates(payload, cmd_size(&c, off));
			off = cmd_next(&c, off);
			progress = 1;
			continue;
		case CCB_NULL:
			off = run_updates(&c, cmd_next(&c, off));
			progress = 1;
			host_irq();
			continue;
		case CCB_FRAG_PR:
			if (!frag_pr_needed(&(struct job){ .hwrt = cmd_hwrt(t, payload) })) {
				/* no partial render pending: only signal the fence */
				off = run_updates(&c, cmd_next(&c, off));
				progress = 1;
				host_irq();
				continue;
			}
			/* fall through */
		case CCB_CDM:
		case CCB_TQ_3D:
		case CCB_GEOM:
		case CCB_FRAG:
			set_read(&c, off);
			if (running[job_dm(t)].ctx || !start_job(ctx, &c, off, t, 0))
				return progress;
			return 1;
		default:
			/* fences (checked by dep_walk) and unknown commands */
			off = cmd_next(&c, off);
			continue;
		}
	}
	set_read(&c, off);
	if (off == c.woff)
		ready_del(ctx);
	return progress;
}

static u32 ctx_dm(u32 ctx)
{
	return FW32(ctx + OFF_FWCOMMONCONTEXT_DM);
}

/* The fence command @ctx stopped at names a UFO signalled just now. */
static int ctx_woken(u32 ctx)
{
	struct cccb c;
	u32 d, t, p;

	cccb_open(ctx, &c);
	d = FW32(c.ctl + OFF_CCCB_CTL_DEP_OFFSET);
	if (d == c.woff)
		return 0;
	t = cmd_type(&c, d);
	if (t != CCB_FENCE && t != CCB_FENCE_PR)
		return 0;
	p = c.ccb + d + hdr_size;
	for (u32 u = p; u + 8 <= p + cmd_size(&c, d); u += 8)
		for (u32 i = 0; i < nsignalled; i++)
			if (FW32(u) == signalled[i])
				return 1;
	return 0;
}

/*
 * Process the ready contexts of one data master: highest priority first
 * (PVR_CTX_PRIORITY_*), then in the order they became runnable.
 */
static int run_dm(u32 dm)
{
	u32 list[MAX_READY], n = 0;
	int progress = 0;


	for (u32 i = 0; i < nready; i++) {
		u32 c = ready[i], p = FW32(c + OFF_FWCOMMONCONTEXT_PRIORITY), k = n++;

		if (ctx_dm(c) != dm) {
			n--;
			continue;
		}
		/* stable insertion by descending priority */
		while (k && FW32(list[k - 1] + OFF_FWCOMMONCONTEXT_PRIORITY) < p) {
			list[k] = list[k - 1];
			k--;
		}
		list[k] = c;
	}
	for (u32 i = 0; i < n; i++)
		progress |= process(list[i]);
	return progress;
}

/*
 * Data masters are served in a fixed order: the 3D pipe first (fragment
 * and transfer jobs; finishing renders frees parameter memory), then
 * geometry, then compute. After a completion the data master that just
 * finished is refilled first (sched_irq).
 */
static const u8 dm_order[] = { DM_FRAG, DM_GEOM, DM_CDM };

static void sched_schedule(void)
{
	int progress;

	do {
		progress = 0;
		for (u32 o = 0; o < sizeof(dm_order); o++)
			progress |= run_dm(dm_order[o]);
	} while (progress);
}

/*
 * Idle is reported in two steps, as the reference firmware does: when no
 * job runs a power-off query is started and an interrupt task queued;
 * that task reports IDLE (pow_state, host interrupt) if the GPU is still
 * idle. A job starting in between cancels the query.
 */
static void sched_idle_report(void)
{
	if (sched_idle() && pow_state() == POW_ON && !pow_query) {
		pow_query = 1;
		mts_schedule(0x20);
	}
}

static void sched_idle_confirm(void)
{
	if (pow_query && sched_idle() && pow_state() == POW_ON) {
		/* Idle: let the kernel power the GPU down. */
		pow_query = 0;
		set_pow_state(POW_IDLE);
		if (!g.kccb_irq)
			host_irq();
	}
}

void sched_run(void)
{
	sched_schedule();
	sched_idle_report();
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

/* -- partial renders ------------------------------------------------------------------ */

/*
 * A TA that ran out of parameter memory with no way to grow it has been
 * stopped (kicks.c); its render's partial-render command (FRAG_PR, in the
 * fragment context of the same render context) now runs on the 3D pipe to
 * free memory, ahead of anything else there. The paired geometry job's
 * done fence among its FENCE_PRs is ignored: that is the geometry being
 * rendered. The command stays in the client CCB; once the geometry
 * finishes it is consumed like any partial-render command that is not
 * needed.
 */
static int spm_try(void)
{
	u32 frag = spm.geom_ctx + OFF_FWRENDERCONTEXT_FRAG_CONTEXT;
	struct cccb c;
	u32 off;

	if (!spm.hwrt || running[DM_FRAG].ctx)
		return 0;
	/* the queue's previous geometry job is done (pvr_queue.c) */
	if (!ufo_satisfied(spm.fence_addr, spm.fence_value))
		return 0;
	cccb_open(frag, &c);
	off = FW32(c.ctl + OFF_CCCB_CTL_READ_OFFSET);
	while (off != c.woff) {
		u32 t = cmd_type(&c, off), p = c.ccb + off + hdr_size;

		switch (t) {
		case CCB_PADDING:
			off = 0;
			continue;
		case CCB_FENCE:
		case CCB_FENCE_PR:
			for (u32 u = p; u + 8 <= p + cmd_size(&c, off); u += 8)
				if (FW32(u) != spm.fence_addr && !ufo_satisfied(FW32(u), FW32(u + 4)))
					return 0;
			break;
		case CCB_UPDATE:
		case CCB_UNFENCED_UPDATE:
			break;
		case CCB_FRAG_PR:
			if (cmd_hwrt(t, p) != spm.hwrt)
				return 0;
			spm.hwrt = 0;
			start_job(frag, &c, off, t, 1);
			return 1;
		default:
			return 0;	/* behind another job */
		}
		off = cmd_next(&c, off);
	}
	return 0;
}

void sched_request_pr(struct job *g)
{
	spm.geom_ctx = g->ctx;
	spm.hwrt = g->hwrt;
	spm.fence_addr = FW32(g->payload + OFF_CMD_GEOM_PARTIAL_RENDER_GEOM_FRAG_FENCE_ADDR);
	spm.fence_value = FW32(g->payload + OFF_CMD_GEOM_PARTIAL_RENDER_GEOM_FRAG_FENCE_VALUE);
	spm_try();
}

/* -- completion ------------------------------------------------------------------------ */

static void complete(u32 dm)
{
	struct job *j = &running[dm];
	struct cccb c;

	if (!j->ctx)
		return;
	if (j->pr) {
		/* the command stays in the CCB; the stopped TA restarts */
		memctx_deactivate(j->memctx, dm);
		pr_finished(j);
		j->ctx = 0;
		return;
	}
	cccb_open(j->ctx, &c);
	set_read(&c, run_updates(&c, j->end));
	if (j->hwrt && j->type != CCB_FRAG_PR) {
		u32 cl = j->hwrt + OFF_HWRTDATA_CLEANUP_STATE;

		FW32(cl + OFF_CLEANUP_CTL_EXECUTED_COMMANDS) += 1;
	}
	memctx_deactivate(j->memctx, dm);
	j->ctx = 0;
	hwr_done(dm);
	host_irq();
}

/*
 * Hardware recovery: the job on @dm was lost in the GPU reset. It is
 * skipped like a finished one: the commands after it signal its fences.
 */
void sched_skip(u32 dm)
{
	struct job *j = &running[dm];
	struct cccb c;

	if (!j->ctx)
		return;
	if (dm == DM_FRAG && spm.hwrt)
		spm.hwrt = 0;
	cccb_open(j->ctx, &c);
	set_read(&c, run_updates(&c, j->end));
	if (j->hwrt && j->type != CCB_FRAG_PR && !j->pr) {
		u32 cl = j->hwrt + OFF_HWRTDATA_CLEANUP_STATE;

		FW32(cl + OFF_CLEANUP_CTL_EXECUTED_COMMANDS) += 1;
	}
	memctx_deactivate(j->memctx, dm);
	j->ctx = 0;
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
	int frag_done = 0;
	u32 query = pow_query;		/* started by an earlier task */

	if ((ev & EVENT_PM_OUT_OF_MEMORY) && running[DM_GEOM].ctx)
		oom_geom(&running[DM_GEOM]);

	for (u32 i = 0; i < sizeof(dm_events) / sizeof(dm_events[0]); i++) {
		u32 dm = dm_events[i].dm;
		struct job *j = &running[dm];
		u32 partner;

		if (!(ev & dm_events[i].event) || !j->ctx)
			continue;
		/* the other half of a render context waits on this one */
		partner = j->type == CCB_GEOM ? j->ctx + OFF_FWRENDERCONTEXT_FRAG_CONTEXT :
			  (j->type == CCB_FRAG || j->type == CCB_FRAG_PR) && !j->pr ?
			  j->ctx - OFF_FWRENDERCONTEXT_FRAG_CONTEXT : 0;
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
		nsignalled = 0;
		record_signalled = 1;
		complete(dm);
		record_signalled = 0;
		if (dm == DM_FRAG) {
			spm_try();		/* a partial render waits for the 3D */
			frag_done = 1;
		}
		run_dm(dm);
		if (partner && ctx_is_ready(partner))
			process(partner);
		/* after a 3D pipe job: geometry waiting on the fences it signalled */
		if (dm == DM_FRAG && nsignalled) {
			u32 list[MAX_READY], n = nready;

			for (u32 k = 0; k < n; k++)
				list[k] = ready[k];
			for (u32 k = 0; k < n; k++)
				if (list[k] != partner && ctx_dm(list[k]) == DM_GEOM &&
				    ctx_is_ready(list[k]) && ctx_woken(list[k]))
					process(list[k]);
		}
	}
	if (frag_done && !running[DM_FRAG].ctx)
		reg_write(0x6300, 1);		/* the 3D pipe goes idle */
	/* other contexts (waiting on fences this signalled, or on a data
	 * master that is free now): the background task re-checks them */
	if (nready)
		mts_schedule(0);
	if (query)
		sched_idle_confirm();
	else
		sched_idle_report();
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
		pm_unload_freelists(addr);
		gpu_slc_mmu_flush(BIF_CTRL_INVAL_PC);
		break;
	}
	/* the kernel frees the object next: drop any stale TLB entries */
	fw_tlb_flush();
	return KCCB_RTN_CMD_EXECUTED;
}

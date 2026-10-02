// SPDX-License-Identifier: MIT
/*
 * openfw: an open firmware for the PowerVR BXE-4-32 MIPS firmware
 * processor, for the upstream Linux powervr driver: boot handshake, kernel
 * CCB processing, MMU cache maintenance, power requests, firmware tracing
 * and job execution (sched.c, kicks.c, gpu.c).
 *
 * Execution model: fw_main() initialises and idles in `wait`. All work
 * happens in non-nesting handlers (start.S) entered from the idle loop:
 * the MTS background task (kernel CCB kicks), the MTS interrupt task (GPU
 * events) and a periodic CP0 timer that re-checks the kernel CCB.
 */
#include "fw.h"
#include "mmu.h"

#define POW_OFF_REQ		1u
#define POW_FORCED_IDLE_REQ	2u
#define POW_NUM_UNITS_CHANGE	3u
#define POWER_FORCE_IDLE	1u
#define POWER_CANCEL_FORCED_IDLE 2u

#define CONNECTION_FW_ACTIVE	2u

/* kccb_handle(): commands the kernel does not wait for through the
 * return slots (kicks, grow updates, power requests) get no return value */
#define KCCB_NO_RTN		0xFFFFFFFFu

#define MMUCACHE_BIF_MASK	0xFu	/* PT | PD | PC | TLB1 -> BIF_CTRL_INVAL */

#define TIMER_PERIOD		0x01000000u

#define TRAP_EXCEPTION		0
#define TRAP_TIMER		1
#define TRAP_BG			2
#define TRAP_IRQ		3

/* Exception frame laid out by start.S */
struct frame {
	u32 gpr[32];
	u32 epc, status, hi, lo;
};

struct fw_globals g;

/* ------------------------------------------------------------------------
 * Tracing into the kernel's trace buffer (decoded by pvr_fw_trace.c)
 */

/* rogue_fw_log_sfgroups index -> ROGUE_FWIF_LOG_TYPE_GROUP_* bit */
static const u32 group_bit[16] = {
	0, 0x2, 0x8, 0x10, 0x40, 0x80, 0x100, 0x4,
	0x20, 0x4000, 0x200, 0x400, 0x800, 0x1000, 0x2000, 0x80000000u,
};

static u64 timer_read(void)
{
	u32 hi, lo;

	do {
		hi = reg_read(CR_TIMER + 4);
		lo = reg_read(CR_TIMER);
	} while (hi != reg_read(CR_TIMER + 4));
	return ((u64)hi << 32 | lo) & 0x0000FFFFFFFFFFFFull;
}

void trace_n(u32 id, u32 n, const u32 *args, int force)
{
	u32 tb = g.tracebuf_ctl, log_type, size, buf, p;

	if (!tb)
		return;
	log_type = FW32(tb + OFF_TRACEBUF_LOG_TYPE);
	if (!force && (!(log_type & 1) || !(log_type & group_bit[(id >> 12) & 0xf])))
		return;
	size = FW32(tb + OFF_TRACEBUF_TRACEBUF_SIZE_IN_DWORDS);
	buf = FW32(tb + OFF_TRACEBUF_TRACEBUF0_TRACE_BUFFER_FW_ADDR);
	p = FW32(tb + OFF_TRACEBUF_TRACEBUF0_TRACE_POINTER);
	if (!buf || !size || p >= size)
		return;

	u64 ts = timer_read();
	u32 words[3] = { id, (u32)(ts >> 32), (u32)ts };

	for (u32 i = 0; i < 3 + n; i++) {
		FW32(buf + 4 * p) = i < 3 ? words[i] : args[i - 3];
		if (++p == size)
			p = 0;
	}
	FW32(tb + OFF_TRACEBUF_TRACEBUF0_TRACE_POINTER) = p;
}


/* ------------------------------------------------------------------------
 * Helpers
 */

static void __attribute__((noreturn)) halt(void)
{
	for (;;) {
		irq_disable();
		mips_wait();
	}
}

int poll_reg(u32 off, u32 mask, u32 value)
{
	for (u32 i = 0; i < 1000000; i++) {
		if ((reg_read(off) & mask) == value)
			return 0;
	}
	TRACE_FORCE(SF_OPENFW_DBG_HEX4, 0xDEAD0001u, off, mask, reg_read(off));
	return -1;
}

void host_irq(void)
{
	mips_sync();
	reg_write(CR_MIPS_WRAPPER_IRQ_STATUS, 1);
}

/* Post a command to the kernel's firmware CCB (pvr_fwccb_process). */
void fwccb_send(u32 type, u32 a0, u32 a1, u32 a2)
{
	u32 ctl = g.fwccb_ctl;
	u32 wo = FW32(ctl + OFF_CCB_CTL_WRITE_OFFSET);
	u32 wrap = FW32(ctl + OFF_CCB_CTL_WRAP_MASK);
	u32 cmd = g.fwccb + wo * SIZEOF_FWCCB_CMD;

	if (((wo + 1) & wrap) == FW32(ctl + OFF_CCB_CTL_READ_OFFSET))
		return;				/* full: the kernel is not reading */
	for (u32 i = 0; i < SIZEOF_FWCCB_CMD; i += 4)
		FW32(cmd + i) = 0;
	FW32(cmd + OFF_FWCCB_CMD_CMD_TYPE) = type;
	FW32(cmd + OFF_FWCCB_CMD_CMD_DATA) = a0;
	FW32(cmd + OFF_FWCCB_CMD_CMD_DATA + 4) = a1;
	FW32(cmd + OFF_FWCCB_CMD_CMD_DATA + 8) = a2;
	mips_sync();
	FW32(ctl + OFF_CCB_CTL_WRITE_OFFSET) = (wo + 1) & wrap;
	host_irq();
}

/* Tell the MTS the current task has finished (the write is read back). */
static void mts_task_done(u32 v)
{
	reg_write(CR_MTS_TASK_DONE, v);
	(void)reg_read(CR_MTS_TASK_DONE);
}

void set_pow_state(u32 s)
{
	FW32(g.sysdata + OFF_SYSDATA_POW_STATE) = s;
}

u32 pow_state(void)
{
	return FW32(g.sysdata + OFF_SYSDATA_POW_STATE);
}

/*
 * Drop every non-wired TLB entry and its remap ranges, so that the next
 * access re-reads the page table: the kernel may have unmapped or moved
 * firmware objects.
 */
KSEG0_TEXT void fw_tlb_flush(void)
{
	for (u32 i = WIRED_ENTRIES; i < TLB_ENTRIES; i++) {
		tlb_write_index(i, 0xF0000000u + (i << 13), 0, 0);
		remap_clear(i);
		remap_clear(i + 16);
	}
	remap_flush();
}

/* SLC flush + invalidate of MMU data, then BIF MMU cache invalidate. */
static void gpu_mmu_inval(u32 bif_flags)
{
	reg_write(CR_SLC_CTRL_FLUSH_INVAL, SLC_FLUSH_INVAL_DM_MMU);
	poll_reg(CR_SLC_STATUS0, 0x4, 0);
	if (bif_flags) {
		reg_write(CR_BIF_CTRL_INVAL, bif_flags);
		poll_reg(CR_BIF_CTRL_INVAL, MMUCACHE_BIF_MASK, 0);
	}
}

/* ------------------------------------------------------------------------
 * Kernel CCB
 */

static void cmd_mmucache(u32 cmd)
{
	u32 flags = FW32(cmd + OFF_KCCB_CMD_CMD_DATA_MMU_CACHE_DATA_CACHE_FLAGS);
	u32 sync = FW32(cmd + OFF_KCCB_CMD_CMD_DATA_MMU_CACHE_DATA_MMU_CACHE_SYNC_FW_ADDR);

	gpu_mmu_inval(flags & MMUCACHE_BIF_MASK);
	fw_tlb_flush();
	if (sync)
		FW32(sync) = FW32(cmd + OFF_KCCB_CMD_CMD_DATA_MMU_CACHE_DATA_MMU_CACHE_SYNC_UPDATE_VALUE);
}

/* MTS DM0 interrupt routing of GPIO requests (the kernel's GPIO channel) */
static void gpio_irq_enable(int on)
{
	u32 v = reg_read(CR_MTS_DM0_INTERRUPT_ENABLE);

	reg_write(CR_MTS_DM0_INTERRUPT_ENABLE, on ? v | EVENT_GPIO_REQ : v & ~EVENT_GPIO_REQ);
}

/*
 * Before the kernel removes power: everything the GPU holds that must
 * survive goes to memory (parameter manager state, pm_save), the units
 * are shut down and the SLC written back, then the firmware stops. The
 * kernel waits for power_sync, checks the GPU is idle and soft-resets
 * everything, the MIPS core included; the next power-up reboots openfw.
 */
static void gpu_power_off(void)
{
	gpio_irq_enable(0);
	pm_save();
	TRACE(SF_OPENFW_GPU_DEINIT);
	reg_write(0x6300, 1);
	reg_write(CR_XPU_BROADCAST, 1);
	reg_write(CR_EVENT_CLEAR, EVENT_GPIO_REQ | EVENT_GPIO_ACK);
	reg_write(CR_MTS_UNNAMED_B90, 0x1000);
	gpio_irq_enable(0);
	gpu_slc_mmu_flush_nofence(BIF_CTRL_INVAL_PC);
}

static void cmd_pow(u32 cmd)
{
	u32 type = FW32(cmd + OFF_KCCB_CMD_CMD_DATA_POW_DATA_POW_TYPE);
	u32 arg = FW32(cmd + OFF_KCCB_CMD_CMD_DATA_POW_DATA_POWER_REQ_DATA_POW_REQUEST_TYPE);

	switch (type) {
	case POW_OFF_REQ:
		TRACE(SF_OPENFW_POW_OFF, arg, 0);
		gpu_power_off();
		set_pow_state(POW_OFF);
		g.halt = 1;
		break;
	case POW_FORCED_IDLE_REQ:
		if (arg == POWER_FORCE_IDLE) {
			TRACE(SF_OPENFW_POW_IDLE, 0);
			gpio_irq_enable(0);
			set_pow_state(POW_FORCED_IDLE);
		} else {
			TRACE(SF_OPENFW_POW_CANCEL_IDLE, 0);
			set_pow_state(POW_IDLE);
			host_irq();
		}
		break;
	case POW_NUM_UNITS_CHANGE:
		TRACE(SF_OPENFW_POW_DUSTS, g.dusts, arg);
		g.dusts = arg;
		break;
	}
	mips_sync();
	FW32(g.power_sync) = 1;
}

static u32 kccb_handle(u32 cmd, u32 type, u32 slot)
{
	switch (type) {
	case KCCB_HEALTH_CHECK:
		return KCCB_NO_RTN;	/* the kernel only watches kccb_cmds_executed */
	case KCCB_MMUCACHE:
		cmd_mmucache(cmd);
		break;
	case KCCB_POW:
		cmd_pow(cmd);
		return KCCB_NO_RTN;	/* the kernel waits for power_sync */
	case KCCB_FREELISTS_RECONSTRUCTION_UPDATE:
		/* answers a reconstruction request, which openfw never makes */
		return KCCB_NO_RTN;
	case KCCB_LOGTYPE_UPDATE:
		/* log_type is re-read on every trace */
		fw_tlb_flush();
		break;
	case KCCB_KICK:
		sched_kick(cmd + OFF_KCCB_CMD_CMD_DATA);
		return KCCB_NO_RTN;
	case KCCB_COMBINED_KICK:
		/* geometry and fragment kick data, back to back */
		sched_kick(cmd + OFF_KCCB_CMD_CMD_DATA);
		sched_kick(cmd + OFF_KCCB_CMD_CMD_DATA + SIZEOF_KCCB_CMD_KICK_DATA);
		return KCCB_NO_RTN;
	case KCCB_FREELIST_GROW_UPDATE:
		freelist_grow_update(cmd + OFF_KCCB_CMD_CMD_DATA_FREE_LIST_GS_DATA);
		return KCCB_NO_RTN;
	case KCCB_CLEANUP:
		return sched_cleanup(FW32(cmd + OFF_KCCB_CMD_CMD_DATA + OFF_CLEANUP_REQUEST_CLEANUP_TYPE),
				     FW32(cmd + OFF_KCCB_CMD_CMD_DATA + OFF_CLEANUP_REQUEST_CLEANUP_DATA));
	case KCCB_SLCFLUSHINVAL:
		reg_write(CR_SLC_CTRL_FLUSH_INVAL, 1);	/* ALL */
		poll_reg(CR_SLC_STATUS0, SLC_STATUS0_PENDING, 0);
		break;
	default:
		TRACE_FORCE(SF_OPENFW_KCCB_UNKNOWN, g.kccb_ctl, g.kccb, slot,
			    FW32(g.kccb_ctl + OFF_CCB_CTL_WRITE_OFFSET),
			    FW32(g.kccb_ctl + OFF_CCB_CTL_WRAP_MASK), cmd, type);
		break;
	}
	return KCCB_RTN_CMD_EXECUTED;
}

static void kccb_process(void)
{
	u32 ctl = g.kccb_ctl, irq = 0;

	while (!g.halt) {
		u32 ro = FW32(ctl + OFF_CCB_CTL_READ_OFFSET);
		u32 wo = FW32(ctl + OFF_CCB_CTL_WRITE_OFFSET);
		u32 wrap = FW32(ctl + OFF_CCB_CTL_WRAP_MASK);

		if (ro == wo)
			break;
		u32 cmd = g.kccb + ro * SIZEOF_KCCB_CMD;
		u32 type = FW32(cmd + OFF_KCCB_CMD_CMD_TYPE);

		TRACE(SF_OPENFW_KCCB, ro, type, 0);
		u32 rtn = kccb_handle(cmd, type, ro);

		if (rtn != KCCB_NO_RTN) {
			/* the kernel waits for this one: return value and interrupt */
			FW32(g.kccb_rtn + 4 * ro) = rtn;
			TRACE(SF_OPENFW_KCCB_RTN, ro, rtn);
			irq = 1;
		}
		FW32(g.osdata + OFF_OSDATA_KCCB_CMDS_EXECUTED) += 1;
		mips_sync();
		FW32(ctl + OFF_CCB_CTL_READ_OFFSET) = (ro + 1) & wrap;
	}
	if (irq) {
		host_irq();
		g.kccb_irq = 1;		/* covers an IDLE report in the same task */
	}
}

/* ------------------------------------------------------------------------
 * Handlers (called from start.S with EXL = 0, IE = 0)
 */

static void fw_bg_task(void)
{
	TRACE(SF_OPENFW_BG, 0);
	g.kccb_irq = 0;
	kccb_process();
	sched_run();		/* reports IDLE when nothing runs */
	mts_task_done(MTS_TASK_DONE_BG);
	if (g.halt)
		halt();
}

static void fw_irq_task(void)
{
	u32 ev = reg_read(CR_EVENT_STATUS);

	TRACE(SF_OPENFW_IRQ, ev);
	g.kccb_irq = 0;
	/* job events are cleared when their job is finished */
	ev &= ~(EVENT_TA_FINISHED | EVENT_PIXELBE_END_RENDER | EVENT_COMPUTE_FINISHED |
		EVENT_PM_3D_MEM_FREE | EVENT_PM_OUT_OF_MEMORY);
	if (ev)
		reg_write(CR_EVENT_CLEAR, ev);
	sched_irq();
	mts_task_done(MTS_TASK_DONE_IRQ);
}

static void fw_timer(void)
{
	mtc0(C0_COMPARE, 0, mfc0(C0_COUNT, 0) + TIMER_PERIOD);
	g.kccb_irq = 0;
	kccb_process();		/* safety net for a lost MTS kick */
	sched_run();
	if (g.halt)
		halt();
}

/* Reload a TLB entry whose page was mapped after the entry was loaded. */
static KSEG0_TEXT int tlb_fixup(u32 exc, u32 va)
{
	u32 pair = va & ~0x1FFFu, idx, pte0, pte1, pte;

	if (va - FW_HEAP_BASE >= FW_HEAP_SIZE)
		return 0;
	pte0 = pte_ptr(pair)[0];
	pte1 = pte_ptr(pair)[1];
	pte = (va & 0x1000) ? pte1 : pte0;
	if (!(pte & PTE_VALID) || (exc == EXC_MOD && !(pte & ENTRYLO_D)))
		return 0;
	if (va == g.fault_va && ++g.fault_count > 4)
		return 0;
	if (va != g.fault_va) {
		g.fault_va = va;
		g.fault_count = 0;
	}

	mtc0(C0_ENTRYHI, 0, pair);
	tlbp();
	idx = mfc0(C0_INDEX, 0);
	if (idx & 0x80000000u)
		idx = mfc0(C0_RANDOM, 0);
	tlb_load_pair(idx, pair, pte0, pte1);
	return 1;
}

static void fw_exception(struct frame *f)
{
	u32 cause = mfc0(C0_CAUSE, 0), bad = mfc0(C0_BADVADDR, 0);
	u32 exc = CAUSE_EXC(cause);

	if ((exc == EXC_MOD || exc == EXC_TLBL || exc == EXC_TLBS) && tlb_fixup(exc, bad))
		return;
	if (exc == EXC_MOD || exc == EXC_TLBL || exc == EXC_TLBS) {
		u32 pte0 = 0, pte1 = 0;

		if (bad - FW_HEAP_BASE < FW_HEAP_SIZE) {
			pte0 = pte_ptr(bad & ~0x1FFFu)[0];
			pte1 = pte_ptr(bad & ~0x1FFFu)[1];
		}
		TRACE_FORCE(SF_OPENFW_PAGE_FAULT, bad, pte0, pte1);
	}
	TRACE_FORCE(SF_OPENFW_DBG_HEX4, cause, f->epc, bad, f->status);
	halt();
}

void openfw_trap(u32 id, struct frame *f)
{
	switch (id) {
	case TRAP_EXCEPTION:
		fw_exception(f);
		break;
	case TRAP_TIMER:
		fw_timer();
		break;
	case TRAP_BG:
		fw_bg_task();
		break;
	case TRAP_IRQ:
		fw_irq_task();
		break;
	}
}

/* ------------------------------------------------------------------------
 * Initialisation
 */

/*
 * GPU-side setup, mirroring the register writes the reference firmware
 * makes at boot (docs/firmware.md): fault page, event routing to the MTS
 * and data-master to thread association.
 */
static void gpu_init(void)
{
	u64 fault = (u64)FW32(FW_SYSINIT + OFF_SYSINIT_FAULT_PHYS_ADDR + 4) << 32 |
		    FW32(FW_SYSINIT + OFF_SYSINIT_FAULT_PHYS_ADDR);

	reg_write64(CR_BIF_FAULT_READ, fault);
	reg_write(CR_EVENT_CLEAR, EVENT_GPIO_REQ | EVENT_GPIO_ACK);
	reg_write(CR_MTS_UNNAMED_B90, 0x1000);
	reg_write(CR_MTS_DM0_INTERRUPT_ENABLE,
		  reg_read(CR_MTS_DM0_INTERRUPT_ENABLE) | EVENT_GPIO_REQ);
	reg_write(CR_EVENT_CLEAR, 0x41FCu);
	/* Events that start the MTS interrupt task, per data master:
	 * DM0 (GP) slave request, USC trigger, GPIO, MMU fault; DM2 (geometry)
	 * TA finished, PM out of memory; DM3 (3D) end of render; DM4 compute. */
	reg_write(CR_MTS_DM0_INTERRUPT_ENABLE,
		  EVENT_SLAVE_REQ | EVENT_USC_TRIGGER | EVENT_GPIO_REQ | EVENT_MMU_PAGE_FAULT);
	reg_write(CR_MTS_DM2_INTERRUPT_ENABLE, EVENT_TA_FINISHED | EVENT_PM_OUT_OF_MEMORY);
	reg_write(CR_MTS_DM3_INTERRUPT_ENABLE, EVENT_PIXELBE_END_RENDER);
	reg_write(CR_MTS_DM4_INTERRUPT_ENABLE, EVENT_COMPUTE_FINISHED);
	reg_write(CR_EVENT_ENABLE, 0);
	/* All interrupt tasks and DM0's background task run on thread 0. */
	reg_write(CR_MTS_INTCTX_THREAD0_DM_ASSOC, 0xFFFF);
	reg_write(CR_MTS_BGCTX_THREAD0_DM_ASSOC, 1);
}

void __attribute__((noreturn)) fw_main(void)
{
	u32 osinit = FW_OSINIT, sysinit = FW_SYSINIT;

	reg_write(CR_XPU_BROADCAST, 1);
	(void)reg_read(CR_XPU_BROADCAST);
	reg_write(CR_MIPS_WRAPPER_IRQ_ENABLE, 1);

	g.kccb_ctl = FW32(osinit + OFF_OSINIT_KERNEL_CCBCTL_FW_ADDR);
	g.kccb = FW32(osinit + OFF_OSINIT_KERNEL_CCB_FW_ADDR);
	g.kccb_rtn = FW32(osinit + OFF_OSINIT_KERNEL_CCB_RTN_SLOTS_FW_ADDR);
	g.fwccb_ctl = FW32(osinit + OFF_OSINIT_FIRMWARE_CCBCTL_FW_ADDR);
	g.fwccb = FW32(osinit + OFF_OSINIT_FIRMWARE_CCB_FW_ADDR);
	g.osdata = FW32(osinit + OFF_OSINIT_FW_OS_DATA_FW_ADDR);
	g.power_sync = FW32(g.osdata + OFF_OSDATA_POWER_SYNC_FW_ADDR);
	g.sysdata = FW32(sysinit + OFF_SYSINIT_FW_SYS_DATA_FW_ADDR);
	g.tracebuf_ctl = FW32(sysinit + OFF_SYSINIT_TRACE_BUF_CTL_FW_ADDR);
	g.dusts = 1;

	gpu_init();

	FW32(FW_CONN_CTL + OFF_CONNECTION_CTL_CONNECTION_FW_STATE) = CONNECTION_FW_ACTIVE;
	set_pow_state(POW_ON);

	TRACE(SF_OPENFW_BOOT, FW32(g.sysdata + OFF_SYSDATA_CONFIG_FLAGS), 0);
	TRACE(SF_OPENFW_OS_INIT, 0, FW32(g.osdata + OFF_OSDATA_FW_OS_CONFIG_FLAGS));
	TRACE(SF_OPENFW_CLOCK, FW32(sysinit + OFF_SYSINIT_INITIAL_CORE_CLOCK_SPEED));
	TRACE(SF_OPENFW_GPU_INIT);

	mtc0(C0_COUNT, 0, 0);
	mtc0(C0_COMPARE, 0, TIMER_PERIOD);

	mips_sync();
	FW32(sysinit + OFF_SYSINIT_FIRMWARE_STARTED) = 1;

	irq_enable();
	for (;;)
		mips_wait();
}

/* In case the compiler emits calls for block moves. */
void *memset(void *d, int c, unsigned int n)
{
	unsigned char *p = d;

	while (n--)
		*p++ = (unsigned char)c;
	return d;
}

void *memcpy(void *d, const void *s, unsigned int n)
{
	unsigned char *p = d;
	const unsigned char *q = s;

	while (n--)
		*p++ = *q++;
	return d;
}

/* SPDX-License-Identifier: MIT */
/*
 * GPU and MIPS-wrapper registers used by openfw, as offsets from the
 * firmware's register window. Names and fields follow the Linux
 * drm/imagination pvr_rogue_cr_defs.h; registers that file does not name
 * are marked "unnamed" and documented by what the reference firmware
 * does with them (docs/firmware.md, tools/fwemu).
 */
#ifndef OPENFW_REGS_H
#define OPENFW_REGS_H

#include "mips.h"	/* REG_WINDOW: wired 4 MiB mapping of the register bank */

#define CR_CLK_CTRL		0x0000u
#define CR_CORE_ID_PBVNC	0x0020u		/* 64-bit */
#define CR_POWER_EVENT		0x0038u		/* not in the Rogue defs; see docs/power.md */
#define CR_EVENT_ENABLE		0x0128u		/* DDK name RGX_CR_EVENT_ENABLE */
#define CR_EVENT_STATUS		0x0130u
#define CR_EVENT_CLEAR		0x0138u
#define CR_TIMER		0x0160u		/* 64-bit, used for trace timestamps */
#define CR_MIPS_ADDR_REMAP_RANGE_CONFIG	0x0878u	/* 64-bit, written lo then hi */
#define CR_XPU_BROADCAST	0x0890u
#define CR_MIPS_WRAPPER_IRQ_ENABLE	0x08A0u
#define CR_MIPS_WRAPPER_IRQ_STATUS	0x08A8u
#define CR_MTS_SCHEDULE		0x0B00u
#define CR_MTS_TASK_DONE	0x0B08u		/* unnamed: written at the end of each MTS task */
#define CR_MTS_UNNAMED_B90	0x0B90u		/* unnamed: 0x1000 at init ("GPIO enabled") */
#define CR_MTS_BGCTX_THREAD0_DM_ASSOC	0x0B30u
#define CR_MTS_INTCTX_THREAD0_DM_ASSOC	0x0B40u
#define CR_MTS_DM0_INTERRUPT_ENABLE	0x0B58u
#define CR_MTS_DM2_INTERRUPT_ENABLE	0x0B68u
#define CR_MTS_DM3_INTERRUPT_ENABLE	0x0B70u
#define CR_MTS_DM4_INTERRUPT_ENABLE	0x0B78u
#define CR_BIF_CTRL_INVAL	0x12A0u
#define CR_BIF_FAULT_READ	0x13E0u		/* 64-bit */
#define CR_SLC_CTRL_FLUSH_INVAL	0x3818u
#define CR_SLC_STATUS0		0x3820u
#define CR_MULTICORE_SYSTEM	0xF308u

/* EVENT_STATUS / EVENT_CLEAR / MTS_DMn_INTERRUPT_ENABLE bits */
#define EVENT_SLAVE_REQ		(1u << 19)
#define EVENT_USC_TRIGGER	(1u << 15)
#define EVENT_ZLS_FINISHED	(1u << 14)
#define EVENT_GPIO_ACK		(1u << 13)
#define EVENT_GPIO_REQ		(1u << 12)
#define EVENT_POWER_ABORT	(1u << 11)
#define EVENT_POWER_COMPLETE	(1u << 10)
#define EVENT_MMU_PAGE_FAULT	(1u << 9)
#define EVENT_PM_OUT_OF_MEMORY	(1u << 7)
#define EVENT_TA_TERMINATE	(1u << 6)
#define EVENT_TA_FINISHED	(1u << 5)
#define EVENT_ISP_END_MACROTILE	(1u << 4)
#define EVENT_PIXELBE_END_RENDER (1u << 3)
#define EVENT_COMPUTE_FINISHED	(1u << 2)

/* MTS_TASK_DONE values (from the reference firmware) */
#define MTS_TASK_DONE_BG	0u
#define MTS_TASK_DONE_IRQ	2u

/* MIPS_ADDR_REMAP_RANGE_CONFIG */
#define REMAP_ENABLE		1u
#define REMAP_ENTRY(n)		((n) << 1)	/* 32 entries */
#define REMAP_REGION_4KB	(0u << 7)
#define REMAP_REGION_4MB	(5u << 7)
#define REMAP_ADDR_OUT_HI(pa)	((unsigned int)((pa) >> 8))	/* bits 63:36 = PA >> 12 */

#define BIF_CTRL_INVAL_PT	(1u << 0)
#define BIF_CTRL_INVAL_PD	(1u << 1)
#define BIF_CTRL_INVAL_PC	(1u << 2)
#define BIF_CTRL_INVAL_TLB1	(1u << 3)

#define SLC_FLUSH_INVAL_DM_MMU	(1u << 4)
#define SLC_STATUS0_PENDING	0x7u

static inline __attribute__((always_inline)) volatile unsigned int *reg_ptr(unsigned int off)
{
	return (volatile unsigned int *)(REG_WINDOW + off);
}

static inline __attribute__((always_inline)) unsigned int reg_read(unsigned int off)
{
	return *reg_ptr(off);
}

static inline __attribute__((always_inline)) void reg_write(unsigned int off, unsigned int val)
{
	*reg_ptr(off) = val;
}

static inline __attribute__((always_inline)) void reg_write64(unsigned int off, unsigned long long val)
{
	reg_write(off, (unsigned int)val);
	reg_write(off + 4, (unsigned int)(val >> 32));
}

#endif

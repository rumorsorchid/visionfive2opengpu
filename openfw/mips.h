/* SPDX-License-Identifier: MIT */
/*
 * MIPS32r2 (microAptiv, microMIPS) helpers and the firmware memory map
 * fixed by the Linux driver (pvr_rogue_mips.h, pvr_fw_mips.c).
 */
#ifndef OPENFW_MIPS_H
#define OPENFW_MIPS_H

/* Firmware virtual memory map */
#define FW_HEAP_BASE		0xC0000000	/* 16 MiB, identity mapped via the TLB */
#define FW_HEAP_SIZE		0x01000000
#define FW_CONN_CTL		0xC0FD0000	/* config heap: top 192 KiB of the heap */
#define FW_OSINIT		0xC0FE0000
#define FW_SYSINIT		0xC0FF0000
#define FW_PRIVATE_DATA		0xC0032000	/* MIPS_PRIVATE_DATA layout entry */
#define FW_PT_VIRT		0xCF000000	/* 4 page-table pages */
#define FW_STACK_VIRT		0xCF600000
#define FW_STACK_SIZE		0x1000
#define FW_BOOT_DATA		0xBFC01000	/* kseg1 view of the boot data page */
#define FW_EBASE		0x9FC02000	/* kseg0 view of the exceptions page */
#define REG_WINDOW		0xCF800000	/* GPU registers (MIPS_WRAPPER_CONFIG.REGBANK) */
#define GPUMEM_WINDOW		0xC0ED0000	/* first page of the heap area the kernel
						 * reserves for firmware mappings */
#define GPUMEM_TLB		5		/* its wired TLB entry (remap entry 5) */
#define REG_WINDOW_SIZE		0x00400000

/* MIPS page-table entries as written by pvr_vm_mips.c */
#define PTE_PFN_SHIFT		6
#define PTE_PFN_MASK		0x3FFFFFC0	/* 36-bit physical bus */
#define PTE_VALID		(1 << 1)

/* EntryLo flags for the wired mappings */
#define ENTRYLO_G		(1 << 0)
#define ENTRYLO_V		(1 << 1)
#define ENTRYLO_D		(1 << 2)
#define ENTRYLO_C(c)		((c) << 3)
#define ENTRYLO_XI		(1 << 30)
#define CACHE_UNCACHED		2
#define CACHE_WRITEBACK		3

/* PageMask values (microAptiv supports 1 KiB pages, hence MaskX = 0x1800) */
#define PAGEMASK_4K		0x00001800
#define PAGEMASK_4M		0x007FF800

#define TLB_ENTRIES		16
#define WIRED_ENTRIES		6

/* CP0 Status */
#define ST_IE			(1 << 0)
#define ST_EXL			(1 << 1)
#define ST_ERL			(1 << 2)
#define ST_IM(n)		(1 << (8 + (n)))
#define ST_NMI			(1 << 19)
#define ST_BEV			(1 << 22)

/* CP0 Cause */
#define CAUSE_IV		(1 << 23)
#define CAUSE_EXC(c)		(((c) >> 2) & 0x1f)
#define EXC_MOD			1
#define EXC_TLBL		2
#define EXC_TLBS		3

#ifndef __ASSEMBLER__

#define STR(x) #x
#define XSTR(x) STR(x)

#define mfc0(reg, sel) ({ unsigned int __v; \
	__asm__ __volatile__("mfc0 %0, $" XSTR(reg) ", " XSTR(sel) : "=r"(__v)); __v; })
#define mtc0(reg, sel, v) \
	__asm__ __volatile__("mtc0 %0, $" XSTR(reg) ", " XSTR(sel) "\n\tehb" :: "r"(v) : "memory")

#define C0_INDEX	0
#define C0_RANDOM	1
#define C0_ENTRYLO0	2
#define C0_ENTRYLO1	3
#define C0_PAGEMASK	5
#define C0_WIRED	6
#define C0_BADVADDR	8
#define C0_COUNT	9
#define C0_ENTRYHI	10
#define C0_COMPARE	11
#define C0_STATUS	12
#define C0_CAUSE	13
#define C0_EPC		14
#define C0_EBASE	15	/* sel 1 */
#define C0_CONFIG	16
#define C0_TAGLO	28

static inline __attribute__((always_inline)) void tlbwi(void)
{
	__asm__ __volatile__("ehb\n\ttlbwi\n\tehb" ::: "memory");
}

static inline __attribute__((always_inline)) void tlbp(void)
{
	__asm__ __volatile__("ehb\n\ttlbp\n\tehb" ::: "memory");
}

static inline __attribute__((always_inline)) void mips_sync(void)
{
	__asm__ __volatile__("sync" ::: "memory");
}

static inline __attribute__((always_inline)) void mips_wait(void)
{
	__asm__ __volatile__("wait" ::: "memory");
}

static inline __attribute__((always_inline)) void irq_disable(void)
{
	__asm__ __volatile__("di\n\tehb" ::: "memory");
}

static inline __attribute__((always_inline)) void irq_enable(void)
{
	__asm__ __volatile__("ei\n\tehb" ::: "memory");
}

#endif /* !__ASSEMBLER__ */

#endif

// SPDX-License-Identifier: MIT
/*
 * Boot-time processor setup. Runs from the uncached boot page
 * (0xBFC00000, wrapper remap 1) on a temporary stack in the boot data
 * page, before anything in kseg2 is mapped, so it must not touch .data,
 * .bss or .rodata: everything here is code and immediates.
 */
#include "fwif.h"
#include "mips.h"
#include "mmu.h"
#include "regs.h"

#define BOOT __attribute__((section(".boot.text"), noinline))

/* MIPS_BOOT_DATA, filled in by the kernel (pvr_fw_mips.c: pvr_mips_init) */
#define BOOT_DATA_U64(off) (*(volatile unsigned long long *)(FW_BOOT_DATA + (off)))
#define BOOT_DATA_U32(off) (*(volatile unsigned int *)(FW_BOOT_DATA + (off)))

/*
 * Initialise the L1 caches by storing zero tags into every line, sized
 * from Config1 (the reference firmware does the same over a fixed 2 KiB).
 * Must run before kseg0 or any cacheable TLB mapping is used.
 */
static BOOT void cache_init(void)
{
	unsigned int c1 = mfc0(C0_CONFIG, 1);
	unsigned int il = (c1 >> 19) & 7, dl = (c1 >> 10) & 7;

	mtc0(C0_TAGLO, 0, 0);
	if (il) {
		unsigned int line = 2u << il;
		unsigned int size = (64u << ((c1 >> 22) & 7)) * (((c1 >> 16) & 7) + 1) * line;

		for (unsigned int a = 0x80000000u; a < 0x80000000u + size; a += line)
			__asm__ __volatile__("cache 0x08, 0(%0)" :: "r"(a));	/* Index Store Tag I */
	}
	if (dl) {
		unsigned int line = 2u << dl;
		unsigned int size = (64u << ((c1 >> 13) & 7)) * (((c1 >> 7) & 7) + 1) * line;

		for (unsigned int a = 0x80000000u; a < 0x80000000u + size; a += line)
			__asm__ __volatile__("cache 0x09, 0(%0)" :: "r"(a));	/* Index Store Tag D */
	}
	__asm__ __volatile__("sync\n\tehb" ::: "memory");
}

static BOOT void boot_halt(void)
{
	for (;;) {
		irq_disable();
		mips_wait();
	}
}

void BOOT boot_setup(void)
{
	unsigned int pte0, pte1, i;

	/* Interrupts off, ERL/BEV cleared, IP2 (timer), IP3 (MTS background
	 * task) and IP4 (MTS interrupt task) unmasked for later. */
	mtc0(C0_STATUS, 0, ST_IM(2) | ST_IM(3) | ST_IM(4));
	mtc0(C0_CAUSE, 0, 0);
	mtc0(C0_COUNT, 0, 0);
	mtc0(C0_COMPARE, 0, 0xFFFFFFFFu);
	/* kseg0 cacheable, write-back */
	mtc0(C0_CONFIG, 0, (mfc0(C0_CONFIG, 0) & ~7u) | CACHE_WRITEBACK);
	mtc0(C0_EBASE, 1, FW_EBASE);
	/* Vectored interrupts, 0x100 bytes per vector (IntCtl.VS = 8) */
	mtc0(C0_CAUSE, 0, CAUSE_IV);
	mtc0(12, 1, (mfc0(12, 1) & ~0x3E0u) | (8u << 5));

	cache_init();

	if (BOOT_DATA_U32(OFF_MIPSFW_BOOT_DATA_PT_LOG2_PAGE_SIZE) != 12 ||
	    BOOT_DATA_U32(OFF_MIPSFW_BOOT_DATA_PT_NUM_PAGES) != 4)
		boot_halt();

	/* 0: register bank, one 4 MiB uncached page (+ write alias at +2 MiB).
	 * The TLB's G bit is the AND of both halves: keep it on the unused,
	 * invalid odd half too. */
	mtc0(C0_PAGEMASK, 0, PAGEMASK_4M);
	tlb_write_index(0, REG_WINDOW,
			entrylo_identity(REG_WINDOW, ENTRYLO_XI | ENTRYLO_C(CACHE_UNCACHED) |
					 ENTRYLO_D | ENTRYLO_V | ENTRYLO_G), ENTRYLO_G);
	remap_set(0, REG_WINDOW, REMAP_REGION_4MB,
		  BOOT_DATA_U64(OFF_MIPSFW_BOOT_DATA_REG_BASE));
	remap_clear(16);	/* no odd page */

	mtc0(C0_PAGEMASK, 0, PAGEMASK_4K);

	/* 1, 2: the four page-table pages. Mapped uncached so that the
	 * refill handler always sees the kernel's latest entries. */
	for (i = 0; i < 2; i++) {
		unsigned int va = FW_PT_VIRT + i * 0x2000;
		unsigned int fl = ENTRYLO_C(CACHE_UNCACHED) | ENTRYLO_D | ENTRYLO_V | ENTRYLO_G;

		tlb_write_index(1 + i, va, entrylo_identity(va, fl),
				entrylo_identity(va + 0x1000, fl));
		remap_set(1 + i, va, REMAP_REGION_4KB,
			  BOOT_DATA_U64(OFF_MIPSFW_BOOT_DATA_PT_PHYS_ADDR + 16 * i));
		remap_set(17 + i, va + 0x1000, REMAP_REGION_4KB,
			  BOOT_DATA_U64(OFF_MIPSFW_BOOT_DATA_PT_PHYS_ADDR + 16 * i + 8));
	}

	/* 3: stack (one page, the odd half stays invalid as a guard) */
	tlb_write_index(3, FW_STACK_VIRT,
			entrylo_identity(FW_STACK_VIRT, ENTRYLO_C(CACHE_WRITEBACK) |
					 ENTRYLO_D | ENTRYLO_V | ENTRYLO_G), ENTRYLO_G);
	remap_set(3, FW_STACK_VIRT, REMAP_REGION_4KB,
		  BOOT_DATA_U64(OFF_MIPSFW_BOOT_DATA_STACK_PHYS_ADDR));
	remap_clear(19);

	/* 4: the first two pages of private data (.bss), from the page table */
	remap_flush();
	pte0 = *pte_ptr(FW_PRIVATE_DATA);
	pte1 = *pte_ptr(FW_PRIVATE_DATA + 0x1000);
	tlb_load_pair(4, FW_PRIVATE_DATA,
		      (pte0 & ~0x3Fu) | ENTRYLO_C(CACHE_WRITEBACK) | ENTRYLO_D | ENTRYLO_V | ENTRYLO_G,
		      (pte1 & ~0x3Fu) | ENTRYLO_C(CACHE_WRITEBACK) | ENTRYLO_D | ENTRYLO_V | ENTRYLO_G);

	mtc0(C0_WIRED, 0, WIRED_ENTRIES);

	/* Invalidate the rest: unique, never-used kseg3 tags, no remap. */
	for (i = WIRED_ENTRIES; i < TLB_ENTRIES; i++) {
		tlb_write_index(i, 0xF0000000u + (i << 13), 0, 0);
		remap_clear(i);
		remap_clear(i + 16);
	}
	remap_flush();
}

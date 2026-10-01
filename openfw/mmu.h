/* SPDX-License-Identifier: MIT */
/*
 * Firmware address translation.
 *
 * The MIPS core's physical address space is 32-bit, the GPU's system bus
 * is 36-bit. Every mapping therefore has two halves:
 *
 *   1. a TLB entry mapping a virtual page to the *same* MIPS physical
 *      address (identity), carrying the access/cache flags, and
 *   2. a MIPS-wrapper remap range (ROGUE_CR_MIPS_ADDR_REMAP_RANGE_CONFIG)
 *      translating that MIPS physical page to the system address.
 *
 * TLB entry i owns remap entries i (even page) and i + 16 (odd page), so
 * replacing a TLB entry also replaces its remap ranges. Entries 0-4 are
 * wired by the boot code; the refill handler (start.S) fills the rest
 * from the page table the kernel maintains (pvr_vm_mips.c). This is the
 * scheme the reference firmware uses (docs/firmware.md).
 */
#ifndef OPENFW_MMU_H
#define OPENFW_MMU_H

#include "mips.h"
#include "regs.h"

#define ALWAYS_INLINE inline __attribute__((always_inline))

static ALWAYS_INLINE void remap_set(unsigned int entry, unsigned int mips_pa,
				    unsigned int region, unsigned long long sys_pa)
{
	reg_write(CR_MIPS_ADDR_REMAP_RANGE_CONFIG,
		  mips_pa | region | REMAP_ENTRY(entry) | REMAP_ENABLE);
	reg_write(CR_MIPS_ADDR_REMAP_RANGE_CONFIG + 4, REMAP_ADDR_OUT_HI(sys_pa));
}

static ALWAYS_INLINE void remap_clear(unsigned int entry)
{
	reg_write(CR_MIPS_ADDR_REMAP_RANGE_CONFIG, REMAP_ENTRY(entry));
	reg_write(CR_MIPS_ADDR_REMAP_RANGE_CONFIG + 4, 0);
}

/* Make posted remap writes take effect before the TLB entry is used. */
static ALWAYS_INLINE void remap_flush(void)
{
	(void)reg_read(CR_MIPS_ADDR_REMAP_RANGE_CONFIG + 4);
}

static ALWAYS_INLINE unsigned int entrylo_identity(unsigned int va, unsigned int flags)
{
	return ((va >> (12 - PTE_PFN_SHIFT)) & PTE_PFN_MASK) | flags;
}

static ALWAYS_INLINE void tlb_write_index(unsigned int index, unsigned int entryhi,
					  unsigned int lo0, unsigned int lo1)
{
	mtc0(C0_ENTRYHI, 0, entryhi);
	mtc0(C0_ENTRYLO0, 0, lo0);
	mtc0(C0_ENTRYLO1, 0, lo1);
	mtc0(C0_INDEX, 0, index);
	tlbwi();
}

/* System address held in a page-table entry. */
static ALWAYS_INLINE unsigned long long pte_to_pa(unsigned int pte)
{
	return (unsigned long long)(pte & PTE_PFN_MASK) << (12 - PTE_PFN_SHIFT);
}

/*
 * Install TLB entry @index for the 8 KiB-aligned heap pair at @va from
 * its two page-table entries, exactly as the refill handler does.
 */
static ALWAYS_INLINE void tlb_load_pair(unsigned int index, unsigned int va,
					unsigned int pte0, unsigned int pte1)
{
	remap_set(index, va, REMAP_REGION_4KB, pte_to_pa(pte0));
	remap_set(index + 16, va + 0x1000, REMAP_REGION_4KB, pte_to_pa(pte1));
	remap_flush();
	tlb_write_index(index, va,
			(pte0 & ~PTE_PFN_MASK) | entrylo_identity(va, 0),
			(pte1 & ~PTE_PFN_MASK) | entrylo_identity(va + 0x1000, 0));
}

/* Address of the page-table entry for heap address @va. */
static ALWAYS_INLINE volatile unsigned int *pte_ptr(unsigned int va)
{
	return (volatile unsigned int *)(FW_PT_VIRT + (((va - FW_HEAP_BASE) >> 12) << 2));
}

#endif

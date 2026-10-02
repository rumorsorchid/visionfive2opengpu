// SPDX-License-Identifier: MIT
/*
 * Firmware access to GPU virtual memory.
 *
 * A few jobs need the firmware to write into a context's GPU memory: a
 * geometry phase that was cut short (hardware recovery, an abandoned
 * multi-kick render) leaves the tail pointer cache and render target cache
 * of its render target dirty, and the next first geometry kick must start
 * from zeroed ones (as Imagination's firmware does, hwrtdata
 * geom_caches_need_zeroing).
 *
 * The context's page tables are walked in system memory (pvr_mmu.c
 * format, 4 KiB device pages on Linux/RISC-V): page catalogue entries are
 * 32-bit (PD address >> 12 in bits 31:4, valid in bit 0), directory and
 * table entries 64-bit (next level / page address in bits 39:12, valid in
 * bit 0; a directory's page size in bits 3:1, a page's read-only flag in
 * bit 1). Each system page is reached through one wired TLB entry in the
 * heap area the kernel reserves for the firmware's own mappings
 * (ROGUE_FW_HEAP_MIPS_RESERVED_SIZE), mapped uncached and pointed at the
 * page by its MIPS-wrapper remap range.
 */
#include "fw.h"
#include "mmu.h"

#define PC_INDEX(va)	((u32)((va) >> 30) & 0x3FFu)
#define PD_INDEX(va)	((u32)((va) >> 21) & 0x1FFu)
#define PT_INDEX(va)	((u32)((va) >> 12) & 0x1FFu)
#define ENTRY_ADDR	0xFFFFFFF000ull		/* bits 39:12 */
#define PD_PAGE_SIZE	0xEu			/* 0: 4 KiB pages */
#define PT_READ_ONLY	0x2u

/* Point the window at the system page holding @pa; returns its address. */
static volatile u32 *win(u64 pa)
{
	mips_sync();				/* earlier window writes done */
	remap_set(GPUMEM_TLB, GPUMEM_WINDOW, REMAP_REGION_4KB, pa & ~0xFFFull);
	remap_flush();
	return (volatile u32 *)(GPUMEM_WINDOW + ((u32)pa & 0xFFFu));
}

static u64 win_read64(u64 pa)
{
	volatile u32 *p = win(pa);
	u32 lo = p[0];

	return (u64)p[1] << 32 | lo;
}

/*
 * System address of the writable page holding GPU virtual address @va in
 * the address space whose page catalogue is at @pc; 0 when it is not
 * mapped, read-only or not a 4 KiB page.
 */
static u64 gpu_va_to_pa(u64 pc, u64 va)
{
	u32 pce = *win(pc + 4 * PC_INDEX(va));
	u64 pde, pte;

	if (!(pce & 1))
		return 0;
	pde = win_read64(((u64)(pce & ~0xFu) << 8) + 8 * PD_INDEX(va));
	if (!(pde & 1) || (pde & PD_PAGE_SIZE))
		return 0;
	pte = win_read64((pde & ENTRY_ADDR) + 8 * PT_INDEX(va));
	if (!(pte & 1) || (pte & PT_READ_ONLY))
		return 0;
	return (pte & ENTRY_ADDR) | (va & 0xFFF);
}

/* Zero @size bytes (a multiple of 4) of GPU memory at @va. */
void gpu_mem_zero(u64 pc, u64 va, u32 size)
{
	while (size) {
		u32 n = 0x1000 - ((u32)va & 0xFFFu);
		u64 pa;

		if (n > size)
			n = size;
		pa = gpu_va_to_pa(pc, va);
		if (pa) {
			volatile u32 *w = win(pa);

			for (u32 i = 0; i < n / 4; i++)
				w[i] = 0;
			mips_sync();
			(void)w[0];		/* the writes have reached memory */
		}
		va += n;
		size -= n;
	}
	remap_clear(GPUMEM_TLB);
	remap_flush();
}

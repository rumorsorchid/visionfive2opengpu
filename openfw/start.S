/* SPDX-License-Identifier: MIT */
/*
 * Reset entry, TLB refill handler and exception/interrupt vectors
 * (microMIPS, MIPS32r2).
 *
 * Vectors, at EBase = 0x9FC02000 (kseg0 view of the exceptions page that
 * the kernel remaps with MIPS_ADDR_REMAP3):
 *   +0x000  TLB refill
 *   +0x180  general exception
 *   +0x400  IP2: CP0 timer
 *   +0x500  IP3: MTS background task (kernel CCB kicks)
 *   +0x600  IP4: MTS interrupt task (GPU events)
 */
#include "mips.h"

#define TRAP_EXCEPTION	0
#define TRAP_TIMER	1
#define TRAP_BG		2
#define TRAP_IRQ	3

#define FRAME_SIZE	144
#define F_GPR(n)	n*4		/* $1..$31 at 4..124 */
#define F_EPC		128
#define F_STATUS	132
#define F_HI		136
#define F_LO		140

	.set	micromips
	.set	noreorder
	.set	noat

/* ---------------------------------------------------------------------- */
	.section .boot.entry, "ax"
	.globl	_reset
	.type	_reset, @function
_reset:
	mfc0	$k0, $12
	ext	$k0, $k0, 19, 1		/* Status.NMI: not a cold reset */
	bnez	$k0, 9f
	nop
	li	$sp, 0xBFC01FF0		/* top of the boot data page */
	la	$t9, boot_setup
	jalr	$t9
	nop

	/* Firmware stack (wired by boot_setup) */
	li	$sp, (FW_STACK_VIRT + FW_STACK_SIZE - 16)

	/* .bss is reused across GPU power cycles: clear it on every boot */
	la	$t0, __bss_start
	la	$t1, __bss_end
1:	beq	$t0, $t1, 2f
	nop
	sw	$zero, 0($t0)
	b	1b
	addiu	$t0, $t0, 4
2:
	la	$t9, fw_main
	jr.hb	$t9
	nop

9:	di
	wait
	b	9b
	nop
	.size	_reset, . - _reset

/* ---------------------------------------------------------------------- */
	.section .exc, "ax"
	.globl	_exc_base
_exc_base:

/*
 * TLB refill. Identity-maps the faulting heap page pair with the flags of
 * its page-table entries and points remap entries Random / Random + 16 at
 * the system pages. Uses k0, k1 and two words below the interrupted
 * context's sp (always the wired stack).
 */
tlb_refill:
	sw	$t0, -4($sp)
	sw	$t1, -8($sp)
	mfc0	$k0, $10			/* EntryHi = VPN2 of the faulting address */
	srl	$k1, $k0, 24
	li	$t0, (FW_HEAP_BASE >> 24)
	bne	$k1, $t0, refill_bad
	nop
	ext	$k1, $k0, 10, 14		/* byte offset of the even page's PTE */
	li	$t0, FW_PT_VIRT
	addu	$k1, $k1, $t0
	mfc0	$t0, $1			/* Random */
	mtc0	$t0, $0			/* Index */

	srl	$k0, $k0, 13
	sll	$k0, $k0, 13		/* even page address */
	sll	$t0, $t0, 1
	or	$k0, $k0, $t0		/* remap ENTRY = Random */
	ori	$k0, $k0, 1		/* MODE_ENABLE */
	li	$t1, REG_WINDOW
	sw	$k0, 0x878($t1)
	lw	$t0, 0($k1)
	ext	$t0, $t0, 6, 24		/* PFN */
	sll	$t0, $t0, 4
	sw	$t0, 0x87C($t1)
	addiu	$k0, $k0, 0x1020		/* odd page, ENTRY + 16 */
	sw	$k0, 0x878($t1)
	lw	$t0, 4($k1)
	ext	$t0, $t0, 6, 24
	sll	$t0, $t0, 4
	sw	$t0, 0x87C($t1)
	lw	$t0, 0x87C($t1)		/* remap live before the TLB entry */

	mfc0	$k0, $10
	srl	$k0, $k0, 12		/* VPN of the even page */
	lw	$t0, 0($k1)
	ins	$t0, $k0, 6, 24		/* identity PFN, PTE flags */
	mtc0	$t0, $2
	addiu	$k0, $k0, 1
	lw	$t0, 4($k1)
	ins	$t0, $k0, 6, 24
	mtc0	$t0, $3
	ehb
	tlbwi
	lw	$t0, -4($sp)
	lw	$t1, -8($sp)
	eret

refill_bad:				/* not a heap address: report and halt */
	lw	$t0, -4($sp)
	lw	$t1, -8($sp)
	b	general_exception
	nop

	.org	0x180
general_exception:
	li	$k0, TRAP_EXCEPTION
	b	save_and_call
	nop

	.org	0x400
	li	$k0, TRAP_TIMER
	b	save_and_call
	nop

	.org	0x500
	li	$k0, TRAP_BG
	b	save_and_call
	nop

	.org	0x600
	li	$k0, TRAP_IRQ
	b	save_and_call
	nop

/*
 * Save the full context, leave exception level with interrupts disabled
 * (so the C handler may take TLB refills, which need EXL = 0), call
 * openfw_trap(k0, frame), restore and return. Handlers never nest except
 * for TLB refills and fixable TLB exceptions.
 */
	.org	0x700
save_and_call:
	addiu	$k1, $sp, -FRAME_SIZE
	sw	$1, F_GPR(1)($k1)
	sw	$2, F_GPR(2)($k1)
	sw	$3, F_GPR(3)($k1)
	sw	$4, F_GPR(4)($k1)
	sw	$5, F_GPR(5)($k1)
	sw	$6, F_GPR(6)($k1)
	sw	$7, F_GPR(7)($k1)
	sw	$8, F_GPR(8)($k1)
	sw	$9, F_GPR(9)($k1)
	sw	$10, F_GPR(10)($k1)
	sw	$11, F_GPR(11)($k1)
	sw	$12, F_GPR(12)($k1)
	sw	$13, F_GPR(13)($k1)
	sw	$14, F_GPR(14)($k1)
	sw	$15, F_GPR(15)($k1)
	sw	$16, F_GPR(16)($k1)
	sw	$17, F_GPR(17)($k1)
	sw	$18, F_GPR(18)($k1)
	sw	$19, F_GPR(19)($k1)
	sw	$20, F_GPR(20)($k1)
	sw	$21, F_GPR(21)($k1)
	sw	$22, F_GPR(22)($k1)
	sw	$23, F_GPR(23)($k1)
	sw	$24, F_GPR(24)($k1)
	sw	$25, F_GPR(25)($k1)
	sw	$28, F_GPR(28)($k1)
	sw	$29, F_GPR(29)($k1)
	sw	$30, F_GPR(30)($k1)
	sw	$31, F_GPR(31)($k1)
	mfc0	$t0, $14
	sw	$t0, F_EPC($k1)
	mfhi	$t0
	sw	$t0, F_HI($k1)
	mflo	$t0
	sw	$t0, F_LO($k1)
	mfc0	$t0, $12
	sw	$t0, F_STATUS($k1)
	move	$sp, $k1
	move	$a0, $k0			/* k0/k1 are the refill handler's from here on */
	move	$a1, $sp
	li	$t1, ~(ST_EXL | ST_IE)
	and	$t0, $t0, $t1
	mtc0	$t0, $12
	ehb
	la	$t9, openfw_trap
	jalr	$t9
	nop

	di
	ehb
	lw	$t0, F_STATUS($sp)	/* EXL = 1 again */
	mtc0	$t0, $12
	ehb
	lw	$t0, F_EPC($sp)
	mtc0	$t0, $14
	ehb
	lw	$t0, F_HI($sp)
	mthi	$t0
	lw	$t0, F_LO($sp)
	mtlo	$t0
	move	$k1, $sp
	lw	$1, F_GPR(1)($k1)
	lw	$2, F_GPR(2)($k1)
	lw	$3, F_GPR(3)($k1)
	lw	$4, F_GPR(4)($k1)
	lw	$5, F_GPR(5)($k1)
	lw	$6, F_GPR(6)($k1)
	lw	$7, F_GPR(7)($k1)
	lw	$8, F_GPR(8)($k1)
	lw	$9, F_GPR(9)($k1)
	lw	$10, F_GPR(10)($k1)
	lw	$11, F_GPR(11)($k1)
	lw	$12, F_GPR(12)($k1)
	lw	$13, F_GPR(13)($k1)
	lw	$14, F_GPR(14)($k1)
	lw	$15, F_GPR(15)($k1)
	lw	$16, F_GPR(16)($k1)
	lw	$17, F_GPR(17)($k1)
	lw	$18, F_GPR(18)($k1)
	lw	$19, F_GPR(19)($k1)
	lw	$20, F_GPR(20)($k1)
	lw	$21, F_GPR(21)($k1)
	lw	$22, F_GPR(22)($k1)
	lw	$23, F_GPR(23)($k1)
	lw	$24, F_GPR(24)($k1)
	lw	$25, F_GPR(25)($k1)
	lw	$28, F_GPR(28)($k1)
	lw	$30, F_GPR(30)($k1)
	lw	$31, F_GPR(31)($k1)
	lw	$29, F_GPR(29)($k1)
	eret

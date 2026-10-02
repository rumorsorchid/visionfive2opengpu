/* SPDX-License-Identifier: MIT */
/*
 * openfw memory layout. Addresses are fixed by the Linux driver and the
 * firmware container's layout table (tools/pvrfw.py pack):
 *
 *   .exc   0x9FC02000  exceptions page  (MIPS_EXCEPTIONS_CODE, <= 4 KiB):
 *                      vectors + TLB maintenance code (unmapped kseg0)
 *   .boot  0xBFC00000  boot page        (MIPS_BOOT_CODE,       <= 4 KiB)
 *   .text  0xC0000000  code + rodata    (MIPS_CODE; with .exc/.boot it
 *                                        must end below the data at
 *                                        0xC0032000)
 *   .bss   0xC0032000  private data     (MIPS_PRIVATE_DATA, 12 KiB)
 *
 * There is no initialised writable data: the data object survives GPU
 * power cycles while the firmware reboots, so state lives in .bss, which
 * the reset code clears on every boot, or in .persist, which it keeps.
 */
OUTPUT_ARCH(mips)
ENTRY(_reset)

PHDRS
{
	exc  PT_LOAD FLAGS(5);
	boot PT_LOAD FLAGS(5);
	text PT_LOAD FLAGS(5);
	data PT_LOAD FLAGS(6);
}

SECTIONS
{
	.exc 0x9FC02000 : { KEEP(*(.exc)) *(.exc.text) } :exc
	.boot 0xBFC00000 : { KEEP(*(.boot.entry)) *(.boot.text) } :boot
	.text 0xC0000000 : {
		*(.text .text.*)
		*(.rodata .rodata.* .rodata1)
	} :text
	.data 0xC0032000 : {
		__data_start = .;
		*(.data .data.* .sdata .sdata.*)
		__data_end = .;
	} :data
	.bss : {
		. = ALIGN(4);
		__bss_start = .;
		*(.bss .bss.* .sbss .sbss.* COMMON)
		. = ALIGN(4);
		__bss_end = .;
	} :data
	/* not cleared on boot: zero when the kernel loads the firmware,
	 * kept across GPU power cycles (the firmware reboots) */
	.persist (NOLOAD) : {
		. = ALIGN(8);
		*(.persist)
		__persist_end = .;
	} :data

	/DISCARD/ : {
		*(.MIPS.abiflags) *(.reginfo) *(.comment) *(.note*) *(.pdr)
		*(.gnu.attributes) *(.mdebug*) *(.eh_frame*)
	}
}

ASSERT(SIZEOF(.exc) <= 0x1000, "exception code exceeds its 4 KiB page")
ASSERT(SIZEOF(.boot) <= 0x1000, "boot code exceeds its 4 KiB page")
ASSERT(SIZEOF(.text) <= 0x30000, "code would overlap the private data at 0xC0032000")
ASSERT(__data_end == __data_start, "initialised writable data is not supported (use .bss)")
ASSERT(__persist_end <= 0xC0034000, "private data exceeds the 8 KiB the boot code wires")

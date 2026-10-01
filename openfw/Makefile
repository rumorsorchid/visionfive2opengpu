# SPDX-License-Identifier: MIT
#
# openfw: open firmware for the PowerVR BXE-4-32 (JH7110) MIPS firmware
# processor.
#
#   make                         build rogue_36.50.54.182_v1.fw
#   make fwif KERNEL=~/linux     regenerate fwif.h from a kernel tree
#   make emu KERNEL=~/linux      boot it in tools/fwemu
#   make test KERNEL=~/linux     MMU unit test + kernel-contract test
#
CROSS   ?= mipsel-linux-gnu-
CC       = $(CROSS)gcc
OBJDUMP  = $(CROSS)objdump
PYTHON  ?= python3
KERNEL  ?=
BVNC     = 36.50.54.182
VERSION ?= 1.0.0

CFLAGS  = -march=mips32r2 -mmicromips -EL -msoft-float -mno-abicalls -fno-pic \
          -G0 -mno-gpopt -mlong-calls -Os -std=gnu11 -ffreestanding -fno-builtin \
          -fno-jump-tables -fno-stack-protector -fno-asynchronous-unwind-tables \
          -fno-unwind-tables -Wall -Wextra -Werror
ASFLAGS = -march=mips32r2 -mmicromips -EL -msoft-float -mno-abicalls -fno-pic -G0
LDFLAGS = -nostdlib -static -T link.ld -Wl,--build-id=none -Wl,-z,max-page-size=4096 \
          -Wl,-Map,openfw.map

OBJS = start.o boot.o main.o
FW   = rogue_$(BVNC)_v1.fw

all: $(FW)

%.o: %.c fwif.h mips.h mmu.h regs.h
	$(CC) $(CFLAGS) -c -o $@ $<

%.o: %.S mips.h
	$(CC) $(ASFLAGS) -c -o $@ $<

openfw.elf: $(OBJS) link.ld
	@# boot.o runs before kseg2 is mapped: it may not call into or read
	@# from .text/.rodata/.data/.bss
	@$(CROSS)size -A boot.o | awk '$$1 ~ /^\.(text|rodata|data|bss|sdata|sbss)/ && $$2 != 0 \
		{ print "boot.o: non-empty " $$1; bad = 1 } END { exit bad }'
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(OBJS) -lgcc

openfw.dis: openfw.elf
	$(OBJDUMP) -d -M micromips $< > $@

$(FW): openfw.elf device-$(BVNC).json ../tools/pvrfw.py
	$(PYTHON) ../tools/pvrfw.py pack --elf openfw.elf --device device-$(BVNC).json \
		--version $(VERSION) -o $@

fwif:
	@test -n "$(KERNEL)" || { echo "set KERNEL=<linux source tree>"; exit 1; }
	$(PYTHON) gen_fwif.py --layout ../tools/fwemu/layout.json --kernel $(KERNEL) -o fwif.h

emu: $(FW)
	@test -n "$(KERNEL)" || { echo "set KERNEL=<linux source tree>"; exit 1; }
	$(PYTHON) ../tools/fwemu/fwemu.py $(FW) --kernel $(KERNEL) \
		--kccb health --kccb mmucache --kccb logtype --kccb pow-idle --kccb pow-off

REFERENCE ?=

test: $(FW)
	$(PYTHON) test_mmu.py openfw.elf
	@test -n "$(KERNEL)" || { echo "set KERNEL=<linux source tree> for the contract test"; exit 1; }
	$(PYTHON) test_contract.py $(FW) $(REFERENCE) --kernel $(KERNEL)

clean:
	rm -f *.o openfw.elf openfw.map openfw.dis $(FW)

.PHONY: all fwif emu test clean

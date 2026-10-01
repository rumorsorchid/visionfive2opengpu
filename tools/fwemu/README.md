# fwemu — run the PowerVR MIPS firmware without a GPU

`fwemu.py` boots Imagination's `rogue_36.50.54.182_v1.fw` (or any MIPS
Rogue image in the upstream container format) in Unicorn, against the same
memory layout and init structures the Linux `powervr` driver builds, and
logs what the firmware does.

```sh
pip install unicorn pyelftools
sudo apt install binutils-mipsel-linux-gnu      # objdump for microMIPS
# once per kernel version (needs gcc):
python3 extract_layout.py --kernel ~/linux -o layout.json
python3 fwemu.py rogue_36.50.54.182_v1.fw --kernel ~/linux [--trace-regs] [--config-flags 0x10]
```

Output on the v1.0 BXE-4-32 image:

```
executed 6279 instructions; firmware_started=1
connection: fw_state=2 os_state=0 alive_fw_token=0
firmware trace:
  [198] OSid 0 fw state transition request: from 2 to 0 ... Status 1 (1-ok 0-fail)
  [212] OSid 0 CCB init status: 1 (1-ok 0-fail): kCCBCtl@0xc0100000 kCCB@0xc0101000 ...
  [245] Initialised Firmware with config flags 0x00000000 and extended config flags 0x00000000
  [282] Initialised OS 0 with config flags 0x00000000
  [305] Core clock set to 409600000 Hz
  [313] Active PM latency set to 0ms. Core clock: 409600000 Hz
  [325] GPU init
  [332] GPIO enabled
  [382] Host Sync Partition marker: 1
```

This is the same sequence the firmware logs on a real VisionFive 2
(compare `trace_capture_2026-08-26` in domibel/visionfive2_gpu_bringup), so
the boot path is reproduced faithfully.

## How it works

| Piece | Source of truth |
|---|---|
| Struct layouts (`layout.json`) | kernel `pvr_rogue_fwif*.h` compiled on the host against `shim/`; the kernel's own `OFFSET_CHECK`/`SIZE_CHECK` asserts run during that compile |
| Init structures | `pvr_fw.c` (`fw_sysinit_init`, `fw_osinit_init`, ...) |
| Boot data, remaps | `pvr_fw_mips.c` (`pvr_mips_init`, `pvr_mips_wrapper_init`) |
| Page table entries | `pvr_vm_mips.c` |
| Trace decoding (`fwtrace.py`) | `pvr_rogue_fwif_sf.h` format strings |

Firmware memory model learnt along the way:

* The MIPS wrapper decodes the register bank at **physical** `0xCF800000`
  (`MIPS_WRAPPER_CONFIG.REGBANK = 0xCF80`), not at the SoC address.
* The boot loader programs wrapper *remap ranges* for the page table
  (`0xCF000000`), stack (`0xCF600000`) and private data, from boot data.
* The TLB refill handler (`0x9FC02000`) maps each heap page identity
  (VA = MIPS PA) and programs a remap range to the system address from the
  page table. fwemu reproduces the result with a fixed-mapping MMU.

## Emulator workarounds (not firmware issues)

Two Unicorn/QEMU microMIPS bugs had to be worked around; both are worth
reporting upstream:

1. **`jal`/`j` region**: microMIPS jump targets keep PC bits 31:27, QEMU
   keeps 31:28, so jumps from `0xBFC0xxxx` land in `0xB7C0xxxx`. Fixed by
   aliasing those physical pages.
2. **`swm` stores 16 bits per register** (both SWM16 and SWM32; `lwm`,
   `swp`, `lwp` are fine). Fixed by rewriting the stored words after each
   `swm` executes.

Also: TLB refills are not delivered to the guest, so the M14K fixed-mapping
MMU is used and the firmware's six TLB instructions are replaced with nops
in emulator memory.

## Next steps

* Deliver kernel CCB commands (health check, MMU cache ops, power
  requests) by modelling the MTS kick interrupt, then a compute kick. That
  turns this into the differential test bench for an open firmware
  (`docs/firmware.md`).
* Use it to watch what the firmware does with `POW_RASCALDUST` when work
  arrives (the open question in `docs/power.md`).

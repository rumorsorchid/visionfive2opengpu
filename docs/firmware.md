# The BXE-4-32 firmware: what it is, what it does, and what "fully open" would take

## Two different firmwares

| | DDK firmware | Open-source-driver firmware |
|---|---|---|
| File | `rgx.fw.36.50.54.182` (+ `rgx.sh.*`) | `powervr/rogue_36.50.54.182_v1.fw` |
| Used by | StarFive's proprietary `pvrsrvkm` | upstream `drm/imagination` |
| Host interface | DDK 1.19 FWIF | the FWIF in `pvr_rogue_fwif*.h` (MIT) |
| Licence | proprietary, shipped by StarFive | redistributable binary from Imagination |

They are not interchangeable. Everything below is about the second one.

## Container format

Decoded by `tools/pvrfw.py` (format from `pvr_fw_info.h`):

```
ELF (MIPS32r2, little endian, microMIPS ASE, entry 0xbfc00001)
device info  : BRN/ERN/feature bitmasks + feature values
info header  : version 3, BVNC, FW version, flags (OPEN_SOURCE)
layout table : where each section lives in the FW address space
```

| Section | FW address | Size (alloc) |
|---|---|---|
| `MIPS_BOOT_CODE` | `0xbfc00000` (kseg1, remapped by the MIPS wrapper) | 4 KiB |
| `MIPS_BOOT_DATA` | `0xbfc01000` | 4 KiB |
| `MIPS_EXCEPTIONS_CODE` | `0x9fc02000` | 4 KiB |
| `MIPS_CODE` | `0xc0000000` (kseg2, TLB-mapped) | 92 KiB (93 568 B used) |
| `MIPS_PRIVATE_DATA` | `0xc0032000` | 12 KiB (10 720 B used) |
| `MIPS_STACK` | `0xcf600000` | 4 KiB |

Other fixed windows: page table at `0xcf000000`, GPU registers at
`0xcf800000`, and a write-only alias of the registers at `0xcfa00000`.

## The processor

A MIPS microAptiv-class core inside the GPU (16-byte cache lines, 16 TLB
entries), booting in **microMIPS** mode. Practical consequences:

* Disassemble with `mipsel-linux-gnu-objdump -b binary -m mips:micromips -EL`;
  Capstone 5's microMIPS decoder misses CP0 instructions.
* GCC (`mipsel-linux-gnu-gcc -march=mips32r2 -mmicromips`) can target it,
  so an open firmware needs no exotic toolchain.
* Unicorn 2 (QEMU M14Kc model) executes the boot code, with one QEMU bug
  to work around: microMIPS `jal` targets the 128 MiB region given by PC
  bits 31:27, but QEMU masks with 31:28, so jumps from `0xbfc0xxxx` land
  in `0xb7c0xxxx`. Aliasing those physical pages fixes it.

## What it does (static analysis)

The assert strings name ten source files: `rgxfw_init.c`,
`rgxfw_kernel_ccb.c`, `rgxfw_client_ccb.c`, `rgxfw_irq.c`,
`rgxfw_gpu_memory.c`, `mips/rgxfw_utils_mips.c`, `rogue/rgxfw_gpu_init.c`,
`rogue/rgxfw_bif.c`, `rogue/rgxfw_hwr.c`, `rogue/rgxfw_pm.c`.

`tools/fwregs.py` reconstructs the register traffic from the code:

* 878 accesses: 72 distinct offsets through the normal window, plus 374
  **writes only** through the `0xcfa00000` alias (211 offsets; event
  clears, SLC, BIF, ISP, MTS, ...).
* Most frequent: `TIMER` (80 reads, timekeeping), `XPU_BROADCAST`,
  `CORE_ID__PBVNC`, `MTS_SCHEDULE` (36 writes: kicking work onto data
  masters), `MULTICORE_SYSTEM`, `EVENT_STATUS`.
* It programs the **rascal/dust power island itself** through
  `POWER_EVENT` (7 write sites, routine at `0xc000a690`) and tests the
  `POW_RASCALDUST` config bit at 15 sites. That is what led to the
  `rd_power_island` experiment, see [power.md](power.md).

## A fully open firmware: honest assessment

**What is already open:** the complete host interface (structures, command
formats, trace format strings, register definitions) is MIT-licensed in
the upstream kernel, and the DDK kernel source documents the boot, power
and recovery protocol from the host side. The firmware's job is bounded:
take commands from the kernel CCB and per-context client CCBs, check and
update fences (UFOs), program and kick the geometry/fragment/compute/
transfer data masters, take their interrupts, handle parameter-buffer
out-of-memory and partial renders, manage power and do hardware recovery.

**What is not:** the exact hardware sequencing the firmware performs. The
register *names* are known; the *order, timing and values* for the
parameter manager, context switching, power islands and recovery are not
documented anywhere public. The binary is ~29 000 microMIPS instructions.

**Effort:** realistically many months of reverse engineering and testing
for a single core, even with the interface given. A project to write one
would be the first open PowerVR Rogue firmware.

### A sensible path

1. **Tooling** (started here): image parser/diff (`pvrfw.py`), register
   census (`fwregs.py`). Next: an emulator harness that boots the real
   firmware against a modelled register file and the kernel's real init
   structures, logging every register access. That gives an executable
   specification and a differential test bench for a replacement.
2. **M0 – boot handshake.** An open image in the same container format
   that boots, reports its OS state as active and answers health checks.
   The upstream driver then probes and stays up with no jobs submitted.
3. **M1 – kernel CCB.** MMU cache invalidation, cleanup requests, power
   requests.
4. **M2 – compute/transfer.** Single data master, UFO fence check/update,
   completion interrupt. First real jobs (Vulkan compute, blits).
5. **M3 – geometry + fragment.** Parameter manager, free lists, partial
   renders. This is the hard core of the work.
6. **M4 – power and recovery.** Idle power-down, rascal/dust island, HWR.

Each milestone can be validated on the board with the same kernel and
Mesa, comparing behaviour and register traces against Imagination's
firmware.

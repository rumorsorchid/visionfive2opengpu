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
* Unicorn 2 runs it. `tools/fwemu` uses the fixed-mapping M14K model
  (Unicorn never delivers TLB refills to the guest); `openfw/test_mmu.py`
  uses the TLB-equipped M14Kc model to run TLB code directly. QEMU bugs to
  work around: microMIPS `jal` targets the 128 MiB region given by PC bits
  31:27, but QEMU masks with 31:28 (alias the pages); `swm` stores only 16
  bits per register; code rewritten at the same address needs
  `ctl_remove_cache`.

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

## Runtime protocol (from tracing the reference in fwemu)

* **Memory.** Boot code wires five TLB entries (register bank as one 4 MiB
  page, the four page-table pages, the stack, the first two private-data
  pages), sets `Wired = 5` and invalidates the rest. Every other access
  goes through the TLB refill handler, which maps the page pair *identity*
  (virtual = MIPS physical) with the flags of the kernel's page-table
  entries and points two wrapper remap ranges (`Random`, `Random + 16`) at
  the system pages. `MMUCACHE` and `LOGTYPE_UPDATE` flush the non-wired
  TLB entries and their remap ranges.
* **Tasks.** Vectored interrupts at EBase `0x9FC02000`: IP2 CP0 timer
  (`0x400`), IP3 MTS background task (`0x500`, kernel CCB), IP4 MTS
  interrupt task (`0x600`, GPU events). A task ends by writing the unnamed
  register `0xB08` (`0` background, `2` interrupt task) and reading it
  back. The firmware queues its own tasks through `MTS_SCHEDULE`
  (`0x20` = interrupt task, `0x0` = background task); the background
  self-kick is how forced idle and power-off complete.
* **Host interrupt.** `MIPS_WRAPPER_IRQ_STATUS = 1`, raised after the
  return slot is written.
* **Return slots.** Set (`CMD_EXECUTED`) for the commands the kernel waits
  on (MMU cache, log type, cleanup); left at 0 for health checks and power
  requests, which the kernel tracks through `kccb_cmds_executed` and
  `power_sync`.
* **Power.** Health check → idle timer → `pow_state = IDLE`. Forced idle →
  `FORCED_IDLE`, `power_sync = 1`. Power off → "GPU units deinit / GPU
  deinit", SLC flush of MMU data (`SLC_CTRL_FLUSH_INVAL = 0x10`), page
  catalogue invalidate (`BIF_CTRL_INVAL = 0x4`), `pow_state = OFF`,
  `power_sync = 1`, then `di; wait`. On runtime resume the kernel reboots
  the same image; the kernel CCB continues where it stopped.

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

1. **Tooling** (done here): image parser/diff (`pvrfw.py`), register
   census (`fwregs.py`), and **`tools/fwemu`**, which boots the real
   firmware against a modelled register file and the kernel's own init
   structures. It reaches `firmware_started` with the same init trace as
   real hardware, consumes kernel-CCB commands (health check, power
   requests) and runs a complete **compute job** (kick → "Kick Compute" →
   completion → "Compute finished"). That is the executable specification
   and test bench for M0–M2 of a replacement. Next: geometry/fragment.
2. **M0 – boot handshake** — *done in emulation*: [`openfw/`](../openfw/README.md)
   boots, reports active and answers health checks.
3. **M1 – kernel CCB** — *done in emulation*: MMU cache invalidation,
   cleanup, log type, forced idle, power off, resume. `openfw` and
   Imagination's image both pass the 25-step `test_contract.py`; the TLB
   code passes `test_mmu.py`. Waiting for the first board run
   (`board/openfw-test.sh`).
4. **M2 – compute/transfer.** Single data master, UFO fence check/update,
   completion interrupt. First real jobs (Vulkan compute, blits).
5. **M3 – geometry + fragment.** Parameter manager, free lists, partial
   renders. This is the hard core of the work.
6. **M4 – power and recovery.** Idle power-down, rascal/dust island, HWR.

Each milestone can be validated on the board with the same kernel and
Mesa, comparing behaviour and register traces against Imagination's
firmware.

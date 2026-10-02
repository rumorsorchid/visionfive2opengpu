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
  on (MMU cache, log type, cleanup); left at 0 for kicks, grow updates,
  health checks and power requests, which the kernel tracks through
  `kccb_cmds_executed` and `power_sync`.
* **Power.** Health check → idle timer → `pow_state = IDLE`. Forced idle →
  `FORCED_IDLE`, `power_sync = 1`. Power off → "GPU units deinit / GPU
  deinit", SLC flush of MMU data (`SLC_CTRL_FLUSH_INVAL = 0x10`), page
  catalogue invalidate (`BIF_CTRL_INVAL = 0x4`), `pow_state = OFF`,
  `power_sync = 1`, then `di; wait`. On runtime resume the kernel reboots
  the same image; the kernel CCB continues where it stopped.

## Job execution (from tracing the reference in fwemu)

`tools/fwemu/host.py` builds what the kernel builds for a job —
contexts with their static state, client CCBs, HWRT data sets, free lists,
memory contexts — with the kernel's own formulas; `jobs.py` submits jobs
the way `pvr_queue.c` does and models the GPU (work completes, events are
raised, polls are satisfied, the PM can run out of memory). Every command
field is filled with a tag (`0x5A0nn000`), so each register write of
Imagination's firmware can be traced to the field it came from;
`vary.py` changes one input at a time and `sweep.py` sets fields to
patterns, which separates values the firmware computes from values it
copies. openfw implements the result and `openfw/test_jobs.py` compares
the two firmwares step by step (32 scenarios, 406 field values, all
identical).

**Client CCB.** `KICK` carries the new write offset; the firmware moves
`dep_offset` over every command whose fences hold (`FENCE` and
`FENCE_PR`: `(s32)(*ufo - value) >= 0`, a sync checkpoint when bit 0 of
the address is set), across several queued jobs, and stops at the first
fence that does not. When a job starts, `read_offset` points at it; when it
completes, the `UPDATE`s after it are applied and `read_offset` moves past
them. A partial-render command (`FRAG_PR`) is only run after the PM ran
out of memory; otherwise only its `UPDATE` is applied.

**Scheduling.** One job per data master. Order: the 3D pipe (fragment and
transfer jobs), geometry, compute; within a data master the higher
context priority first, then the order contexts became ready; after a
completion the freed data master is refilled before the others. Idle is
"no job running" and is reported with a host interrupt.

**Per job.** Units init before the first job after boot (soft-reset
release, SLC bypass, PDS/USC execution bases, pipeline defaults); cancel
of a pending power-off; memory context activation (`MULTICORE_*_CTRL`,
page catalogue set reuse by read-back, SLC/BIF invalidate for a new set,
`BIF_CAT_BASE_INDEX` with one 3-bit field per data master); the data
master's registers; the kick. Completion: clear the event, drain the data
master (`0x4000`, `0x688`, `0x668`, `0x1608`, `MCU_FENCE`, `0x1720`, SLC
flush of the data master's bits), signal fences.

| Data master | Kick | Done event | Computed values (everything else is copied) |
|---|---|---|---|
| Compute | `0x478 = 1` | `COMPUTE_FINISHED` | context-store PDS program alternates between `cdm_context_pds0` and `_b` |
| Transfer (3D pipe) | `0xF00 = 1` | `PIXELBE_END_RENDER` | tiles in flight `0xFD8 = min(1 + ISP_CTL[15:12], 4)`; FAST_2D region headers in the transfer heap; MSAA writes centre sample positions |
| Geometry (TA) | `0x400 = 1` | `TA_FINISHED` | PM free list loads, VHEAP init or reload, region header init on first kicks, TE state from the HWRT common data, TPC flush at the end |
| Fragment (3D) | `0xF00 = 1` | `PIXELBE_END_RENDER` (+ `PM_3D_MEM_FREE`, `ZLS_FINISHED` with forced Z store) | PM hand-over from the TA, tiles in flight, occlusion query base with `GET_VIS_RESULTS`, `0x6D0` with `DISABLE_PIXELMERGE` |

**Parameter manager.** Two PM contexts (0 for the TA, 1 for the 3D), each
with free list registers per local/global list (base, stack top — at bit
32 for the TA's local list, bit 22 for the 3D's, a 32-bit register for
global lists — page counts, load trigger) and status registers reporting
the current state. `CONTEXT_PB_BASE` (`0x2B0`) has a bit per list that
differs between the contexts. Lists are loaded only when a context does
not hold them; the TA's PM state goes to the HWRT data when the 3D takes
the render (or before the next TA reuses context 0) and to the free lists
when the 3D loads them.

**Out of memory.** `PM_OUT_OF_MEMORY` during a TA: pause TA allocation
(`0x2A0`/`0x2A8`), add the ready pages below the free list base in every
PM context holding the list (stack top + ready pages, counts unchanged),
resume allocation and the TA (`0x328`), drain, then firmware CCB
`FREELIST_GROW` and `UPDATE_STATS`. `FREELIST_GROW_UPDATE` turns the new
pages into ready pages; for a TA still waiting, it loads them keeping the
kernel's ready pages in reserve.

**Power-off after work.** Free lists from both PM contexts and the 3D
context's render state go to memory, then "GPU units deinit".

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
register *names* are mostly not public for the firmware-private units; the
*order, timing and values* for the parameter manager, context switching,
power islands and recovery are not documented anywhere. The binary is
~29 000 microMIPS instructions.

**Where this stands:** running Imagination's firmware in an emulator
against the kernel's real structures turned that sequencing into
something that can be observed and compared. openfw now reproduces the
reference's register traffic for everything the upstream driver and Mesa
submit, including concurrent work, out-of-memory and suspend/resume
(job execution above). What emulation cannot show is how the real
hardware responds (timing, status values, faults); that needs the board.
The remaining gaps (partial renders on exhausted parameter memory,
layered framebuffers, in-firmware recovery) are listed in
[openfw/README.md](../openfw/README.md#limitations).

### A sensible path

1. **Tooling** (done here): image parser/diff (`pvrfw.py`), register
   census (`fwregs.py`), and **`tools/fwemu`**, which boots the real
   firmware against a modelled register file and the kernel's own init
   structures. It reaches `firmware_started` with the same init trace as
   real hardware, consumes kernel-CCB commands (health check, power
   requests) and runs complete compute, transfer, geometry and fragment
   jobs. That is the executable specification and test bench for the
   replacement.
2. **M0 – boot handshake** — *done in emulation*: [`openfw/`](../openfw/README.md)
   boots, reports active and answers health checks.
3. **M1 – kernel CCB** — *done in emulation*: MMU cache invalidation,
   cleanup, log type, forced idle, power off, resume. `openfw` and
   Imagination's image both pass the 25-step `test_contract.py`; the TLB
   code passes `test_mmu.py`. Waiting for the first board run
   (`board/openfw-test.sh`).
4. **M2 – compute/transfer** — *done in emulation*: client CCB, fences,
   memory contexts, compute and transfer data masters.
5. **M3 – geometry + fragment** — *done in emulation*: parameter manager,
   free lists, partial-render commands, multi-kick geometry, out-of-memory
   with free list growth, concurrent geometry and fragment work.
6. **M4 – power and recovery** — *partly*: idle reporting and runtime
   suspend with PM state preserved are done; hardware recovery is left to
   the kernel's GPU reset, partial renders on exhausted parameter memory
   and layered framebuffers are missing.

Each milestone can be validated on the board with the same kernel and
Mesa, comparing behaviour and register traces against Imagination's
firmware.

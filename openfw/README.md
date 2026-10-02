# openfw — an open firmware for the BXE-4-32 (JH7110)

The PowerVR GPU in the JH7110 is driven by firmware running on a small MIPS
core inside the GPU. The upstream Linux `powervr` driver loads it from
`powervr/rogue_36.50.54.182_v1.fw`; Imagination ships that image as a
binary. `openfw/` is a from-scratch, MIT-licensed replacement, built with
an ordinary GCC cross compiler and packed into the same container format.

**Status: executes GPU jobs (milestones M0–M3, most of M4); verified in
emulation against Imagination's firmware, not yet run on a board.**

It schedules and runs everything the upstream driver and Mesa submit:
compute, transfer (blits), geometry and fragment jobs, with fences,
several contexts and VMs, context priorities, concurrent data masters,
geometry split over several kicks, parameter-buffer growth when a render
runs out of memory, occlusion queries, MSAA, depth/stencil load/store,
context and render-target teardown, and runtime suspend/resume of the GPU
with all parameter-manager state preserved. 15 KiB of code, 544 bytes of
RAM.

What it does not do yet is listed under [Limitations](#limitations).

## What has been verified, and how

Nothing has run on a VisionFive 2 yet. Everything below is emulation and
static checks; the board test (next section) is the step that matters
now.

| Check | Tool | Result |
|---|---|---|
| Container + device info accepted by the driver's parser | `tools/pvrfw.py pack --compare` | device info byte-identical to Imagination's; re-packing Imagination's own ELF reproduces their file byte for byte |
| Boot-time register programming | `tools/fwemu --trace-regs`, diffed against the reference | identical |
| Kernel contract: probe, health check, MMU flush, log type, forced idle, power off, resume | `test_contract.py` (25 checks, the driver's own sequence) | all pass, as for Imagination's image |
| GPU jobs and power, step by step | `test_jobs.py`: 32 scenarios, both firmwares, same kernel structures and hardware model | **identical register writes, polls, host-visible memory and results** in all 32 |
| Every userspace command field and kernel flag | `test_jobs.py --sweep`: each field of `pvr_stream_defs.c` and each kernel-settable flag bit set to bit patterns | **406 of 406** identical |
| Paths where openfw deliberately differs | `test_jobs.py` outcome cases (out of memory without ready pages) | same results and final memory |
| Wired TLB entries, TLB refill handler, stale-entry fix-up, TLB flush | `test_mmu.py` on Unicorn's TLB-equipped M14Kc model | all pass |

The 32 step-by-step scenarios (`tools/fwemu/jobs.py`):
`power` (all maintenance/power commands), `compute`, `compute-chained`,
`transfer`, `render` (geometry + partial-render skip + fragment, also with
PM status values, 4×MSAA and a small target), `geom-only`, `cleanup`,
`frames` (alternating HWRT data, as Mesa does), `frames-pipelined` (next
frame's geometry while the previous fragment job runs), `multivm` (two
page catalogues, sequential and concurrent), `blocked` (a job waiting for
another context), `cleanup-busy`, `teardown`, `mixed` (compute, transfer
and a render in flight), `wrap` (200 jobs: client CCB wrap-around),
`oom`, `oom-live`, `oom-frames` (parameter memory exhausted, free list
grown through the firmware CCB), `priority`, `multikick` (geometry in
three kicks), `suspend` (frames with runtime suspend/resume between).

`make test KERNEL=~/linux REFERENCE=path/to/imagination.fw` runs all of
it (about a minute; `--sweep` adds three).

## Trying it on the board

```sh
# on the build host (Debian/Ubuntu)
sudo apt install gcc-mipsel-linux-gnu    # tests also need: pip install unicorn
make -C openfw            # -> openfw/rogue_36.50.54.182_v1.fw
# on the board, kernel from kernel/, Mesa with the powervr Vulkan driver,
# and nothing using the GPU:
sudo board/openfw-test.sh openfw/rogue_36.50.54.182_v1.fw
```

`prebuilt/rogue_36.50.54.182_v1.fw` is the same build (GCC 12.4, Ubuntu
24.04 `gcc-mipsel-linux-gnu`; the build is reproducible, compare with
`prebuilt/SHA256SUMS`) for boards without a cross compiler at hand.

`openfw-test.sh` saves the installed firmware, installs openfw, reloads
`powervr` with firmware tracing on and always restores the previous
firmware at the end. Stage 1 checks the probe and cycles runtime
suspend/resume. Stage 2 runs `vulkaninfo`, short `vkmark` scenes (clears,
geometry + fragment, texture uploads through transfer jobs, blending, a
1080p scene that grows the parameter buffer) and, if installed, a
`dEQP-VK` smoke and compute subset, letting the GPU suspend between them;
any job timeout or firmware reset fails the step and saves the firmware
trace. Please send the log it writes: it answers what emulation cannot
(real hardware timing, the TLB on the real core, units the model only
approximates).

## How it works

```
reset (0xBFC00000, kseg1, temp stack in the boot-data page)
  boot_setup(): CP0, caches, EBase, vectored interrupts, wired TLB entries
                (registers, page table, stack, .bss) + MIPS-wrapper remaps
  clear .bss, jump to fw_main() in kseg2
fw_main(): GPU-side init, report ON, firmware_started = 1, idle in `wait`
interrupts (non-nesting):
  IP3  MTS background task -> kernel CCB commands, then the scheduler
  IP4  MTS interrupt task  -> GPU events: job completion, PM out of memory
  IP2  CP0 timer           -> re-check the kernel CCB and the scheduler
  TLB refill               -> identity mapping from the kernel's page table
```

**Scheduling** (`sched.c`). A KICK names a context and its new client-CCB
write offset. Ready contexts are processed per data master, the 3D pipe
first (fragment and transfer jobs), then geometry, then compute; within a
data master by context priority, then in the order they became ready;
after a completion the freed data master is refilled first. Processing a
context moves `dep_offset` over every command whose fences are satisfied
(`FENCE`/`FENCE_PR`: the 32-bit UFO value is at least the required one),
applies `UPDATE`s that are not behind a job, signals `NULL` commands and
partial-render commands that are not needed, and starts the next job when
its data master is free. On completion the job's `UPDATE`s signal its
timeline UFO, `read_offset` moves past them, HWRT cleanup counters advance
and the host is interrupted; the kernel signals the job's fence from the
UFO. The GPU reports IDLE when no job runs.

**Jobs** (`kicks.c`, `gpu.c`). Before the first job after boot the GPU
units are initialised. Each job activates its memory context: page
catalogue sets `BIF_CAT_BASE1-7`, reused while they still hold the
context's catalogue, selected per data master in `BIF_CAT_BASE_INDEX`.
Then the data master's registers are loaded from the command and the
static context state, and it is kicked. Completion drains the data
master's memory traffic (MCU fence, SLC flush) before fences are signalled.

**Parameter manager.** PM context 0 serves geometry, context 1 fragment.
Free lists are loaded into a context when it does not hold them yet; the
state the TA leaves (page catalogues, stack pointers, free list tops and
page counts) is stored to the HWRT data and free lists when the 3D takes
the render, before another TA reuses context 0, before a free list is
destroyed and before power-off. When the TA runs out of parameter memory
the growable free list's ready pages are handed to the PM at once, the TA
resumes, and a `FREELIST_GROW` request goes to the kernel through the
firmware CCB; the kernel's `FREELIST_GROW_UPDATE` replenishes the ready
pages.

Files:

| File | Content |
|---|---|
| `start.S` | reset entry, TLB refill handler, exception and interrupt vectors |
| `boot.c` | processor and MMU setup (runs before kseg2 is mapped) |
| `main.c` | kernel CCB, firmware CCB, power requests, tracing, fault handling |
| `sched.c` | client CCB processing, fences, ready lists, completion, cleanup |
| `kicks.c` | compute, transfer, geometry and fragment programming; parameter manager; out of memory |
| `gpu.c` | unit initialisation, page catalogue sets, cache maintenance, end-of-job fence |
| `fw.h`, `mmu.h`, `mips.h`, `regs.h` | internal interfaces, address translation, CP0 helpers, register map |
| `fwif.h` | firmware-interface offsets and trace IDs, generated by `gen_fwif.py` from the kernel headers via `tools/fwemu/layout.json` |
| `device-36.50.54.182.json` | the core's features/BRNs/ERNs, packed into the container |
| `link.ld` | fixed section addresses from the kernel's MIPS layout |
| `test_mmu.py`, `test_contract.py`, `test_jobs.py` | the checks above |

Where the reference firmware's register values were learnt from: every
sequence comes from running Imagination's firmware through the same
commands in `tools/fwemu` (see [docs/firmware.md](../docs/firmware.md),
"Job execution"), with command fields tagged so each register value can be
traced to its source, and inputs varied to find which values the firmware
computes rather than copies.

## Kernel commands handled

| Command | Behaviour |
|---|---|
| `KICK`, `COMBINED_GEOM_FRAG_KICK` | new client-CCB write offset, cleanup counters, schedule |
| `CLEANUP` (context, HWRT data, free list) | busy while in use; a free list is stored back from the PM first |
| `FREELIST_GROW_UPDATE` | new pages become ready pages, or go straight to a TA waiting for them |
| `MMUCACHE` | SLC flush of MMU data, `BIF_CTRL_INVAL`, firmware TLB flush, sync update |
| `SLCFLUSHINVAL` | full SLC flush+invalidate |
| `LOGTYPE_UPDATE` | firmware TLB flush (log type is read on every trace) |
| `HEALTH_CHECK` | counted |
| `POW` forced idle / cancel, dust count | `pow_state`, GPIO interrupt routing, `power_sync` |
| `POW` off | PM state to memory, units shut down, SLC/PC flush, `pow_state` OFF, `power_sync`, core parks |
| `FREELISTS_RECONSTRUCTION_UPDATE` | accepted (openfw never requests a reconstruction) |

Return slots, `kccb_cmds_executed` and host interrupts follow the
reference: return values only for commands the kernel waits on.

## Limitations

* **Not run on hardware yet.** The hardware model in `tools/fwemu`
  completes work instantly and returns fixed values for status registers;
  timing, real PM behaviour and the real MIPS core are untested.
* **Out of parameter memory with nothing left to grow.** When the free
  list is at its maximum (256 MiB with Mesa) and no ready pages remain,
  Imagination's firmware runs a partial render to free memory. openfw
  leaves the TA stalled; the kernel's job timeout resets the GPU. Without
  ready pages but still growable, openfw waits for the kernel's grow
  instead of storing the TA for a possible partial render (same outcome).
* **Layered framebuffers** (render target arrays with more than one
  layer): not implemented. The firmware would have to read the render
  target cache through a GPU virtual address mapping.
* **Hardware recovery.** No lockup detection or context reset inside the
  firmware; the kernel's job timeout and full GPU reset are the recovery
  path. MMU page faults are cleared but not reported.
* **Context switching / preemption** is not implemented; the upstream
  driver does not request it.

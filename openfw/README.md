# openfw — an open firmware for the BXE-4-32 (JH7110)

The PowerVR GPU in the JH7110 is driven by firmware running on a small MIPS
core inside the GPU. The upstream Linux `powervr` driver loads it from
`powervr/rogue_36.50.54.182_v1.fw`; Imagination ships that image as a
binary. `openfw/` is a from-scratch, MIT-licensed replacement, built with
an ordinary GCC cross compiler and packed into the same container format.

**Status: feature-complete for the upstream driver and Mesa (milestones
M0–M4); verified in emulation against Imagination's firmware, not yet run
on a board.**

It schedules and runs everything the upstream driver and Mesa submit:
compute, transfer (blits), geometry and fragment jobs, with fences,
several contexts and VMs, context priorities, concurrent data masters,
geometry split over several kicks, parameter-buffer growth when a render
runs out of memory, **partial renders when it cannot grow any more**,
occlusion queries, MSAA, depth/stencil load/store, context and
render-target teardown, runtime suspend/resume of the GPU with all
parameter-manager state preserved, and **hardware recovery**: lockup and
overrun detection, GPU page-fault reporting, GPU reset and free-list
reconstruction. 18 KiB of code, 1.3 KiB of RAM.

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
| GPU jobs and power, step by step | `test_jobs.py`: 83 cases over 47 scenarios, both firmwares, same kernel structures and hardware model | **identical register writes, polls, host-visible memory and results** in all 83 |
| Every userspace command field and kernel flag | `test_jobs.py --sweep`: each field of `pvr_stream_defs.c` and each kernel-settable flag bit set to bit patterns | **638 of 638** identical |
| Desktop-like traffic | `test_jobs.py --stress N`: seeded random mixes (below) | **160 of 160** workloads identical |
| No use of freed memory | every completed `CLEANUP` arms a write trap on the freed object (and a context's CCB control); any later firmware write fails the case | none, in every case and workload above |
| Wired TLB entries, TLB refill handler, stale-entry fix-up, TLB flush | `test_mmu.py` on Unicorn's TLB-equipped M14Kc model | all pass |

The step-by-step scenarios (`tools/fwemu/jobs.py`):

* **Work:** `power` (all maintenance/power commands), `compute`,
  `compute-chained`, `transfer`, `render` (geometry + partial-render skip +
  fragment, also with PM status values, 4×MSAA, a small target and a
  render target array), `geom-only`, `frames` (alternating HWRT data, as
  Mesa does), `frames-pipelined` (next frame's geometry while the previous
  fragment job runs), `multikick` (geometry in three kicks), `mixed`
  (compute, transfer and a render in flight), `wrap` (200 jobs: client CCB
  wrap-around), `priority`.
* **Several clients:** `multivm` (two page catalogues, sequential and
  concurrent), `blocked` (a job waiting for another context),
  `cleanup`, `cleanup-busy`, `teardown`, `suspend` (frames with runtime
  suspend/resume between).
* **Parameter memory:** `oom`, `oom-live`, `oom-frames` (free list grown
  through the firmware CCB), `oom-wait` (no ready pages: the TA waits for
  the kernel), `partial-render-*` (nothing left to grow: twice in a row,
  with frames, with the 3D busy, depth/stencil and scratch buffers, MSAA,
  multi-kick geometry, a failed or too-large grow, slow 3D).
* **Recovery:** `hang-compute`, `hang-ta`, `hang-3d`, `hang-transfer`,
  `hang-two` (several data masters stuck; with and without progress),
  `hang-compute-usc` (progress only in the shader slots), `overrun`
  (past the context deadline), `oom-wait-hang`, `fault-*` (GPU page
  faults with one or two data masters busy).
* **Robustness:** `teardown-power*` (applications quit or recreate their
  swapchains, then the GPU powers down), `many-clients*` (48 contexts
  waiting on one fence at once), `*-wrap` (the GPU timer's low 32 bits
  wrap during the work, i.e. after about 46 minutes of the GPU powered),
  `*-396`, `*-594` (the vendor clock rates), `fwccb-full` (the kernel is
  slow to read the firmware CCB when a free list grow must be requested),
  `blocked-power` (a context still waiting on a fence when the GPU powers
  down).
* **Stress:** `stress<seed>`: a compositor-like render context drawing
  frames on two HWRT data sets, applications' compute and transfer work in
  one or two VMs, priorities, cross-queue fences, completions in any
  order, parameter-memory exhaustion, timer ticks, suspend/resume when
  idle, swapchains recreated and applications quitting while others start.

The emulator powers the GPU down the way the hardware does: every
register is back at its reset value when the firmware restarts.

**What the random and robustness runs found** (all fixed, and each now
covered by a case):

* After an application destroyed its render targets, the next power-down
  wrote 9 words into the freed HWRT data. On a board that is a firmware
  crash or another application's memory corrupted, after nearly every
  window resize or application exit.
* The ready list was a 32-entry table: with more contexts waiting at once
  (a busy desktop gets there), one was silently dropped and the GPU
  deadlocked. It is now unbounded, linked through the contexts' own
  `run_node`, which the kernel leaves to the firmware.
* With the firmware CCB full, a free list grow request was dropped and
  that render never finished. The firmware now waits for the kernel's
  interrupt thread to make room, as Imagination's does.
* A context still waiting on a fence when the GPU powered down was
  forgotten by the restarted firmware. The ready list now survives power
  cycles, as Imagination's lists do.
* Twelve scheduling and parameter-manager details (which context runs
  first, which page catalogue set is reused, when the PM is paused or
  the PC cache flushed) differed from Imagination's firmware.

`make test KERNEL=~/linux REFERENCE=path/to/imagination.fw` runs the cases
(about three minutes); `--sweep` and `--stress N` add the rest.

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
1080p scene that grows the parameter buffer), two `vkmark` clients at once
(as a compositor and an application would be), OpenGL through Zink and,
if installed, a `dEQP-VK` smoke and compute subset, letting the GPU
suspend between them;
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
data master by context priority, then in the order they became runnable
(a context waiting on a fence goes to the back once the fence holds);
contexts still blocked on a fence are tried last, in the order they
started waiting.
Processing a context moves `dep_offset` over every command whose fences
are satisfied (`FENCE`/`FENCE_PR`: the 32-bit UFO value is at least the
required one); if its data master is free it then applies `UPDATE`s that
are not behind a job, signals `NULL` commands and partial-render commands
that are not needed, and starts the next job. On completion the job's
`UPDATE`s signal its timeline UFO, `read_offset` moves past them, HWRT
cleanup counters advance and the host is interrupted; the kernel signals
the job's fence from the UFO. Then, in this order: the freed data master
is refilled, the other half of the render context (geometry ↔ fragment)
is processed, after a 3D-pipe job the geometry contexts waiting on a
fence it signalled are processed, and anything else still ready is left
to a background re-check. Idle is reported in two steps: a power-off query,
confirmed by the next interrupt task if nothing started meanwhile.

**Jobs** (`kicks.c`, `gpu.c`). Before the first job after boot the GPU
units are initialised. Each job activates its memory context: page
catalogue sets `BIF_CAT_BASE1-7`, reused while they still hold the
context's catalogue (a powered-down GPU reads back 0), otherwise a set
never used or the least recently used free one, selected per data master
in `BIF_CAT_BASE_INDEX`.
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
pages. Without ready pages the TA is stopped and its state stored until
the grow arrives. When the list is at its maximum (or the kernel's grow
fails), the render's partial-render command runs on the 3D pipe ahead of
anything else: it renders what has been binned so far, which frees its
parameter memory, and the TA resumes from where it stopped. Free lists
move between the two PM contexts with TA allocation or 3D deallocation
paused while the other side is running.

**Hardware recovery** (`hwr.c`). The upstream kernel leaves lockups to
the firmware: its job timeout only re-arms and its watchdog only checks
that kernel commands still execute. openfw does what Imagination's
firmware does. Every 31250 GPU timer ticks each busy data master is
checked for progress: a hash of its signature registers, then the
registers one by one, then the shader (USC) slots it holds. Compute gets
15 checks without progress, geometry and fragment 3; a job past its
context's deadline (30 s) overruns. A locked-up data master is recorded
in the HWR info buffer and reported to the kernel
(`CONTEXT_RESET_NOTIFICATION`, guilty or innocent; the kernel logs it).
Once every busy data master has timed out the GPU is reset and
initialised again, the lost jobs are skipped (their fences signal, so
nothing waits forever; the lost frame or dispatch simply has wrong
contents) and the kernel rebuilds the free lists
(`FREELISTS_RECONSTRUCTION`); fragment jobs for render targets whose
geometry was lost are discarded. A GPU page fault is
reported at once with the faulting address. Everything else keeps
running.

**GPU memory access** (`gpumem.c`). Some jobs need the firmware to write
into a context's GPU memory: a geometry phase that was cut short leaves
the render target's tail pointer and render target caches dirty, and the
next first geometry kick must start from zeroed ones. The firmware walks
the context's page tables in system memory and maps each page through a
wired TLB entry in the heap area the kernel reserves for the firmware's
own mappings.

Files:

| File | Content |
|---|---|
| `start.S` | reset entry, TLB refill handler, exception and interrupt vectors |
| `boot.c` | processor and MMU setup (runs before kseg2 is mapped) |
| `main.c` | kernel CCB, firmware CCB, power requests, tracing, fault handling |
| `sched.c` | client CCB processing, fences, ready lists, completion, cleanup, partial-render scheduling |
| `kicks.c` | compute, transfer, geometry and fragment programming; parameter manager; out of memory, partial renders |
| `gpu.c` | unit initialisation, GPU reset, page catalogue sets, cache maintenance, end-of-job fence |
| `hwr.c` | lockup/overrun detection, page faults, recovery, free list reconstruction |
| `gpumem.c` | GPU virtual memory access through the context's page tables |
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
| `FREELISTS_RECONSTRUCTION_UPDATE` | ends a hardware recovery: geometry and fragment scheduling resumes |

Return slots, `kccb_cmds_executed` and host interrupts follow the
reference: return values only for commands the kernel waits on.

## Limitations

* **Not run on hardware yet.** The hardware model in `tools/fwemu`
  completes work instantly or when a scenario says so, and returns fixed
  or scripted values for status registers. Real timing, real PM
  behaviour, the real MIPS core and real lockups are untested. The first
  board run (`board/openfw-test.sh`) is the step that matters now.
* **Layered framebuffers** (render target arrays with more than one
  layer in one render): Mesa does not use them on this GPU. Its BXE-4-32
  device description leaves `gs_rta_support` off, so multi-layer
  framebuffers are drawn one layer per render, which openfw handles like
  any other render. The firmware side of layered rendering (the render
  target array state across partial renders) follows the reference only
  as far as the emulator could show it with one layer active.
* **Context switching / preemption** is not implemented; the upstream
  driver does not request it.
* **Recovery timing** is the reference's, in GPU timer ticks (the GPU
  clock / 256): one check every 31250 ticks, about 20 ms at 400 MHz. A
  compute job is reset after about 16 checks (0.3 s) in which none of its
  signature registers or shader slots changed, geometry and fragment jobs
  after 4 (80 ms). Work that is running changes them all the time; a
  shader stuck in a loop does not. The kernel does not tell userspace
  about a reset (no `VK_ERROR_DEVICE_LOST`), it only logs it.

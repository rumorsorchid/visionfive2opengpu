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

## Submitting work

`--kccb` submits kernel-CCB commands after boot, in order, and delivers
the interrupts the hardware would raise:

```sh
fwemu.py FW --kernel ~/linux --kccb health --kccb mmucache --kccb logtype
fwemu.py FW --kernel ~/linux --kccb pow-idle --kccb pow-off --kccb reboot --kccb health
fwemu.py FW --kernel ~/linux --kccb pow-idle --kccb pow-units=1 --kccb pow-cancel-idle
fwemu.py FW --kernel ~/linux --kccb compute                  # CDM job + completion
fwemu.py FW --kernel ~/linux --config-flags 0x10 --kccb compute   # with POW_RASCALDUST
```

`compute` builds what the kernel builds for a Vulkan dispatch (FW memory
context, compute context, client CCB with a CDM command, KICK), then
signals `EVENT_STATUS.COMPUTE_FINISHED`. The firmware logs the same steps
as on hardware ("Kick Compute: FWCtx …", "Compute finished",
"Deactivate MemCtx"), and the client CCB read offset advances.

`pow-idle` then `pow-off` is the driver's runtime-suspend sequence;
`reboot` is runtime resume (the same image restarted, kernel CCB
continuing). The report shows `pow_state`, `kccb_cmds_executed` and
`power_sync`, the fields the driver reads.

Interrupt model (vectored, EBase `0x9FC02000`, 0x100 spacing): IP2
CP0 timer, IP3 MTS background task (kernel CCB), IP4 MTS interrupt task.
A handler runs from its vector to the first `eret`. When the firmware
kicks its own MTS, the follow-up task (interrupt task for `0x20`,
background task otherwise) is delivered after the handler returns. A core
that stops in `wait` with interrupts disabled is reported as parked and
gets no further interrupts.

Any image in the container format runs, including `openfw/`;
`openfw/test_contract.py` uses this emulator to compare images against
the driver's expectations.

Hardware completion model: Imagination's poll routines (`0xc0008c7c`,
`0xc0008d8c`, `0xc0008ee4`, `0xc0008f20` in v1.0 b6503725) and openfw's
`poll_reg` (found through `openfw.map` next to the image) are hooked and
every polled condition is satisfied immediately; `EVENT_STATUS` bits stay
set until the firmware writes `EVENT_CLEAR`; `POWER_EVENT` with `REQ_EN`
raises `POWER_COMPLETE`; `CLK_CTRL` starts at its all-auto reset value;
`MULTICORE_GPU` reports one geometry-capable core.

## Jobs: host model, scenarios and comparisons

| Tool | What it does |
|---|---|
| `host.py` | builds what `drm/imagination` builds: VM contexts, compute/render/transfer contexts with static state, client CCBs, free lists, HWRT data sets (kernel formulas), jobs with fences, `KICK`/`COMBINED_GEOM_FRAG_KICK`, cleanup; answers firmware-CCB free list grow requests like `pvr_free_list_process_grow_req` |
| `jobs.py` | job scenarios on top of a GPU model: kicks complete in order and raise their events; optional PM status values (`status_tags`), out-of-memory events (`oom`), TA stall until resumed, runtime suspend/resume |
| `spec.py` | functional register trace per step (MTS bookkeeping and firmware MMU maintenance filtered) |
| `vary.py`, `sweep.py` | change one input or one command field at a time and report which register values depend on it |

```sh
python3 jobs.py FW render --kernel ~/linux             # steps, writes, memory, firmware log
python3 spec.py FW frames-pipelined --kernel ~/linux --set status_tags=1
python3 ../../openfw/test_jobs.py OPENFW.fw IMG.fw --kernel ~/linux [--sweep]
```

Scenarios: `power`, `compute`, `compute2`, `transfer`, `render`, `geom`,
`cleanup`, `cleanup-busy`, `teardown`, `frames`, `frames-pipelined`,
`multivm`, `multivm-concurrent`, `blocked`, `wrap`, `mixed`, `priority`,
`priority2`, `multikick`, `oom`, `oom-live`, `oom-frames`, `suspend`.
Command fields are filled with tags (`0x5A0nn000`) so the report names
the field behind every register value.

## Next steps

* A model of render-target-array (layered) rendering and of partial
  renders, the two firmware paths openfw does not implement yet.
* Timing: the model completes work instantly; real hardware does not.

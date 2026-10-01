# Rascal/dust power-up on the JH7110

## Symptom

After reset the BXE-4-32's shader cluster ("rascal" + "dust") power island
is gated. Early community kernels needed a two-pass driver load; later the
island was ungated from the host by writing `ROGUE_CR_POWER_EVENT` (0x0038)
with a partly guessed value before booting the firmware.

## Evidence

### From StarFive's DDK (1.19, kernel side)

1. The DDK's JH7110 system layer sets `bEnableRDPowIsland = IMG_TRUE`,
   which makes `rgxfwutils.c` pass **`RGXFWIF_INICFG_POW_RASCALDUST`**
   (config flag bit 4) to the firmware: *the firmware manages the island*.
2. The upstream driver has never set `ROGUE_FWIF_INICFG_POW_RASCALDUST`
   (`fw_sysdata_init()` only ever sets `DISABLE_DM_OVERLAP`), so the
   firmware is told the island is always powered.
3. The DDK glue also unmasks the JH7110 PMU's *hardware event turn-off*
   mask (PMU + 0x08, bit 7 = GPU event) on runtime resume and masks it on
   suspend. Upstream never programs the PMU event masks.
4. `RGX_CR_POWER_EVENT` is absent from the DDK's Rogue register
   definitions; the host-side Rogue DDK never writes it.

### From the firmware (`tools/fwregs.py`, v1.0 build 6503725)

5. The firmware **writes POWER_EVENT itself**, at 7 sites in a routine at
   `0xc000a690` driven by a power state machine (`0xc000ab00`-`0xc000b250`).
6. Its sequence: `XPU_BROADCAST = 1`; `POWER_EVENT = value`; wait one
   `ROGUE_CR_TIMER` tick; `POWER_EVENT = value | REQ_EN`; later
   `POWER_EVENT = 0` and `EVENT_CLEAR = POWER_COMPLETE | POWER_ABORT`.
7. `value` = `((1 << MULTICORE_SYSTEM) - 1) << 24 | 0x700 | type`: bits
   31:24 are a GPU mask (0x01 here, the community patch used 0xff) and
   bits 10:8 three power domains.
8. The firmware keeps `sysdata.config_flags` in a global and tests bit 4
   (`POW_RASCALDUST`) at 15 sites.

### From running the firmware (`tools/fwemu`, transcripts in `evidence/`)

9. Booting the firmware and submitting the same compute job twice in the
   emulator:
   * with upstream's config flags it never touches `POWER_EVENT` and runs
     the job as if the island were powered
     ([fwemu-compute-default.txt](evidence/fwemu-compute-default.txt));
   * with `POW_RASCALDUST` it logs "Changing number of dusts from 0 to 1",
     writes `POWER_EVENT = 0x01000701` then `0x01000703`, logs
     "HW Request On(1)/Off(0): 1, Units: 0x0000000001000703 … Completed",
     and only then runs the job
     ([fwemu-compute-pow-rascaldust.txt](evidence/fwemu-compute-pow-rascaldust.txt)).
10. That value and two-step sequence are exactly what the host-side
    default (`jh7110_power_event=1`) writes, and differ from the community
    patch's `0xff000703` in the GPU mask.

## Conclusion so far

The firmware **powers the island itself when `POW_RASCALDUST` is set** —
which StarFive's driver does and the upstream driver doesn't — and the
emulator shows it doing so with the same register writes the host-side
workaround performs. The host-side POWER_EVENT write is a re-implementation
of a piece of the firmware.

Prediction for hardware: case **D** passes. The new risk in D is the other
half of the firmware's behaviour, powering the island **down** when idle
("Initiate powoff query for RD-DMs"), which has never run on a JH7110.
That is why A stays the default until D has survived long benchmark runs. Whether the PMU hardware-event mask is
also required (i.e. whether the island's power switch is outside the GPU)
is still open.

The kernel series therefore offers both paths:

| Parameter | Meaning |
|---|---|
| `powervr.jh7110_power_event=1` | host power-up mirroring the firmware's sequence (default) |
| `powervr.jh7110_power_event=2` | original community single write `0xff000703` |
| `powervr.jh7110_power_event=0` | no host power-up |
| `powervr.rd_power_island=1` | set `POW_RASCALDUST`, firmware manages the island |

## The experiment

On the board, with the kernel from this repo and nothing else using the
GPU:

```sh
sudo board/power-ab-test.sh            # cases A L D E C B
```

| Case | power_event | rd_power_island | PMU GPU event | Expectation |
|---|---|---|---|---|
| A | 1 | 0 | untouched | PASS |
| L | 2 | 0 | untouched | PASS (community baseline) |
| D | 0 | **1** | untouched | PASS ⇒ firmware handles it on its own |
| E | 0 | **1** | unmasked | PASS where D fails ⇒ PMU event needed too |
| C | 0 | 0 | unmasked | tells whether the PMU alone changes anything |
| B | 0 | 0 | untouched | FAIL (reproduces the original hang) |

A failing case can wedge the GPU until reboot; the log is appended as it
goes, so reboot and re-run the remaining cases (`... power-ab-test.sh C B`).

While a workload runs in another terminal:

```sh
sudo board/vf2-regs.py pmu     # event masks, current power mode
sudo board/vf2-regs.py gpu     # PBVNC, EVENT_STATUS, POWER_EVENT, idle regs
```

## What to do with the result

* **D passes:** make `rd_power_island` the default for BXE-4-32 (matching
  the vendor stack) and drop the host-side POWER_EVENT write. Also gives
  real power savings: the firmware can gate the shader cluster when idle.
* **E passes, D fails:** add the GPU hardware-event unmasking to
  `jh71xx-pmu` (tied to the GPUA domain) and then do the above.
* **Only A/L pass:** keep the host sequence; prefer A if it is as stable
  as L over a long glmark2/vkmark run.

# StarFive DDK vs. the upstream open stack

This compares what StarFive ships for the JH7110 GPU with the fully open
stack (Linux `drm/imagination` + Mesa PowerVR + Imagination's open-source
firmware), to find where the open stack is guessing and where it differs.

Sources compared:

| Component | Version | Where |
|---|---|---|
| StarFive DDK kernel module ("services") | Rogue DDK 1.19@6345021, `PVR_SYSTEM=sf_7110` | `starfive-tech/linux`, branch `JH7110_VisionFive2_6.12.y_devel`, `drivers/gpu/drm/img/img-rogue` (dual MIT/GPLv2) |
| Upstream kernel driver | Linux v7.3-rc5 + this repo's series | `drivers/gpu/drm/imagination` |
| Mesa | 26.2.3 | `src/imagination` |
| Open-source firmware | `rogue_36.50.54.182_v1.fw` v1.0 build 6503725 (user supplied), and TH1520's `rogue_36.52.104.182_v1.fw` v1.1 build 6976702 from linux-firmware 20260915 | analysed with `tools/pvrfw.py` |

StarFive's proprietary userspace (`soft_3rdpart/IMG_GPU/out/img-gpu-powervr-bin-*.tar.gz`,
Git LFS) and the DDK firmware were deliberately **not** downloaded or
used: everything below comes from the DDK's openly licensed kernel source
and the redistributable open-source-driver firmware.

## 1. The GPU core itself: same as an already-supported part

DDK hardware definitions (`hwdefs/rogue/km/configs/rgxconfig_km_36.V.54.182.h`)
compared with the TH1520's BXM-4-64 (`36.V.104.182`), which upstream already
lists as *supported*:

| | BXE-4-32 (JH7110) | BXM-4-64 (TH1520) |
|---|---|---|
| `NUM_ISP_IPP_PIPES` | 4 | 6 |
| every other KM feature | identical | identical |
| BRN/ERN mask (`gaErnsBrns`) | `0x10212a` | `0x10212a` |
| firmware processor | MIPS32r2 + microMIPS | MIPS32r2 + microMIPS |

Mesa's `bxe-4-32.h` vs `bxm-4-64.h` agree: they differ only in values
derived from the pipe count (common store size, ISP tiles in flight, max
partitions, USC tasks, unified store depth) and in BRN 44079 (see §3).

**Consequence:** the GPU core is not where the JH7110 trouble comes from.
Every problem the community hit was SoC integration: coherency, power
gating, clocks. That's also why marking the core `PVR_GPU_EXPERIMENTAL`
(instead of patching out the support check) is a reasonable upstream path.

## 2. Firmware device information vs. DDK

`tools/pvrfw.py check-ddk` decodes the device-info block embedded in the
firmware image (which is what the upstream driver uses to set its
features/quirks) and compares it with the DDK hwdefs:

* Every feature the DDK's kernel-mode table defines **matches** the
  firmware image, including all valued features (`PHYS_BUS_WIDTH=36`,
  `SLC_CACHE_LINE_SIZE_BITS=512`, `VIRTUAL_ADDRESS_SPACE_BITS=40`,
  `NUM_OSIDS=8`, `XPU_MAX_SLAVES=3`, ...).
* Features only in the firmware image (`COMMON_STORE_SIZE_IN_DWORDS`,
  `ISP_MAX_TILES_IN_FLIGHT`, `MAX_PARTITIONS`, `XE_TPU2`, ...) are
  user-mode features that the DDK keeps out of its *KM* table by design.
  Their values agree with Mesa.
* ERNs 35421 and 38748 are in the firmware image but not in DDK 1.19's
  table. The firmware's tables come from a newer Imagination core database,
  and the TH1520 image has the same two ERNs.

### Quirks (BRNs)

| BRN | DDK 1.19 | FW image | Upstream use | Assessment |
|---|---|---|---|---|
| 63553 | yes | yes | MIPS boot: `pvr_fw_mips.c` | consistent |
| 71242 | **no** | yes | sets `SLC_CTRL_MISC.LAZYWB_OVERRIDE` on non-multicore | newer DB; the TH1520 path is identical and works |
| 71317 | yes | **no** | none | DDK: device heaps must not use 1 MB/2 MB pages. Upstream maps the GPU with `PVR_DEVICE_PAGE_SIZE == PAGE_SIZE` (4 KB), so it cannot trigger. |
| 44079 | no | no (v1.0 BXE) / **yes** (v1.1 BXM) | common-store split point → compute shared-memory size reported to Mesa | Mesa also omits it for BXE-4-32. Re-check with the v1.1 BXE image (§5). |

## 3. Power: what the DDK does that upstream doesn't

### 3.1 Rascal/dust power island

The community found the shader cluster ("rascal/dust") comes up gated, and
ungates it from the host by writing `RGX_CR_POWER_EVENT` (0x0038) with a
partly guessed value (`GUESS_*` constants).

What the DDK says:

* StarFive's system layer sets `bEnableRDPowIsland = IMG_TRUE`, so the DDK
  passes **`RGXFWIF_INICFG_POW_RASCALDUST`** (bit 4) to the firmware,
  letting it manage the island. **Upstream never sets
  `ROGUE_FWIF_INICFG_POW_RASCALDUST`**, telling the firmware the island is
  always on.
* `RGX_CR_POWER_EVENT` is absent from the Rogue DDK register definitions
  and never written by the host-side Rogue DDK.
* The DDK glue toggles the JH7110 PMU *hardware event turn-off mask*
  (PMU + 0x08, bit 7 = GPU event) around runtime PM; upstream never
  programs the PMU event masks.

What the firmware says (`tools/fwregs.py`): it **writes POWER_EVENT
itself** from a power state machine, using `XPU_BROADCAST = 1`, a two-step
write (`value`, one timer tick, `value | REQ_EN`), GPU mask
`(1 << MULTICORE_SYSTEM) - 1` in bits 31:24 and domains 8-10, and it tests
config bit 4 at 15 places.

So the host-side write reproduces part of the firmware's own routine, and
the vendor-equivalent fix is probably to set `POW_RASCALDUST`. The series
mirrors the firmware's sequence on the host by default (fully explained
values instead of guesses) and adds `powervr.rd_power_island` to test the
vendor path; `board/power-ab-test.sh` decides between them on hardware.
Details: [power.md](power.md).

### 3.2 Clocks and resets

| DDK name | Upstream (this series) | JH7110 clock |
|---|---|---|
| `clk_bv` (rate set to 594 MHz) | parent of `core` | `GPU_CORE` divider |
| `clk_core` | `core` | `GPU_CORE_CLK` gate |
| `clk_sys` | `sys` | `GPU_SYS_CLK` |
| `clk_axi` | `mem` | `NOC_BUS_GPU_AXI` |
| `clk_apb` | `apb` (new, optional) | `GPU_APB` |
| `clk_rtc` | `rtc` (new, optional) | `GPU_RTC_TOGGLE` |
| `rst_apb`, `rst_doma` | reset array, same order | `GPU_APB`, `GPU_DOMA` |

The `NOC_BUS_GPU_AXI` *reset* (ID 27) is used by neither driver.

**GPU clock:** the DDK runs the core at **594 MHz** (396 MHz on the
1.25 GHz bin). Upstream inherits whatever divider U-Boot left: the
community firmware trace shows **409.6 MHz** (PLL2 = 1228.8 MHz / 3).
`gpu_root` muxes PLL2 or PLL1, the divider is /1../7, so with PLL2 at
1228.8 MHz the choices are 614.4 (above the vendor's maximum) or
409.6 MHz. Reaching 594 MHz needs PLL2 = 1188 MHz (the other entry in the
upstream PLL table), which also moves the bus clocks derived from PLL2.
Not changed by default; see [tuning.md](tuning.md).

### 3.3 Active power management

The DDK enables active power management with a 100 ms idle latency and the
rascal/dust power island (`bEnableRDPowIsland`). The upstream driver's
runtime-PM autosuspend plus the firmware's own idle handling covers the
first; the second is tied to §3.1.

## 4. Cache coherency

| | DDK | Upstream (with this stack) |
|---|---|---|
| Model | `PVRSRV_DEVICE_SNOOP_EMULATED` + explicit `sifive_l2_flush64_range()` for every cache op | `dma-noncoherent` in DT; streaming DMA uses the SiFive ccache ops; *uncached* CPU mappings via XPbmtUC |
| Uncached/WC CPU mappings | flushes instead | PTE bit 32 → physical alias +0x4_0000_0000 (uncached through the L2 system port) |

Without XPbmtUC, `pgprot_writecombine()` on the U74 (no Svpbmt) is a no-op:
CPU mappings of firmware-shared memory are cacheable, so the host reads
stale CCB/fence data and misses completions ("missing FW completion IRQ",
job timeouts, FW hard resets). That was the community's worst bug and
Bo Gan's `ERRATA_SIFIVE_XPBMTUC` fixes it. The alias covers
0x4000_0000–0x2_3FFF_FFFF, i.e. **all 8 GB** of the 8 GB VisionFive 2.

## 5. Firmware versions

| Image | Build | Notes |
|---|---|---|
| `rogue_36.50.54.182_v1.fw` (yours, sha256 `b5232ac6…6326`) | v1.0 build 6503725 | older |
| `rogue_36.50.54.182_v1.fw` @ Imagination linux-firmware `8a58f818` | v1.1 build 6976702 | what the community tested; same build as the TH1520 image in linux-firmware |

The driver accepts both (`fw_version_major == 1`), but use **v1.1 build
6976702**. Between v1.0 (BXE) and v1.1 (BXM) the MIPS code grew by 348
bytes and gained BRN 44079 in its device info; whether v1.1 BXE does too
is the open question from §2. Run
`tools/pvrfw.py diff old.fw new.fw` and `tools/pvrfw.py check-ddk new.fw --ddk …`
on the v1.1 image to find out.

As of linux-firmware 20260915 the BXE-4-32 image is **not** in upstream
linux-firmware (only 33.15.11.3, 36.52.104.182, 36.53.104.796).

## 6. The PDS/USC "heap guard"

The community maps an 8 MiB guard BO above the highest allocation in the
PDS code/data and USC code heaps on every submit, because the GPU otherwise
faults just past the end. Mesa pads USC code by one instruction
(`ROGUE_MAX_INSTR_BYTES`) and PDS by nothing. The DDK's `USCCODE_HEAP_SIZE`
is 4 GiB − 32 MiB, but that hole is a reserved *Volcanic* region, not a
guard. There is no documented explanation; it stays an empirical
workaround behind `powervr.kernel_heap_guards` (default 1). A proper fix
would find the real prefetch distance and pad in Mesa instead.

## 7. Mesa

* `bxe-4-32.h` matches the firmware's device info (see §1–2).
* Only BXS-4-64 is whitelisted as conformant, hence
  `PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1`. Getting BXE-4-32 whitelisted
  needs a Vulkan CTS run on this board (`board/cts.md`).
* "Core count fetching is unimplemented" is harmless on this single-core
  GPU.

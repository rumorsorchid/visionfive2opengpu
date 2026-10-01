# OpenBSD on the VisionFive 2: graphics roadmap

Surveyed against `openbsd/src` master (October 2026).

## What OpenBSD already has

| Piece | Status | Where |
|---|---|---|
| JH7110 clocks (sys/aon/stg), pinctrl, PCIe, RNG, temp | yes | `sys/arch/riscv64/dev/stf*.c` |
| SiFive L2 cache controller (flush) | yes | `sys/arch/riscv64/dev/sfcc.c` |
| Uncached DRAM alias in pmap | **JH7100 only** | `pmap.c`: `pmap_cached_start`/`pmap_uncached_start` |
| DRM core, atomic KMS, bridges | yes | `sys/dev/pci/drm` |
| `drm_gpuvm`, GPU scheduler, `drm_exec`, syncobj, GEM DMA helpers | yes (ported for amdgpu/xe/apple) | `sys/dev/pci/drm` |
| `drm_gem_shmem_helper` | **no** | needed by powervr |
| FDT display drivers to copy from | `rkdrm` + `rkdwhdmi`, `qcdrm`, `apldrm` | `sys/dev/fdt`, `sys/dev/pci/drm/apple` |
| JH7110 PMU (power domains) | **no** | |
| JH7110 VOUT clocks / DC8200 / Inno HDMI | **no** | |

The PowerVR driver's heavy infrastructure (gpuvm, scheduler, dma-fence,
dma-resv) is already there, which makes a port realistic.

## Steps, in order

1. **Uncached alias for JH7110 in pmap** — `openbsd/0001-riscv64-pmap-jh7110-uncached-alias.diff`.
   Same mechanism OpenBSD already uses for the JH7100, with the JH7110
   window (cached 0x4000_0000–0x2_3FFF_FFFF, uncached alias +0x4_0000_0000).
   This is the OpenBSD equivalent of Linux's `ERRATA_SIFIVE_XPBMTUC`; without
   it a GPU driver will lose firmware completions exactly as Linux did.
   *Untested: needs a build and a boot on hardware.*
2. **`stfpmu(4)`**: JH7110 PMU power-domain driver registering with
   `power_domain_register()`; GPUA and VOUT domains are what graphics
   needs. Port from Linux `drivers/pmdomain/starfive/jh71xx-pmu.c`
   (~350 lines). Include the hardware-event masks once
   [power.md](power.md)'s experiment says what they should be.
3. **VOUT clocks** in `stfclock(4)` (Linux `clk-starfive-jh7110-vout.c`),
   including the HDMI-PHY-sourced pixel clock with rate propagation.
4. **`stfdrm(4)`**: DC8200 CRTC/planes + JH7110 Inno HDMI bridge + PHY,
   structured like `rkdrm` + `rkdwhdmi`. Port from the Linux
   `drivers/gpu/drm/verisilicon` and `jh7110-inno-hdmi` drivers in this
   series. Gives a proper (unaccelerated) KMS console and X/Wayland
   via `wsdisplay`.
5. **`pvrdrm(4)`**: port `drivers/gpu/drm/imagination`. Main work items:
   * GEM backing: either port `drm_gem_shmem_helper` or back objects with
     `uvm_aobj` like `amdgpu`/`i915` do on OpenBSD;
   * MIPS firmware page tables (`pvr_vm_mips.c`) and FW loading via
     `loadfirmware(9)` from `/etc/firmware/powervr/`;
   * runtime PM → `power_domain_enable()`/clock/reset calls.
6. **Mesa**: enable `-Dvulkan-drivers=imagination` and Zink in the
   `x11/mesa` port for riscv64, plus the OpenBSD winsys glue for the
   `powervr` render node.

Steps 1–4 are useful on their own (a real framebuffer/KMS desktop) and are
the right first contributions; 5–6 depend on the Linux driver being
upstream-stable for this core.

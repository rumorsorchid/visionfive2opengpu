# Booting the stack: U-Boot HDMI handoff and Debian sid

This page reviews how the `jh7110_hdmi` U-Boot driver (`u-boot.patch`,
`drivers/video/jh7110_hdmi.c`) hands the display to the operating system,
what Linux does with it, and what to set up on Debian sid so that the
open firmware (`openfw`) is the only GPU firmware on the board.

Everything here comes from reading the driver, the kernel tree in
`kernel/` and Mesa 26.2.3. None of it has been tried on a board.

## What U-Boot hands over

| | What `jh7110_hdmi` does | What it means for Linux |
|---|---|---|
| Display pipeline | powers `PD_VOUT`, enables the SYSCRG display clocks and the VOUTCRG clocks, routes DC8200 panel 0 to the HDMI transmitter, starts the PHY and transmitter, scans out at 1080p60 (or 720p60) | the screen works before the kernel starts; Linux's own display drivers program everything again when they probe |
| Pixel clock | divides PLL2 as SPL left it (1188 MHz); it never reprograms PLL2 | the GPU clock (`gpu_root` hangs off PLL2) is not changed by U-Boot. The driver prints the PLL2 rate at boot (`jh7110-hdmi: framebuffer ..., PLL2 ... Hz`); [tuning.md](tuning.md) explains how the GPU clock follows from it |
| GPU | not touched: no GPU power domain, clock or reset | `drm/imagination` brings the GPU up from cold, as without the patch |
| Framebuffer RAM | `1920 × 1080 × 4` bytes (7.9 MiB) below 4 GiB, which is all the DC8200 can address | 7.9 MiB is reserved for good, also after Linux's display driver takes over. Not worth reclaiming on an 8 GB board |
| Coherency | the CPU writes through the uncached alias of DRAM at +16 GiB (physical address bit 34), so no cache maintenance is needed | this is the same alias Linux uses for every uncached mapping on the JH7110 (XPbmtUC errata, kernel patches 23–25: PTE bit 32 = physical address bit 34). The two agree |
| EFI boot (`bootefi`, GRUB, systemd-boot) | EFI GOP at the uncached alias; the framebuffer RAM is `EFI_RESERVED_MEMORY_TYPE` in the EFI memory map | Linux never allocates the scanout RAM. With `CONFIG_DRM_EFIDRM` (now in the config fragment) the kernel console appears on HDMI from the start |
| Device tree | `/reserved-memory/framebuffer` (`no-map`) added; the U-Boot-only `hdmi-framebuffer` node deleted | holds for `booti`/extlinux boots too, which get no GOP |
| Console | `stdout=serial,vidconsole`, `stdin=serial,usbkbd` | U-Boot menus work on the HDMI screen with a USB keyboard |

So the handoff is correct for Linux and nothing in it gets in the way of
the GPU stack. Reserving the RAM in both the EFI map and the device tree
is the important part: if either one were missing, the console would
overwrite live kernel memory, or the kernel would hand out memory that is
still being scanned out.

## What Linux does with it

1. **Early boot.** With an EFI boot, `efidrm` drives the GOP framebuffer.
   It is not in the EFI memory map (only the cached address is reserved),
   so `efidrm` maps it write-combined, which is uncached on the JH7110.
   With a `booti`/extlinux boot (Debian's `u-boot-menu`) there is no early
   framebuffer: the screen keeps U-Boot's last picture, which is safe
   because that RAM is reserved.
2. **`late_initcall`.** The kernel turns off clocks and power domains that
   no driver has claimed. The display drivers are modules
   (`verisilicon-dc`, `jh7110-inno-hdmi`, `phy-jh7110-inno-hdmi`), so they
   have not claimed them yet: the screen goes **dark for a few seconds**.
   This is expected.
3. **The display driver loads.** `verisilicon-dc` removes the firmware
   framebuffer (`aperture_remove_all_conflicting_devices`) and sets the
   monitor's preferred mode. The console comes back.

To shorten the dark gap, load the display modules from the initramfs
(the VOUT clock controller and subsystem drivers first: the display
driver needs them but does not depend on them by symbol, so
initramfs-tools would not pull them in by itself):

```sh
printf '%s\n' clk-starfive-jh7110-vout jh7110-vout-subsystem jh7110-hdmi-subsystem \
    phy-jh7110-inno-hdmi jh7110-inno-hdmi verisilicon-dc \
    | sudo tee -a /etc/initramfs-tools/modules
sudo update-initramfs -u
```

`clk_ignore_unused pd_ignore_unused` on the kernel command line keeps the
U-Boot picture up the whole time. It also keeps every other unused clock
and power domain on, so use it only to debug.

## Debian sid checklist

1. **Kernel: use the one from `kernel/`.** The JH7110 display series and
   the XPbmtUC errata (patches 1–26) are not in mainline v7.3, so Debian's
   own kernel has no HDMI and loses GPU firmware completions. Build with
   `kernel/build.sh`, install with `sudo board/install-kernel.sh
   linux-image-*.deb`. It handles both `u-boot-menu` (extlinux) and EFI
   boot.
2. **Firmware: openfw.**

   ```sh
   sudo board/openfw-test.sh openfw/prebuilt/rogue_36.50.54.182_v1.fw   # trial run, restores the old file
   sudo board/setup.sh openfw/prebuilt/rogue_36.50.54.182_v1.fw         # make it the installed firmware
   ```

   `setup.sh` installs it as `/lib/firmware/powervr/rogue_36.50.54.182_v1.fw`,
   sets `exp_hw_support=1` (BXE-4-32 is still marked experimental in
   `drm/imagination`) and rebuilds the initramfs, which matters if
   `powervr` is loaded from there. Check after a reboot:

   ```sh
   sudo dmesg | grep -i 'powervr.*FW version'   # openfw: "FW version v1.0 (build 0 OS)"
   ```

3. **Mesa: the Vulkan driver `imagination`, 26.1 or later.** Check that the
   ICD is there:

   ```sh
   ls /usr/share/vulkan/icd.d/ | grep -i powervr      # powervr_mesa_icd.riscv64.json
   PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1 vulkaninfo --summary
   ```

   If sid's `mesa-vulkan-drivers` does not ship it, build Mesa with
   `-Dvulkan-drivers=imagination -Dgallium-drivers=zink` (Zink provides
   OpenGL and GLES on top of Vulkan). Mesa's BXE-4-32 support is not
   conformance tested yet, hence `PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1`,
   which `setup.sh` sets for login shells and graphical sessions.
4. **Desktop.** There is no native OpenGL driver for this GPU, and Mesa's
   render-only (`kmsro`) list does not include the `verisilicon` display
   driver. So a compositor that renders with OpenGL on the display device
   gets software rendering. Use Vulkan for the compositor and Zink for GL
   clients:

   ```sh
   export WLR_RENDERER=vulkan                 # labwc, sway and other wlroots compositors
   export MESA_LOADER_DRIVER_OVERRIDE=zink    # GL/GLES clients through Zink
   labwc                                      # or: sway; weston --renderer=vulkan
   ```

   The PowerVR Vulkan driver has the extensions a Vulkan compositor needs
   to scan out through KMS (`VK_EXT_image_drm_format_modifier`,
   `VK_EXT_external_memory_dma_buf`, `VK_EXT_physical_device_drm`,
   `VK_EXT_queue_family_foreign`). `board/bench.sh wayland` runs vkmark and
   glmark2 inside such a session.
5. **Memory.** The config fragment reserves 256 MiB of CMA for scanout
   buffers. Raise it with `cma=` on the command line for several 4K
   buffers.

## When something goes wrong

* **No picture in U-Boot:** stop autoboot and run `hdmitest` (driver
  profiles) and `hdmiregs`, as the driver suggests.
* **Picture in U-Boot, black for good after the kernel starts:** check that
  `verisilicon-dc` loaded (`lsmod`, `dmesg | grep -i verisilicon`) and that the
  DTB is the v1.3b one from the kernel package (`install-kernel.sh` wires
  it up).
* **GPU problems:** `sudo board/vf2-gpu-check.sh --run` collects the state.
  With openfw, a job timeout or firmware reset in `dmesg` plus the firmware
  trace (`/sys/kernel/debug/dri/*/pvr_fw/trace_0`, enabled by
  `openfw-test.sh`) is what to send. Reinstalling Imagination's firmware is
  a one-line way back (`setup.sh path/to/imagination.fw`).

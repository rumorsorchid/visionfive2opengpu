# Flashable Debian sid image for the VisionFive 2

A complete disk image: write it to the NVMe drive (or an SD card), boot,
and you get a Debian sid desktop running on the open GPU stack. Nothing
to install afterwards.

| | |
|---|---|
| Board | VisionFive 2 rev 1.3B (JH7110); boots with mainline U-Boot + OpenSBI already in the board's flash |
| Kernel | v7.3-rc5 + the patches in [`kernel/`](../kernel), blob-free (below) |
| GPU firmware | openfw (MIT), the only firmware in the image |
| Graphics | Mesa from Debian sid: PowerVR Vulkan driver, Zink for OpenGL/GLES |
| Desktop | labwc (Wayland) with GPU compositing, waybar, foot, Thunar, XWayland, PipeWire |
| Browser | Firefox ESR with WebRender and WebGL on the GPU through Zink |
| Tests | first-boot GPU self-test, `vf2-desktop-test`, vkmark, glmark2, vkcube, board scripts |
| Extras | RetroArch with free cores and PPSSPP when Debian builds them for riscv64 (no ROMs or BIOS files) |
| Archive | Debian `main` only: no contrib, non-free or non-free-firmware |

Login: user `vf2`, password `vf2`. SSH is on, so change the password
first (`passwd`).

## What has and has not been tested

* The build script was trial-run against Ubuntu's riscv64 archive, because
  Debian's mirrors were out of reach of the machine it was written on. That
  image booted in QEMU through mainline U-Boot with the VisionFive 2's
  standard-boot configuration, and `qemu-test.py` passed all eight checks:
  * U-Boot found the extlinux bootflow on the bootable partition;
  * the compressed kernel and initramfs loaded;
  * root mounted by UUID, and the system came up with no failed units;
  * first boot grew the root filesystem from 2 GB to fill a larger disk;
  * the GPU self-test skipped cleanly (no JH7110);
  * greetd logged `vf2` into labwc, which drew the panel and the welcome
    terminal.

  See [docs/evidence/image-qemu-test.txt](../docs/evidence/image-qemu-test.txt)
  and the screenshot [image-qemu-desktop.png](../docs/evidence/image-qemu-desktop.png).
  The trial runs caught three bugs before any of them reached a board:
  * a file conflict with `systemd-zram-generator`;
  * u-boot-menu 4.2 dropping the device-tree line, which would have booted
    without GPU and HDMI;
  * the desktop refusing to start without a keyboard plugged in.

  Reviewing the boot path found a fourth: the NVMe root depended on clock
  and PCIe modules the initramfs would not have loaded. These drivers are
  now built into the kernel.
* The released Debian sid images are built by GitHub Actions
  ([`.github/workflows/image.yml`](../.github/workflows/image.yml)) with the
  same script. Debian packages that are missing for riscv64 fail that build,
  except for the optional ones listed in each release's `.build-info`.
  Each image must pass the same QEMU boot test before it is published. The
  results and a screenshot are attached to the release.
* Nobody has booted the image on a VisionFive 2 yet. QEMU cannot emulate
  the JH7110, its HDMI or its GPU. The first boot runs the GPU tests
  before the desktop starts, so the first boot tells you how it went.

## 1. Get the image

Download the newest `vf2-debian-sid-*.img.gz` and its `.sha256` from the
repository's [releases](https://github.com/rumorsorchid/visionfive2opengpu/releases).
From OpenBSD:

```sh
ftp https://github.com/rumorsorchid/visionfive2opengpu/releases/download/<tag>/<name>.img.gz
ftp https://github.com/rumorsorchid/visionfive2opengpu/releases/download/<tag>/<name>.img.gz.sha256
sha256 -C <name>.img.gz.sha256 <name>.img.gz
```

Or build it yourself (section 6).

## 2. Write it to the NVMe drive from OpenBSD

Run OpenBSD from somewhere other than the target drive (SD card, eMMC or
USB). On OpenBSD the NVMe drive is an `sd` disk:

```sh
dmesg | grep -A2 '^nvme'        # "sd1 at scsibus1 ...": the NVMe disk is sd1
sysctl hw.disknames             # the same, with the DUIDs
```

Then, as root, with `sd1` replaced by your NVMe disk (the `c` partition is
the whole disk; **everything on it is lost**):

```sh
gunzip -c <name>.img.gz | dd of=/dev/rsd1c bs=1m
sync
```

The image is about 5 GB. On first boot it grows to fill the drive.
OpenBSD may warn that the backup GPT is not at the end of the disk. That
is expected: first boot moves it.

From Linux, use the same command with `of=/dev/nvme0n1` (or the SD card
device). On an SD card the image boots too (U-Boot finds it the same way).

## 3. Make U-Boot boot the NVMe drive

U-Boot's standard boot scans SD and eMMC before NVMe, so a bootable
OpenBSD on an SD card or eMMC would still win. Pick one of these:

* remove the SD card that has OpenBSD on it, or
* boot the NVMe once: interrupt autoboot (serial console, or a USB
  keyboard with the HDMI screen) and type `bootflow scan -lb nvme`, or
* make NVMe the first choice for good, keeping SD (`mmc1`) and eMMC (`mmc0`)
  as fallbacks:

  ```
  setenv boot_targets "nvme mmc1 mmc0 usb dhcp"
  saveenv
  ```

  OpenBSD stays reachable with `bootflow scan -lb mmc1` (or `mmc0`).

U-Boot finds the image's partition because it is marked bootable. It
reads `/boot/extlinux/extlinux.conf` from it and loads the kernel,
initramfs and the rev 1.3B device tree from the installed kernel package.

## 4. First boot

1. U-Boot loads the kernel within a few seconds. The screen can go dark
   for a few seconds while Linux takes over the display (docs/boot.md).
   Then the console appears on HDMI and on the serial port (115200 8N1).
2. **vf2-firstboot** grows the partition and filesystem to fill the drive
   and creates the SSH host keys.
3. **vf2-selftest** runs the GPU tests before the desktop starts. This takes
   a few minutes and prints its progress on the screen. It checks:
   * the stack (XPbmtUC, CMA, firmware, modules, DRM devices, HDMI);
   * openfw on real jobs (`openfw-test.sh`): runtime suspend/resume, Vulkan,
     vkmark scenes, parameter-buffer growth at 1080p, two clients at once,
     Zink;
   * a final Vulkan run.

   Reports go to `/var/log/vf2/`, the summary to
   `/var/log/vf2/selftest-summary.txt`.
4. greetd logs `vf2` into the labwc desktop. A welcome terminal shows the
   self-test result and offers the on-screen test (`vf2-desktop-test`:
   compositor renderer, vkcube, vkmark and glmark2 windowed, Zink on
   Wayland and X11).

If the self-test fails, the desktop composites in software, so you can
still read the reports. If the board hangs during the self-test, switch it
off and on: the second boot sees the unfinished run, skips it, marks the
GPU as failed and starts the desktop in software.

## 5. Using it

| | |
|---|---|
| Super+Enter | terminal (foot) |
| Super+D, Super+Space | application launcher (fuzzel) |
| Super+B | Firefox |
| Super+E | files (Thunar) |
| Super+Q, Super+F, Super+Up | close, fullscreen, maximise |
| right-click the desktop | menu: GPU tests, benchmarks, log out, reboot |
| panel "GPU: PowerVR" | GPU compositing is active; click for the report |

* `vf2-desktop-test`: on-screen GPU test inside the desktop.
* `sudo vf2-selftest`: the full job test again. Log out first: it needs an
  idle GPU. `sudo vf2-selftest --quick` works while the desktop runs.
* `sudo /usr/lib/vf2/board/vf2-gpu-check.sh --run`: a report for bug
  reports, together with `/var/log/vf2/` and
  `~/.local/state/vf2-session.log`.
* `/usr/lib/vf2/board/bench.sh wayland` (or `headless`): benchmarks.
* Software compositing: set `VF2_RENDERER=pixman` in
  `/etc/default/vf2-desktop`.
* Keyboard layout: `XKB_DEFAULT_LAYOUT=de` (for example) in
  `~/.config/labwc/environment`.
* Updates: `sudo apt update && sudo apt full-upgrade` updates Debian. The
  kernel and openfw come from this repository: a newer release carries
  them as `.deb` files.

Why GLES2 through Zink for the compositor: wlroots' Vulkan renderer
needs `VK_KHR_synchronization2`, which Mesa's PowerVR driver does not
offer yet. labwc therefore renders with GLES2, which Zink turns into
Vulkan on the PowerVR GPU. The scanout buffers live on the display
controller (wlroots allocates them there), and the GPU renders into them.

## Troubleshooting

* **U-Boot boots OpenBSD (or nothing) instead.** See section 3. At the U-Boot
  prompt, `nvme scan; part list nvme 0` should list partition 1 as
  bootable, and `bootflow scan -l nvme` should list an extlinux bootflow.
  If `printenv bootmeths` shows a value (for example only `efi`), clear it:
  `setenv bootmeths; saveenv`.
* **The kernel starts but the screen stays black.** Log in over the serial
  console (115200 8N1) or SSH (host `vf2`, user `vf2`), then run
  `sudo /usr/lib/vf2/board/vf2-gpu-check.sh` and see docs/boot.md, "When
  something goes wrong".
* **The panel says "GPU: software".** The reason is in
  `/var/log/vf2/selftest-summary.txt` (the first-boot test) and in
  `~/.local/state/vf2-session.log` (the compositor). To run the first-boot
  test again on the next boot: `sudo rm /var/lib/vf2/selftest-done
  /var/lib/vf2/gpu-status; sudo reboot`.
* **A GPU job hangs or the firmware resets.** Send the output of
  `sudo vf2-selftest`, `/var/log/vf2/openfw-test-*.txt` (it includes the
  firmware trace) and `journalctl -k -b`.
* **Starting over.** Write the image again. It holds no state from earlier
  boots.

## How open is it

| Stage | What runs | Source |
|---|---|---|
| Boot ROM | mask ROM inside the JH7110 | fixed in silicon, not replaceable |
| SPL, OpenSBI, U-Boot | your flash: mainline U-Boot (with open DDR init) + OpenSBI + `jh7110_hdmi` | open source |
| Kernel | v7.3-rc5 + this repository's patches | open source, blob-free: see below |
| GPU firmware | openfw | MIT; CI rebuilds it from source and refuses an image unless the result equals the published file byte for byte |
| Userspace | Debian sid `main` | DFSG-free; the build fails if any installed package comes from contrib or non-free, or if `/usr/lib/firmware` holds anything besides openfw |

**Blob-free kernel.** Mainline Linux contains no firmware files, but many
drivers ask userspace for them. linux-libre removes those requests. It
would also block the PowerVR firmware by name, even though here that file
is openfw. So this kernel is kept blob-free by configuration instead:
[`kernel/config/vf2-libre-desktop.config`](../kernel/config/vf2-libre-desktop.config)
switches off every driver that would load non-free firmware. None of that
hardware is on the board: radeon, nouveau, r8169, and Realtek and
Microsemi PHYs. [`kernel/blob-audit.sh`](../kernel/blob-audit.sh) then
checks every built object and module after each build. The only code that
can load firmware is powervr, which loads openfw. The devlink and ethtool
flash commands also remain, but they only write a file you name yourself.

**Not loaded and not used.** The JH7110's video codecs and ISP need
closed firmware, so they stay off. The video decoder is disabled in
Firefox as well.

**Chips that carry their own firmware.** The NVMe drive's controller and
the board's VL805 USB 3 controller run firmware stored on the chips
themselves. The OS never loads it, and every computer with an SSD or USB 3
has the same.

## 6. Building the image

On a Debian or Ubuntu x86_64 machine (or a riscv64 one), as root:

```sh
apt install mmdebstrap debian-archive-keyring arch-test qemu-user-static fdisk e2fsprogs pigz build-essential \
            gcc-riscv64-linux-gnu bc bison flex kmod libssl-dev libelf-dev libdw-dev rsync debhelper dpkg-dev
./kernel/build.sh                                  # kernel debs in ./, with the blob audit
image/build.sh --kernel-debs .                     # out/vf2-debian-sid-<date>.img.gz
image/qemu-test.py out/*.img.gz --uboot "$(image/qemu-uboot.sh | tail -n 1)"   # boot test
```

`image/build.sh --help` lists the options: user, password, host name,
package lists, and the archive. With `--packages` you can build a smaller
or a different desktop.

What goes into the image:

* `packages.txt`: the required packages (the build fails without them).
* `packages-optional.txt`: installed when Debian has them for riscv64.
* `customize.sh`: configures the system inside the chroot and checks the result.
* `overlay/`: becomes the `vf2-integration` package: session, first boot,
  self-test, desktop defaults and the board scripts in `/usr/lib/vf2/board`.
* `openfw-firmware`: built from `openfw/prebuilt`, with openfw's source
  inside the package.

Each release also carries these three packages as `.deb` files, for adding
the stack to an existing Debian sid installation
(`apt install ./linux-image-*.deb ./openfw-firmware_*.deb ./vf2-integration_*.deb`).

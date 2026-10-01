# VisionFive 2: open GPU + HDMI stack

Accelerated graphics on the StarFive VisionFive 2 (JH7110, tested target:
rev 1.3B 8 GB) using only open drivers:

```
 Vulkan (Mesa PowerVR "imagination")  ──┐
 OpenGL / GLES (Mesa Zink on Vulkan) ───┤ userspace
 Wayland / X (labwc, Weston, XWayland) ─┘
 drm/imagination (powervr.ko)  + verisilicon-dc + JH7110 Inno HDMI   kernel
 rogue_36.50.54.182_v1.fw  (Imagination, redistributable binary)     GPU firmware
```

This repository assembles the pieces that exist (upstream Linux, the
JH7110 display series, the community GPU bring-up, Mesa), checks them
against StarFive's vendor DDK and Imagination's firmware, fixes what can
be fixed without hardware, and provides the tools to test and debug the
rest on a real board.

## Status

| Piece | State | Where |
|---|---|---|
| HDMI display (DC8200 + Inno HDMI) | works with the on-list series | `kernel/patches` 1–21, 26 |
| Cache coherency (U74 has no Svpbmt) | fixed by Bo Gan's XPbmtUC errata | patches 22–25 |
| GPU kernel driver | runs Vulkan/Zink; BXE-4-32 now a proper *experimental* core with DT binding | patches 27–41 |
| Rascal/dust power-up | firmware-derived host sequence; vendor-style `rd_power_island` path to test | [docs/power.md](docs/power.md) |
| Mesa | 26.1+ supports BXE-4-32, non-conformant (`PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1`) | [board/cts.md](board/cts.md) |
| GPU firmware | Imagination binary; v1.1 b6976702 recommended | [firmware/](firmware/README.md) |
| Open firmware | analysis tooling + roadmap, no replacement yet | [docs/firmware.md](docs/firmware.md) |
| OpenBSD | roadmap + first patch (uncached DRAM alias) | [docs/openbsd.md](docs/openbsd.md) |

Community results with this stack (Mesa 26.2, KMS, 1080p): vkmark
`clear` 485 FPS, `cube` 140, `shading` 84; glmark2-es2 under labwc
(Zink) 1–60 FPS depending on scene. Source:
[domibel/visionfive2_gpu_bringup](https://github.com/domibel/visionfive2_gpu_bringup).

## Quick start

On an x86_64 (or riscv64) build host:

```sh
sudo apt install gcc-riscv64-linux-gnu flex bison bc libssl-dev libelf-dev dpkg-dev
./kernel/build.sh                 # clones v7.3-rc5, applies patches, builds .debs
```

On the board (Debian/Ubuntu riscv64), after installing the kernel `.deb`s
and the DTB `jh7110-starfive-visionfive-2-v1.3b.dtb`:

```sh
sudo board/setup.sh rogue_36.50.54.182_v1.fw   # firmware, modprobe opts, Mesa env
sudo reboot
sudo board/vf2-gpu-check.sh --run              # verify + report file
board/bench.sh headless                        # benchmarks
```

## Findings worth knowing

* The BXE-4-32 core is, by Imagination's own tables, identical to the
  TH1520's already-supported BXM-4-64 except for ISP pipe count. Every
  JH7110 problem has been SoC integration.
* The "missing FW completion IRQ" was cache coherency: write-combined
  mappings are cacheable on the U74 without XPbmtUC.
* StarFive's driver tells the firmware to manage the shader power island
  (`POW_RASCALDUST`); upstream never does. The firmware contains its own
  `POWER_EVENT` routine, which settles the formerly guessed bits.
* v1.0 build 6503725 images of the firmware are in circulation; the
  tested build is v1.1 build 6976702.
* Two real `drm/imagination` bugs from the community series (remap NULL
  deref / GEM leak) affect every PowerVR SoC and should go upstream.
* Full write-up: [docs/ddk-vs-upstream.md](docs/ddk-vs-upstream.md).

## Help needed on hardware

1. `sudo board/power-ab-test.sh` → which power-up path works (decides the
   upstream fix).
2. `tools/pvrfw.py check-ddk` on the v1.1 firmware.
3. Vulkan CTS run ([board/cts.md](board/cts.md)) → path to Mesa
   conformance whitelisting.

## Layout

```
kernel/    patch series on v7.3-rc5, config fragment, build script
board/     on-board scripts: setup, health check, benchmarks, power A/B, register probe, CTS guide
tools/     pvrfw.py (firmware container/device-info/DDK cross-check), fwregs.py (register census)
docs/      analysis, power investigation, firmware, tuning, OpenBSD
firmware/  where to get the firmware and how to verify it
openbsd/   OpenBSD patches
```

## Credits

The display and GPU enablement this builds on is the work of Michal
Wilczynski, Icenowy Zheng, Dominique Belhachemi, Bo Gan, Samuel Holland
and the Imagination drm/imagination and Mesa teams (Alessio Belle, Simon
Perretta, Frank Binns, and others). Vendor reference: StarFive's
`JH7110_VisionFive2_6.12.y_devel` kernel (DDK 1.19, dual MIT/GPLv2).

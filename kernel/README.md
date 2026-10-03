# Kernel patch series: VisionFive 2 GPU + HDMI on Linux v7.3-rc5

`patches/` applies with `git am` on top of **v7.3-rc5**
(`72d3fcf802c4`). `build.sh` clones, patches, configures with the
fragments in `config/` and builds Debian packages:

* `vf2-gpu-hdmi.config`: the GPU and HDMI stack;
* `vf2-libre-desktop.config`: no drivers that load non-free firmware,
  plus desktop options (FUSE, exFAT/NTFS, zram, USB audio, game
  controllers with uinput/hidraw, SysRq).

## Blob-free

Mainline Linux ships no firmware files, but drivers request them at
runtime. `blob-audit.sh`, which `build.sh` runs after every build, checks
what was linked: every member of `vmlinux.a` and every module in
`modules.order` that calls a firmware loader, plus every declared firmware
name. Only powervr may load firmware, and on this board that is openfw.
The devlink and ethtool flash commands are also allowed, because they
write only a file the user names. The riscv `defconfig` alone fails the
audit: radeon, nouveau, r8169, and the Realtek and Microsemi PHY drivers
request non-free microcode, so `vf2-libre-desktop.config` turns them off.
None of that hardware is on the VisionFive 2. linux-libre itself is not
used: its deblobbing blocks the PowerVR firmware by file name, which here
is the open firmware.

## Patch 42: virtio-gpu (QEMU)

v7.3-rc5's virtio-gpu creates the cursor plane's "pixel blend mode"
property twice. Reading the first copy fails with -EINVAL, which makes
wlroots 0.20 refuse to start on QEMU. Patch 42 drops the duplicate. It
does not affect the VisionFive 2's display driver (verisilicon-dc
creates the property once); it lets `image/qemu-test.py` boot the image
into labwc.

## Verification done for this series

On an x86_64 host with `riscv64-linux-gnu-gcc` 13.3:

| Check | Result |
|---|---|
| `defconfig` + fragments, `make Image modules dtbs` | builds; every fragment line survives `olddefconfig`, every disabled symbol stays off |
| `blob-audit.sh` on that build | clean: only powervr (openfw) can load firmware |
| `make W=1 drivers/gpu/drm/imagination/` | clean (after patch 41) |
| `dt_binding_check DT_SCHEMA_FILES=gpu/img,powervr-rogue.yaml` | passes, incl. the new JH7110 example |
| Binding negative tests (JH7110 node with 3 clocks / 1 reset; TI node with 5 clocks) | rejected as intended |
| `CHECK_DTBS=y jh7110-starfive-visionfive-2-v1.3b.dtb` | no warnings |
| `checkpatch.pl --strict` on patches 27–30, 40–44 | only a trailer-capitalisation nit (`Co-Authored-By`) |
| `build.sh` on a fresh v7.3-rc5 clone (`git am` of all 44, build, `bindeb-pkg`; also in CI on every image build) | `linux-image` .deb with `powervr.ko`, the display drivers built in, and the v1.3b DTB |

**Not done here:** booting it. There is no VisionFive 2 in this
environment; everything hardware-facing needs `board/` run on a real
board.

## Patches

### Display (1–26), unchanged

From Dominique Belhachemi's `jh7110_dc8200_hdmi_v7.3-rc5` branch, which
carries the on-list series:

| # | Author | Content |
|---|---|---|
| 1–21 | Michal Wilczynski | JH7110 display: DC8200 binding, Inno HDMI bridge rework + JH7110 HDMI controller, Inno HDMI PHY helpers + JH7110 PHY, VOUT/HDMI subsystem drivers, DT, MAINTAINERS |
| 22 | Samuel Holland | `ALTERNATIVE_3` macro |
| 23–25 | Bo Gan | **XPbmtUC**: uncached mappings via the JH7110's uncached DRAM alias (PTE bit 32). Without this the GPU loses firmware completions. |
| 26 | Michal Wilczynski | VOUT pixel-clock rate propagation (already in clk-next) |

### GPU (27–41)

| # | Author | Content | Upstream readiness |
|---|---|---|---|
| 27 | new | dt-bindings: `starfive,jh7110-gpu` (5 clocks, 2 resets, 1 PD, `dma-noncoherent`) | ready for review |
| 28 | new, based on D. Belhachemi | driver: optional `apb`/`rtc` clocks, resets as an ordered array; no-op for other SoCs | ready for review |
| 29 | new | BXE-4-32 as `PVR_GPU_EXPERIMENTAL` (needs `exp_hw_support=1`) instead of deleting the support check | ready for review |
| 30 | new, based on D. Belhachemi | DT node matching the binding (`core` = the `GPU_CORE_CLK` gate) | ready for review |
| 31 | Icenowy Zheng | host rascal/dust power-up through `POWER_EVENT` | superseded in behaviour by 40; kept for authorship |
| 32 | D. Belhachemi | flush fence-release work before teardown | needs description + Signed-off-by |
| 33 | D. Belhachemi | `drm_warn` on FW hard reset | debugging aid |
| 34 | D. Belhachemi | skip FW cleanup for never-kicked contexts/free lists | workaround; root cause unknown |
| 35 | D. Belhachemi | no MMU flush after device lost | needs description |
| 36 | D. Belhachemi | **remap links split VAs to the wrong `vm_bo`** (NULL deref on partial unmap: `gpuvm_bo` is only set for map ops) | real upstream bug on every PowerVR SoC; squash with 37, add Fixes: |
| 37 | D. Belhachemi | **remap leaks a GEM reference per split** | real upstream bug; squash with 36 |
| 38 | D. Belhachemi | 8 MiB guard BO above the PDS/USC heap high-water marks | empirical workaround, see docs §6 |
| 39 | D. Belhachemi | don't send CLEANUP for a context with jobs in flight | workaround; the context is freed either way, which no firmware survives if it still holds it. Should not trigger: jobs hold context references until they complete, and with openfw's in-firmware recovery even a hung job completes |
| 40 | new | POWER_EVENT sequence taken from the firmware's own routine; `jh7110_power_event` selector; `rd_power_island` parameter (`POW_RASCALDUST`) | depends on `board/power-ab-test.sh` results |
| 41 | new | kernel-doc fix + param description for 38 | fold into 38 |
| 42 | new | virtio-gpu: create the cursor plane's blend mode property once (the duplicate made wlroots' atomic commits fail with `EINVAL`; QEMU only, not used on the board) | upstreamable, add Fixes: 6947b78df4d2 |
| 43 | new | `jh7110_power_event` defaults to 2, the community's single `0xff000703` write that runs on boards; the firmware-derived sequence stays as 1 | fold into 40 once `board/power-ab-test.sh` has picked a winner |
| 44 | new | JH7110: release GPU APB, wait 1 µs, then GPU domain A; assert in reverse with the same gap (the community's tested order; the array released both at once). Other SoCs keep the array | needs a DT binding note if upstreamed |

"new" patches were written in this session (git author "Claude") and have
no `Signed-off-by`: whoever submits them must review them and add their own
DCO sign-off.

## Module parameters

| Parameter | Default | Purpose |
|---|---|---|
| `exp_hw_support` | 0 | **must be 1** for BXE-4-32 (see `board/setup.sh`) |
| `jh7110_power_event` | 2 | 0 none, 1 firmware sequence, 2 community single write tested on boards |
| `rd_power_island` | 0 | pass `POW_RASCALDUST` to the firmware like StarFive's DDK |
| `kernel_heap_guards` | 1 | PDS/USC guard mapping (patch 38) |
| `init_fw_trace_mask` | 0 | firmware trace groups, read via `/sys/kernel/debug/dri/*/pvr_fw/trace_0` |

## Refreshing the series

```sh
git -C linux checkout vf2-gpu-hdmi
git -C linux format-patch --no-signature -o ../kernel/patches v7.3-rc5..
```

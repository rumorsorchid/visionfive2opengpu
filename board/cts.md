# Running the Vulkan CTS on the VisionFive 2

Mesa only enables PowerVR cores that have passed the Khronos Vulkan
Conformance Test Suite (today: BXS-4-64). Getting BXE-4-32 off the
`PVR_I_WANT_A_BROKEN_VULKAN_DRIVER` list needs CTS results from real
hardware, which nobody has published for this board yet.

## Build

Building VK-GL-CTS natively on the VisionFive 2 takes many hours; cross
compiling on an x86 host is far quicker.

```sh
git clone https://github.com/KhronosGroup/VK-GL-CTS
cd VK-GL-CTS
python3 external/fetch_sources.py
cmake -B build-rv64 -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE=<riscv64 toolchain file with a Debian sysroot> \
    -DDEQP_TARGET=default -DCMAKE_BUILD_TYPE=Release
ninja -C build-rv64 deqp-vk
```

Copy `build-rv64/external/vulkancts/modules/vulkan/` to the board. Install
[`deqp-runner`](https://gitlab.freedesktop.org/mesa/deqp-runner)
(`cargo install deqp-runner`) for parallel runs, crash isolation and
result summaries.

## Run

Start small; the full suite is ~1 M tests.

```sh
export PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1 MESA_VK_DEVICE_SELECT=1010:36054182
cd vulkan
# smoke test
./deqp-vk --deqp-case='dEQP-VK.api.smoke.*'
# a 1/50 sample of everything, 4 jobs, with per-test timeouts
deqp-runner run --deqp ./deqp-vk --caselist ../mustpass/main/vk-default.txt \
    --fraction 50 --jobs 4 --timeout 120 --output results-1of50
```

Watch `dmesg` while it runs; job timeouts or FW hard resets are kernel or
firmware bugs worth reporting separately from plain test failures.

## Reporting

Keep `results-*/failures.csv` together with the output of
`board/vf2-gpu-check.sh` (kernel, firmware build, Mesa version). Mesa's CI
keeps per-device expectation files under `src/imagination/ci/`
(`bxs-4-64-{fails,flakes,skips}.txt`, run via `deqp-pvr-vk.toml`); a
`bxe-4-32-*` set in the same format is what a Mesa merge request enabling
this core would carry.

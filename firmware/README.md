# GPU firmware for the JH7110 (BXE-4-32)

The upstream `powervr` driver loads `powervr/rogue_36.50.54.182_v1.fw`.
This is Imagination's redistributable firmware **for the open-source
driver**; it is a different binary from (and not compatible with) the
`rgx.fw.36.50.54.182` that StarFive ships with its proprietary DDK.

This repository does not redistribute it. Get it from Imagination:

| Build | Status | Source |
|---|---|---|
| v1.1 build 6976702 | **recommended**, tested by the community | `https://gitlab.freedesktop.org/imagination/linux-firmware/-/raw/8a58f81883f7be458daa34e418cc4079f995b279/powervr/rogue_36.50.54.182_v1.fw` |
| v1.0 build 6503725 | loads, older | sha256 `b5232ac64c0c708ee66400f40033ba4da8895a04ae688bc14821f59c5d4a6326` |

As of linux-firmware 20260915 the image is **not** in upstream
linux-firmware (only 33.15.11.3, 36.52.104.182 and 36.53.104.796 are).

Install on the board:

```sh
sudo board/setup.sh rogue_36.50.54.182_v1.fw
```

Inspect any image before trusting it:

```sh
tools/pvrfw.py info rogue_36.50.54.182_v1.fw
tools/pvrfw.py check-ddk rogue_36.50.54.182_v1.fw --ddk <starfive linux>/drivers/gpu/drm/img/img-rogue
tools/pvrfw.py diff old.fw new.fw
```

`check-ddk` compares the device information embedded in the image (which
the driver uses for its feature and quirk flags) against the hardware
definitions in StarFive's openly licensed DDK kernel source. Please report
the output for the v1.1 image; in particular whether it adds BRN 44079
(see `docs/ddk-vs-upstream.md` §2).

See `docs/firmware.md` for the image format, what the firmware does and
what an open replacement would involve.

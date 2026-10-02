# Optional tuning

None of this is applied by default.

## GPU core clock

StarFive's DDK runs the GPU at 594 MHz (396 MHz on boards whose device tree
has the 1.25 GHz CPU operating point). With mainline U-Boot/OpenSBI the
GPU usually runs at **409.6 MHz**:

```
gpu_root  = mux(pll2_out, pll1_out)   (pll2 selected)
gpu_core  = gpu_root / N, N = 1..7
pll2_out  = 1228.8 MHz  ->  /2 = 614.4 MHz, /3 = 409.6 MHz
```

Which PLL2 rate a board has depends on the SPL, and the sources disagree:
the `jh7110_hdmi` U-Boot driver expects 1188 MHz ("as the mainline SPL
sets it"), which gives `gpu_core` = 396 MHz (/3) or 594 MHz (/2), the
vendor rates. That driver prints the rate it finds at boot
(`jh7110-hdmi: framebuffer ..., PLL2 ... Hz`), which settles it for a
given board. The firmware's lockup detection counts GPU timer ticks
(core clock / 256), so it scales with whichever rate is set.

Check on the board:

```sh
sudo cat /sys/kernel/debug/clk/gpu_core/clk_rate
sudo cat /sys/kernel/debug/clk/pll2_out/clk_rate
sudo cat /sys/kernel/debug/clk/pll1_out/clk_rate
```

Options, in increasing order of risk:

1. **Leave it.** 409.6 MHz is what all published community numbers use.
2. **PLL1 as parent.** If `pll1_out / 2` lands at or below 594 MHz, a
   device tree override can reparent `gpu_root`:

   ```dts
   &gpu {
       assigned-clocks = <&syscrg JH7110_SYSCLK_GPU_ROOT>,
                         <&syscrg JH7110_SYSCLK_GPU_CORE>;
       assigned-clock-parents = <&pllclk JH7110_PLLCLK_PLL1_OUT>;
       assigned-clock-rates = <0>, <533000000>;
   };
   ```

   PLL1 also clocks DDR, so its rate is fixed by the memory configuration;
   only the GPU's divider changes.
3. **PLL2 = 1188 MHz** (vendor setting) gives exactly 594 MHz, but PLL2
   also feeds `bus_root`, `perh_root` and audio, so it has to be done in
   U-Boot/OpenSBI and tested system-wide.

The firmware is told the core clock at boot (`Core clock set to ... Hz` in
its trace), so a rate change needs a driver reload. Measure with
`board/bench.sh` before and after, and watch for thermal throttling.

## CMA

The display controller scans out of contiguous memory. Add `cma=256M`
(or more for multiple 4K buffers) to the kernel command line if you see
allocation failures; the config fragment sets 256 MiB as default.

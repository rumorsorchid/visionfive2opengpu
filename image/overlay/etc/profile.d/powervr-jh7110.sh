# VisionFive 2 / BXE-4-32: Mesa's PowerVR Vulkan driver is not yet
# conformance tested on this core, so it has to be enabled explicitly.
export PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1
export MESA_VK_DEVICE_SELECT=1010:36054182

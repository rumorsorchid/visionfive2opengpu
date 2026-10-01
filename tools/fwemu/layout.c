// SPDX-License-Identifier: MIT
/*
 * Compiled on the host only to capture the firmware interface layouts in
 * DWARF (see extract_layout.py). Including the headers also runs the
 * kernel's own static_assert offset checks against this compiler.
 */
#include "pvr_rogue_fwif.h"
#include "pvr_rogue_fwif_client.h"
#include "pvr_rogue_fwif_shared.h"
#include "pvr_rogue_fwif_dev_info.h"
#include "pvr_rogue_mips.h"
#include "pvr_rogue_heap_config.h"

/* Keep the anonymous-in-use types alive. */
struct rogue_mipsfw_boot_data fwemu_boot_data;

/* SPDX-License-Identifier: MIT */
#ifndef SHIM_LINUX_BUILD_BUG_H
#define SHIM_LINUX_BUILD_BUG_H
#include <assert.h>
#define BUILD_BUG_ON(c) _Static_assert(!(c), #c)
#endif

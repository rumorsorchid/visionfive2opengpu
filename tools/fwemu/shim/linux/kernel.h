/* SPDX-License-Identifier: MIT */
#ifndef SHIM_LINUX_KERNEL_H
#define SHIM_LINUX_KERNEL_H
#include <linux/types.h>
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define ALIGN(x, a) (((x) + (a) - 1) & ~((__typeof__(x))(a) - 1))
#define DIV_ROUND_UP(n, d) (((n) + (d) - 1) / (d))
#endif

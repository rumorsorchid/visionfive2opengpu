/* SPDX-License-Identifier: MIT */
#ifndef SHIM_LINUX_COMPILER_H
#define SHIM_LINUX_COMPILER_H
#define __aligned(x) __attribute__((aligned(x)))
#define __packed __attribute__((packed))
#endif

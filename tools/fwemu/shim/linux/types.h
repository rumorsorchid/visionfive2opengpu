/* SPDX-License-Identifier: MIT */
/* Minimal userspace stand-ins for the kernel headers used by pvr_rogue_fwif*.h */
#ifndef SHIM_LINUX_TYPES_H
#define SHIM_LINUX_TYPES_H
#include <stdbool.h>
#include <stdint.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int8_t s8;
typedef int16_t s16;
typedef int32_t s32;
typedef int64_t s64;
typedef u64 __attribute__((aligned(8))) aligned_u64;
#endif

/* SPDX-License-Identifier: MIT */
#ifndef SHIM_LINUX_OVERFLOW_H
#define SHIM_LINUX_OVERFLOW_H
#define flex_array_size(p, member, count) ((count) * sizeof(*(p)->member))
#endif

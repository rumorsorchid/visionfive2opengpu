/* SPDX-License-Identifier: MIT */
#ifndef SHIM_LINUX_STDDEF_H
#define SHIM_LINUX_STDDEF_H
#include <stddef.h>
#define sizeof_field(TYPE, MEMBER) sizeof((((TYPE *)0)->MEMBER))
#endif

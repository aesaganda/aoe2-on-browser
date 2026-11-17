/*
 * aoc_shims.c - answers for DLLs that ship with Age of Empires II: The Conquerors
 * itself (not system DLLs), registered ahead of win32hle's modules so they win.
 *
 * EBUEulaX.dll (the EULA dialog). The game calls, cdecl,
 *     int EBUEula(const char *reg_key, const char *eula_file, int, int)
 * and exits unless it returns nonzero. Accepting the licence agreement is the
 * player's decision, never the port's: the shim returns 1 only when the player
 * has set AOE2_ACCEPT_EULA=1 after reading the agreement (EULAX.RTF on the
 * Conquerors disc); otherwise it says so and returns 0, and the game quits.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "win32hle.h"

extern const win32hle_shim win32hle_module[];

#define EULA_DLL_HANDLE 0x7D000000u      /* pseudo-module handle, outside every mapping */

static recomp_func_t original(const char *name) {
    for (const win32hle_shim *s = win32hle_module; s->name; s++)
        if (!strcmp(s->name, name)) return s->fn;
    hle_fatal("aoc_shims: win32hle has no %s", name);
    return NULL;
}

static int is_eula_dll(const char *name) {
    if (!name) return 0;
    const char *b = strrchr(name, '\\');
    b = b ? b + 1 : name;
    return !strcasecmp(b, "EBUEulaX.dll") || !strcasecmp(b, "EBUEulaX");
}

static void a_LoadLibraryA(void) {
    if (is_eula_dll(ASTR(0))) RET(EULA_DLL_HANDLE, 1);
    original("LoadLibraryA")();
}

static void a_FreeLibrary(void) {
    if (A32(0) == EULA_DLL_HANDLE) RET(1, 1);
    original("FreeLibrary")();
}

static void a_GetProcAddress(void) {
    if (A32(0) == EULA_DLL_HANDLE) {
        uint32_t n = A32(1);
        uint32_t va = (n >= 0x10000u && !strcmp((const char *)hle_ptr(n), "EBUEula")) ? hle_resolve("aoc!EBUEula") : 0;
        RET(va, 2);
    }
    original("GetProcAddress")();
}

static void a_EBUEula(void) {                       /* cdecl: the caller pops */
    const char *accept = getenv("AOE2_ACCEPT_EULA");
    if (accept && !strcmp(accept, "1")) {
        fprintf(stderr, "[aoc] EULA: accepted by the player (AOE2_ACCEPT_EULA=1)\n");
        RET(1, 0);
    }
    fprintf(stderr,
            "[aoc] The game asks you to accept its End User License Agreement (EULAX.RTF on\n"
            "      the Conquerors disc). Read it; if you accept it, run again with\n"
            "      AOE2_ACCEPT_EULA=1. The game will now quit, as it does when the dialog is declined.\n");
    RET(0, 0);
}

static const win32hle_shim aoc_shims[] = {
    { "LoadLibraryA",   a_LoadLibraryA },
    { "FreeLibrary",    a_FreeLibrary },
    { "GetProcAddress", a_GetProcAddress },
    { "aoc!EBUEula",    a_EBUEula },
    { NULL, NULL },
};

void aoc_shims_register(void) { win32hle_register(aoc_shims); }

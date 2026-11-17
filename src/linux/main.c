/*
 * aoe2-recomp native host (Phase 1): boots the lifted age2_x1.exe on pcrecomp's
 * win32hle (libc + SDL2, no Wine).
 *
 *   aoe2 <install-root>
 *
 * <install-root> holds the game as the installer lays it out
 * (age2_x1/age2_x1.exe, data/, sound/, ...). It is mapped as drive C: and the
 * game runs from C:\age2_x1\, as it would from its install folder.
 */
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "host.h"
#include "win32hle.h"

void aoc_shims_register(void);     /* aoc_shims.c */
void win32_gaps_register(void);    /* win32_gaps.c */
void dplay_stub_register(void);    /* dplay_stub.c */
void dplay_stub_register_com(void);

#define HKLM 0x80000002u
#define AOC_KEY "Software\\Microsoft\\Microsoft Games\\Age of Empires II: The Conquerors Expansion\\1.0"
#define AOK_KEY "Software\\Microsoft\\Microsoft Games\\Age of Empires\\2.0"

static void seed_registry(void) {
    /* What the installer writes and the game reads at start. Values are added here
     * as the boot log shows the game asking for them (see docs/phase1-bringup.md). */
    hle_reg_set_string(HKLM, AOC_KEY, "EXE Path", "C:\\age2_x1");
    hle_reg_set_string(HKLM, AOC_KEY, "CDPath", "C:\\");
    hle_reg_set_string(HKLM, AOK_KEY, "EXE Path", "C:\\");
    hle_reg_set_string(HKLM, AOK_KEY, "CDPath", "C:\\");
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <install-root>\n", argv[0]);
        return 64;
    }
    char root[PATH_MAX], exe[PATH_MAX + 32];
    if (!realpath(argv[1], root)) { perror(argv[1]); return 66; }
    snprintf(exe, sizeof exe, "%s/age2_x1/age2_x1.exe", root);
    if (access(exe, R_OK) != 0) { perror(exe); return 66; }

    win32hle_trace = getenv("AOE2_TRACE_IMPORTS") != NULL;   /* every import call, by name */
    hle_set_drive('C', root);
    /* The AoC shortcut starts age2_x1\age2_x1.exe with the install root as the
     * working directory: language_x1.dll and data\ are found relative to it.
     * win32hle's current directory is the host's. */
    if (chdir(root) != 0) { perror(root); return 66; }
    aoc_shims_register();           /* first: same-named shims registered earlier win */
    dplay_stub_register();
    recomp_host_init();
    dplay_stub_register_com();
    win32_gaps_register();          /* last: fills only what win32hle does not answer */
    seed_registry();
    /* The SDL window DirectDraw presents into (pcrecomp screen.c). AOE2_HEADLESS=1: none. */
    const char *hl = getenv("AOE2_HEADLESS");
    if (hle_screen_open("Age of Empires II: The Conquerors", 0, hl && !strcmp(hl, "1")) != 0)
        fprintf(stderr, "[aoe2] no window; continuing headless\n");
    if (recomp_host_boot_permissive(exe) != 0) {
        fprintf(stderr, "boot failed: %s\n", exe);
        return 70;
    }
    return (int)recomp_host_run();
}

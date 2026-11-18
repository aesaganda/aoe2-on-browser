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
#include <sys/stat.h>
#include <unistd.h>

#include "host.h"
#include "win32hle.h"

void aoc_shims_register(void);     /* aoc_shims.c */
void win32_gaps_register(void);    /* win32_gaps.c */
void dplay_stub_register(void);    /* dplay_stub.c */
void dplay_stub_register_com(void);
void cdrom_mount(char letter, const char *dir, const char *label);   /* cdrom.c */
void edit_overlay_install(void);   /* edit_overlay.c */
void mciwnd_register(void);        /* mciwnd.c: the AVI cinematics */
void dplay_net_install(void);      /* src/net/dplay_net.c: multiplayer over the relay */
void ddraw_debug_register(void);   /* ddraw_debug.c: AOE2_TRACE_BLT / AOE2_DUMP_SURFACES */

#define HKLM 0x80000002u
#define AOC_KEY "Software\\Microsoft\\Microsoft Games\\Age of Empires II: The Conquerors Expansion\\1.0"
#define AOK_KEY "Software\\Microsoft\\Microsoft Games\\Age of Empires\\2.0"

static void seed_registry(int disc) {
    /* What the installer writes and the game reads at start. Values are added here
     * as the boot log shows the game asking for them (see docs/phase1-bringup.md). */
    hle_reg_set_string(HKLM, AOC_KEY, "EXE Path", "C:\\age2_x1");
    hle_reg_set_string(HKLM, AOC_KEY, "CDPath", disc ? "D:\\" : "C:\\");
    hle_reg_set_string(HKLM, AOK_KEY, "EXE Path", "C:\\");
    hle_reg_set_string(HKLM, AOK_KEY, "CDPath", "C:\\");
}

#ifdef __EMSCRIPTEN__
const char *web_game_root(void);   /* src/web/web_fs.c: the install tree, in the browser's OPFS */
#endif

int main(int argc, char **argv) {
    const char *where = argc >= 2 ? argv[1] : NULL;
#ifdef __EMSCRIPTEN__
    if (!where) where = web_game_root();
#endif
    if (!where) {
        fprintf(stderr, "usage: %s <install-root>\n", argv[0]);
        return 64;
    }
    char root[PATH_MAX], exe[PATH_MAX + 32];
    if (!realpath(where, root)) { perror(where); return 66; }
    snprintf(exe, sizeof exe, "%s/age2_x1/age2_x1.exe", root);
    if (access(exe, R_OK) != 0) { perror(exe); return 66; }

    win32hle_trace = getenv("AOE2_TRACE_IMPORTS") != NULL;   /* every import call, by name */
    hle_set_drive('C', root);
    /* The AoC shortcut starts age2_x1\age2_x1.exe with the install root as the
     * working directory: language_x1.dll and data\ are found relative to it.
     * win32hle's current directory is the host's. */
    if (chdir(root) != 0) { perror(root); return 66; }
    /* win32hle's GetTempPathA is C:\WINDOWS\TEMP\. The game extracts DRS bitmaps
     * (the minimap's mask, map800.bmp) to temp files there and OpenFile()s them, so
     * the directory has to exist under the install root. */
    mkdir("windows", 0755), mkdir("windows/temp", 0755);
    aoc_shims_register();           /* first: same-named shims registered earlier win */
    mciwnd_register();              /* before win32_gaps' MCIWndCreateA */
    dplay_stub_register();
    ddraw_debug_register();
    /* The Conquerors disc image's contents, mounted as D: (cdrom.c) */
    char disc[PATH_MAX + 16], labelf[PATH_MAX + 16], label[64] = "";
    snprintf(disc, sizeof disc, "%s/cdrom", root);
    snprintf(labelf, sizeof labelf, "%s/cdrom.label", root);
    FILE *lf = fopen(labelf, "r");
    int have_disc = lf && fgets(label, sizeof label, lf) && access(disc, R_OK) == 0;
    if (lf) fclose(lf);
    label[strcspn(label, "\r\n ")] = 0;      /* ISO 9660 pads the volume ID with spaces */
    if (have_disc) cdrom_mount('D', disc, label);
    else fprintf(stderr, "[aoe2] no disc image at %s: single player will ask for the CD\n", disc);
    recomp_host_init();
    dplay_stub_register_com();
    win32_gaps_register();          /* last: fills only what win32hle does not answer */
    seed_registry(have_disc);
    /* The SDL window DirectDraw presents into (pcrecomp screen.c). AOE2_HEADLESS=1: none. */
    const char *hl = getenv("AOE2_HEADLESS");
    if (hle_screen_open("Age of Empires II: The Conquerors", 0, hl && !strcmp(hl, "1")) != 0)
        fprintf(stderr, "[aoe2] no window; continuing headless\n");
    edit_overlay_install();         /* EDIT controls over the picture (chains screen.c's hook) */
    dplay_net_install();            /* DirectPlay's messages, taken at each pump (chains the poll hook) */
    /* The game's own command-line switches (NOSTARTUP, DONTIGNORESYNC, ...): AOE2_ARGS.
     * GetCommandLineA answers the exe's Windows path with them, as the shortcut would. */
    static char cmdline[1024];
    const char *args = getenv("AOE2_ARGS");
    snprintf(cmdline, sizeof cmdline, "\"C:\\age2_x1\\age2_x1.exe\"%s%s", args && *args ? " " : "", args ? args : "");
    hle_set_command_line(cmdline);
    if (recomp_host_boot_permissive(exe) != 0) {
        fprintf(stderr, "boot failed: %s\n", exe);
        return 70;
    }
    return (int)recomp_host_run();
}

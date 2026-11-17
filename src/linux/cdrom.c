/*
 * cdrom.c - the owner's Conquerors disc, presented as a CD-ROM drive.
 *
 * Before single player the game asks for its disc: registry CDPath -> GetDriveTypeA
 * must be DRIVE_CDROM -> GetVolumeInformationA's label must be the disc's. On Windows
 * that is satisfied by inserting the disc or mounting its image; here the host mounts
 * the image's contents (<install-root>/cdrom, from tools/install_from_iso.sh) as D:,
 * with the label read from the image. Nothing in the game is patched or bypassed.
 *
 * Registered ahead of win32hle; every other drive goes to win32hle's own shims.
 */
#include <ctype.h>
#include <stdio.h>
#include <string.h>

#include "win32hle.h"

#define DRIVE_CDROM 5u

static char g_letter;               /* 0: no disc mounted */
static char g_label[33];

static recomp_func_t original(const char *name) {
    static const win32hle_shim *const tables[] = { win32hle_kernel32, win32hle_kernel32_ext };
    for (size_t t = 0; t < sizeof tables / sizeof *tables; t++)
        for (const win32hle_shim *s = tables[t]; s->name; s++)
            if (!strcmp(s->name, name)) return s->fn;
    hle_fatal("cdrom: win32hle has no %s", name);
    return NULL;
}

static int is_disc(const char *root) {
    return g_letter && root && toupper((unsigned char)root[0]) == g_letter && root[1] == ':';
}

static void c_GetDriveTypeA(void) {                 /* (root) */
    if (is_disc(ASTR(0))) RET(DRIVE_CDROM, 1);
    original("GetDriveTypeA")();
}

static void c_GetVolumeInformationA(void) {         /* (root, name, nsize, &serial, &maxlen, &flags, fs, fsize) */
    if (!is_disc(ASTR(0))) { original("GetVolumeInformationA")(); return; }
    char *name = ASTR(1), *fs = ASTR(6);
    uint32_t nsize = A32(2), fsize = A32(7);
    if (name && nsize) snprintf(name, nsize, "%s", g_label);
    if (A32(3)) MEM32(A32(3)) = 0x00000000u;        /* serial: a pressed disc reports none to speak of */
    if (A32(4)) MEM32(A32(4)) = 110;                /* ISO 9660 + Joliet component length */
    if (A32(5)) MEM32(A32(5)) = 0x00080000u;        /* FILE_READ_ONLY_VOLUME */
    if (fs && fsize) snprintf(fs, fsize, "CDFS");
    RET(1, 8);
}

static const win32hle_shim cdrom_shims[] = {
    { "GetDriveTypeA", c_GetDriveTypeA },
    { "GetVolumeInformationA", c_GetVolumeInformationA },
    { NULL, NULL },
};

/* Mount `dir` as drive `letter` with volume label `label`. Call before recomp_host_init. */
void cdrom_mount(char letter, const char *dir, const char *label) {
    g_letter = (char)toupper((unsigned char)letter);
    snprintf(g_label, sizeof g_label, "%s", label);
    hle_set_drive(g_letter, dir);
    win32hle_register(cdrom_shims);
    fprintf(stderr, "[cdrom] %c: = %s (label \"%s\")\n", g_letter, dir, g_label);
}

/*
 * win32_gaps.c - imports of age2_x1.exe that pcrecomp's win32hle does not answer
 * (docs/phase0-report.md §5). Registered *after* win32hle, so where win32hle gains
 * an implementation upstream, its own wins and the one here goes unused.
 *
 * Generic Win32 behaviour only; anything game-specific belongs in aoc_shims.c.
 * Each shim follows win32hle's ABI: stdcall arguments read with A32/ASTR/APTR,
 * returned with RET(value, argc), which pops ret + argc args.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "win32hle.h"

static int gap_trace = -1;
#define TRACE(...) do { if (gap_trace < 0) gap_trace = getenv("AOE2_TRACE_GAPS") != NULL; \
                        if (gap_trace) fprintf(stderr, "[gaps] " __VA_ARGS__); } while (0)

/* A win32hle shim by name, for delegating (DrawTextExA -> DrawTextA). */
static recomp_func_t win32hle_fn(const char *name) {
    static const win32hle_shim *const tables[] = {
        win32hle_kernel32, win32hle_kernel32_ext, win32hle_kernel32_crt, win32hle_module,
        win32hle_gdi32, win32hle_gdidc, win32hle_user32, win32hle_winmm,
    };
    for (size_t t = 0; t < sizeof tables / sizeof *tables; t++)
        for (const win32hle_shim *s = tables[t]; s->name; s++)
            if (!strcmp(s->name, name)) return s->fn;
    hle_fatal("win32_gaps: win32hle has no %s to delegate to", name);
    return NULL;
}

/* ---- KERNEL32 ---- */
static void g_IsDBCSLeadByte(void)   { RET(0, 1); }          /* code page 1252: no lead bytes */
static void g_GetPriorityClass(void) { RET(0x20, 1); }       /* NORMAL_PRIORITY_CLASS */
static void g_WinExec(void) {                                /* (cmdline, show): launching programs is not ported */
    fprintf(stderr, "[gaps] WinExec(\"%s\") not run\n", ASTR(0) ? ASTR(0) : "");
    RET(2, 2);                                               /* ERROR_FILE_NOT_FOUND (<32 = failure) */
}

/* ---- USER32 ---- */
static void g_GetWindowThreadProcessId(void) {               /* (hwnd, &pid) -> tid */
    uint32_t *pid = APTR(1);
    if (pid) *pid = 1;
    RET(1, 2);
}
static void g_SetClassLongA(void)  { TRACE("SetClassLongA(%u)\n", A32(1)); RET(0, 3); }
static void g_IsClipboardFormatAvailable(void) { RET(0, 1); }
static void g_IsIconic(void)       { RET(0, 1); }
static void g_GetLastActivePopup(void) { RET(A32(0), 1); }
static void g_GetCaretBlinkTime(void)  { RET(530, 0); }
static void g_SetSysColors(void)   { RET(1, 3); }
static void g_SystemParametersInfoA(void) {                  /* (action, uiParam, pvParam, winIni) */
    uint32_t action = A32(0);
    uint32_t *pv = APTR(2);
    TRACE("SystemParametersInfoA(0x%X)\n", action);
    switch (action) {
    case 0x10:   /* SPI_GETSCREENSAVEACTIVE */
    case 0x0E:   /* SPI_GETSCREENSAVETIMEOUT */
    case 0x0A:   /* SPI_GETKEYBOARDSPEED */
    case 0x16:   /* SPI_GETKEYBOARDDELAY */
        if (pv) *pv = 0;
        break;
    case 0x03:   /* SPI_GETMOUSE: thresholds 6, 10, acceleration 1 */
        if (pv) pv[0] = 6, pv[1] = 10, pv[2] = 1;
        break;
    default:     /* the SET calls (screen saver, sticky keys...) are accepted and ignored */
        break;
    }
    RET(1, 4);
}
static void g_DrawTextExA(void) {                            /* DrawTextA's 5 args + DRAWTEXTPARAMS* */
    win32hle_fn("DrawTextA")();                              /* pops ret + 5 args */
    g_esp += 4;                                              /* and the 6th */
}

/* ---- GDI32 ---- */
static void g_CreateICA(void) {                              /* an information context: a DC we never draw on */
    win32hle_fn("CreateDCA")();
}

/* ---- IMM32 ---- */
static void g_ImmGetOpenStatus(void)    { RET(0, 1); }
static void g_ImmGetDefaultIMEWnd(void) { RET(0, 1); }

/* ---- MSVFW32: AVI cinematics (not ported yet: the game skips them on failure) ---- */
static void g_ICInfo(void)        { RET(0, 3); }
static void g_MCIWndCreateA(void) { fprintf(stderr, "[gaps] MCIWndCreateA(\"%s\"): videos not ported yet\n", ASTR(3) ? ASTR(3) : ""); RET(0, 4); }

/* ---- WINMM: MCI (mp3 music) not ported yet ---- */
static void g_mciSendCommandA(void) {                        /* (device, msg, flags, params) */
    TRACE("mciSendCommandA(dev %u, msg 0x%X)\n", A32(0), A32(1));
    RET(A32(1) == 0x803 /* MCI_OPEN */ ? 256 + 50 /* MCIERR_DEVICE_NOT_INSTALLED */ : 0, 4);
}

/* DPLAYX: see dplay_stub.c (the start-up DirectX check needs a working object). */

static const win32hle_shim gaps[] = {
    { "IsDBCSLeadByte", g_IsDBCSLeadByte },   { "GetPriorityClass", g_GetPriorityClass },
    { "WinExec", g_WinExec },
    { "GetWindowThreadProcessId", g_GetWindowThreadProcessId }, { "SetClassLongA", g_SetClassLongA },
    { "IsClipboardFormatAvailable", g_IsClipboardFormatAvailable }, { "IsIconic", g_IsIconic },
    { "GetLastActivePopup", g_GetLastActivePopup }, { "GetCaretBlinkTime", g_GetCaretBlinkTime },
    { "SetSysColors", g_SetSysColors }, { "SystemParametersInfoA", g_SystemParametersInfoA },
    { "DrawTextExA", g_DrawTextExA }, { "CreateICA", g_CreateICA },
    { "ImmGetOpenStatus", g_ImmGetOpenStatus }, { "ImmGetDefaultIMEWnd", g_ImmGetDefaultIMEWnd },
    { "ICInfo", g_ICInfo }, { "MCIWndCreateA", g_MCIWndCreateA },
    { "mciSendCommandA", g_mciSendCommandA },
    { NULL, NULL },
};

void win32_gaps_register(void) { win32hle_register(gaps); }

/*
 * ddraw_debug.c - eyes on the game's DirectDraw surfaces during bring-up.
 *
 * Uses pcrecomp's blit hook (ddraw_int.h: every Blt/BltFast that copies between
 * surfaces passes through it; returning 0 lets ddraw.c do the copy), remembers every
 * surface object seen, and:
 *   AOE2_TRACE_BLT=1   logs each distinct blit (sizes, rectangles, keying) once;
 *   kill -USR1 <pid>   dumps every live surface seen to /tmp/aoe2-dump/<object>_<w>x<h>.ppm
 *                      in the primary's palette, from the game thread at its next blit.
 * Surfaces are remembered by their guest object, not by host pointer: the game frees
 * surfaces (menu -> game) and a host pointer would outlive them.
 */
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "win32hle.h"
#include "ddraw_int.h"

int hle_dd_frame(const uint8_t **px, int *w, int *h, int *pitch, int *bpp, const uint32_t **palette, unsigned *seq);
int hle_dd_surface_pixels(uint32_t obj, uint8_t **px, int *w, int *h, int *pitch, int *bpp);

#define MAX_SEEN 512
static uint32_t g_seen[MAX_SEEN];
static int g_nseen, g_trace;
static volatile sig_atomic_t g_dump;

static void seen(const surf_t *s) {
    if (!s || !s->self) return;                       /* a flip chain's internal back buffer has no object */
    for (int i = 0; i < g_nseen; i++) if (g_seen[i] == s->self) return;
    if (g_nseen < MAX_SEEN) g_seen[g_nseen++] = s->self;
}

static void dump_all(void) {
    const uint8_t *fpx; int fw, fh, fp, fb; const uint32_t *pal = NULL;
    hle_dd_frame(&fpx, &fw, &fh, &fp, &fb, &pal, NULL);
    mkdir("/tmp/aoe2-dump", 0755);
    int n = 0;
    for (int i = 0; i < g_nseen; i++) {
        uint8_t *px; int w, h, pitch, bpp;
        /* checks the object is still a live surface (its vtable, in guest memory) */
        if (!hle_dd_surface_pixels(g_seen[i], &px, &w, &h, &pitch, &bpp) || bpp != 8 || !px) continue;
        char path[128];
        snprintf(path, sizeof path, "/tmp/aoe2-dump/%08X_%dx%d.ppm", g_seen[i], w, h);
        FILE *f = fopen(path, "wb");
        if (!f) continue;
        n++;
        fprintf(f, "P6\n%d %d\n255\n", w, h);
        for (int y = 0; y < h; y++)
            for (int x = 0; x < w; x++) {
                uint8_t v = px[(size_t)y * pitch + x];
                uint32_t e = pal ? pal[v] : v * 0x010101u;               /* PALETTEENTRY: r, g, b, flags */
                fputc(e & 0xFF, f), fputc((e >> 8) & 0xFF, f), fputc((e >> 16) & 0xFF, f);
            }
        fclose(f);
    }
    fprintf(stderr, "[ddraw-debug] dumped %d live surfaces to /tmp/aoe2-dump\n", n);
}

/* ---- a poor man's watchpoint on one back-buffer pixel (AOE2_WATCH_PIXEL=x,y) ----
 * The page holding the pixel is made read-only once a frame; the first write into it
 * faults, the handler counts the guest function running (g_cur_func), makes the page
 * writable again and the write is retried. A histogram of the writers prints on SIGUSR1
 * (with the dump). Writers of the whole page are counted, not only of the one pixel. */
#include <sys/mman.h>
#include <unistd.h>
extern uint32_t g_cur_func;
static uint8_t *g_watch_page;
static int g_watch_x = -1, g_watch_y = -1;
#define MAX_WRITERS 64
/* func and the return address on the guest stack at its entry (global esp is only
 * written back at calls, so it still holds the entry esp): who called the writer */
static struct { uint32_t func, ret; unsigned hits; } g_writers[MAX_WRITERS];
static struct sigaction g_old_segv;

static void on_segv(int sig, siginfo_t *si, void *ctx) {
    uint8_t *a = (uint8_t *)si->si_addr;
    if (g_watch_page && a >= g_watch_page && a < g_watch_page + 4096) {
        uint32_t f = g_cur_func, r = MEM32(g_esp);
        int i = 0;
        while (i < MAX_WRITERS && g_writers[i].hits && !(g_writers[i].func == f && g_writers[i].ret == r)) i++;
        if (i < MAX_WRITERS) g_writers[i].func = f, g_writers[i].ret = r, g_writers[i].hits++;
        mprotect(g_watch_page, 4096, PROT_READ | PROT_WRITE);
        return;                                                         /* the write is retried */
    }
    sigaction(SIGSEGV, &g_old_segv, NULL);                              /* not ours: the usual crash */
    (void)sig, (void)ctx;
}

static void watch_frame(surf_t *back) {
    if (g_watch_x < 0 || !back || !back->px) return;
    uint8_t *p = back->px + (size_t)g_watch_y * back->pitch + g_watch_x;
    g_watch_page = (uint8_t *)((uintptr_t)p & ~(uintptr_t)4095);
    mprotect(g_watch_page, 4096, PROT_READ);
}

static void print_writers(void) {
    if (g_watch_x < 0) return;
    fprintf(stderr, "[watch] writers of the page holding back-buffer pixel (%d,%d):\n", g_watch_x, g_watch_y);
    for (int i = 0; i < MAX_WRITERS && g_writers[i].hits; i++)
        fprintf(stderr, "  sub_%08X  called from 0x%08X  %u\n", g_writers[i].func, g_writers[i].ret, g_writers[i].hits);
}

static int hook(surf_t *d, int dl, int dt, int dr, int db, surf_t *s, int sl, int st, int sr, int sb,
                int keyed, uint32_t lo, uint32_t hi) {
    if (g_dump) g_dump = 0, dump_all(), print_writers();
    if (d->primary && s && s->w == d->w && s->h == d->h && dl == 0 && dt == 0) watch_frame(s);   /* the frame's flip */
    seen(d), seen(s);
    if (g_trace) {
        static struct { uint32_t d, s; int r[8]; } logged[512];
        static int n;
        int r[8] = { dl, dt, dr, db, sl, st, sr, sb };
        uint32_t dso = d->self, sso = s ? s->self : 0;
        for (int i = 0; i < n; i++)
            if (logged[i].d == dso && logged[i].s == sso && !memcmp(logged[i].r, r, sizeof r)) return 0;
        if (n < 512) logged[n].d = dso, logged[n].s = sso, memcpy(logged[n].r, r, sizeof r), n++;
        fprintf(stderr, "[blt] dst %08X %dx%d%s (%d,%d,%d,%d) <- src %08X %dx%d (%d,%d,%d,%d)%s key %u-%u\n",
                dso, d->w, d->h, d->primary ? " primary" : "", dl, dt, dr, db,
                sso, s ? s->w : 0, s ? s->h : 0, sl, st, sr, sb, keyed ? " keyed" : "", lo, hi);
    }
    return 0;                                                            /* ddraw.c does the copy */
}

static void on_usr1(int sig) { (void)sig; g_dump = 1; }

void ddraw_debug_register(void) {
    g_trace = getenv("AOE2_TRACE_BLT") != NULL;
    const char *wp = getenv("AOE2_WATCH_PIXEL");
    if (wp && sscanf(wp, "%d,%d", &g_watch_x, &g_watch_y) == 2) {
        struct sigaction sa;
        memset(&sa, 0, sizeof sa);
        sa.sa_sigaction = on_segv, sa.sa_flags = SA_SIGINFO | SA_NODEFER;
        sigaction(SIGSEGV, &sa, &g_old_segv);
    } else g_watch_x = -1;
    if (!g_trace && !getenv("AOE2_DUMP_SURFACES") && g_watch_x < 0) return;
    if (hle_dd_blit_hook) { fprintf(stderr, "[ddraw-debug] blit hook already taken\n"); return; }
    signal(SIGUSR1, on_usr1);
    hle_dd_blit_hook = hook;
}

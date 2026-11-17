/*
 * edit_overlay.c - Win32 EDIT controls drawn over the DirectDraw picture.
 *
 * Genie's text fields (player name, chat, save names...) are real Win32 EDIT child
 * windows. On Windows they paint themselves on the screen, on top of the DirectDraw
 * primary; the game only draws the field's background. win32hle keeps the controls
 * (their text, caret, focus) but composites no child window onto DirectDraw, so the
 * typed text was invisible. This draws every visible EDIT control's text (and its
 * caret when focused) into the primary each time the primary changes, then hands
 * over to the presenter (screen.c's hook, chained).
 */
#include <SDL.h>
#include <SDL_ttf.h>
#include <stdio.h>
#include <string.h>

#include "win32hle.h"
#include "ddraw_int.h"

int hle_dd_frame(const uint8_t **px, int *w, int *h, int *pitch, int *bpp, const uint32_t **palette, unsigned *seq);
extern void (*hle_dd_primary_hook)(void);

#define ES_PASSWORD 0x0020u
#define FONT_PATH "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"

static void (*g_next)(void);
static TTF_Font *g_font;
static int g_busy;

static uint8_t nearest(const uint32_t *pal, int r, int g, int b) {
    int best = 0, bd = 1 << 30;
    for (int i = 0; i < 256; i++) {
        int dr = (int)(pal[i] & 0xFF) - r, dg = (int)((pal[i] >> 8) & 0xFF) - g, db = (int)((pal[i] >> 16) & 0xFF) - b;
        int d = dr * dr + dg * dg + db * db;
        if (d < bd) bd = d, best = i;
    }
    return (uint8_t)best;
}

static void draw_edit(uint8_t *px, int sw, int sh, int pitch, const uint32_t *pal, uint32_t hwnd) {
    int l, t, r, b;
    if (!hle_window_screen_rect(hwnd, &l, &t, &r, &b) || r - l < 4 || b - t < 4) return;
    char text[512];
    const char *src = hle_window_text(hwnd);
    size_t n = strlen(src);
    if (n >= sizeof text) n = sizeof text - 1;
    if (hle_window_style(hwnd) & ES_PASSWORD) memset(text, '*', n);
    else memcpy(text, src, n);
    text[n] = 0;
    uint8_t ink = nearest(pal, 255, 255, 255);
    int x0 = l + 3, h = TTF_FontHeight(g_font), y0 = t + ((b - t) - h) / 2;
    if (n) {
        SDL_Color white = { 255, 255, 255, 255 };
        SDL_Surface *s = TTF_RenderText_Solid(g_font, text, white);   /* 8-bit: index 0 background, 1 ink */
        if (s) {
            for (int y = 0; y < s->h; y++)
                for (int x = 0; x < s->w; x++) {
                    int dx = x0 + x, dy = y0 + y;
                    if (dx < l || dx >= r - 2 || dy < t || dy >= b || dx >= sw || dy >= sh || dx < 0 || dy < 0) continue;
                    if (((uint8_t *)s->pixels)[y * s->pitch + x]) px[(size_t)dy * pitch + dx] = ink;
                }
            SDL_FreeSurface(s);
        }
    }
    if (hle_focus_window() == hwnd) {                          /* the caret, after its character */
        int caret = hle_edit_caret(hwnd), cw = 0;
        if (caret > (int)n) caret = (int)n;
        char save = text[caret];
        text[caret] = 0;
        if (caret) TTF_SizeText(g_font, text, &cw, NULL);
        text[caret] = save;
        int cx = x0 + cw;
        for (int y = y0; y < y0 + h && y < b && y < sh; y++)
            if (cx >= l && cx < r && cx < sw && y >= 0) px[(size_t)y * pitch + cx] = ink;
    }
}

static void overlay(void) {
    const uint8_t *cpx; int w, h, pitch, bpp; const uint32_t *pal = NULL;
    if (g_busy || !g_font || !hle_dd_frame(&cpx, &w, &h, &pitch, &bpp, &pal, NULL) || bpp != 8 || !pal) return;
    g_busy = 1;
    uint32_t hw[256];
    int n = hle_windows(hw, 256);
    for (int i = 0; i < n; i++)
        if (hle_window_is_edit(hw[i]) && hle_window_visible(hw[i]))
            draw_edit((uint8_t *)cpx, w, h, pitch, pal, hw[i]);
    g_busy = 0;
}

static void hook(void) {
    overlay();
    if (g_next) g_next();
}

/* After hle_screen_open, which installs the presenter's hook that this one chains. */
void edit_overlay_install(void) {
    if (!TTF_WasInit() && TTF_Init() != 0) { fprintf(stderr, "[edit-overlay] TTF_Init: %s\n", TTF_GetError()); return; }
    g_font = TTF_OpenFont(FONT_PATH, 13);
    if (!g_font) { fprintf(stderr, "[edit-overlay] %s: %s\n", FONT_PATH, TTF_GetError()); return; }
    g_next = hle_dd_primary_hook;
    hle_dd_primary_hook = hook;
}

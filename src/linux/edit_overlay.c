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
int hle_dd_surface_pixels(uint32_t obj, uint8_t **px, int *w, int *h, int *pitch, int *bpp);
extern void (*hle_dd_primary_hook)(void);
extern int (*hle_screen_rgb_hook)(uint32_t *px, int w, int h);

#define ES_PASSWORD 0x0020u
#define FONT_PATH "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
#define GUEST_MOUSE_POINTER_GLOBAL 0x68CA24u

static void (*g_next)(void);
static int (*g_next_rgb)(uint32_t *px, int w, int h);
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

static void draw_arrow_fallback(uint32_t *px, int w, int h, int mx, int my) {
    /* 12x19 monochrome+ivory pointer (# border, . fill), hotspot at (0, 0). */
    static const char *const rows[19] = {
        "#",
        "##",
        "#.#",
        "#..#",
        "#...#",
        "#....#",
        "#.....#",
        "#......#",
        "#.......#",
        "#........#",
        "#.....#####",
        "#..#..#",
        "#.# #..#",
        "##  #..#",
        "#    #..#",
        "     #..#",
        "      #..#",
        "      #..#",
        "       ##",
    };
    for (int y = 0; y < 19; y++) {
        int dy = my + y;
        if (dy < 0 || dy >= h) continue;
        const char *row = rows[y];
        for (int x = 0; row[x]; x++) {
            if (row[x] == ' ') continue;
            int dx = mx + x;
            if (dx < 0 || dx >= w) continue;
            px[(size_t)dy * w + dx] = row[x] == '#' ? 0xFF000000u : 0xFFF4ECD8u;
        }
    }
}

/* Composite the game's mcursors.slp frame (or a fallback arrow) when TMousePointer
 * is inactive so menus and dialogs show a cursor under SDL_ShowCursor(SDL_DISABLE). */
static void draw_cursor_rgb(uint32_t *px, int w, int h) {
    int mx = 0, my = 0;
    hle_input_position(&mx, &my);
    uint32_t mp = MEM32(GUEST_MOUSE_POINTER_GLOBAL);
    if (mp && MEM32(mp + 0x140) == 1) {
        int in_game = MEM32(mp + 0x144) != 0 && MEM32(mp + 0x148) != 0;
        uint32_t facet = in_game ? MEM32(mp + 0x98) : MEM32(mp + 0x94);
        int cw = (int)MEM32(mp + (in_game ? 0x7Cu : 0x74u));
        int ch = (int)MEM32(mp + (in_game ? 0x80u : 0x78u));
        int hx = (int)MEM32(mp + (in_game ? 0xB8u : 0xA8u));
        int hy = (int)MEM32(mp + (in_game ? 0xB4u : 0xA4u));
        uint32_t count = MEM32(mp + 0x10);
        if (facet >= count || facet >= 19) facet = 0;
        uint32_t area = MEM32(mp + 0x18 + facet * 4);
        uint32_t surf = area ? MEM32(area + 0x3C) : 0;
        uint8_t *spx = NULL;
        int sw = 0, sh = 0, spitch = 0, sbpp = 0;
        const uint8_t *fpx = NULL;
        int fw = 0, fh = 0, fpitch = 0, fbpp = 0;
        const uint32_t *pal = NULL;
        if (surf && hle_dd_surface_pixels(surf, &spx, &sw, &sh, &spitch, &sbpp) &&
            hle_dd_frame(&fpx, &fw, &fh, &fpitch, &fbpp, &pal, NULL) &&
            spx && sbpp == 8 && pal && cw > 0 && ch > 0) {
            if (cw > sw) cw = sw;
            if (ch > sh) ch = sh;
            for (int y = 0; y < ch; y++) {
                int dy = my - hy + y;
                if (dy < 0 || dy >= h) continue;
                const uint8_t *src_row = spx + (size_t)y * spitch;
                uint32_t *dst_row = px + (size_t)dy * w;
                for (int x = 0; x < cw; x++) {
                    uint8_t idx = src_row[x];
                    if (idx == 0) continue;
                    int dx = mx - hx + x;
                    if (dx < 0 || dx >= w) continue;
                    uint32_t e = pal[idx];
                    dst_row[dx] = 0xFF000000u | ((e & 0xFFu) << 16) | (e & 0xFF00u) | ((e >> 16) & 0xFFu);
                }
            }
            return;
        }
    }
    draw_arrow_fallback(px, w, h, mx, my);
}

static int rgb_hook(uint32_t *px, int w, int h) {
    if (g_next_rgb && g_next_rgb(px, w, h)) return 1;
    uint32_t mp = MEM32(GUEST_MOUSE_POINTER_GLOBAL);
    int mp_drew = mp && MEM32(mp) != 0 && MEM32(mp + 0x134) != 0 && MEM32(mp + 0x138) != 0;
    if (mp_drew || !hle_cursor_shown()) return 0;
    if (!px) return 1;
    draw_cursor_rgb(px, w, h);
    return 1;
}

static void hook(void) {
    overlay();
    if (g_next) g_next();
}

/* After hle_screen_open, which installs the presenter's hook that this one chains. */
void edit_overlay_install(void) {
    hle_input_cursor(400, 300);
    g_next = hle_dd_primary_hook;
    hle_dd_primary_hook = hook;
    g_next_rgb = hle_screen_rgb_hook;
    hle_screen_rgb_hook = rgb_hook;
    if (!TTF_WasInit() && TTF_Init() != 0) { fprintf(stderr, "[edit-overlay] TTF_Init: %s\n", TTF_GetError()); return; }
    g_font = TTF_OpenFont(FONT_PATH, 13);
    if (!g_font) { fprintf(stderr, "[edit-overlay] %s: %s\n", FONT_PATH, TTF_GetError()); return; }
}

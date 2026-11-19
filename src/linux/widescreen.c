/*
 * widescreen.c - Runtime widescreen (16:9 / arbitrary custom WxH) adaptation
 * for Age of Empires II: The Conquerors 1.0 without modifying the game binary,
 * DRS assets, or lifted C files.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "recomp_types.h"
#include "win32hle.h"

void sub_004A7950(void);
void sub_004E2840(void);
void sub_004E7E10(void);

static int g_ws_w;
static int g_ws_h;
static uint32_t g_ws_slp_va;
static uint32_t g_ws_slp_cap;

static int ws_is_custom(int w, int h) {
    if (w < 1024 || w > 2560 || h < 720 || h > 1440) return 0;
    if ((w == 1024 && h == 768) || (w == 1280 && h == 1024)) return 0;
    return 1;
}

static void ws_base_for(int w, int h, int *bw, int *bh, int *topH, int *botH) {
    if (h >= 1024 && w >= 1280) {
        *bw = 1280; *bh = 1024; *topH = 32; *botH = 218;
    } else {
        *bw = 1024; *bh = 768; *topH = 25; *botH = 175;
    }
}

void widescreen_init(void) {
    const char *env = getenv("AOE2_RESOLUTION");
    if (!env) return;
    int w = 0, h = 0;
    if (sscanf(env, "%d%*1[xX]%d", &w, &h) != 2) return;
    w &= ~1;
    if (!ws_is_custom(w, h)) return;
    g_ws_w = w;
    g_ws_h = h;
    int bw, bh, topH, botH;
    ws_base_for(w, h, &bw, &bh, &topH, &botH);
    fprintf(stderr, "[widescreen] configured %dx%d (base HUD %dx%d)\n", w, h, bw, bh);
}

int widescreen_base_width(void) {
    if (g_ws_w <= 0 || g_ws_h <= 0) return 0;
    int bw, bh, topH, botH;
    ws_base_for(g_ws_w, g_ws_h, &bw, &bh, &topH, &botH);
    return bw;
}

static void ws_call_panel_size(uint32_t panel_va, int w, int h) {
    if (!panel_va) return;
    uint32_t sb = g_ebx, ss = g_esi, sd = g_edi, sp = g_ebp;
    g_esp -= 4; MEM32(g_esp) = (uint32_t)h;
    g_esp -= 4; MEM32(g_esp) = (uint32_t)w;
    g_esp -= 4; MEM32(g_esp) = RECOMP_RETADDR;
    g_ecx = panel_va;
    sub_004A7950();
    g_ebx = sb; g_esi = ss; g_edi = sd; g_ebp = sp;
}

static void ws_set_fixed(uint32_t panel_va, int x, int y, int w, int h) {
    if (!panel_va) return;
    uint32_t vt = MEM32(panel_va);
    uint32_t fn_va = vt ? MEM32(vt + 0x1Cu) : 0;
    recomp_func_t fn = fn_va ? recomp_resolve(fn_va) : NULL;
    if (!fn) return;
    uint32_t sb = g_ebx, ss = g_esi, sd = g_edi, sp = g_ebp;
    g_esp -= 4; MEM32(g_esp) = (uint32_t)h;
    g_esp -= 4; MEM32(g_esp) = (uint32_t)w;
    g_esp -= 4; MEM32(g_esp) = (uint32_t)y;
    g_esp -= 4; MEM32(g_esp) = (uint32_t)x;
    g_esp -= 4; MEM32(g_esp) = RECOMP_RETADDR;
    g_ecx = panel_va;
    fn();
    g_ebx = sb; g_esi = ss; g_edi = sd; g_ebp = sp;
}

static uint32_t u32_le(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static uint16_t u16_le(const uint8_t *p) {
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}
static void put32_le(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
static void put16_le(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8);
}

static int ws_decode_slp_row(const uint8_t *slp, uint32_t cmd_off, uint32_t out_off, int baseW, int y, uint8_t *out) {
    uint16_t lp = u16_le(slp + out_off + (size_t)y * 4u);
    if (lp == 0x8000u) return 0;
    memset(out, 0, (size_t)baseW);
    uint32_t p = u32_le(slp + cmd_off + (size_t)y * 4u);
    int x = (int)lp;
    for (;;) {
        uint8_t b = slp[p++];
        uint8_t op = b & 0x0Fu;
        if (op == 0x0Fu) break;
        if ((b & 0x03u) == 0x00u) {
            int n = b >> 2;
            if (x + n <= baseW) memcpy(out + x, slp + p, (size_t)n);
            p += (uint32_t)n;
            x += n;
        } else if ((b & 0x03u) == 0x01u) {
            int n = b >> 2;
            if (!n) n = slp[p++];
            x += n;
        } else if (op == 0x02u) {
            int n = ((int)(b & 0xF0u) << 4) + slp[p++];
            if (x + n <= baseW) memcpy(out + x, slp + p, (size_t)n);
            p += (uint32_t)n;
            x += n;
        } else if (op == 0x03u) {
            int n = ((int)(b & 0xF0u) << 4) + slp[p++];
            x += n;
        } else if (op == 0x07u) {
            int n = b >> 4;
            if (!n) n = slp[p++];
            uint8_t c = slp[p++];
            if (x + n <= baseW) memset(out + x, c, (size_t)n);
            x += n;
        } else {
            return 0;
        }
    }
    return 1;
}

static void ws_fill_mirrored(uint8_t *dst, int count, const uint8_t *src_row, int tile_x, int tile_w) {
    if (count <= 0 || tile_w <= 0) return;
    int period = tile_w * 2;
    for (int i = 0; i < count; i++) {
        int m = i % period;
        int idx = (m < tile_w) ? m : (period - 1 - m);
        dst[i] = src_row[tile_x + idx];
    }
}

static void ws_build_hud_slp(uint32_t tshape, int baseW, int baseH, int topH, int botH, int W, int H) {
    if (!tshape || W <= baseW) return;
    uint32_t orig_va = MEM32(tshape + 0x0Cu);
    if (!orig_va) return;
    const uint8_t *orig = (const uint8_t *)hle_ptr(orig_va);
    if (!orig) return;

    uint32_t num_frames = u32_le(orig + 4);
    if (num_frames < 1) return;
    uint32_t orig_cmd = u32_le(orig + 32);
    uint32_t orig_out = u32_le(orig + 36);
    uint32_t orig_props = u32_le(orig + 44);
    int fw = (int)u32_le(orig + 48);
    int fh = (int)u32_le(orig + 52);
    if (fw != baseW || fh != baseH) return;

    uint32_t need = 64u + (uint32_t)H * 8u + (uint32_t)(topH + botH) * (uint32_t)(W + 4) + 64u;
    if (need > g_ws_slp_cap || !g_ws_slp_va) {
        void *buf = hle_alloc(need);
        if (!buf) return;
        g_ws_slp_va = hle_va(buf);
        g_ws_slp_cap = need;
    }
    uint8_t *dst = (uint8_t *)hle_ptr(g_ws_slp_va);
    if (!dst) return;

    /* Copy SLP file header and rewrite single-frame W x H table metadata */
    memcpy(dst, orig, 32);
    put32_le(dst + 4, 1u);
    uint32_t out_off = 64u;
    uint32_t cmd_off = out_off + (uint32_t)H * 4u;
    put32_le(dst + 32, cmd_off);
    put32_le(dst + 36, out_off);
    put32_le(dst + 40, 0u);
    put32_le(dst + 44, orig_props);
    put32_le(dst + 48, (uint32_t)W);
    put32_le(dst + 52, (uint32_t)H);
    put32_le(dst + 56, 0u);
    put32_le(dst + 60, 0u);

    uint32_t cur = cmd_off + (uint32_t)H * 4u;
    uint32_t empty_row_off = cur;
    dst[cur++] = 0x0Fu;

    for (int y = 0; y < H; y++) {
        put16_le(dst + out_off + (size_t)y * 4u, 0x8000u);
        put16_le(dst + out_off + (size_t)y * 4u + 2u, 0x8000u);
        put32_le(dst + cmd_off + (size_t)y * 4u, empty_row_off);
    }

    uint8_t src_row[2560];
    uint8_t wide_row[2560];
    int dx = W - baseW;

    /* Top HUD bar */
    int top_split = (baseW == 1280) ? 450 : 445;
    int top_tile_x = top_split;
    int top_tile_w = 64;
    for (int y = 0; y < topH; y++) {
        if (!ws_decode_slp_row(orig, orig_cmd, orig_out, baseW, y, src_row)) continue;
        memcpy(wide_row, src_row, (size_t)top_split);
        ws_fill_mirrored(wide_row + top_split, dx, src_row, top_tile_x, top_tile_w);
        memcpy(wide_row + top_split + dx, src_row + top_split, (size_t)(baseW - top_split));

        put16_le(dst + out_off + (size_t)y * 4u, 0u);
        put16_le(dst + out_off + (size_t)y * 4u + 2u, 0u);
        put32_le(dst + cmd_off + (size_t)y * 4u, cur);
        dst[cur++] = (uint8_t)(((W >> 4) & 0xF0) | 0x02);
        dst[cur++] = (uint8_t)(W & 0xFF);
        memcpy(dst + cur, wide_row, (size_t)W);
        cur += (uint32_t)W;
        dst[cur++] = 0x0Fu;
    }

    /* Bottom HUD bar */
    int dxL = dx / 2;
    int dxR = dx - dxL;
    int splitL = (baseW == 1280) ? 352 : 296;
    int tileLW = 48;
    int splitR = (baseW == 1280) ? 824 : 656;
    int tileRW = 48;
    int tileR = splitR - tileRW;
    for (int r = 0; r < botH; r++) {
        int sy = baseH - botH + r;
        int dy = H - botH + r;
        if (!ws_decode_slp_row(orig, orig_cmd, orig_out, baseW, sy, src_row)) continue;
        memcpy(wide_row, src_row, (size_t)splitL);
        ws_fill_mirrored(wide_row + splitL, dxL, src_row, splitL, tileLW);
        memcpy(wide_row + splitL + dxL, src_row + splitL, (size_t)(splitR - splitL));
        ws_fill_mirrored(wide_row + splitR + dxL, dxR, src_row, tileR, tileRW);
        memcpy(wide_row + splitR + dx, src_row + splitR, (size_t)(baseW - splitR));

        put16_le(dst + out_off + (size_t)dy * 4u, 0u);
        put16_le(dst + out_off + (size_t)dy * 4u + 2u, 0u);
        put32_le(dst + cmd_off + (size_t)dy * 4u, cur);
        dst[cur++] = (uint8_t)(((W >> 4) & 0xF0) | 0x02);
        dst[cur++] = (uint8_t)(W & 0xFF);
        memcpy(dst + cur, wide_row, (size_t)W);
        cur += (uint32_t)W;
        dst[cur++] = 0x0Fu;
    }

    /* Mark ownership = 2 so TShape::~TShape does not free/DRS-unlock our shared buffer */
    MEM32(tshape + 0x00u) = 2u;
    MEM32(tshape + 0x0Cu) = g_ws_slp_va;
    MEM32(tshape + 0x10u) = g_ws_slp_va + 32u;
    MEM8(tshape + 0x14u)  = 1u;
    MEM32(tshape + 0x18u) = 1u;
}

static void ws_reflow_widgets(uint32_t self, int baseW, int baseH, int W, int H) {
    uint32_t map = MEM32(self + 0x1070u);
    if (!map) return;

    int dx = W - baseW;
    int dy = H - baseH;
    int topY = (baseW == 1280) ? 32 : 25;
    int botH = (baseW == 1280) ? 218 : 175;
    uint32_t chat = MEM32(self + 0x11B8u);
    int chat_vis = chat && MEM32(chat + 0x6Cu) != 0;

    if (chat_vis) {
        ws_set_fixed(chat, 0, topY, W, 30);
        ws_set_fixed(map, 0, topY + 30, W, H - (topY + 30 + botH));
    } else {
        ws_set_fixed(map, 0, topY, W, H - (topY + botH));
    }

    if (baseW == 1024) {
        ws_set_fixed(MEM32(self + 0x11BCu), 400 + dx / 2, 2, 310, 20);
        ws_set_fixed(MEM32(self + 0x1218u), 400 + dx / 2, 16, 310, 20);

        static const uint32_t top_offs[5] = { 0x10ECu, 0x10D0u, 0x10BCu, 0x10C0u, 0x10C4u };
        static const int top_xs[5]        = { 764, 814, 864, 914, 964 };
        for (int i = 0; i < 5; i++) ws_set_fixed(MEM32(self + top_offs[i]), top_xs[i] + dx, 3, 50, 19);

        for (int i = 0; i < 15; i++) {
            ws_set_fixed(MEM32(self + 0x1080u + (uint32_t)i * 4u), 37 + (i % 5) * 41, 624 + (i / 5) * 41 + dy, 40, 40);
        }

        ws_set_fixed(MEM32(self + 0x107Cu), 286 + dx / 2, 616 + dy, 375, 150);
        ws_set_fixed(MEM32(self + 0x1074u), 688 + dx, 599 + dy, 326, 164);

        ws_set_fixed(MEM32(self + 0x10E8u), 716 + dx, 614 + dy, 35, 35);
        ws_set_fixed(MEM32(self + 0x10E4u), 715 + dx, 719 + dy, 35, 35);
        ws_set_fixed(MEM32(self + 0x10CCu), 928 + dx, 612 + dy, 25, 25);
        ws_set_fixed(MEM32(self + 0x10C8u), 955 + dx, 606 + dy, 25, 25);
        ws_set_fixed(MEM32(self + 0x10F0u), 964 + dx, 631 + dy, 25, 25);
        ws_set_fixed(MEM32(self + 0x10D8u), 963 + dx, 709 + dy, 25, 25);
        ws_set_fixed(MEM32(self + 0x10DCu), 950 + dx, 733 + dy, 25, 25);
        ws_set_fixed(MEM32(self + 0x10E0u), 922 + dx, 729 + dy, 25, 25);
    } else {
        ws_set_fixed(MEM32(self + 0x11BCu), 521 + dx / 2, 2, 322, 20);
        ws_set_fixed(MEM32(self + 0x1218u), 521 + dx / 2, 16, 322, 20);

        static const uint32_t top_offs[5] = { 0x10ECu, 0x10D0u, 0x10BCu, 0x10C0u, 0x10C4u };
        static const int top_xs[5]        = { 1019, 1069, 1119, 1169, 1219 };
        for (int i = 0; i < 5; i++) ws_set_fixed(MEM32(self + top_offs[i]), top_xs[i] + dx, 5, 50, 19);

        for (int i = 0; i < 15; i++) {
            ws_set_fixed(MEM32(self + 0x1080u + (uint32_t)i * 4u), 44 + (i % 5) * 41, 845 + (i / 5) * 41 + dy, 40, 40);
        }

        ws_set_fixed(MEM32(self + 0x107Cu), 339 + dx / 2, 827 + dy, 375, 150);
        ws_set_fixed(MEM32(self + 0x1074u), 868 + dx, 817 + dy, 394, 198);

        ws_set_fixed(MEM32(self + 0x10E8u), 887 + dx, 829 + dy, 53, 53);
        ws_set_fixed(MEM32(self + 0x10E4u), 886 + dx, 956 + dy, 53, 53);
        ws_set_fixed(MEM32(self + 0x10CCu), 1148 + dx, 826 + dy, 33, 33);
        ws_set_fixed(MEM32(self + 0x10C8u), 1187 + dx, 826 + dy, 33, 33);
        ws_set_fixed(MEM32(self + 0x10F0u), 1210 + dx, 856 + dy, 33, 33);
        ws_set_fixed(MEM32(self + 0x10D8u), 1211 + dx, 946 + dy, 33, 33);
        ws_set_fixed(MEM32(self + 0x10DCu), 1186 + dx, 975 + dy, 33, 33);
        ws_set_fixed(MEM32(self + 0x10E0u), 1145 + dx, 979 + dy, 33, 33);
    }
}

int recomp_enter_hook(uint32_t va) {
    static int in_4e2840;
    static int in_4e7e10;

    if (va == 0x00463F80u) {
        if (g_ws_w <= 0) return 0;
        int w = (int)MEM32(g_esp + 4u);
        int h = (int)MEM32(g_esp + 8u);
        int bpp = (int)MEM32(g_esp + 12u);
        if (w == g_ws_w && (h == g_ws_h || h == 0) && (bpp == 8 || bpp == 0)) {
            g_eax = 1u;
            g_esp += 16u; /* ret 0xc */
            return 1;
        }
        return 0;
    }

    if (va == 0x00424A00u) {
        if (g_ws_w > 0 && g_ws_h > 0) {
            uint32_t prog = MEM32(g_ecx + 0x28u);
            if (prog) {
                MEM32(prog + 0x8F4u) = (uint32_t)g_ws_w;
                MEM32(prog + 0x8FCu) = (uint32_t)g_ws_h;
            }
        }
        return 0;
    }

    if (va == 0x00424A40u) {
        int w = (int)MEM32(g_esp + 4u);
        int h = (int)MEM32(g_esp + 8u);
        if ((w == 800 && h == 600) || (w == 1024 && h == 768) || (w == 1280 && h == 1024)) {
            g_ws_w = 0;
            g_ws_h = 0;
        }
        return 0;
    }

    if (va == 0x004E7E10u) {
        if (in_4e7e10) return 0;
        uint32_t self = g_ecx;
        int W = (int)MEM32(self + 0x14u);
        int H = (int)MEM32(self + 0x18u);
        if (!ws_is_custom(W, H)) return 0;

        int baseW, baseH, topH, botH;
        ws_base_for(W, H, &baseW, &baseH, &topH, &botH);
        MEM32(self + 0x14u) = (uint32_t)baseW;
        MEM32(self + 0x18u) = (uint32_t)baseH;

        uint32_t sb = g_ebx, ss = g_esi, sd = g_edi, sp = g_ebp;
        in_4e7e10 = 1;
        sub_004E7E10(); /* ret 8 pops return address + arguments */
        in_4e7e10 = 0;
        g_ebx = sb; g_esi = ss; g_edi = sd; g_ebp = sp;

        MEM32(self + 0x14u) = (uint32_t)W;
        MEM32(self + 0x18u) = (uint32_t)H;
        ws_build_hud_slp(MEM32(self + 0x100Cu), baseW, baseH, topH, botH, W, H);
        return 1;
    }

    if (va == 0x004E2840u) {
        if (in_4e2840) return 0;
        uint32_t self = g_ecx;
        int W = (int)MEM32(g_esp + 4u);
        int H = (int)MEM32(g_esp + 8u);
        if (!ws_is_custom(W, H)) return 0;

        int baseW, baseH, topH, botH;
        ws_base_for(W, H, &baseW, &baseH, &topH, &botH);

        uint32_t sb = g_ebx, ss = g_esi, sd = g_edi, sp = g_ebp;
        MEM32(g_esp + 4u) = (uint32_t)baseW;
        MEM32(g_esp + 8u) = (uint32_t)baseH;
        in_4e2840 = 1;
        sub_004E2840(); /* pops return address + 8 bytes of arguments */
        in_4e2840 = 0;

        ws_call_panel_size(self, W, H);
        ws_reflow_widgets(self, baseW, baseH, W, H);
        ws_build_hud_slp(MEM32(self + 0x100Cu), baseW, baseH, topH, botH, W, H);

        g_ebx = sb; g_esi = ss; g_edi = sd; g_ebp = sp;
        g_eax = 1u;
        return 1;
    }

    return 0;
}

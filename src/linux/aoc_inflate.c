/*
 * aoc_inflate.c - host replacement (HLE) for age2_x1's inflate step, 0x006074CB.
 *
 * The original is hand-written assembly (0x606C70..0x607590): a resumable raw-deflate
 * decoder that, when its output window is full, copies the guest stack between itself
 * and its entry frame - return addresses included - into its state (0x606E0D) and
 * copies it back on the next call (0x606E5D) to resume mid-call-chain. Statically
 * lifted code cannot follow that (each guest function is a C function), so the lift
 * renames the body to sub_006074CB_lifted (tools/lift/overrides.txt) and this
 * function takes its calls. Its contract, from the code and its only caller
 * (0x004C5A20, the game's compressed-file reader):
 *
 *   int __cdecl inflate_step(u8 *in, u32 in_off, u32 *in_len, u8 *out, u32 out_off,
 *                            u32 *out_len, state *s, u32 flag)
 *     reads from in+in_off (in_len is the whole input's length), writes up to
 *     *out_len bytes at out+out_off; on return *in_len = bytes consumed by this call,
 *     *out_len = bytes produced. s is calloc'd by the caller: s+0xA958 == 0 starts a
 *     new stream. Returns 0 at the end of the stream, -2 when the output is full,
 *     -1 on an error. (The caller ignores the result and uses the counts.)
 */
#include <stdio.h>
#include <string.h>
#include <zlib.h>

#include "win32hle.h"

#define STATE_PHASE 0xA958u          /* the original's state-machine index; 0 = not started */
#define MAX_STREAMS 16

static struct { uint32_t state; int live; z_stream z; } g_s[MAX_STREAMS];

static int stream_for(uint32_t state) {
    int fresh = MEM32(state + STATE_PHASE) == 0;
    for (int i = 0; i < MAX_STREAMS; i++)
        if (g_s[i].live && g_s[i].state == state) {
            if (!fresh) return i;
            inflateEnd(&g_s[i].z), g_s[i].live = 0;     /* a new stream in a reused block */
        }
    for (int i = 0; i < MAX_STREAMS; i++)
        if (!g_s[i].live) {
            memset(&g_s[i].z, 0, sizeof g_s[i].z);
            if (inflateInit2(&g_s[i].z, -15) != Z_OK) return -1;   /* raw deflate, 32 KB window */
            g_s[i].state = state, g_s[i].live = 1;
            MEM32(state + STATE_PHASE) = 2;                       /* "running", as the original marks it */
            return i;
        }
    return -1;
}

void sub_006074CB(void) {
    uint32_t sp = g_esp;                                      /* [ret][in][in_off][&in_len][out][out_off][&out_len][state][flag] */
    uint32_t in = MEM32(sp + 4), in_off = MEM32(sp + 8), p_in = MEM32(sp + 12);
    uint32_t out = MEM32(sp + 16), out_off = MEM32(sp + 20), p_out = MEM32(sp + 24);
    uint32_t state = MEM32(sp + 28);
    uint32_t in_len = MEM32(p_in), out_len = MEM32(p_out);
    int i = stream_for(state);
    if (i < 0) {
        fprintf(stderr, "[inflate] no stream for state 0x%08X\n", state);
        MEM32(p_in) = 0, MEM32(p_out) = 0;
        g_eax = 0xFFFFFFFFu, g_esp += 4;
        return;
    }
    z_stream *z = &g_s[i].z;
    uint32_t avail = in_len > in_off ? in_len - in_off : 0;
    z->next_in = (Bytef *)hle_ptr(in + in_off), z->avail_in = avail;
    z->next_out = (Bytef *)hle_ptr(out + out_off), z->avail_out = out_len;
    int rc = inflate(z, Z_NO_FLUSH);
    MEM32(p_in) = avail - z->avail_in;
    MEM32(p_out) = out_len - z->avail_out;
    if (rc == Z_STREAM_END) {
        inflateEnd(z), g_s[i].live = 0;
        g_eax = 0;
    } else if (rc == Z_OK || rc == Z_BUF_ERROR) {
        g_eax = 0xFFFFFFFEu;
    } else {
        fprintf(stderr, "[inflate] zlib error %d (%s) on state 0x%08X\n", rc, z->msg ? z->msg : "", state);
        inflateEnd(z), g_s[i].live = 0;
        g_eax = 0xFFFFFFFFu;
    }
    g_esp += 4;                                               /* ret: cdecl, the caller pops the args */
}

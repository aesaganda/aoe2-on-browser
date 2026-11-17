/* Symbols the lifted code expects from recomp32/native32 files that include
 * <windows.h>, rewritten portably for the win32hle (Linux) host.
 *
 * EH frame registry: a lifted body with an MSVC exception frame registers a
 * landing pad (RECOMP_EH_ENTER / RECOMP_EH_FRAME) on entry and drops it on exit.
 * This is the bookkeeping half of pcrecomp's native32/eh32.c (same semantics,
 * thread-local list, newest first). The dispatch half (RaiseException walking
 * the guest's SEH chain, _CxxThrowException, catch funclets) is not here yet:
 * win32hle ends the run on RaiseException. Tracked in docs/phase1-bringup.md. */
#include <setjmp.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "recomp_types.h"

static _Thread_local recomp_ehframe_t *t_frames;   /* newest first */

void recomp_eh_push(recomp_ehframe_t *f) { f->prev = t_frames; t_frames = f; }

void recomp_eh_pop(recomp_ehframe_t *f) {
    /* a longjmp past this frame already dropped it */
    for (recomp_ehframe_t *p = t_frames; p; p = p->prev)
        if (p == f) { t_frames = f->prev; return; }
}

recomp_ehframe_t *recomp_eh_owner(uint32_t esp) {
    for (recomp_ehframe_t *f = t_frames; f; f = f->prev)
        if (f->entry_esp > esp) return f;
    return NULL;
}

void recomp_eh_resume(recomp_ehframe_t *f, uint32_t resume) {
    t_frames = f;
    f->resume = resume;
    g_cur_func = f->func;
    longjmp(f->jb, 1);
}

#ifdef RECOMP_TRACE
/* Trace build (make native-trace): every lifted function's entry is counted.
 * kill -USR2 <pid> once resets the counts, a second time writes them to
 * /tmp/aoe2-funcs.txt ("VA count", most called first is up to sort), so a window of
 * play can be profiled: which guest functions ran, and how often. */
#include <signal.h>
#include <stdlib.h>
uint32_t g_enter_trace[RECOMP_ENTER_SIZE];
uint32_t g_enter_idx;
#define CBITS 17
static uint32_t c_key[1u << CBITS], c_n[1u << CBITS];
static volatile sig_atomic_t c_req, c_phase;

static void c_dump(void) {
    FILE *f = fopen("/tmp/aoe2-funcs.txt", "w");
    if (!f) return;
    for (uint32_t i = 0; i < (1u << CBITS); i++) if (c_n[i]) fprintf(f, "%08X %u\n", c_key[i], c_n[i]);
    fclose(f);
    fprintf(stderr, "[trace] function counts written to /tmp/aoe2-funcs.txt\n");
}
static void c_sig(int s) { (void)s; c_req = 1; }

void recomp_trace_enter(uint32_t va) {
    static int armed;
    if (!armed) armed = 1, signal(SIGUSR2, c_sig);
    if (c_req) {
        c_req = 0;
        if (c_phase++ % 2 == 0) { memset(c_n, 0, sizeof c_n); memset(c_key, 0, sizeof c_key); fprintf(stderr, "[trace] counts reset\n"); }
        else c_dump();
    }
    uint32_t i = (va * 2654435761u) >> (32 - CBITS);
    while (c_n[i] && c_key[i] != va) i = (i + 1) & ((1u << CBITS) - 1);
    c_key[i] = va, c_n[i]++;
}
void recomp_dump_trace(const char *why) { (void)why; }
#endif

recomp_ehframe_t *recomp_eh_find(uint32_t va, uint32_t ebp) {
    for (recomp_ehframe_t *f = t_frames; f; f = f->prev) {
        if (!f->resume_at || f->entry_esp != ebp + f->off) continue;
        for (const uint32_t *r = f->resume_at; *r; r++)
            if (*r == va) return f;
    }
    return NULL;
}

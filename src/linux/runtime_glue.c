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

recomp_ehframe_t *recomp_eh_find(uint32_t va, uint32_t ebp) {
    for (recomp_ehframe_t *f = t_frames; f; f = f->prev) {
        if (!f->resume_at || f->entry_esp != ebp + f->off) continue;
        for (const uint32_t *r = f->resume_at; *r; r++)
            if (*r == va) return f;
    }
    return NULL;
}

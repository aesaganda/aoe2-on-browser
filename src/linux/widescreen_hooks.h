#ifndef AOE2_WIDESCREEN_HOOKS_H
#define AOE2_WIDESCREEN_HOOKS_H

/* Filter checked at compile time inside RECOMP_ENTER(va) so only these
 * 5 layout/mode functions emit a branch to recomp_enter_hook(). */
#define RECOMP_ENTER_HOOK(va) \
    ((va) == 0x00463F80u || (va) == 0x00424A00u || (va) == 0x00424A40u || \
     (va) == 0x004E2840u || (va) == 0x004E7E10u)

#endif

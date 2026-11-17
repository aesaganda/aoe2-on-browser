/* Prepended to the lifted body under test. Defines the guest-state globals that
 * pcrecomp's recomp32 ABI declares extern, and routes every way out of the body
 * (indirect call/jump, call to code that was not lifted) to dt_escape(): a leaf
 * function under test must never leave, so an escape is a failure. */
#define RECOMP_GENERATED_CODE
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "recomp_types.h"

uint32_t g_eax, g_ecx, g_edx, g_esp, g_ebx, g_esi, g_edi, g_ebp;
double   g_st[8];
int      g_fp_top;
uint16_t g_fpu_cw = 0x027F;
uint16_t g_seg_cs, g_seg_ds, g_seg_es, g_seg_fs, g_seg_gs, g_seg_ss;
uint32_t g_fs_base = 0x7FFDE000u, g_gs_base, g_es_base, g_cs_base, g_cur_func;
uint64_t g_mm[8];
xmm_t    g_xmm[8];
uint32_t g_mxcsr = 0x1F80;
ptrdiff_t g_mem_base;
uint32_t g_icall_trace[ICALL_TRACE_SIZE], g_icall_from[ICALL_TRACE_SIZE], g_icall_trace_idx, g_icall_count;

void dt_escape(uint32_t va);
static uint32_t dt_target;
static void dt_icall(void) { dt_escape(dt_target); }
void recomp_dump_trace(const char *why) { (void)why; }
void recomp_not_lifted(uint32_t va) { dt_escape(va); }
recomp_func_t recomp_lookup_manual(uint32_t va) { (void)va; return 0; }
recomp_func_t recomp_lookup(uint32_t va) { dt_target = va; return dt_icall; }
recomp_func_t recomp_lookup_import(uint32_t va) { dt_target = va; return dt_icall; }

/* Differential-test driver: runs one lifted function over a file of input cases
 * and prints the resulting machine state. Written for this project; it talks to
 * the pcrecomp recomp32 ABI (global guest registers, g_mem_base, g_st/g_fp_top).
 *
 *   driver <image.bin> <cases.bin>
 *
 * Guest memory: 4 GiB reserved, regions committed at their guest addresses, so a
 * guest address is valid on the host exactly when the oracle mapped it.
 * Output per case (text):  C <i> / S ok|fault|timeout|escape <va> / R x8 / T depth / V st.. / M addr byte.. / E
 */
#include <setjmp.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/time.h>

extern uint32_t g_eax, g_ecx, g_edx, g_ebx, g_esp, g_ebp, g_esi, g_edi;
extern double g_st[8];
extern int g_fp_top;
extern uint16_t g_fpu_cw;
extern ptrdiff_t g_mem_base;
void dt_entry(void);              /* calls the function under test */

#define NREG 3
struct region { uint32_t va, len; unsigned char *init; };
static struct region R[NREG];
static unsigned char *mem;
static sigjmp_buf jb;
static volatile uint32_t escape_va;

void dt_escape(uint32_t va) { escape_va = va; siglongjmp(jb, 3); }
static void on_fault(int sig) { siglongjmp(jb, sig == SIGALRM ? 2 : 1); }

static unsigned char *slurp(const char *p, long *n) {
    FILE *f = fopen(p, "rb");
    if (!f) { perror(p); exit(2); }
    fseek(f, 0, SEEK_END); *n = ftell(f); fseek(f, 0, SEEK_SET);
    unsigned char *b = malloc(*n);
    if (fread(b, 1, *n, f) != (size_t)*n) exit(2);
    fclose(f);
    return b;
}

/* cases.bin layout (little endian), all u32 unless noted:
 *   header: magic 'DTC1', ncase, nregion, {va, len} * nregion
 *   per case: regs[8], depth, pad, st[8] (f64, st[0] = top), then the initial bytes of regions 1..n-1
 *   (region 0 is the image, identical for every case and read from image.bin) */
int main(int argc, char **argv) {
    long ni, nc;
    unsigned char *img = slurp(argv[1], &ni), *cs = slurp(argv[2], &nc);
    uint32_t *h = (uint32_t *)cs;
    if (h[0] != 0x31435444u) { fprintf(stderr, "bad cases file\n"); return 2; }
    uint32_t ncase = h[1], nreg = h[2];
    if (nreg != NREG) { fprintf(stderr, "expected %d regions\n", NREG); return 2; }
    unsigned char *p = cs + 12;
    for (uint32_t i = 0; i < nreg; i++) { memcpy(&R[i].va, p, 4); memcpy(&R[i].len, p + 4, 4); p += 8; }

    mem = mmap(0, 1ull << 32, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    if (mem == MAP_FAILED) { perror("mmap"); return 2; }
    g_mem_base = (ptrdiff_t)mem;
    for (uint32_t i = 0; i < nreg; i++)
        mprotect(mem + R[i].va, R[i].len, PROT_READ | PROT_WRITE);
    R[0].init = img;

    struct sigaction sa = {0};
    sa.sa_handler = on_fault;
    sa.sa_flags = SA_NODEFER;
    sigaction(SIGSEGV, &sa, 0); sigaction(SIGBUS, &sa, 0); sigaction(SIGFPE, &sa, 0);
    sigaction(SIGILL, &sa, 0); sigaction(SIGALRM, &sa, 0);

    for (uint32_t c = 0; c < ncase; c++) {
        uint32_t regs[8], depth;
        double st[8];
        memcpy(regs, p, 32); memcpy(&depth, p + 32, 4); memcpy(st, p + 40, 64); p += 104;
        for (uint32_t i = 1; i < nreg; i++) { R[i].init = p; p += R[i].len; }
        for (uint32_t i = 0; i < nreg; i++) memcpy(mem + R[i].va, R[i].init, R[i].len);
        g_eax = regs[0]; g_ecx = regs[1]; g_edx = regs[2]; g_ebx = regs[3];
        g_esp = regs[4]; g_ebp = regs[5]; g_esi = regs[6]; g_edi = regs[7];
        memcpy(g_st, st, sizeof g_st);   /* g_st[0] is st(0), the top */
        g_fp_top = (int)depth; g_fpu_cw = 0x027F;

        printf("C %u\n", c);
        struct itimerval t = {{0, 0}, {0, 200000}};      /* 200 ms per case */
        setitimer(ITIMER_REAL, &t, 0);
        int r = sigsetjmp(jb, 1);
        if (r == 0) dt_entry();
        struct itimerval z = {{0, 0}, {0, 0}};
        setitimer(ITIMER_REAL, &z, 0);
        if (r == 1) { printf("S fault\nE\n"); continue; }
        if (r == 2) { printf("S timeout\nE\n"); continue; }
        if (r == 3) { printf("S escape %08X\nE\n", escape_va); continue; }
        printf("S ok\nR %08X %08X %08X %08X %08X %08X %08X %08X\nT %d\n",
               g_eax, g_ecx, g_edx, g_ebx, g_esp, g_ebp, g_esi, g_edi, g_fp_top);
        for (int k = 0; k < g_fp_top && k < 8; k++) {
            uint64_t bits; memcpy(&bits, &g_st[k], 8);
            printf("V %016llX\n", (unsigned long long)bits);
        }
        for (uint32_t i = 0; i < nreg; i++) {
            unsigned char *now = mem + R[i].va;
            for (uint32_t o = 0; o < R[i].len; o += 4096) {
                uint32_t n = R[i].len - o < 4096 ? R[i].len - o : 4096;
                if (!memcmp(now + o, R[i].init + o, n)) continue;
                for (uint32_t j = o; j < o + n; j++)
                    if (now[j] != R[i].init[j]) printf("M %08X %02X\n", R[i].va + j, now[j]);
            }
        }
        printf("E\n");
    }
    return 0;
}

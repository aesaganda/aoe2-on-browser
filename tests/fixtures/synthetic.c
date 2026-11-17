/* Synthetic leaf functions for the binary-free test path (CI). Compiled as a
 * 32-bit PE by mingw-w64 with flags that keep the code in the shape of the
 * MSVC 6 era: x87 floats (no SSE), no cmov/fcomi (-march=pentium), no builtin
 * memset/memcpy calls. Each function is a leaf: it calls nothing. */
#include <stdint.h>

#define LEAF __attribute__((noinline, used))

struct unit { float x, y, vx, vy; int16_t hp, max_hp; uint8_t flags, owner; uint16_t pad; };

LEAF int sum_array(const int *a, int n) { int s = 0; for (int i = 0; i < n && i < 256; i++) s += a[i]; return s; }
LEAF int clamp(int v, int lo, int hi) { return v < lo ? lo : v > hi ? hi : v; }
LEAF unsigned hash_str(const unsigned char *s) {
    unsigned h = 2166136261u;
    for (int i = 0; i < 64 && s[i]; i++) h = (h ^ s[i]) * 16777619u;
    return h;
}
LEAF void fill_bytes(unsigned char *p, int c, int n) { for (int i = 0; i < (n & 0x3ff); i++) p[i] = (unsigned char)c; }
LEAF int64_t mul_wide(int a, int b) { return (int64_t)a * b; }
LEAF int div_round(int a, int b) { return (a + b / 2) / b; }
LEAF unsigned rotl_mix(unsigned x, int r) { r &= 31; return ((x << r) | (x >> ((32 - r) & 31))) ^ (x >> 3); }
LEAF int bit_count(unsigned x) { int n = 0; while (x) { x &= x - 1; n++; } return n; }
LEAF float lerp(float a, float b, float t) { return a + (b - a) * t; }
LEAF float dot3(const float *a, const float *b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
LEAF int float_to_int(float f) { return (int)f; }                 /* fnstcw/fldcw/fistp: rounding control */
LEAF int float_less(float a, float b) { return a < b; }          /* fucompp + fnstsw + sahf/test */
LEAF void move_unit(struct unit *u, float dt) {
    u->x += u->vx * dt;
    u->y += u->vy * dt;
    if (u->hp > u->max_hp) u->hp = u->max_hp;
    if (u->x < 0) { u->x = 0; u->flags |= 1; }
}
LEAF double dist2(const struct unit *a, const struct unit *b) {
    double dx = (double)a->x - b->x, dy = (double)a->y - b->y;
    return dx * dx + dy * dy;
}

int main(void) { return 0; }

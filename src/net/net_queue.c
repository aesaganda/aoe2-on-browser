/* net_queue.c - the frames that arrived from the relay, until the game thread takes them. */
#include <pthread.h>
#include <stdlib.h>
#include <string.h>

#include "net.h"

struct frame { struct frame *next; size_t n; uint8_t b[]; };
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static struct frame *g_head, *g_tail;
static volatile int g_down;

void net_inbound(const uint8_t *b, size_t n) {
    struct frame *f = malloc(sizeof *f + n);
    if (!f) return;
    f->next = NULL, f->n = n;
    memcpy(f->b, b, n);
    pthread_mutex_lock(&g_lock);
    if (g_tail) g_tail->next = f; else g_head = f;
    g_tail = f;
    pthread_mutex_unlock(&g_lock);
}

size_t net_take(uint8_t **out) {
    pthread_mutex_lock(&g_lock);
    struct frame *f = g_head;
    if (f) { g_head = f->next; if (!g_head) g_tail = NULL; }
    pthread_mutex_unlock(&g_lock);
    if (!f) return 0;
    size_t n = f->n;
    uint8_t *b = malloc(n ? n : 1);
    if (b) memcpy(b, f->b, n);
    free(f);
    *out = b;
    return b ? n : 0;
}

void net_closed(void) { g_down = 1; }
int  net_down(void) { return g_down; }

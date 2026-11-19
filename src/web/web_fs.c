/*
 * web_fs.c - where the game's files are in a browser (Phase 2, ADR-0005).
 *
 * The page (web/aoe2.js) copies the owner's install tree into the origin private
 * file system (OPFS) under aoe2/ before it starts the module; here WASMFS's OPFS
 * backend is mounted at /opfs, so the game reads and writes it in place (saved
 * games, recordings, the player profile persist across visits). The install root
 * is then /opfs/aoe2, mapped to C: by main.c as on Linux.
 */
#ifdef __EMSCRIPTEN__
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/stat.h>

#include <emscripten.h>
#include <emscripten/wasmfs.h>

#include "win32hle.h"

#ifndef WM_LBUTTONDOWN
#define WM_LBUTTONDOWN 0x0201u
#define WM_LBUTTONUP   0x0202u
#define WM_RBUTTONDOWN 0x0204u
#define WM_RBUTTONUP   0x0205u
#define WM_MBUTTONDOWN 0x0207u
#define WM_MBUTTONUP   0x0208u
#endif

#define WEB_INPUT_CAP 128

typedef struct {
    int kind; /* 0 = relative motion (dx, dy), 1 = button (btn, down) */
    int a;
    int b;
} web_input_ev_t;

static pthread_mutex_t g_web_in_lock = PTHREAD_MUTEX_INITIALIZER;
static web_input_ev_t g_web_in_q[WEB_INPUT_CAP];
static int g_web_in_n;
static void (*g_prev_web_poll)(void);

const char *web_game_root(void) {
    static int mounted;
    if (!mounted) {
        backend_t opfs = wasmfs_create_opfs_backend();
        if (!opfs || wasmfs_create_directory("/opfs", 0777, opfs) != 0)
            fprintf(stderr, "[web] cannot mount the origin private file system\n");
        mounted = 1;
    }
    return "/opfs/aoe2";
}

/* Invalidate win32hle's cached directory listings after browser OPFS writes. */
EMSCRIPTEN_KEEPALIVE void web_fs_notify_change(void) {
    hle_path_changed();
}

/* Notify win32hle when the browser tab is hidden/visible to avoid background CPU throttling. */
EMSCRIPTEN_KEEPALIVE void web_set_hidden(int hidden) {
    hle_screen_set_hidden(hidden);
}

/* Enqueue relative pointer delta from Pointer Lock on the main browser thread. */
EMSCRIPTEN_KEEPALIVE void web_input_motion(int dx, int dy) {
    if (!dx && !dy) return;
    pthread_mutex_lock(&g_web_in_lock);
    if (g_web_in_n > 0 && g_web_in_q[g_web_in_n - 1].kind == 0) {
        g_web_in_q[g_web_in_n - 1].a += dx;
        g_web_in_q[g_web_in_n - 1].b += dy;
    } else if (g_web_in_n < WEB_INPUT_CAP) {
        g_web_in_q[g_web_in_n++] = (web_input_ev_t){ 0, dx, dy };
    }
    pthread_mutex_unlock(&g_web_in_lock);
}

/* Enqueue mouse button transitions while Pointer Lock bypasses SDL's canvas listener. */
EMSCRIPTEN_KEEPALIVE void web_input_button(int btn, int down) {
    pthread_mutex_lock(&g_web_in_lock);
    if (g_web_in_n < WEB_INPUT_CAP) {
        g_web_in_q[g_web_in_n++] = (web_input_ev_t){ 1, btn, down ? 1 : 0 };
    }
    pthread_mutex_unlock(&g_web_in_lock);
}

static void web_input_poll(void) {
    if (g_prev_web_poll) g_prev_web_poll();
    web_input_ev_t batch[WEB_INPUT_CAP];
    pthread_mutex_lock(&g_web_in_lock);
    int n = g_web_in_n;
    if (n > 0) {
        for (int i = 0; i < n; i++) batch[i] = g_web_in_q[i];
        g_web_in_n = 0;
    }
    pthread_mutex_unlock(&g_web_in_lock);
    for (int i = 0; i < n; i++) {
        if (batch[i].kind == 0) {
            hle_input_motion(batch[i].a, batch[i].b);
        } else {
            int gx = 0, gy = 0;
            hle_input_position(&gx, &gy);
            int down = batch[i].b;
            uint32_t msg = batch[i].a == 2 ? (down ? WM_RBUTTONDOWN : WM_RBUTTONUP)
                         : batch[i].a == 1 ? (down ? WM_MBUTTONDOWN : WM_MBUTTONUP)
                                           : (down ? WM_LBUTTONDOWN : WM_LBUTTONUP);
            hle_input_mouse(msg, gx, gy);
        }
    }
}

void web_input_install(void) {
    g_prev_web_poll = hle_screen_poll_hook;
    hle_screen_poll_hook = web_input_poll;
}
#endif

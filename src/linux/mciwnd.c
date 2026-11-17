/*
 * mciwnd.c - the game's AVI cinematics: MCIWndCreateA and the MCIWnd window.
 *
 * The game (0x5CA080) makes an MCIWnd child of its window for avi\<name>.avi,
 * subclasses it (clicks skip the video), centres it by its client rect, sends it
 * MCI_PLAY, and from its main loop polls MCIWNDM_GETMODEA (0x5D17E9): "stopped"
 * ends the video, "paused" gets an MCI_RESUME. To close it sends MCI_STOP and
 * WM_CLOSE. That is the whole interface used, and what is answered here:
 *
 *   MCIWndCreateA (cdecl)  a win32hle child window of class "AOE2MCIWnd" whose
 *                          procedure is the host's (aoe2!MCIWndProc), sized to
 *                          the video;
 *   MCI_PLAY / MCI_STOP / MCI_PAUSE / MCI_RESUME, MCIWNDM_GETMODEA, WM_CLOSE.
 *
 * The pictures: the original's Indeo 5 is transcoded at install time to an MPEG-1
 * copy beside it (tools/transcode_videos.sh, ADR-0004), decoded here with pl_mpeg.
 * Frames are painted in true colour over the presented picture, in the window's
 * rectangle (patch 0004's hle_screen_rgb_hook). The sound is a win32hle audio
 * stream, mixed with the game's DirectSound buffers on their one device (a browser
 * has one). Decoding follows the wall clock from MCI_PLAY.
 *
 * AOE2_SKIP_VIDEOS=1 answers no window, and the game goes on without the video
 * (scripted sessions: tools/native/session.sh sets it unless told otherwise).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <SDL.h>

#define PL_MPEG_IMPLEMENTATION
#include "pl_mpeg.h"

#include "win32hle.h"

#define WM_DESTROY 0x0002u
#define WM_CLOSE   0x0010u
#define MCI_PLAY   0x0806u
#define MCI_STOP   0x0808u
#define MCI_PAUSE  0x0809u
#define MCI_RESUME 0x0855u
#define MCIWNDM_GETMODEA 0x046Au
#define WS_CHILD   0x40000000u
#define WS_VISIBLE 0x10000000u

static struct {
    uint32_t hwnd;
    plm_t *plm;
    int w, h;
    uint32_t *rgb;                       /* the latest frame, XRGB8888 w x h */
    int have_frame;
    enum { STOPPED, PLAYING, PAUSED } mode;
    int audio;                           /* hle_audio_stream id, -1 none */
    uint64_t t0;                         /* SDL_GetPerformanceCounter at the last (re)start */
    double played;                       /* media seconds played before t0 */
} V;

static uint32_t g_class_registered;
static int g_trace = -1;
#define TRACE(...) do { if (g_trace < 0) g_trace = getenv("AOE2_TRACE_VIDEO") != NULL; \
                        if (g_trace) fprintf(stderr, "[video] " __VA_ARGS__); } while (0)

static uint32_t call(const char *api, int n, const uint32_t *args) { return hle_call_guest(hle_resolve(api), n, args); }

/* A guest copy of a host string (class name, window title). */
static uint32_t guest_str(const char *s) {
    char *g = (char *)hle_alloc((uint32_t)strlen(s) + 1);
    strcpy(g, s);
    return hle_va(g);
}

static void on_video(plm_t *plm, plm_frame_t *f, void *user) {
    (void)plm, (void)user;
    plm_frame_to_bgra(f, (uint8_t *)V.rgb, V.w * 4);       /* B G R A in memory: XRGB8888 little-endian */
    V.have_frame = 1;
}

static void on_audio(plm_t *plm, plm_samples_t *s, void *user) {
    (void)plm, (void)user;
    int16_t pcm[PLM_AUDIO_SAMPLES_PER_FRAME * 2];
    for (unsigned i = 0; i < s->count * 2; i++) {
        float v = s->interleaved[i] * 32767.0f;
        pcm[i] = (int16_t)(v > 32767.0f ? 32767 : v < -32768.0f ? -32768 : v);
    }
    if (V.audio >= 0) hle_audio_stream_push(V.audio, pcm, (int)s->count);
}

static double now_s(void) { return (double)SDL_GetPerformanceCounter() / (double)SDL_GetPerformanceFrequency(); }

static void video_close(void) {
    if (V.audio >= 0) hle_audio_stream_close(V.audio);
    if (V.plm) plm_destroy(V.plm);
    free(V.rgb);
    memset(&V, 0, sizeof V);
    V.audio = -1;
}

static void play(void) {
    if (V.mode == PLAYING) return;
    if (V.mode == STOPPED) plm_rewind(V.plm), V.played = 0;
    V.mode = PLAYING;
    V.t0 = SDL_GetPerformanceCounter();
    TRACE("play\n");
}

static void hold(int stop) {
    if (V.mode != PLAYING) { if (stop) V.mode = STOPPED; return; }
    V.played += now_s() - (double)V.t0 / (double)SDL_GetPerformanceFrequency();
    V.mode = stop ? STOPPED : PAUSED;
    /* what is queued plays out (a fraction of a second: the decoder leads by little) */
    TRACE("%s\n", stop ? "stop" : "pause");
}

/* Decode up to the wall clock; the end stops it (the game polls the mode). */
static void advance(void) {
    if (V.mode != PLAYING) return;
    double t = V.played + now_s() - (double)V.t0 / (double)SDL_GetPerformanceFrequency();
    double behind = t - plm_get_time(V.plm);
    if (behind > 0) plm_decode(V.plm, behind > 0.5 ? 0.5 : behind);
    if (plm_has_ended(V.plm)) { TRACE("ended\n"); hold(1); }
}

/* hle_screen_rgb_hook: the frame, scaled into the window's rectangle (the game
 * switches to 640x480x16 for its videos and doubles the window when it can). */
static int paint(uint32_t *px, int w, int h) {
    if (!V.hwnd || !V.plm) return 0;
    if (!px) return V.mode == PLAYING || V.have_frame;   /* the presenter asks first */
    advance();
    int l, t, r, b;
    if (V.have_frame && hle_window_visible(V.hwnd) && hle_window_screen_rect(V.hwnd, &l, &t, &r, &b) && r > l && b > t) {
        for (int y = t < 0 ? 0 : t; y < b && y < h; y++) {
            const uint32_t *src = V.rgb + (size_t)((y - t) * V.h / (b - t)) * V.w;
            for (int x = l < 0 ? 0 : l; x < r && x < w; x++)
                px[(size_t)y * w + x] = 0xFF000000u | src[(x - l) * V.w / (r - l)];
        }
    }
    return 1;
}

/* The window's procedure (stdcall: hwnd, msg, wParam, lParam). */
static void mciwnd_proc(void) {
    uint32_t hwnd = A32(0), msg = A32(1), wp = A32(2), lp = A32(3);
    if (hwnd == V.hwnd) {
        switch (msg) {
        case MCI_PLAY: play(); RET(0, 4);
        case MCI_RESUME: play(); RET(0, 4);
        case MCI_STOP: hold(1); RET(0, 4);
        case MCI_PAUSE: hold(0); RET(0, 4);
        case MCIWNDM_GETMODEA: {
            advance();
            const char *m = V.mode == PLAYING ? "playing" : V.mode == PAUSED ? "paused" : "stopped";
            if (lp && wp) snprintf((char *)hle_ptr(lp), wp, "%s", m);
            RET(0, 4);
        }
        case WM_DESTROY: TRACE("closed\n"); video_close(); break;
        default: break;
        }
    }
    uint32_t a[4] = { hwnd, msg, wp, lp };
    RET(call("DefWindowProcA", 4, a), 4);   /* WM_CLOSE: DestroyWindow */
}

/* HWND MCIWndCreateA(HWND parent, HINSTANCE, DWORD style, LPCSTR file): cdecl. */
static void m_MCIWndCreateA(void) {
    uint32_t parent = A32(0), hinst = A32(1), style = A32(2);
    const char *file = ASTR(3) ? ASTR(3) : "";
    char host[1024], mpg[1040];
    if (getenv("AOE2_SKIP_VIDEOS") && strcmp(getenv("AOE2_SKIP_VIDEOS"), "0")) RET(0, 0);   /* as if not installed */
    if (V.hwnd) { uint32_t a[1] = { V.hwnd }; call("DestroyWindow", 1, a); }
    if (!hle_host_path(file, host, sizeof host)) { fprintf(stderr, "[video] %s: not found\n", file); RET(0, 0); }
    snprintf(mpg, sizeof mpg, "%s", host);
    char *dot = strrchr(mpg, '.');
    if (dot) strcpy(dot, ".mpg");
    plm_t *plm = plm_create_with_filename(mpg);
    if (!plm || !plm_probe(plm, 5000 * 1024)) {
        fprintf(stderr, "[video] %s: no MPEG-1 copy (%s); run tools/transcode_videos.sh on the install\n", file, mpg);
        if (plm) plm_destroy(plm);
        RET(0, 0);                       /* the game goes on without the video */
    }
    V.audio = -1;
    V.plm = plm, V.w = plm_get_width(plm), V.h = plm_get_height(plm);
    V.rgb = (uint32_t *)calloc((size_t)V.w * V.h, 4);
    plm_set_video_decode_callback(plm, on_video, NULL);
    plm_set_loop(plm, 0);
    if (plm_get_num_audio_streams(plm) > 0) {
        V.audio = hle_audio_stream_open(plm_get_samplerate(plm), 2);
        plm_set_audio_enabled(plm, V.audio >= 0);
        plm_set_audio_decode_callback(plm, on_audio, NULL);
        plm_set_audio_lead_time(plm, 2048.0 / plm_get_samplerate(plm));
    }
    if (!g_class_registered) {
        uint32_t *wc = (uint32_t *)hle_alloc(40);            /* WNDCLASSA */
        memset(wc, 0, 40);
        wc[1] = hle_resolve("aoe2!MCIWndProc");              /* lpfnWndProc */
        wc[4] = hinst;
        wc[9] = guest_str("AOE2MCIWnd");                     /* lpszClassName */
        uint32_t a[1] = { hle_va(wc) };
        g_class_registered = call("RegisterClassA", 1, a);
    }
    uint32_t a[12] = { 0, guest_str("AOE2MCIWnd"), guest_str(file), style & (WS_CHILD | WS_VISIBLE),
                       0, 0, (uint32_t)V.w, (uint32_t)V.h, parent, 0, hinst, 0 };
    V.hwnd = call("CreateWindowExA", 12, a);
    V.mode = STOPPED;
    TRACE("%s: %dx%d, %.1f s, window %05X\n", mpg, V.w, V.h, plm_get_duration(plm), V.hwnd);
    if (!V.hwnd) video_close();
    RET(V.hwnd, 0);
}

/* BOOL ICInfo(fccType, fccHandler, ICINFO*): the game asks for 'vidc' 'iv50' (Indeo
 * Video 5) at start-up and turns its videos off without it (0x5C6B43). The copies
 * played here are MPEG-1, so the answer is yes for that codec, as an installed
 * Indeo driver would give it; anything else, no. */
static void m_ICInfo(void) {
    uint32_t type = A32(0), handler = A32(1) | 0x20202020u, info = A32(2);   /* fourcc, lower-cased */
    if (type != 0x63646976u /* vidc */ || handler != 0x30357669u /* iv50 */) RET(0, 3);
    if (info) MEM32(info + 4) = type, MEM32(info + 8) = A32(1);           /* ICINFO fccType, fccHandler */
    RET(1, 3);
}

static const win32hle_shim shims[] = {
    { "MCIWndCreateA", m_MCIWndCreateA },
    { "ICInfo", m_ICInfo },
    { "aoe2!MCIWndProc", mciwnd_proc },
    { NULL, NULL },
};

/* Before win32_gaps (whose MCIWndCreateA says "not ported"): the first name wins. */
void mciwnd_register(void) {
    win32hle_register(shims);
    hle_screen_rgb_hook = paint;
}

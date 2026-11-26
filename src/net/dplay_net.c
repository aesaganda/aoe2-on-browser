/*
 * dplay_net.c - IDirectPlay4A for the game's multiplayer, over the relay (ADR-0006).
 *
 * The game (AoC 1.0) lists service providers with DirectPlayEnumerateA, creates
 * DirectPlay for the one picked, and then uses the session calls: EnumSessions for
 * the game list, Open to create or join, CreatePlayer, Send / Receive with
 * GetMessageCount every frame, Get/SetSessionDesc for the game's settings, and the
 * player queries. Those are answered here; the relay (tools/net/relay.py) keeps the
 * sessions ("rooms") and their players and forwards every message.
 *
 * Threading: all state is the game thread's. Frames that arrive wait in net_queue.c
 * and are applied whenever the game calls into DirectPlay or the screen pumps
 * (hle_screen_poll_hook); a message for a local player then sets the event the game
 * passed to CreatePlayer, as DirectPlay does.
 *
 * Not here: groups (AoC uses none), secure sessions, lobby launch (dplay_stub.c).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "win32hle.h"
#include "net.h"

#define DP_OK                 0x00000000u
#define DPERR_GENERIC         0x80004005u
#define DPERR_UNSUPPORTED     0x80004001u
#define DPERR_INVALIDPARAMS   0x80070057u
#define DPERR_BUFFERTOOSMALL  0x8877001Eu
#define DPERR_INVALIDPLAYER   0x88770096u
#define DPERR_NOCONNECTION    0x887700AAu
#define DPERR_NOMESSAGES      0x887700BEu
#define DPERR_NOSESSIONS      0x887700DCu
#define DPERR_TIMEOUT         0x887700F0u
#define DPERR_UNINITIALIZED   0x88770140u
#define DPERR_CONNECTIONLOST  0x88770168u

#define DPRECEIVE_TOPLAYER    0x02u
#define DPRECEIVE_FROMPLAYER  0x04u
#define DPRECEIVE_PEEK        0x08u
#define DPOPEN_JOIN           0x01u
#define DPOPEN_CREATE         0x02u
#define DPENUMSESSIONS_ASYNC  0x10u
#define DPENUMSESSIONS_STOPASYNC 0x20u
#define DPESC_TIMEDOUT        0x01u
#define DPPLAYER_LOCAL        0x08u
#define DPENUMPLAYERS_LOCAL   0x08u
#define DPENUMPLAYERS_REMOTE  0x10u
#define DPENUMPLAYERS_GROUP   0x20u
#define DPPLAYERTYPE_PLAYER   1u

#define DPSYS_CREATEPLAYERORGROUP  0x0003u
#define DPSYS_DESTROYPLAYERORGROUP 0x0005u
#define DPSYS_SESSIONLOST          0x0031u
#define DPSYS_HOST                 0x0101u
#define DPSYS_SETPLAYERORGROUPDATA 0x0102u
#define DPSYS_SETPLAYERORGROUPNAME 0x0103u
#define DPSYS_SETSESSIONDESC       0x0104u

enum { HELLO = 1, LIST, CREATE, JOIN, NEWPLAYER, DELPLAYER, SEND, SETDESC, SETPDATA, SETPNAME, LEAVE };
enum { REPLY = 100, MSG, SYS_NEWPLAYER, SYS_DELPLAYER, SYS_SETDESC, SYS_SETPDATA, SYS_SETPNAME, SYS_HOST, SYS_LOST };

static int g_trace = -1;
#define TRACE(...) do { if (g_trace < 0) g_trace = getenv("AOE2_TRACE_DPLAY") != NULL; \
                        if (g_trace) fprintf(stderr, "[dplay] " __VA_ARGS__); } while (0)

/* ---------------------------------------------------------------- frames */

typedef struct { uint8_t *b; size_t n, cap; } buf_t;
static void put(buf_t *w, const void *p, size_t n) {
    if (w->n + n > w->cap) { w->cap = (w->n + n) * 2 + 64; w->b = realloc(w->b, w->cap); }
    memcpy(w->b + w->n, p, n), w->n += n;
}
static void put8(buf_t *w, uint8_t v)   { put(w, &v, 1); }
static void put32(buf_t *w, uint32_t v) { uint8_t b[4] = { (uint8_t)v, (uint8_t)(v >> 8), (uint8_t)(v >> 16), (uint8_t)(v >> 24) }; put(w, b, 4); }
static void putstr(buf_t *w, const char *s) {
    size_t n = s ? strlen(s) : 0;
    uint8_t h[2] = { (uint8_t)n, (uint8_t)(n >> 8) };
    put(w, h, 2), put(w, s, n);
}
static void putblob(buf_t *w, const void *p, uint32_t n) { put32(w, n), put(w, p, n); }

typedef struct { const uint8_t *b; size_t n, o; int bad; } rd_t;
static const uint8_t *get(rd_t *r, size_t n) { if (r->o + n > r->n) { r->bad = 1; return NULL; } const uint8_t *p = r->b + r->o; r->o += n; return p; }
static uint32_t get32(rd_t *r) { const uint8_t *p = get(r, 4); return p ? (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24 : 0; }
static uint8_t get8(rd_t *r) { const uint8_t *p = get(r, 1); return p ? *p : 0; }
static char *getstr(rd_t *r) {                          /* a NUL-terminated host copy */
    const uint8_t *h = get(r, 2);
    size_t n = h ? (size_t)h[0] | (size_t)h[1] << 8 : 0;
    const uint8_t *p = get(r, n);
    char *s = malloc(n + 1);
    if (p) memcpy(s, p, n);
    s[p ? n : 0] = 0;
    return s;
}
static uint8_t *getblob(rd_t *r, uint32_t *n) {
    *n = get32(r);
    const uint8_t *p = get(r, *n);
    uint8_t *b = malloc(*n ? *n : 1);
    if (p) memcpy(b, p, *n); else *n = 0;
    return b;
}

/* ---------------------------------------------------------------- state */

typedef struct player {
    struct player *next;
    uint32_t id, flags, data_n;
    char *short_name, *long_name;
    uint8_t *data;
    int local;
    uint32_t event;                                     /* the game's hEvent for a local player */
} player_t;

typedef struct msg {
    struct msg *next;
    uint32_t from, to, n;
    uint8_t *b;                                         /* a data message, or a system message's fields */
    uint32_t sys;                                       /* 0: data; else DPSYS_* */
    player_t *p;                                        /* a system message's player (a copy) */
} msg_t;

static struct {
    int connected, in_session, host;
    uint8_t desc[80];                                   /* DPSESSIONDESC2, pointers zero */
    char *name, *password;
    player_t *players;
    msg_t *head, *tail;
    uint32_t next_req;
    struct { uint32_t req, hr; uint8_t *b; size_t n; int done; } reply;
    uint8_t *rooms; size_t rooms_n;                     /* the last LIST reply */
} S;

static player_t *find(uint32_t id) { for (player_t *p = S.players; p; p = p->next) if (p->id == id) return p; return NULL; }
static player_t *first_local(void) { for (player_t *p = S.players; p; p = p->next) if (p->local) return p; return NULL; }
static void free_player(player_t *p) { if (p) free(p->short_name), free(p->long_name), free(p->data), free(p); }
static player_t *copy_player(const player_t *p) {
    player_t *c = calloc(1, sizeof *c);
    *c = *p, c->next = NULL;
    c->short_name = strdup(p->short_name ? p->short_name : ""), c->long_name = strdup(p->long_name ? p->long_name : "");
    c->data = malloc(p->data_n ? p->data_n : 1), memcpy(c->data, p->data, p->data_n);
    return c;
}
static player_t *read_player(rd_t *r) {
    player_t *p = calloc(1, sizeof *p);
    p->id = get32(r), p->flags = get32(r);
    p->short_name = getstr(r), p->long_name = getstr(r);
    p->data = getblob(r, &p->data_n);
    return p;
}

static void queue(msg_t *m) {
    if (S.tail) S.tail->next = m; else S.head = m;
    S.tail = m;
}
static void sys_msg(uint32_t type, const player_t *p) {
    msg_t *m = calloc(1, sizeof *m);
    player_t *l = first_local();
    m->sys = type, m->from = 0, m->to = l ? l->id : 0;
    m->p = p ? copy_player(p) : NULL;
    queue(m);
}

/* A lobby restore (AOE2_RESTORE, the room's save, for every player): the game takes a
 * joining player's slot in the saved game from the first character of its long name, as
 * the Zone set it (0x42D0C6; "Bob" reads as slot 0 and is turned away). Find it by name in
 * the save's header: players 1-8 at 0x5D4 + 0xA0 * n, name first. */
static void restore_slot(player_t *p) {
    const char *file = getenv("AOE2_RESTORE");
    char guest[300], host[1024];
    uint8_t h[0xE48];
    if (!file) return;
    snprintf(guest, sizeof guest, "savegame\\multi\\%s", file);
    FILE *f = hle_host_path(guest, host, sizeof host) ? fopen(host, "rb") : NULL;
    if (!f) return;
    size_t n = fread(h, 1, sizeof h, f);
    fclose(f);
    size_t len = strlen(p->short_name) + 1;
    for (int i = 1; n == sizeof h && i <= 8; i++)
        if (len <= 0xA0 && !memcmp(h + 0x5D4 + 0xA0 * i, p->short_name, len)) {
            char s[4];
            snprintf(s, sizeof s, "%d", i);
            free(p->long_name), p->long_name = strdup(s);
            TRACE("restore: \"%s\" is player %d of the save\n", p->short_name, i);
            return;
        }
}

static void read_desc(rd_t *r) {
    const uint8_t *d = get(r, 80);
    if (d) memcpy(S.desc, d, 80);
    free(S.name), free(S.password);
    S.name = getstr(r), S.password = getstr(r);
}

/* Apply what arrived; a reply is kept for the request waiting on it. */
static void apply(const uint8_t *b, size_t n) {
    rd_t r = { b, n, 0, 0 };
    uint8_t t = get8(&r);
    get32(&r);
    switch (t) {
    case REPLY: {
        uint32_t req = get32(&r), hr = get32(&r);
        if (req == S.reply.req) {
            S.reply.hr = hr, S.reply.done = 1;
            S.reply.n = n - r.o, S.reply.b = malloc(S.reply.n ? S.reply.n : 1);
            memcpy(S.reply.b, b + r.o, S.reply.n);
        }
        break;
    }
    case MSG: {
        msg_t *m = calloc(1, sizeof *m);
        m->from = get32(&r), m->to = get32(&r);
        m->b = getblob(&r, &m->n);
        queue(m);
        break;
    }
    case SYS_NEWPLAYER: {
        player_t *p = read_player(&r);
        get32(&r);
        if (find(p->id)) { free_player(p); break; }
        restore_slot(p);
        p->next = S.players, S.players = p;
        sys_msg(DPSYS_CREATEPLAYERORGROUP, p);
        TRACE("player %08X \"%s\" joined\n", p->id, p->short_name);
        break;
    }
    case SYS_DELPLAYER: {
        player_t *gone = read_player(&r);
        for (player_t **pp = &S.players; *pp; pp = &(*pp)->next)
            if ((*pp)->id == gone->id) { player_t *p = *pp; *pp = p->next; sys_msg(DPSYS_DESTROYPLAYERORGROUP, p); free_player(p); break; }
        TRACE("player %08X left\n", gone->id);
        free_player(gone);
        break;
    }
    case SYS_SETDESC: read_desc(&r); sys_msg(DPSYS_SETSESSIONDESC, NULL); break;
    case SYS_SETPDATA: {
        uint32_t id = get32(&r), dn;
        uint8_t *d = getblob(&r, &dn);
        player_t *p = find(id);
        if (p) { free(p->data), p->data = d, p->data_n = dn; sys_msg(DPSYS_SETPLAYERORGROUPDATA, p); } else free(d);
        break;
    }
    case SYS_SETPNAME: {
        uint32_t id = get32(&r);
        char *sn = getstr(&r), *ln = getstr(&r);
        player_t *p = find(id);
        if (p) { free(p->short_name), free(p->long_name), p->short_name = sn, p->long_name = ln; sys_msg(DPSYS_SETPLAYERORGROUPNAME, p); }
        else free(sn), free(ln);
        break;
    }
    case SYS_HOST: S.host = 1; sys_msg(DPSYS_HOST, NULL); TRACE("now the host\n"); break;
    case SYS_LOST: S.in_session = 0; sys_msg(DPSYS_SESSIONLOST, NULL); break;
    default: TRACE("unknown frame %u\n", t);
    }
}

/* Take what arrived; tell local players' events there are messages. */
static void pump(void) {
    uint8_t *b = NULL;
    size_t n;
    while ((n = net_take(&b)) > 0) { apply(b, n); free(b); b = NULL; }
    if (S.head)
        for (player_t *p = S.players; p; p = p->next)
            if (p->local && p->event) hle_set_event(p->event);
    if (S.in_session && net_down()) { S.in_session = 0; sys_msg(DPSYS_SESSIONLOST, NULL); }
}
static void (*g_prev_poll)(void);
static void poll_hook(void) { if (S.connected) pump(); if (g_prev_poll) g_prev_poll(); }

/* Send a request and wait for its reply (up to 5 s). The reply body stays in S.reply. */
static uint32_t request(buf_t *w) {
    free(S.reply.b), S.reply.b = NULL, S.reply.done = 0, S.reply.n = 0;
    S.reply.req = ++S.next_req;
    w->b[1] = (uint8_t)S.reply.req, w->b[2] = (uint8_t)(S.reply.req >> 8), w->b[3] = (uint8_t)(S.reply.req >> 16), w->b[4] = (uint8_t)(S.reply.req >> 24);
    net_send(w->b, w->n);
    free(w->b);
    for (int ms = 0; ms < 5000 && !S.reply.done && !net_down(); ms++) {
        pump();
        if (!S.reply.done) usleep(1000);
    }
    return S.reply.done ? S.reply.hr : net_down() ? DPERR_CONNECTIONLOST : DPERR_TIMEOUT;
}
static buf_t frame(uint8_t type) { buf_t w = { 0 }; put8(&w, type), put32(&w, 0); return w; }

static int connect_relay(void) {
    if (S.connected) return 1;
    if (net_open() != 0) return 0;
    S.connected = 1;
    buf_t w = frame(HELLO);
    putstr(&w, "player");
    return request(&w) == DP_OK;
}

/* ---------------------------------------------------------------- guest structures */

/* A guest copy of a host string; scratch the game reads during the call. */
static uint32_t gstr(const char *s) { size_t n = strlen(s ? s : "") + 1; char *g = hle_alloc((uint32_t)n); memcpy(g, s ? s : "", n); return hle_va(g); }
static char *hstr(uint32_t va) { return strdup(va ? (const char *)hle_ptr(va) : ""); }

/* A DPSESSIONDESC2 at guest dst, with the name stored after it when room allows */
static void put_desc(uint32_t dst, const uint8_t *d, const char *name, const char *password) {
    memcpy(hle_ptr(dst), d, 80);
    MEM32(dst) = 80;
    MEM32(dst + 48) = name ? gstr(name) : 0;            /* lpszSessionNameA */
    MEM32(dst + 52) = password && *password ? gstr(password) : 0;
}

/* ---------------------------------------------------------------- methods (stdcall, arg 0 = this) */

#define RETN(v, n) RET(v, n)

static void dp_Initialize(void) { RET(DP_OK, 2); }
static void dp_InitializeConnection(void) { TRACE("InitializeConnection\n"); RET(connect_relay() ? DP_OK : DPERR_NOCONNECTION, 3); }

static void dp_GetCaps(void) {                          /* (this, DPCAPS*, flags) */
    uint32_t c = A32(1);
    if (!c) RET(DPERR_INVALIDPARAMS, 3);
    MEM32(c) = 40, MEM32(c + 4) = 0, MEM32(c + 8) = 0x10000, MEM32(c + 12) = 0, MEM32(c + 16) = 8;
    MEM32(c + 20) = 10000, MEM32(c + 24) = 100, MEM32(c + 28) = 8, MEM32(c + 32) = 0, MEM32(c + 36) = 5000;
    RET(DP_OK, 3);
}
static void dp_GetPlayerCaps(void) {                    /* (this, id, DPCAPS*, flags) */
    uint32_t c = A32(2);
    if (!c) RET(DPERR_INVALIDPARAMS, 4);
    MEM32(c) = 40, MEM32(c + 4) = 0, MEM32(c + 8) = 0x10000, MEM32(c + 12) = 0, MEM32(c + 16) = 8;
    MEM32(c + 20) = 10000, MEM32(c + 24) = 100, MEM32(c + 28) = 8, MEM32(c + 32) = 0, MEM32(c + 36) = 5000;
    RET(DP_OK, 4);
}

/* (this, DPSESSIONDESC2*, timeout, cb, ctx, flags); cb(desc*, &timeout, flags, ctx) */
static void dp_EnumSessions(void) {
    uint32_t sd = A32(1), cb = A32(3), ctx = A32(4), flags = A32(5);
    TRACE("EnumSessions flags %X\n", flags);
    if (flags & DPENUMSESSIONS_STOPASYNC) RET(DP_OK, 6);
    if (!connect_relay()) RET(DPERR_NOCONNECTION, 6);
    buf_t w = frame(LIST);
    if (sd) put(&w, (uint8_t *)hle_ptr(sd) + 24, 16); else { uint8_t z[16] = { 0 }; put(&w, z, 16); }
    uint32_t hr = request(&w);
    if (hr != DP_OK) RET(hr, 6);
    rd_t r = { S.reply.b, S.reply.n, 0, 0 };
    uint32_t n = get32(&r);
    uint32_t g = hle_va(hle_alloc(80 + 4)), tmo = g + 80;
    MEM32(tmo) = A32(2);
    for (uint32_t i = 0; i < n && !r.bad; i++) {
        const uint8_t *d = get(&r, 80);
        char *name = getstr(&r), *pw = getstr(&r);
        if (d) {
            put_desc(g, d, name, NULL);
            TRACE("  session \"%s\" %u/%u\n", name, MEM32(g + 44), MEM32(g + 40));
            uint32_t a[4] = { g, tmo, 0, ctx };
            if (cb && !hle_call_guest(cb, 4, a)) { free(name), free(pw); break; }
        }
        free(name), free(pw);
    }
    if (cb && !(flags & DPENUMSESSIONS_ASYNC)) {
        uint32_t a[4] = { 0, tmo, DPESC_TIMEDOUT, ctx };
        hle_call_guest(cb, 4, a);                       /* once: asking to wait longer is not honoured */
    }
    RET(DP_OK, 6);
}

static uint32_t read_session(void) {                    /* a CREATE/JOIN reply: desc, players */
    rd_t r = { S.reply.b, S.reply.n, 0, 0 };
    read_desc(&r);
    uint32_t n = get32(&r);
    for (uint32_t i = 0; i < n && !r.bad; i++) { player_t *p = read_player(&r); p->next = S.players, S.players = p; }
    S.in_session = 1;
    return DP_OK;
}

/* Create (DPOPEN_CREATE) or join a session: desc's pointers ignored, name and password apart */
static uint32_t open_session(const uint8_t *desc, const char *name, const char *pw, uint32_t flags) {
    if (!connect_relay()) return DPERR_NOCONNECTION;
    uint8_t d[80];
    memcpy(d, desc, 80);
    memset(d + 48, 0, 8);
    buf_t w = frame(flags & DPOPEN_CREATE ? CREATE : JOIN);
    if (flags & DPOPEN_CREATE) {
        put(&w, d, 80), putstr(&w, name), putstr(&w, pw);
        if (getenv("AOE2_RESTORE")) putstr(&w, getenv("AOE2_RESTORE"));   /* the room's restore, for joiners' pages */
    }
    else put(&w, d + 8, 16), putstr(&w, pw);
    TRACE("Open %s \"%s\"\n", flags & DPOPEN_CREATE ? "create" : "join", name);
    uint32_t hr = request(&w);
    if (hr == DP_OK) {
        read_session();
        S.host = (flags & DPOPEN_CREATE) != 0;
    }
    TRACE("Open -> %08X\n", hr);
    return hr;
}

static void dp_Open(void) {                             /* (this, DPSESSIONDESC2*, flags) */
    uint32_t sd = A32(1), flags = A32(2);
    if (!sd) RET(DPERR_INVALIDPARAMS, 3);
    if (S.in_session) RET(DP_OK, 3);                    /* opened already by a lobby launch (Connect) */
    char *name = hstr(MEM32(sd + 48)), *pw = hstr(MEM32(sd + 52));
    uint32_t hr = open_session(hle_ptr(sd), name, pw, flags);
    free(name), free(pw);
    if (hr == DP_OK) memcpy((uint8_t *)hle_ptr(sd) + 8, S.desc + 8, 16);   /* guidInstance, as DirectPlay fills it in */
    RET(hr, 3);
}

static void dp_Close(void) {
    TRACE("Close\n");
    if (S.in_session) { buf_t w = frame(LEAVE); request(&w); }
    S.in_session = 0, S.host = 0;
    while (S.players) { player_t *p = S.players; S.players = p->next; free_player(p); }
    while (S.head) { msg_t *m = S.head; S.head = m->next; free(m->b), free_player(m->p), free(m); }
    S.tail = NULL;
    RET(DP_OK, 1);
}

/* (this, &id, DPNAME*, hEvent, data, size, flags) */
static void dp_CreatePlayer(void) {
    uint32_t pid = A32(1), nm = A32(2), ev = A32(3), data = A32(4), size = A32(5), flags = A32(6);
    if (!S.in_session || !pid) RET(DPERR_INVALIDPARAMS, 7);
    char *sn = nm ? hstr(MEM32(nm + 8)) : strdup(""), *ln = nm ? hstr(MEM32(nm + 12)) : strdup("");
    buf_t w = frame(NEWPLAYER);
    put32(&w, flags), putstr(&w, sn), putstr(&w, ln), putblob(&w, data ? hle_ptr(data) : "", data ? size : 0);
    uint32_t hr = request(&w);
    if (hr != DP_OK) { free(sn), free(ln); RET(hr, 7); }
    rd_t r = { S.reply.b, S.reply.n, 0, 0 };
    player_t *p = calloc(1, sizeof *p);
    p->id = get32(&r), p->flags = flags | DPPLAYER_LOCAL, p->local = 1, p->event = ev;
    p->short_name = sn, p->long_name = ln;
    p->data_n = data ? size : 0, p->data = malloc(p->data_n ? p->data_n : 1);
    if (data) memcpy(p->data, hle_ptr(data), size);
    p->next = S.players, S.players = p;
    MEM32(pid) = p->id;
    TRACE("CreatePlayer \"%s\" -> %08X (event %X)\n", sn, p->id, ev);
    RET(DP_OK, 7);
}

static void dp_DestroyPlayer(void) {                    /* (this, id) */
    uint32_t id = A32(1);
    player_t *p = find(id);
    if (!p || !p->local) RET(DPERR_INVALIDPLAYER, 2);
    buf_t w = frame(DELPLAYER);
    put32(&w, id);
    request(&w);
    for (player_t **pp = &S.players; *pp; pp = &(*pp)->next) if (*pp == p) { *pp = p->next; break; }
    free_player(p);
    RET(DP_OK, 2);
}

/* (this, guid instance, cb, ctx, flags); cb(id, type, DPNAME*, flags, ctx) */
static void dp_EnumPlayers(void) {
    uint32_t cb = A32(2), ctx = A32(3), flags = A32(4);
    pump();
    if (!cb) RET(DPERR_INVALIDPARAMS, 5);
    if (flags & DPENUMPLAYERS_GROUP) RET(DP_OK, 5);
    uint32_t nm = hle_va(hle_alloc(16));
    for (player_t *p = S.players; p; p = p->next) {
        if ((flags & DPENUMPLAYERS_LOCAL) && !p->local) continue;
        if ((flags & DPENUMPLAYERS_REMOTE) && p->local) continue;
        MEM32(nm) = 16, MEM32(nm + 4) = 0, MEM32(nm + 8) = gstr(p->short_name), MEM32(nm + 12) = gstr(p->long_name);
        uint32_t a[5] = { p->id, DPPLAYERTYPE_PLAYER, nm, p->local ? DPPLAYER_LOCAL : 0, ctx };
        if (!hle_call_guest(cb, 5, a)) break;
    }
    RET(DP_OK, 5);
}

/* A DPNAME and its strings into the game's buffer: (data, &size) */
static uint32_t name_into(const player_t *p, uint32_t buf, uint32_t psize) {
    uint32_t sl = (uint32_t)strlen(p->short_name) + 1, ll = (uint32_t)strlen(p->long_name) + 1, need = 16 + sl + ll;
    uint32_t have = psize ? MEM32(psize) : 0;
    if (psize) MEM32(psize) = need;
    if (!buf || have < need) return DPERR_BUFFERTOOSMALL;
    MEM32(buf) = 16, MEM32(buf + 4) = 0, MEM32(buf + 8) = buf + 16, MEM32(buf + 12) = buf + 16 + sl;
    memcpy(hle_ptr(buf + 16), p->short_name, sl), memcpy(hle_ptr(buf + 16 + sl), p->long_name, ll);
    return DP_OK;
}
static void dp_GetPlayerName(void) {                    /* (this, id, data, &size) */
    pump();
    player_t *p = find(A32(1));
    if (!p) RET(DPERR_INVALIDPLAYER, 4);
    RET(name_into(p, A32(2), A32(3)), 4);
}
static void dp_GetPlayerData(void) {                    /* (this, id, data, &size, flags) */
    pump();
    player_t *p = find(A32(1));
    uint32_t buf = A32(2), psize = A32(3);
    if (!p) RET(DPERR_INVALIDPLAYER, 5);
    uint32_t have = psize ? MEM32(psize) : 0;
    if (psize) MEM32(psize) = p->data_n;
    if (!buf || have < p->data_n) RET(p->data_n ? DPERR_BUFFERTOOSMALL : DP_OK, 5);
    memcpy(hle_ptr(buf), p->data, p->data_n);
    RET(DP_OK, 5);
}
static void dp_SetPlayerData(void) {                    /* (this, id, data, size, flags) */
    player_t *p = find(A32(1));
    if (!p) RET(DPERR_INVALIDPLAYER, 5);
    uint32_t n = A32(2) ? A32(3) : 0;
    free(p->data), p->data = malloc(n ? n : 1), p->data_n = n;
    if (n) memcpy(p->data, hle_ptr(A32(2)), n);
    buf_t w = frame(SETPDATA);
    put32(&w, p->id), putblob(&w, p->data, n);
    RET(request(&w), 5);
}
static void dp_SetPlayerName(void) {                    /* (this, id, DPNAME*, flags) */
    player_t *p = find(A32(1));
    uint32_t nm = A32(2);
    if (!p) RET(DPERR_INVALIDPLAYER, 4);
    free(p->short_name), free(p->long_name);
    p->short_name = nm ? hstr(MEM32(nm + 8)) : strdup(""), p->long_name = nm ? hstr(MEM32(nm + 12)) : strdup("");
    buf_t w = frame(SETPNAME);
    put32(&w, p->id), putstr(&w, p->short_name), putstr(&w, p->long_name);
    RET(request(&w), 4);
}

static void dp_GetSessionDesc(void) {                   /* (this, data, &size) */
    pump();
    uint32_t buf = A32(1), psize = A32(2);
    if (!S.in_session) RET(DPERR_NOCONNECTION, 3);
    uint32_t nl = (uint32_t)strlen(S.name ? S.name : "") + 1, pl = (uint32_t)strlen(S.password ? S.password : "") + 1, need = 80 + nl + pl;
    uint32_t have = psize ? MEM32(psize) : 0;
    if (psize) MEM32(psize) = need;
    if (!buf || have < need) RET(DPERR_BUFFERTOOSMALL, 3);
    memcpy(hle_ptr(buf), S.desc, 80);
    MEM32(buf) = 80;
    memcpy(hle_ptr(buf + 80), S.name ? S.name : "", nl), memcpy(hle_ptr(buf + 80 + nl), S.password ? S.password : "", pl);
    MEM32(buf + 48) = buf + 80, MEM32(buf + 52) = buf + 80 + nl;
    RET(DP_OK, 3);
}
static void dp_SetSessionDesc(void) {                   /* (this, DPSESSIONDESC2*, flags) */
    uint32_t sd = A32(1);
    if (!sd || !S.in_session) RET(DPERR_INVALIDPARAMS, 3);
    uint8_t inst[16];
    memcpy(inst, S.desc + 8, 16);
    memcpy(S.desc, hle_ptr(sd), 80);
    memcpy(S.desc + 8, inst, 16);
    memset(S.desc + 48, 0, 8);
    free(S.name), free(S.password);
    S.name = hstr(MEM32(sd + 48)), S.password = hstr(MEM32(sd + 52));
    buf_t w = frame(SETDESC);
    put(&w, S.desc, 80), putstr(&w, S.name), putstr(&w, S.password);
    RET(request(&w), 3);
}

/* (this, from, to, flags, data, size) */
static uint32_t send_to(uint32_t from, uint32_t to, uint32_t flags, uint32_t data, uint32_t size) {
    if (!S.in_session) return DPERR_NOCONNECTION;
    TRACE("send %08X -> %08X, %u bytes: %08X %08X\n", from, to, size,
          data && size >= 4 ? MEM32(data) : 0, data && size >= 8 ? MEM32(data + 4) : 0);
    buf_t w = frame(SEND);
    put32(&w, from), put32(&w, to), put32(&w, flags), putblob(&w, data ? hle_ptr(data) : "", data ? size : 0);
    net_send(w.b, w.n);
    free(w.b);
    /* the sender's other local players get a broadcast too (none, in practice) */
    for (player_t *p = S.players; p; p = p->next)
        if (p->local && p->id != from && (to == 0 || to == p->id)) {
            msg_t *m = calloc(1, sizeof *m);
            m->from = from, m->to = p->id, m->n = size, m->b = malloc(size ? size : 1);
            if (data) memcpy(m->b, hle_ptr(data), size);
            queue(m);
        }
    return DP_OK;
}
static void dp_Send(void) { RET(send_to(A32(1), A32(2), A32(3), A32(4), A32(5)), 6); }
/* (this, from, to, flags, data, size, priority, timeout, context, &msgid) */
static void dp_SendEx(void) { if (A32(9)) MEM32(A32(9)) = 0; RET(send_to(A32(1), A32(2), A32(3), A32(4), A32(5)), 10); }

static uint32_t msg_count(uint32_t id) {
    uint32_t n = 0;
    for (msg_t *m = S.head; m; m = m->next) if (!id || m->to == id || (m->sys && find(id) && find(id)->local)) n++;
    return n;
}
static void dp_GetMessageCount(void) {                  /* (this, id, &count) */
    pump();
    if (A32(2)) MEM32(A32(2)) = msg_count(A32(1));
    RET(DP_OK, 3);
}
static void dp_GetMessageCountV1(void) { dp_GetMessageCount(); }   /* the same arguments in v1 */
/* (this, id, flags, &send, &receive): messages waiting */
static void dp_GetMessageQueue(void) {
    pump();
    if (A32(3)) MEM32(A32(3)) = 0;
    if (A32(4)) MEM32(A32(4)) = msg_count(A32(1));
    RET(DP_OK, 6);
}

/* A system message's bytes, its pointers pointing into the game's buffer at va */
static uint32_t sys_size(const msg_t *m) {
    const player_t *p = m->p;
    uint32_t strs = p ? (uint32_t)(strlen(p->short_name) + strlen(p->long_name) + 2) : 0;
    switch (m->sys) {
    case DPSYS_CREATEPLAYERORGROUP:  return 48 + strs + p->data_n;
    case DPSYS_DESTROYPLAYERORGROUP: return 52 + strs + p->data_n;
    case DPSYS_SETPLAYERORGROUPDATA: return 20 + p->data_n;
    case DPSYS_SETPLAYERORGROUPNAME: return 28 + strs;
    case DPSYS_SETSESSIONDESC:       return 84 + (uint32_t)strlen(S.name ? S.name : "") + 1;
    default:                         return 4;
    }
}
static void sys_write(const msg_t *m, uint32_t va) {
    const player_t *p = m->p;
    MEM32(va) = m->sys;
    switch (m->sys) {
    case DPSYS_CREATEPLAYERORGROUP: case DPSYS_DESTROYPLAYERORGROUP: {
        int create = m->sys == DPSYS_CREATEPLAYERORGROUP;
        uint32_t base = create ? 48 : 52, sl = (uint32_t)strlen(p->short_name) + 1, ll = (uint32_t)strlen(p->long_name) + 1;
        uint32_t s1 = va + base, s2 = s1 + sl, data = s2 + ll, nm = va + (create ? 24 : 28);
        MEM32(va + 4) = DPPLAYERTYPE_PLAYER, MEM32(va + 8) = p->id;
        if (create) {
            MEM32(va + 12) = 0, MEM32(va + 16) = p->data_n ? data : 0, MEM32(va + 20) = p->data_n;
            MEM32(va + 40) = 0, MEM32(va + 44) = p->flags & ~DPPLAYER_LOCAL;
        } else {
            MEM32(va + 12) = 0, MEM32(va + 16) = 0, MEM32(va + 20) = p->data_n ? data : 0, MEM32(va + 24) = p->data_n;
            MEM32(va + 44) = 0, MEM32(va + 48) = p->flags & ~DPPLAYER_LOCAL;
        }
        MEM32(nm) = 16, MEM32(nm + 4) = 0, MEM32(nm + 8) = s1, MEM32(nm + 12) = s2;
        memcpy(hle_ptr(s1), p->short_name, sl), memcpy(hle_ptr(s2), p->long_name, ll);
        if (p->data_n) memcpy(hle_ptr(data), p->data, p->data_n);
        if (create) { uint32_t n = 0; for (player_t *q = S.players; q; q = q->next) n++; MEM32(va + 12) = n; }
        break;
    }
    case DPSYS_SETPLAYERORGROUPDATA:
        MEM32(va + 4) = DPPLAYERTYPE_PLAYER, MEM32(va + 8) = p->id, MEM32(va + 12) = p->data_n ? va + 20 : 0, MEM32(va + 16) = p->data_n;
        if (p->data_n) memcpy(hle_ptr(va + 20), p->data, p->data_n);
        break;
    case DPSYS_SETPLAYERORGROUPNAME: {
        uint32_t sl = (uint32_t)strlen(p->short_name) + 1;
        MEM32(va + 4) = DPPLAYERTYPE_PLAYER, MEM32(va + 8) = p->id;
        MEM32(va + 12) = 16, MEM32(va + 16) = 0, MEM32(va + 20) = va + 28, MEM32(va + 24) = va + 28 + sl;
        memcpy(hle_ptr(va + 28), p->short_name, sl), memcpy(hle_ptr(va + 28 + sl), p->long_name, strlen(p->long_name) + 1);
        break;
    }
    case DPSYS_SETSESSIONDESC:
        memcpy(hle_ptr(va + 4), S.desc, 80);
        MEM32(va + 4) = 80;
        MEM32(va + 4 + 48) = va + 84, MEM32(va + 4 + 52) = 0;
        memcpy(hle_ptr(va + 84), S.name ? S.name : "", strlen(S.name ? S.name : "") + 1);
        break;
    default: break;
    }
}

/* (this, &from, &to, flags, data, &size) */
static void dp_Receive(void) {
    uint32_t pfrom = A32(1), pto = A32(2), flags = A32(3), buf = A32(4), psize = A32(5);
    pump();
    uint32_t want_from = (flags & DPRECEIVE_FROMPLAYER) && pfrom ? MEM32(pfrom) : 0;
    uint32_t want_to = (flags & DPRECEIVE_TOPLAYER) && pto ? MEM32(pto) : 0;
    msg_t **mm = &S.head, *prev = NULL;
    for (; *mm; prev = *mm, mm = &(*mm)->next) {
        msg_t *m = *mm;
        if ((flags & DPRECEIVE_FROMPLAYER) && m->from != want_from) continue;
        if ((flags & DPRECEIVE_TOPLAYER) && m->to != want_to && !m->sys) continue;
        break;
    }
    msg_t *m = *mm;
    if (!m) RET(DPERR_NOMESSAGES, 6);
    uint32_t need = m->sys ? sys_size(m) : m->n, have = psize ? MEM32(psize) : 0;
    if (psize) MEM32(psize) = need;
    if (!buf || have < need) RET(DPERR_BUFFERTOOSMALL, 6);
    if (pfrom) MEM32(pfrom) = m->from;
    if (pto) MEM32(pto) = m->to;
    if (m->sys) sys_write(m, buf); else memcpy(hle_ptr(buf), m->b, m->n);
    if (!(flags & DPRECEIVE_PEEK))
        TRACE("recv %08X -> %08X, %s %u bytes: %08X\n", m->from, m->to, m->sys ? "system" : "data", need, MEM32(buf));
    if (!(flags & DPRECEIVE_PEEK)) {
        *mm = m->next;
        if (S.tail == m) S.tail = prev;
        free(m->b), free_player(m->p), free(m);
    }
    RET(DP_OK, 6);
}

static void dp_GetPlayerFlags(void) {                   /* (this, id, &flags) */
    player_t *p = find(A32(1));
    if (!p) RET(DPERR_INVALIDPLAYER, 3);
    if (A32(2)) MEM32(A32(2)) = p->local ? DPPLAYER_LOCAL : 0;
    RET(DP_OK, 3);
}

/* ---------------------------------------------------------------- the vtable */

static void unsup2(void) { RET(DPERR_UNSUPPORTED, 2); }
static void unsup3(void) { RET(DPERR_UNSUPPORTED, 3); }
static void unsup4(void) { RET(DPERR_UNSUPPORTED, 4); }
static void unsup5(void) { RET(DPERR_UNSUPPORTED, 5); }
static void unsup6(void) { RET(DPERR_UNSUPPORTED, 6); }
static void unsup7(void) { RET(DPERR_UNSUPPORTED, 7); }
static void none5(void)  { RET(DP_OK, 5); }             /* enumerations of groups: none */
static void none6(void)  { RET(DP_OK, 6); }
static void cancel3(void) { RET(DP_OK, 3); }
static void cancel4(void) { RET(DP_OK, 4); }
static void dp_EnumConnectionsNet(void) { RET(DP_OK, 5); }

/* QueryInterface: IID_IDirectPlay (v1), whose slots differ from v2's on, gets a v1
 * object (the host closes its game to new players through it: EnableNewPlayers);
 * every other DirectPlay IID gets the IDirectPlay4A object. Objects are [vtable]
 * [count][the other one]. */
static const uint8_t IID_IDirectPlay[16] = { 0xa0, 0xe9, 0x54, 0x54, 0x65, 0xdb, 0xce, 0x11,
                                             0x92, 0x1c, 0x00, 0xaa, 0x00, 0x6c, 0x49, 0x72 };
static uint32_t g_vt4, g_vt1;
extern const win32hle_shim dplay_net_vtable[], dplay_net_vtable_v1[];
static uint32_t new_pair(void) {
    if (!g_vt4) g_vt4 = hle_com_vtable(dplay_net_vtable), g_vt1 = hle_com_vtable(dplay_net_vtable_v1);
    uint32_t v4 = hle_com_new(g_vt4, 12), v1 = hle_com_new(g_vt1, 12);
    MEM32(v4 + 8) = v1, MEM32(v1 + 8) = v4;
    return v4;
}
uint32_t dplay_net_new(void) { return new_pair(); }
static void dp_QueryInterface(void) {                   /* (this, iid, &out) */
    uint32_t self = A32(0), out = A32(2);
    int want1 = A32(1) && !memcmp(hle_ptr(A32(1)), IID_IDirectPlay, 16);
    uint32_t vt1 = MEM32(self) == g_vt1;
    uint32_t obj = want1 == (int)vt1 ? self : MEM32(self + 8);
    MEM32(obj + 4)++;
    if (out) MEM32(out) = obj;
    RET(DP_OK, 3);
}
static void dp_AddRef(void) { RET(++MEM32(A32(0) + 4), 1); }
static void dp_Release(void) { uint32_t n = MEM32(A32(0) + 4); RET(n ? --MEM32(A32(0) + 4) : 0, 1); }   /* kept: the pair is tiny */

static void dp1_EnableNewPlayers(void) {                /* (this, enable) */
    TRACE("EnableNewPlayers %u\n", A32(1));
    if (!S.in_session || !S.host) RET(DP_OK, 2);
    uint32_t f = (uint32_t)S.desc[4] | (uint32_t)S.desc[5] << 8 | (uint32_t)S.desc[6] << 16 | (uint32_t)S.desc[7] << 24;
    f = A32(1) ? f & ~0x21u : f | 0x21u;                /* DPSESSION_NEWPLAYERSDISABLED | DPSESSION_JOINDISABLED */
    S.desc[4] = (uint8_t)f, S.desc[5] = (uint8_t)(f >> 8), S.desc[6] = (uint8_t)(f >> 16), S.desc[7] = (uint8_t)(f >> 24);
    buf_t w = frame(SETDESC);
    put(&w, S.desc, 80), putstr(&w, S.name), putstr(&w, S.password);
    RET(request(&w), 2);
}

/* IDirectPlay (v1): only EnableNewPlayers is asked for; the rest are its own layout's arities */
const win32hle_shim dplay_net_vtable_v1[] = {
    { "dp1!QueryInterface", dp_QueryInterface }, { "dp1!AddRef", dp_AddRef }, { "dp1!Release", dp_Release },
    { "dp1!AddPlayerToGroup", unsup3 }, { "dp1!Close", dp_Close }, { "dp1!CreatePlayer", unsup5 },
    { "dp1!CreateGroup", unsup4 }, { "dp1!DeletePlayerFromGroup", unsup3 }, { "dp1!DestroyPlayer", dp_DestroyPlayer },
    { "dp1!DestroyGroup", unsup2 }, { "dp1!EnableNewPlayers", dp1_EnableNewPlayers }, { "dp1!EnumGroupPlayers", none5 },
    { "dp1!EnumGroups", none5 }, { "dp1!EnumPlayers", unsup5 }, { "dp1!EnumSessions", unsup6 }, { "dp1!GetCaps", unsup2 },
    { "dp1!GetMessageCount", dp_GetMessageCountV1 }, { "dp1!GetPlayerCaps", unsup3 }, { "dp1!GetPlayerName", unsup6 },
    { "dp1!Initialize", unsup2 }, { "dp1!Open", unsup2 }, { "dp1!Receive", unsup6 }, { "dp1!SaveSession", unsup2 },
    { "dp1!Send", unsup6 }, { "dp1!SetPlayerName", unsup4 },
    { NULL, NULL },
};

/* IDirectPlay4A layout (IDirectPlay2/3 share its first slots) */
const win32hle_shim dplay_net_vtable[] = {
    { "dpn!QueryInterface", dp_QueryInterface }, { "dpn!AddRef", dp_AddRef }, { "dpn!Release", dp_Release },
    { "dpn!AddPlayerToGroup", unsup3 }, { "dpn!Close", dp_Close }, { "dpn!CreateGroup", unsup6 },
    { "dpn!CreatePlayer", dp_CreatePlayer }, { "dpn!DeletePlayerFromGroup", unsup3 }, { "dpn!DestroyGroup", unsup2 },
    { "dpn!DestroyPlayer", dp_DestroyPlayer }, { "dpn!EnumGroupPlayers", none6 }, { "dpn!EnumGroups", none5 },
    { "dpn!EnumPlayers", dp_EnumPlayers }, { "dpn!EnumSessions", dp_EnumSessions }, { "dpn!GetCaps", dp_GetCaps },
    { "dpn!GetGroupData", unsup5 }, { "dpn!GetGroupName", unsup4 }, { "dpn!GetMessageCount", dp_GetMessageCount },
    { "dpn!GetPlayerAddress", unsup4 }, { "dpn!GetPlayerCaps", dp_GetPlayerCaps }, { "dpn!GetPlayerData", dp_GetPlayerData },
    { "dpn!GetPlayerName", dp_GetPlayerName }, { "dpn!GetSessionDesc", dp_GetSessionDesc }, { "dpn!Initialize", dp_Initialize },
    { "dpn!Open", dp_Open }, { "dpn!Receive", dp_Receive }, { "dpn!Send", dp_Send }, { "dpn!SetGroupData", unsup5 },
    { "dpn!SetGroupName", unsup4 }, { "dpn!SetPlayerData", dp_SetPlayerData }, { "dpn!SetPlayerName", dp_SetPlayerName },
    { "dpn!SetSessionDesc", dp_SetSessionDesc }, { "dpn!AddGroupToGroup", unsup3 }, { "dpn!CreateGroupInGroup", unsup7 },
    { "dpn!DeleteGroupFromGroup", unsup3 }, { "dpn!EnumConnections", dp_EnumConnectionsNet },
    { "dpn!EnumGroupsInGroup", none6 }, { "dpn!GetGroupConnectionSettings", unsup5 },
    { "dpn!InitializeConnection", dp_InitializeConnection }, { "dpn!SecureOpen", unsup5 }, { "dpn!SendChatMessage", unsup5 },
    { "dpn!SetGroupConnectionSettings", unsup4 }, { "dpn!StartSession", unsup3 }, { "dpn!GetGroupFlags", unsup3 },
    { "dpn!GetGroupParent", unsup3 }, { "dpn!GetPlayerAccount", unsup5 }, { "dpn!GetPlayerFlags", dp_GetPlayerFlags },
    { "dpn!GetGroupOwner", unsup3 }, { "dpn!SetGroupOwner", unsup3 }, { "dpn!SendEx", dp_SendEx },
    { "dpn!GetMessageQueue", dp_GetMessageQueue }, { "dpn!CancelMessage", cancel3 }, { "dpn!CancelPriority", cancel4 },
    { NULL, NULL },
};

/* ---------------------------------------------------------------- lobby launch
 *
 * Started with LOBBY on its command line, the game asks DirectPlayLobby for its
 * connection settings and connects through it, straight into the game's setup
 * screen (as MSN Zone launched it). The web page (or a shell) says what to do:
 *   AOE2_LOBBY=host            create a room, named AOE2_ROOM
 *   AOE2_LOBBY=join:<32 hex>   join the room with that guidInstance (relay /rooms)
 *   AOE2_PLAYER=<name>         the player's name
 */
static const uint8_t SP_ONLINE_GUID[16] = { 0x61, 0x6F, 0x65, 0x32, 0x2D, 0x6F, 0x6E, 0x2D, 0x62, 0x72, 0x6F, 0x77, 0x73, 0x65, 0x72, 0x21 };
static const uint8_t AOC_APP_GUID[16] = { 0x3f, 0x3f, 0xe9, 0x5d, 0x90, 0xfc, 0xe1, 0x4e, 0xae, 0x5a, 0x63, 0xda, 0xfa, 0x05, 0x59, 0x50 };

static int lobby_mode(uint8_t inst[16]) {               /* 2 host, 1 join, 0 not lobbied */
    const char *l = getenv("AOE2_LOBBY");
    if (!l || !*l) return 0;
    if (!strcmp(l, "host")) return 2;
    if (strncmp(l, "join:", 5) || strlen(l + 5) != 32) return 0;
    for (int i = 0; i < 16; i++) { unsigned v; if (sscanf(l + 5 + 2 * i, "%2x", &v) != 1) return 0; inst[i] = (uint8_t)v; }
    return 1;
}
static const char *lobby_player(void) { const char *p = getenv("AOE2_PLAYER"); return p && *p ? p : "Player"; }
static const char *lobby_room(void) { const char *r = getenv("AOE2_ROOM"); return r && *r ? r : "Online game"; }
static const char *lobby_password(void) { const char *p = getenv("AOE2_PASSWORD"); return p ? p : ""; }
static uint8_t g_lobby_set[80];                         /* the game's SetConnectionSettings desc */
static int g_lobby_set_valid;
static void lobby_desc(uint8_t d[80], const uint8_t inst[16]) {
    memset(d, 0, 80);
    if (g_lobby_set_valid) memcpy(d, g_lobby_set, 80), memset(d + 48, 0, 8);   /* its flags, limits, user data */
    else d[4] = 0x40, d[40] = 8;                        /* DPSESSION_KEEPALIVE, 8 players */
    d[0] = 80, d[1] = d[2] = d[3] = 0;
    memset(d + 8, 0, 16);
    if (inst) memcpy(d + 8, inst, 16);
    memcpy(d + 24, AOC_APP_GUID, 16);
}

/* IDirectPlayLobby::SetConnectionSettings(this, flags, appid, DPLCONNECTION*): the game
 * adjusts the session it will connect with (its flags, its player limit) */
uint32_t dplay_net_lobby_set(uint32_t conn) {
    if (!conn || !MEM32(conn + 8)) return DPERR_INVALIDPARAMS;
    memcpy(g_lobby_set, hle_ptr(MEM32(conn + 8)), 80);
    g_lobby_set_valid = 1;
    TRACE("lobby: settings from the game: flags %X, max %u\n", MEM32(MEM32(conn + 8) + 4), MEM32(MEM32(conn + 8) + 40));
    return DP_OK;
}

/* IDirectPlayLobby::GetConnectionSettings(this, appid, data, &size) */
uint32_t dplay_net_lobby_settings(uint32_t buf, uint32_t psize) {
    uint8_t inst[16];
    int mode = lobby_mode(inst);
    if (!mode) return 0x8877042Eu;                      /* DPERR_NOTLOBBIED */
    const char *room = lobby_room(), *player = lobby_player();
    uint32_t rl = (uint32_t)strlen(room) + 1, pl = (uint32_t)strlen(player) + 1, need = 40 + 80 + 16 + rl + pl;
    uint32_t have = psize ? MEM32(psize) : 0;
    if (psize) MEM32(psize) = need;
    if (!buf || have < need) return DPERR_BUFFERTOOSMALL;
    uint32_t sd = buf + 40, nm = sd + 80, rs = nm + 16, ps = rs + rl;
    uint8_t d[80];
    lobby_desc(d, mode == 1 ? inst : NULL);
    memcpy(hle_ptr(sd), d, 80);
    MEM32(sd + 48) = rs;
    /* dwUser1 = 1: the game's lobby restore (0x5D1DF0) closes its window unless the
     * lobby's session has it (0x4377A8 keeps dwUser1 != 1 at lobby+0x198) */
    if (getenv("AOE2_RESTORE")) MEM32(sd + 64) = 1;
    MEM32(buf) = 40, MEM32(buf + 4) = mode == 2 ? DPOPEN_CREATE : DPOPEN_JOIN, MEM32(buf + 8) = sd, MEM32(buf + 12) = nm;
    memcpy(hle_ptr(buf + 16), SP_ONLINE_GUID, 16);
    MEM32(buf + 32) = 0, MEM32(buf + 36) = 0;
    MEM32(nm) = 16, MEM32(nm + 4) = 0, MEM32(nm + 8) = ps, MEM32(nm + 12) = ps;
    memcpy(hle_ptr(rs), room, rl), memcpy(hle_ptr(ps), player, pl);
    TRACE("lobby: %s \"%s\" as \"%s\"\n", mode == 2 ? "host" : "join", room, player);
    return DP_OK;
}

/* IDirectPlayLobby::Connect / ConnectEx: the session, opened, as a DirectPlay object */
uint32_t dplay_net_lobby_connect(uint32_t *out) {
    uint8_t inst[16], d[80];
    int mode = lobby_mode(inst);
    if (!mode) return 0x8877042Eu;
    lobby_desc(d, mode == 1 ? inst : NULL);
    uint32_t hr = open_session(d, lobby_room(), lobby_password(), mode == 2 ? DPOPEN_CREATE : DPOPEN_JOIN);
    if (hr == DP_OK) *out = new_pair();
    return hr;
}

/* After hle_screen_open (it sets the poll hook this one chains) */
void dplay_net_install(void) {
    g_prev_poll = hle_screen_poll_hook;
    hle_screen_poll_hook = poll_hook;
}

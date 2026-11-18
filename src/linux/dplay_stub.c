/*
 * dplay_stub.c - DirectPlay's entry points, its lobby object and dplayx.dll's version.
 *
 * Even for single player, AoC's start-up checks for DirectX 6.1a+ like this:
 *   DirectPlayCreate -> QueryInterface(IID_IDirectPlay4) -> Release,
 *   then the file version of dplayx.dll (>= 4.7.0.178, DX7; or 4.6.3.516, DX6.1a).
 * This file answers both. For multiplayer, DirectPlayEnumerateA lists one service
 * provider, "Online", and the IDirectPlay4A objects are src/net/dplay_net.c's: sessions
 * and messages over the relay (ADR-0006). The lobby object serves lobby launch.
 *
 * Method arities come from mingw-w64's dplay.h / dplobby.h (tools/recon/dxheaders.py):
 * each stub pops exactly its stdcall arguments.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "win32hle.h"

#define DP_OK              0x00000000u
#define DPERR_UNSUPPORTED  0x80004001u        /* E_NOTIMPL */
#define DPERR_NOTLOBBIED   0x8877042Eu        /* MAKE_DPHRESULT(1070) */

static int dp_trace = -1;
#define TRACE(...) do { if (dp_trace < 0) dp_trace = getenv("AOE2_TRACE_DPLAY") != NULL; \
                        if (dp_trace) fprintf(stderr, "[dplay] " __VA_ARGS__); } while (0)

/* One unsupported stub per arity (argument count including `this`). */
#define UNSUP(n) static void unsup##n(void) { TRACE("unsupported method (%d args)\n", n); RET(DPERR_UNSUPPORTED, n); }
UNSUP(2) UNSUP(3) UNSUP(4) UNSUP(5) UNSUP(6) UNSUP(7)

/* The service provider the game lists (DirectPlayEnumerateA): ours, "Online". */
static const uint8_t SP_ONLINE[16] = { 0x61, 0x6F, 0x65, 0x32, 0x2D, 0x6F, 0x6E, 0x2D,      /* "aoe2-on-browser!" */
                                       0x62, 0x72, 0x6F, 0x77, 0x73, 0x65, 0x72, 0x21 };

/* IDirectPlay4A: src/net/dplay_net.c (sessions and messages over the relay). The
 * start-up check's IDirectPlay (v1) only QueryInterfaces and Releases, slots it shares. */

/* Lobby launch (the game's LOBBY switch): the settings and the connection come from
 * dplay_net.c, which reads the page's AOE2_LOBBY / AOE2_ROOM / AOE2_PLAYER. */
uint32_t dplay_net_lobby_settings(uint32_t buf, uint32_t psize);
uint32_t dplay_net_lobby_connect(uint32_t *out);
static void lb_GetConnectionSettings(void) { RET(dplay_net_lobby_settings(A32(2), A32(3)), 4); }   /* (this, appid, data, &size) */
static void lb_Connect(void) {                          /* (this, flags, &dp, outer) */
    uint32_t dp = 0, hr = dplay_net_lobby_connect(&dp);
    TRACE("lobby Connect -> %08X\n", hr);
    if (A32(2)) MEM32(A32(2)) = hr == DP_OK ? dp : 0;
    RET(hr, 4);
}
static void lb_ConnectEx(void) {                        /* (this, flags, iid, &dp, outer) */
    uint32_t dp = 0, hr = dplay_net_lobby_connect(&dp);
    TRACE("lobby ConnectEx -> %08X\n", hr);
    if (A32(3)) MEM32(A32(3)) = hr == DP_OK ? dp : 0;
    RET(hr, 5);
}
uint32_t dplay_net_lobby_set(uint32_t conn);
static void lb_SetConnectionSettings(void) { RET(dplay_net_lobby_set(A32(3)), 4); }   /* (this, flags, appid, conn) */
/* Lobby messages. After a lobby launch the game asks the lobby for its "preset data"
 * (DPLSYS_GETPROPERTY, sent from 0x437EF0) and waits up to 30 s on the "Setting up
 * multiplayer game" screen until both its player ID is set and that property's
 * answer has come (satisfyLobbyLaunchCriteria, 0x437DE0). We are the lobby and have
 * no presets: we answer "not supported", which the game takes as retrieved
 * (0x437D5A, "PresetData not supported") and launches at once. Other messages
 * (status for the lobby) have nobody to read them. */
#define DPLSYS_GETPROPERTY          7u
#define DPLSYS_GETPROPERTYRESPONSE  8u
#define DPERR_NOMESSAGES            0x887700BEu
#define DPERR_BUFFERTOOSMALL        0x8877001Eu
static uint8_t g_lobby_reply[0x34];                   /* DPLMSG_GETPROPERTYRESPONSE */
static int g_lobby_reply_pending;

static void lb_SendLobbyMessage(void) {                 /* (this, flags, appid, data, size) */
    uint32_t data = A32(3), size = A32(4);
    if (data && size >= 0x28 && MEM32(data) == DPLSYS_GETPROPERTY) {
        /* request: dwType, dwRequestID, guidPlayer, guidPropertyTag */
        memset(g_lobby_reply, 0, sizeof g_lobby_reply);
        uint32_t *r = (uint32_t *)g_lobby_reply;
        r[0] = DPLSYS_GETPROPERTYRESPONSE;
        r[1] = MEM32(data + 4);
        for (int i = 0; i < 8; i++) r[2 + i] = MEM32(data + 8 + 4 * i);   /* both GUIDs */
        r[10] = DPERR_UNSUPPORTED;                      /* hr: no such property here */
        r[11] = 0;                                      /* dwDataSize */
        g_lobby_reply_pending = 1;
        TRACE("lobby GetProperty request %u -> not supported\n", r[1]);
    }
    RET(DP_OK, 5);
}
static void lb_ReceiveLobbyMessage(void) {              /* (this, flags, appid, &msgflags, data, &size) */
    if (!g_lobby_reply_pending) RET(DPERR_NOMESSAGES, 6);
    uint32_t psize = A32(5), need = sizeof g_lobby_reply;
    if (!psize) RET(DPERR_BUFFERTOOSMALL, 6);
    if (!A32(4) || MEM32(psize) < need) { MEM32(psize) = need; RET(DPERR_BUFFERTOOSMALL, 6); }
    memcpy(hle_ptr(A32(4)), g_lobby_reply, need);
    MEM32(psize) = need;
    if (A32(3)) MEM32(A32(3)) = 0;                      /* a standard (not DPLMSG_SYSTEM) message */
    g_lobby_reply_pending = 0;
    RET(DP_OK, 6);
}
static void lb_SetLobbyMessageEvent(void) { RET(DP_OK, 4); }
static void lb_EnumLocalApplications(void) { RET(DP_OK, 4); }

/* IDirectPlayLobby3(A); IDirectPlayLobby(A) and 2(A) share its first 14 slots. */
static const win32hle_shim idirectplaylobby3[] = {
    { "dpl3!QueryInterface", hle_com_QueryInterface }, { "dpl3!AddRef", hle_com_AddRef },
    { "dpl3!Release", hle_com_Release },
    { "dpl3!Connect", lb_Connect }, { "dpl3!CreateAddress", unsup7 }, { "dpl3!EnumAddress", unsup5 },
    { "dpl3!EnumAddressTypes", unsup5 }, { "dpl3!EnumLocalApplications", lb_EnumLocalApplications },
    { "dpl3!GetConnectionSettings", lb_GetConnectionSettings }, { "dpl3!ReceiveLobbyMessage", lb_ReceiveLobbyMessage },
    { "dpl3!RunApplication", unsup5 }, { "dpl3!SendLobbyMessage", lb_SendLobbyMessage }, { "dpl3!SetConnectionSettings", lb_SetConnectionSettings },
    { "dpl3!SetLobbyMessageEvent", lb_SetLobbyMessageEvent }, { "dpl3!CreateCompoundAddress", unsup5 }, { "dpl3!ConnectEx", lb_ConnectEx },
    { "dpl3!RegisterApplication", unsup3 }, { "dpl3!UnregisterApplication", unsup3 },
    { "dpl3!WaitForConnectionSettings", unsup2 },
    { NULL, NULL },
};

static uint32_t g_vt_lobby;

uint32_t dplay_net_new(void);       /* dplay_net.c: an IDirectPlay4A (and its v1 partner) */
static uint32_t new_dp(void)    { return dplay_net_new(); }
static uint32_t new_lobby(void) { if (!g_vt_lobby) g_vt_lobby = hle_com_vtable(idirectplaylobby3); return hle_com_new(g_vt_lobby, 8); }

static void s_DirectPlayCreate(void) {                  /* (guid sp, &out, outer) */
    TRACE("DirectPlayCreate\n");
    if (A32(1)) MEM32(A32(1)) = new_dp();
    RET(DP_OK, 3);
}
static void s_DirectPlayEnumerateA(void) {              /* (cb, ctx): cb(&guid sp, name, major, minor, ctx) */
    uint32_t cb = A32(0), ctx = A32(1);
    TRACE("DirectPlayEnumerateA cb %08X\n", cb);
    if (!cb) RET(DP_OK, 2);
    uint8_t *mem = hle_alloc(16 + 32);
    memcpy(mem, SP_ONLINE, 16);
    strcpy((char *)mem + 16, "Online (aoe2-on-browser)");
    uint32_t args[5] = { hle_va(mem), hle_va(mem) + 16, 6, 0, ctx };
    hle_call_guest(cb, 5, args);
    RET(DP_OK, 2);
}
static void s_DirectPlayLobbyCreateA(void) {            /* (guid, &out, outer, data, size) */
    TRACE("DirectPlayLobbyCreateA\n");
    if (A32(1)) MEM32(A32(1)) = new_lobby();
    RET(DP_OK, 5);
}

/* CoCreateInstance(CLSID_DirectPlay / CLSID_DirectPlayLobby, ..., iid, &out) */
static const uint8_t CLSID_DirectPlay[16] = { 0x20, 0x6D, 0xEB, 0xD1, 0x23, 0x89, 0xD0, 0x11,
                                              0x9D, 0x97, 0x00, 0xA0, 0xC9, 0x0A, 0x43, 0xCB };
static const uint8_t CLSID_DirectPlayLobby[16] = { 0x10, 0xF8, 0xE8, 0x2F, 0xA5, 0xB2, 0xD0, 0x11,
                                                   0xA7, 0x87, 0x00, 0x00, 0xF8, 0x03, 0xAB, 0xFC };
static uint32_t create_dp(const uint8_t *iid, uint32_t *out)    { (void)iid; *out = new_dp(); return 0; }
static uint32_t create_lobby(const uint8_t *iid, uint32_t *out) { (void)iid; *out = new_lobby(); return 0; }

/* ---- dplayx.dll's file version ----
 * win32hle answers GetFileVersionInfo* with "no version info" for every file. For the
 * system DLLs whose version a game checks, report the version of the DirectX 7a file. */
#define VERSION_MAGIC 0x56454C48u   /* "HLEV" */
static const struct { const char *dll; uint32_t ms, ls; } g_sysver[] = {
    { "dplayx.dll", (4u << 16) | 7u, (0u << 16) | 700u },   /* 4.07.00.0700 (DirectX 7a) */
};

static int sysver_index(const char *path) {
    if (!path) return -1;
    const char *b = strrchr(path, '\\');
    b = b ? b + 1 : path;
    for (size_t i = 0; i < sizeof g_sysver / sizeof *g_sysver; i++)
        if (!strcasecmp(b, g_sysver[i].dll)) return (int)i;
    return -1;
}

static recomp_func_t advapi(const char *name) {
    for (const win32hle_shim *s = win32hle_advapi32; s->name; s++)
        if (!strcmp(s->name, name)) return s->fn;
    hle_fatal("dplay_stub: win32hle has no %s", name);
    return NULL;
}

#define BLOCK_SIZE 0x60u
static void v_GetFileVersionInfoSizeA(void) {           /* (path, &handle) */
    if (sysver_index(ASTR(0)) < 0) { advapi("GetFileVersionInfoSizeA")(); return; }
    if (A32(1)) MEM32(A32(1)) = 0;
    RET(BLOCK_SIZE, 2);
}
static void v_GetFileVersionInfoA(void) {               /* (path, handle, len, data) */
    int i = sysver_index(ASTR(0));
    if (i < 0) { advapi("GetFileVersionInfoA")(); return; }
    if (A32(2) < BLOCK_SIZE || !A32(3)) RET(0, 4);
    uint32_t *b = APTR(3);
    memset(b, 0, BLOCK_SIZE);
    b[0] = VERSION_MAGIC;
    uint32_t *ffi = b + 4;                              /* VS_FIXEDFILEINFO at +16 */
    ffi[0] = 0xFEEF04BDu;                               /* dwSignature */
    ffi[1] = 0x00010000u;                               /* dwStrucVersion */
    ffi[2] = ffi[4] = g_sysver[i].ms;                   /* file and product version */
    ffi[3] = ffi[5] = g_sysver[i].ls;
    ffi[10] = 2;                                        /* dwFileType = VFT_DLL */
    RET(1, 4);
}
static void v_VerQueryValueA(void) {                    /* (block, subblock, &ptr, &len) */
    uint32_t block = A32(0);
    if (!block || MEM32(block) != VERSION_MAGIC) { advapi("VerQueryValueA")(); return; }
    const char *sub = ASTR(1);
    if (!sub || strcmp(sub, "\\")) { if (A32(3)) MEM32(A32(3)) = 0; RET(0, 4); }
    if (A32(2)) MEM32(A32(2)) = block + 16;
    if (A32(3)) MEM32(A32(3)) = 52;
    RET(1, 4);
}

static const win32hle_shim dplay_shims[] = {
    { "dplayx.dll#1", s_DirectPlayCreate },       { "DirectPlayCreate", s_DirectPlayCreate },
    { "dplayx.dll#2", s_DirectPlayEnumerateA },   { "DirectPlayEnumerateA", s_DirectPlayEnumerateA },
    { "dplayx.dll#4", s_DirectPlayLobbyCreateA }, { "DirectPlayLobbyCreateA", s_DirectPlayLobbyCreateA },
    { "GetFileVersionInfoSizeA", v_GetFileVersionInfoSizeA },
    { "GetFileVersionInfoA", v_GetFileVersionInfoA },
    { "VerQueryValueA", v_VerQueryValueA },
    { NULL, NULL },
};

/* before win32hle (its VERSION shims are wrapped); COM classes after ole32 exists */
void dplay_stub_register(void) { win32hle_register(dplay_shims); }
void dplay_stub_register_com(void) {
    hle_com_register_class(CLSID_DirectPlay, create_dp);
    hle_com_register_class(CLSID_DirectPlayLobby, create_lobby);
}

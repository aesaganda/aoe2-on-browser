/*
 * dplay_stub.c - a DirectPlay that is present but has no service providers.
 *
 * Even for single player, AoC's start-up checks for DirectX 6.1a+ like this:
 *   DirectPlayCreate -> QueryInterface(IID_IDirectPlay4) -> Release,
 *   then the file version of dplayx.dll (>= 4.7.0.178, DX7; or 4.6.3.516, DX6.1a).
 * This file answers both. The objects enumerate no connections and no sessions,
 * so the multiplayer screens are empty; every other method is unsupported.
 * The real transport (WebRTC/WebSocket) is Phase 3.
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
#define DPERR_NOTLOBBIED   0x887703E8u        /* MAKE_DPHRESULT(1000) */

static int dp_trace = -1;
#define TRACE(...) do { if (dp_trace < 0) dp_trace = getenv("AOE2_TRACE_DPLAY") != NULL; \
                        if (dp_trace) fprintf(stderr, "[dplay] " __VA_ARGS__); } while (0)

/* One unsupported stub per arity (argument count including `this`). */
#define UNSUP(n) static void unsup##n(void) { TRACE("unsupported method (%d args)\n", n); RET(DPERR_UNSUPPORTED, n); }
UNSUP(1) UNSUP(2) UNSUP(3) UNSUP(4) UNSUP(5) UNSUP(6) UNSUP(7) UNSUP(10)

static void dp_Close(void)            { RET(DP_OK, 1); }
static void dp_EnumSessions(void)     { RET(DP_OK, 6); }    /* (this, desc, timeout, cb, ctx, flags): none */
static void dp_EnumConnections(void)  { RET(DP_OK, 5); }    /* (this, app guid, cb, ctx, flags): none */
static void dp_GetMessageCount(void)  { if (A32(2)) MEM32(A32(2)) = 0; RET(DP_OK, 3); }

/* IDirectPlay4(A) layout; IDirectPlay (v1), which DirectPlayCreate hands out, shares
 * slots 0-2, and the game only QueryInterfaces and Releases it. */
static const win32hle_shim idirectplay4[] = {
    { "dp4!QueryInterface", hle_com_QueryInterface }, { "dp4!AddRef", hle_com_AddRef },
    { "dp4!Release", hle_com_Release },
    { "dp4!AddPlayerToGroup", unsup3 }, { "dp4!Close", dp_Close }, { "dp4!CreateGroup", unsup6 },
    { "dp4!CreatePlayer", unsup7 }, { "dp4!DeletePlayerFromGroup", unsup3 }, { "dp4!DestroyGroup", unsup2 },
    { "dp4!DestroyPlayer", unsup2 }, { "dp4!EnumGroupPlayers", unsup6 }, { "dp4!EnumGroups", unsup5 },
    { "dp4!EnumPlayers", unsup5 }, { "dp4!EnumSessions", dp_EnumSessions }, { "dp4!GetCaps", unsup3 },
    { "dp4!GetGroupData", unsup5 }, { "dp4!GetGroupName", unsup4 }, { "dp4!GetMessageCount", dp_GetMessageCount },
    { "dp4!GetPlayerAddress", unsup4 }, { "dp4!GetPlayerCaps", unsup4 }, { "dp4!GetPlayerData", unsup5 },
    { "dp4!GetPlayerName", unsup4 }, { "dp4!GetSessionDesc", unsup3 }, { "dp4!Initialize", unsup2 },
    { "dp4!Open", unsup3 }, { "dp4!Receive", unsup6 }, { "dp4!Send", unsup6 }, { "dp4!SetGroupData", unsup5 },
    { "dp4!SetGroupName", unsup4 }, { "dp4!SetPlayerData", unsup5 }, { "dp4!SetPlayerName", unsup4 },
    { "dp4!SetSessionDesc", unsup3 }, { "dp4!AddGroupToGroup", unsup3 }, { "dp4!CreateGroupInGroup", unsup7 },
    { "dp4!DeleteGroupFromGroup", unsup3 }, { "dp4!EnumConnections", dp_EnumConnections },
    { "dp4!EnumGroupsInGroup", unsup6 }, { "dp4!GetGroupConnectionSettings", unsup5 },
    { "dp4!InitializeConnection", unsup3 }, { "dp4!SecureOpen", unsup5 }, { "dp4!SendChatMessage", unsup5 },
    { "dp4!SetGroupConnectionSettings", unsup4 }, { "dp4!StartSession", unsup3 }, { "dp4!GetGroupFlags", unsup3 },
    { "dp4!GetGroupParent", unsup3 }, { "dp4!GetPlayerAccount", unsup5 }, { "dp4!GetPlayerFlags", unsup3 },
    { "dp4!GetGroupOwner", unsup3 }, { "dp4!SetGroupOwner", unsup3 }, { "dp4!SendEx", unsup10 },
    { "dp4!GetMessageQueue", unsup6 }, { "dp4!CancelMessage", unsup3 }, { "dp4!CancelPriority", unsup4 },
    { NULL, NULL },
};

static void lb_GetConnectionSettings(void) { RET(DPERR_NOTLOBBIED, 4); }   /* not launched from a lobby */
static void lb_EnumLocalApplications(void) { RET(DP_OK, 4); }

/* IDirectPlayLobby3(A); IDirectPlayLobby(A) and 2(A) share its first 14 slots. */
static const win32hle_shim idirectplaylobby3[] = {
    { "dpl3!QueryInterface", hle_com_QueryInterface }, { "dpl3!AddRef", hle_com_AddRef },
    { "dpl3!Release", hle_com_Release },
    { "dpl3!Connect", unsup4 }, { "dpl3!CreateAddress", unsup7 }, { "dpl3!EnumAddress", unsup5 },
    { "dpl3!EnumAddressTypes", unsup5 }, { "dpl3!EnumLocalApplications", lb_EnumLocalApplications },
    { "dpl3!GetConnectionSettings", lb_GetConnectionSettings }, { "dpl3!ReceiveLobbyMessage", unsup6 },
    { "dpl3!RunApplication", unsup5 }, { "dpl3!SendLobbyMessage", unsup5 }, { "dpl3!SetConnectionSettings", unsup4 },
    { "dpl3!SetLobbyMessageEvent", unsup4 }, { "dpl3!CreateCompoundAddress", unsup5 }, { "dpl3!ConnectEx", unsup5 },
    { "dpl3!RegisterApplication", unsup3 }, { "dpl3!UnregisterApplication", unsup3 },
    { "dpl3!WaitForConnectionSettings", unsup2 },
    { NULL, NULL },
};

static uint32_t g_vt_dp, g_vt_lobby;

static uint32_t new_dp(void)    { if (!g_vt_dp) g_vt_dp = hle_com_vtable(idirectplay4); return hle_com_new(g_vt_dp, 8); }
static uint32_t new_lobby(void) { if (!g_vt_lobby) g_vt_lobby = hle_com_vtable(idirectplaylobby3); return hle_com_new(g_vt_lobby, 8); }

static void s_DirectPlayCreate(void) {                  /* (guid sp, &out, outer) */
    TRACE("DirectPlayCreate\n");
    if (A32(1)) MEM32(A32(1)) = new_dp();
    RET(DP_OK, 3);
}
static void s_DirectPlayEnumerateA(void) { RET(DP_OK, 2); }   /* (cb, ctx): no service providers */
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

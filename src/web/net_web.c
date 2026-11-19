/*
 * net_web.c - the relay over the page's WebSocket (browser build).
 *
 * The socket lives on the page's main thread (Module.relayUrl, or wss://<this
 * host>/relay): frames the game sends are copied and posted there; frames that
 * arrive are copied into wasm memory and handed to net_inbound (net_queue.c), which
 * the game thread drains. Frames sent before the socket opens wait in the page.
 */
#ifdef __EMSCRIPTEN__
#include <stdlib.h>
#include <string.h>

#include <emscripten.h>
#include <emscripten/threading.h>

#include "../net/net.h"

EMSCRIPTEN_KEEPALIVE void net_web_inbound(const uint8_t *b, int n) { net_inbound(b, (size_t)n); }
EMSCRIPTEN_KEEPALIVE void net_web_closed(void) { net_closed(); }

int net_open(void) {
    /* (no bare commas below: they would split the macro's argument) */
    MAIN_THREAD_EM_ASM({
        if (Module.relay) return;
        const url = Module.relayUrl || ((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/relay');
        Module.closeRelay = () => {
            const r = Module.relay;
            if (!r) return;
            r.closing = true;
            r.lastLobbyHb = null;
            if (r.retryTimer) { clearTimeout(r.retryTimer); r.retryTimer = 0; }
            if (r.hbTimer) { clearInterval(r.hbTimer); r.hbTimer = 0; }
            Module.relay = null;
            try { r.ws.close(1000, 'exit'); } catch {}
        };
        function startHeartbeat(r) {
            if (r.hbTimer) clearInterval(r.hbTimer);
            r.hbTimer = setInterval(() => {
                if (!r || r.closing || !r.lastLobbyHb || r.ws.readyState !== 1) return;
                if (Date.now() - r.lastHbAt >= 12000) {
                    r.ws.send(r.lastLobbyHb);
                    r.lastHbAt = Date.now();
                }
            }, 8000);
        }
        function connect() {
            const prev = Module.relay;
            if (prev && prev.closing) return;
            if (prev && prev.retryTimer) { clearTimeout(prev.retryTimer); prev.retryTimer = 0; }
            if (prev && prev.hbTimer) { clearInterval(prev.hbTimer); prev.hbTimer = 0; }
            const ws = new WebSocket(url);
            ws.binaryType = 'arraybuffer';
            const r = (Module.relay = ({
                ws: ws,
                pending: prev ? prev.pending : [],
                retries: prev ? prev.retries : 0,
                closing: false,
                retryTimer: 0,
                hbTimer: 0,
                lastLobbyHb: prev ? prev.lastLobbyHb : null,
                lastHbAt: prev ? prev.lastHbAt : 0
            }));
            startHeartbeat(r);
            ws.onopen = () => {
                if (r.closing) return;
                console.log('[net] connected to ' + url);
                r.retries = 0;
                if (Module.onRelayState) Module.onRelayState('connected', 0, 0);
                for (const f of r.pending) ws.send(f);
                r.pending = [];
            };
            ws.onmessage = (e) => {
                if (r.closing) return;
                const a = new Uint8Array(e.data);
                if ((a[0] === 104 && a.length >= 13 && (a[9] & 0x21)) || a[0] === 108) {
                    r.lastLobbyHb = null;
                }
                const p = _malloc(a.length || 1);
                HEAPU8.set(a, p);
                _net_web_inbound(p, a.length);
                _free(p);
            };
            ws.onclose = () => {
                if (r.closing) return;
                if (r.hbTimer) { clearInterval(r.hbTimer); r.hbTimer = 0; }
                if (r.retries < 5) {
                    const delay = Math.min(1000 * Math.pow(2, r.retries), 16000);
                    r.retries++;
                    console.warn('[net] relay disconnected, reconnecting in ' + delay + 'ms (attempt ' + r.retries + ')');
                    if (Module.onRelayState) Module.onRelayState('reconnecting', r.retries, delay);
                    r.retryTimer = setTimeout(connect, delay);
                } else {
                    r.lastLobbyHb = null;
                    console.error('[net] relay connection lost after 5 retries');
                    if (Module.onRelayState) Module.onRelayState('lost', r.retries, 0);
                    Module.relay = null;
                    _net_web_closed();
                }
            };
        }
        connect();
    });
    return 0;
}

void net_send(const uint8_t *b, size_t n) {
    uint8_t *copy = malloc(n ? n : 1);
    if (!copy) return;
    memcpy(copy, b, n);
    MAIN_THREAD_ASYNC_EM_ASM({
        const f = HEAPU8.slice($0, $0 + $1);
        _free($0);
        const r = Module.relay;
        if (!r || r.closing) return;
        if (f[0] === 7 && f.length === 37 && (f[9] | f[10] | f[11] | f[12]) === 0 && f[17] === 16 && f[18] === 0 && f[19] === 0 && f[20] === 0 && f[21] === 0x35 && f[22] === 0x30) {
            r.lastLobbyHb = f;
            r.lastHbAt = Date.now();
        } else if (f[0] === 3 || f[0] === 4 || f[0] === 6 || f[0] === 11 || (f[0] === 8 && f.length >= 13 && (f[9] & 0x21))) {
            r.lastLobbyHb = null;
        }
        if (r.ws.readyState === 1) r.ws.send(f); else if (r.ws.readyState === 0) r.pending.push(f);
    }, copy, (int)n);
}
#endif

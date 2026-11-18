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
        function connect() {
            const ws = new WebSocket(url);
            ws.binaryType = 'arraybuffer';
            const r = (Module.relay = ({ ws: ws, pending: Module.relay ? Module.relay.pending : [], retries: Module.relay ? Module.relay.retries : 0 }));
            ws.onopen = () => {
                console.log('[net] connected to ' + url);
                r.retries = 0;
                for (const f of r.pending) ws.send(f);
                r.pending = [];
            };
            ws.onmessage = (e) => {
                const a = new Uint8Array(e.data);
                const p = _malloc(a.length || 1);
                HEAPU8.set(a, p);
                _net_web_inbound(p, a.length);
                _free(p);
            };
            ws.onclose = () => {
                if (r.retries < 5) {
                    const delay = Math.min(1000 * Math.pow(2, r.retries), 16000);
                    r.retries++;
                    console.warn('[net] relay disconnected, reconnecting in ' + delay + 'ms (attempt ' + r.retries + ')');
                    setTimeout(connect, delay);
                } else {
                    console.error('[net] relay connection lost after 5 retries');
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
        if (!r) return;
        if (r.ws.readyState === 1) r.ws.send(f); else if (r.ws.readyState === 0) r.pending.push(f);
    }, copy, (int)n);
}
#endif

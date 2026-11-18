/*
 * net.h - the link to the relay (tools/net/relay.py) under the DirectPlay emulation.
 *
 * A transport carries whole frames (see relay.py for their layout): native over TCP
 * (net_native.c), in a browser over the page's WebSocket (src/web/net_web.c).
 * Frames that arrive, on whatever thread, go into one queue (net_inbound); the game
 * thread takes them out (net_take) when it next calls into DirectPlay or pumps.
 */
#ifndef AOE2_NET_H
#define AOE2_NET_H
#include <stddef.h>
#include <stdint.h>

int    net_open(void);                               /* 0: connecting or connected; -1: no relay */
void   net_send(const uint8_t *frame, size_t n);     /* one frame, in order */
void   net_inbound(const uint8_t *frame, size_t n);  /* a transport: a frame arrived */
void   net_closed(void);                             /* a transport: the link went down */
size_t net_take(uint8_t **frame);                    /* the game thread: the next frame (free it), 0 if none */
int    net_down(void);                               /* the link went down since net_open */

#endif

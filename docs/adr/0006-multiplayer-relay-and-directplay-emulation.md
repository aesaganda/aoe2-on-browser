# ADR-0006: Multiplayer through a relay server, with DirectPlay emulated over it

- **Status:** accepted
- **Date:** 2026-11-18
- **Deciders:** the owner chose "server relay first", "this PC + Tailscale" and "each player
  uses their own copy"; autopilot chose the rest

## Context

AoC plays multiplayer through DirectPlay 4 (`dplayx.dll`), using lockstep: every
player runs the whole simulation, and only commands and sync checks travel. Native
DirectPlay needs UDP/TCP between the players' machines. A browser has neither, only
WebSocket (and WebRTC). The goal is a play-cs-like site: open the page, see the
games, host or join one.

Options:
1. **Relay server**: each game keeps one connection (WebSocket from the browser,
   TCP natively) to a server that keeps rooms and forwards DirectPlay messages. Easy
   to reason about. Every message takes two hops, but lockstep at AoC's turn rate
   tolerates that.
2. **WebRTC data channels** between players, with a signalling server: fewer hops,
   but NAT traversal, TURN for the cases it fails, and much more code before the
   first game.
3. **Emulating the IPX/TCP service providers** at the socket level: the game's
   DirectPlay would then need a real `dplayx.dll`, which we do not have and must not
   ship.

## Decision

Option 1. Option 2 can be added later behind the same `net.h` interface.

- **The relay** is `tools/net/relay.py` (Python asyncio, `websockets`):
  - WebSocket on `127.0.0.1:8643/relay` and length-prefixed TCP on `127.0.0.1:8644`;
  - it keeps rooms (a DirectPlay session: name, max players, password, open/closed,
    the game's own session description) and the players in each room;
  - it forwards messages to one player or all, moves the host on to another player
    when the host leaves, and sends DirectPlay's system messages (player joined or
    left, description changed, new host);
  - `GET /relay/rooms` lists the rooms as JSON for the page.
- **The game side** is `src/net/dplay_net.c`, an `IDirectPlay4A` implemented on that
  protocol. `src/linux/dplay_stub.c` lists it as the only service provider, "Online
  (aoe2-on-browser)". The transport is `src/net/net_native.c` (TCP) or
  `src/web/net_web.c` (a WebSocket on the browser's main thread, frames queued for the
  game thread).
- **Hosting**: the relay and the page run on the owner's PC, reached through
  `tailscale serve`: `/` is the page, `/relay` is the relay. Only people on the
  owner's tailnet can reach it; Funnel (the public internet) is not turned on.
- **Each player uses their own copy** of the game (ADR-0005). Nothing about the game
  goes through the relay except the game's own messages.
- **Starting from the page**:
  - "Host a game" launches the game as DirectPlay lobby software would (its `LOBBY`
    switch, `IDirectPlayLobby::Connect`). The game creates the room and goes straight
    to the Multiplayer Game setup screen.
  - "Join" launches the game the same way, joining that room. The guest lands on the
    same screen.
  - Our lobby object answers the game's request for "preset data"
    (`DPLSYS_GETPROPERTY`) with "not supported". Without that answer, the game waits
    30 s on "Setting up multiplayer game…" before going on
    (`docs/phase3-multiplayer.md`).
  - The game's own screens (Multiplayer → Online → Show Games) work too.

## Consequences

- Every message goes through the owner's PC, so the latency between the players is
  the sum of each one's latency to that PC. That is fine for a few players on one
  tailnet. A public relay would need a host, which is the owner's decision, and
  then authentication and rate limits, which the relay does not have yet.
- The relay does not interpret game messages, so it cannot cheat for anyone, and
  it cannot stop a desynced or modified client. Out-of-sync detection is the game's
  own.
- The page reaches a game through the lobby launch alone. Whatever else the game
  might ask a real lobby (MSN Gaming Zone presets, restoring saved games) is answered
  "not supported".
- The relay trusts every client: anyone who reaches it can create rooms and join
  open ones. That is acceptable on a tailnet, and not on the internet.

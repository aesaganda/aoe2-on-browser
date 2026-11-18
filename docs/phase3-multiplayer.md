# Phase 3: multiplayer

Players meet on the web page, as on play-cs: open the page, see the games, host one
or join one. Decisions are in ADR-0006; this file is the working log.

## Pieces

| Piece | File | Role |
|---|---|---|
| Relay | `tools/net/relay.py` | rooms, players, message forwarding; `GET /relay/rooms` |
| DirectPlay | `src/net/dplay_net.c` | `IDirectPlay4A` (and the v1 interface) over the relay; lobby launch |
| Service provider, lobby | `src/linux/dplay_stub.c` | lists "Online (aoe2-on-browser)"; `IDirectPlayLobby` |
| Native transport | `src/net/net_native.c` | TCP to `AOE2_RELAY` (default `127.0.0.1:8644`) |
| Browser transport | `src/web/net_web.c` | WebSocket to `Module.relayUrl`, on the main thread |
| Inbound queue | `src/net/net_queue.c` | frames from either transport, for the game thread |
| Page | `web/page.js`, `web/index.html` | name, game list, Host / Join |
| Test | `tools/net/two_players.sh` | two native games meet, chat, start |

The relay's frame format is at the top of `relay.py`.

## Running it

```
python3 tools/net/relay.py                                   # ws :8643/relay, tcp :8644
python3 tools/web/serve.py --install <install-root>          # the page, :8642
tailscale serve --bg --https=443 http://127.0.0.1:8642
tailscale serve --bg --https=443 --set-path=/relay http://127.0.0.1:8643/relay
```

The page uses `//host:8643/relay` when it is served on port 8642 directly, and
`//host/relay` behind a proxy. The native build takes `AOE2_RELAY=host:port`.

Lobby launch, natively, for testing:

```
AOE2_ARGS=LOBBY AOE2_LOBBY=host AOE2_ROOM="My game" AOE2_PLAYER=Alice build/native/aoe2 …
```

## What works (checked 2026-11-18)

| Host | Guest | How | Result |
|---|---|---|---|
| native | native | in-game Create / Show Games → Join | lobby, chat, ready, game started |
| native | browser | in-game | game started |
| browser, "Host a game" on the page | native | guest in-game | setup screen in ~30 s, guest joined, game started on both |
| native, lobby launch | — | | straight to the setup screen |

Not checked yet: two browsers. Two tabs of one browser share one OPFS copy, and
WASMFS takes exclusive access handles to its files, so that needs two browsers or two
browser profiles. Also not checked: players on other machines across the tailnet.

## Bugs found on the way

- **Start Game crashed** in `sub_00433220`. The game asks `QueryInterface` for
  `IID_IDirectPlay` (v1) and calls slot 10, which is `EnableNewPlayers` in v1 but a
  different method in v4, so the arguments popped were wrong. `dplay_net.c` now hands
  out a v1 partner object with its own vtable.
- **System messages** must be DPMSG structures in the game's buffer, with their
  pointers pointing into that same buffer (sizes 48/52/20/28/84 for
  create/destroy player, set description, host, set name).
- `DPERR_NOTLOBBIED` is `0x8877042E` (MAKE_DPHRESULT(1070)). An earlier guess of
  1000 was wrong.

## Open: joining by lobby launch

Here `AOE2_LOBBY=join:<room id>` connects and joins the room. The relay sees the
player; the host sees them arrive, and messages flow both ways. But the guest stays
at "Setting up multiplayer game…".

The traces differ in one way. In a normal join, the guest's first message to the
host is 8 bytes of type `0x41`, which looks like the game's own join request. The
lobby-launched guest sends only pongs (`0x32`) and broadcasts.

In the game:
- `0x437160` handles lobby launch. It calls `GetConnectionSettings` twice, then
  loops on `Connect` while it returns `DPERR_CONNECTING`, with a 30 s timeout. Then it
  calls `QueryInterface` and `EnumPlayers` (callback `0x4370D0`).
- `0x431210` checks whether comm was lobby launched:
  - the host path calls `0x430A40` and sets state `0x7D1`;
  - the join path logs "Lobby Launched Join." and calls `0x432730` (which sends
    `0x17AD`) and `0x437EF0`.
- `0x437850` handles lobby messages; it only logs the system ones.

The next step is to find what the in-game Join screen calls after `Open` to send
`0x41`, and what state the lobby path skips. Until then the page's Join starts the
game and shows the in-game way to the room.

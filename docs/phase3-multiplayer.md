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

By hand:

```
python3 tools/net/relay.py                                   # ws :8643/relay, tcp :8644
python3 tools/web/serve.py --install <install-root> --owner <login>  # the page, :8642
```

As services that start with the machine (systemd), this is how the owner's PC runs:

```
sudo tools/deploy/install-services.sh <install-root> <owner-login>  # aoe2-relay, aoe2-web
sudo tools/deploy/install-services.sh --remove
journalctl -u aoe2-relay -f                                  # who joins what
```

Under WSL, the distro has to be running for its services to run. Run
`tools\deploy\wsl-keepalive.ps1` from Windows once. It puts a shortcut in the Startup
folder that starts `wsl.exe -d Ubuntu-24.04 --exec sleep infinity` without a window
at every sign-in; `-Remove` takes it out. `%USERPROFILE%\.wslconfig` has
`vmIdleTimeout=-1`, so the VM is not shut down when idle.

Tailscale runs on Windows. Its serve configuration persists, and it puts both
services on the tailnet:

```
tailscale serve --bg --https=443 http://127.0.0.1:8642
tailscale serve --bg --https=443 --set-path=/relay http://127.0.0.1:8643
```

After `make web`, the service serves the new build at once, since it reads
`build/web/`. After a change to `relay.py` or `serve.py`, run
`sudo systemctl restart aoe2-relay aoe2-web`.

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
| native, lobby launch | native, lobby launch | `AOE2_LOBBY=host` / `join:<id>` | guest at the setup screen 5–10 s after starting |
| native, lobby launch | browser, "Join" on the page | one click | guest at the setup screen within 20 s; game started on both |
| native, lobby launch | native, lobby launch | both ready, host starts | game runs: turn counter rising, pictures changing on both |
| browser, "Host a game" | native, lobby launch | both ready, host starts | game runs: the native guest's turn went 0x58 → 0x80 in 10 s; a villager queued in the browser was trained |
| browser (localhost origin) | browser (127.0.0.1 origin) | one click each | both reach the game, then stall: see below |

**Two browsers.** Two origins in one browser have separate OPFS copies, so they
stand in for two players. Both joined in one click and the game started on both,
but then neither advanced. The in-app browser used for testing runs only the tab
in front, so the hidden tab's game stops. Lockstep then waits for it, on both sides.
The same build kept turning against a native peer with its tab in front. Two
browsers on two machines, each in front, have not been tried.

Expect the same in a real browser. If a player hides the game's tab, the browser may
throttle it, and everyone's game waits for that player. Keep the game visible while
playing.

Not checked yet: players on other machines across the tailnet.

**Tailnet HTTP verification (2026-11-18):** All HTTP endpoints verified working
through `https://ccenter.adal-shark.ts.net/` via curl: index.html 200 with correct
COOP/COEP headers, `page.js` 200, `/api/me` returns `{"owner": true, "install": true}`
for the owner's Tailscale login, `/relay/rooms` returns the room list. The in-app
browser blocks `.ts.net` scripts (`ERR_BLOCKED_BY_CLIENT`), so the full page has not
been tested there; a real Chrome on the tailnet should work.

## Friends' own installs

Every player copies their own install into their browser with "Choose your install
folder…" (Chrome or Edge). Over the tailnet the page sends the isolation headers
(COOP/COEP) through `tailscale serve`, so the build runs there.

- **The executable must be the one the code was lifted from**: The Conquerors 1.0,
  unpatched. `make web` writes its size and SHA-256 to `build/web/aoe2.json`. The page
  refuses any other `age2_x1.exe` before copying anything, and again before playing.
  A patched 1.0c exe would otherwise crash, because the lifted code would not match
  the executable's data.
- **Videos** play only from the MPEG-1 copies made by `tools/transcode_videos.sh`. A
  plain Windows install has none, so it plays no videos, and the game carries on
  without them.

## Owner auto-load

With `--owner <tailscale-login>`, the page auto-loads the game on the owner's first
visit through the tailnet. The server checks the `Tailscale-User-Login` header that
`tailscale serve` adds: if it matches the configured owner, `/install/` is allowed
and `/api/me` returns `{"owner": true, "install": true}`. The page's `init()`
fetches `/api/me`, auto-imports from the server without any button press, and skips
straight to the game list. On return visits the copy is already in the browser, so
the page shows the game list at once.

For other players, `/install/` returns 404 and `/api/me` returns `owner: false`.
They use "Choose your install folder…" to copy their own install once, and then also
skip straight to the game list on every later visit.

```
sudo tools/deploy/install-services.sh <install-root> <owner-tailscale-login>
```

## The page server and the tailnet

`tools/web/serve.py --install` serves the owner's install tree. With `--owner` it is
also available through the tailnet to the owner's Tailscale identity. Without
`--owner` (or for anyone else), it is served only to direct loopback requests.

Paths are now normalised and confined to their root. Before, a raw `../` in a
request could read any file the server could. Over the tailnet, `/install/…` now
answers 404. The page, `aoe2.js`, `aoe2.wasm` and `/relay` still work. Other players
copy their own installs with "Choose your install folder…".

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

## Lobby launch: the 30-second "Setting up" screen

A lobby-launched game (host or guest) used to sit on "Setting up multiplayer game…"
for 30 s. At first that looked like a stalled join, because the early tests did not
wait that long. In the game:

- `0x431210` runs the lobby launch:
  1. `0x437160` reads the connection settings and calls `Connect`;
  2. the host path calls `0x430A40` and sets state `0x7D1`, and the join path logs
     "Lobby Launched Join." and calls `0x432730`;
  3. `0x437EF0` sends the lobby a `DPLSYS_GETPROPERTY` (type 7) for the game's
     "preset data".
- The caller (`0x5C729F`) then sets `[screen+0x1F84] = 1` and starts a timer.
  Every frame, `0x5D1637` polls the lobby (`0x437850`, `ReceiveLobbyMessage`) until
  30 s have passed (`0x7530` ms), then goes on to the setup screen.
- `0x437DE0` ("satisfyLobbyLaunchCriteria") ends the wait early once both of these
  are true:
  - PLAYER_ID_SET: the host has its own slot, or the guest found its own slot in the
    host's options (`0x42CC72`);
  - PRESETS_RETRIEVED: a property response has come in (`0x437D25`, `0x437D77`).
  It then calls `0x5D1DF0`: with 0, a regular launch; with 1, restoring a saved
  game, which a preset can ask for.
- The property response is a standard lobby message, not a `DPLMSG_SYSTEM` one: a
  `DPLMSG_GETPROPERTYRESPONSE` with the request's GUIDs. If its `hr` is not `DP_OK`,
  the game logs "PresetData not supported" and counts the presets as retrieved.

So `dplay_stub.c`'s lobby answers the game's `GETPROPERTY` with `DPERR_UNSUPPORTED`
from `ReceiveLobbyMessage`. A lobby-launched guest now reaches the setup screen 5–10 s
after starting, which is about the game's own start-up time.

The 8-byte type-`0x41` message seen only in in-game joins is not a join request. It
is what the game's receive function (`0x42ABB0`) sends to a sender it has no player
slot for: "who are you?". The host answers with its options (`0x5A`). A
lobby-launched guest already has the host's slot from `EnumPlayers`, so it never
needs to ask.

# aoe2-on-browser

Static recompilation of the original *Age of Empires II: The Conquerors* (2000,
Genie engine, MSVC 6, 32-bit Windows PE) to C, with WebAssembly in the browser as
the goal. The x86 code is translated ahead of time, Win32/DirectX are provided
by a shim, and the result is compiled with Emscripten.

The Definitive Edition is out of scope.

## Status

- **Phase 0 is done**: the verdict was GO (`docs/phase0-report.md`).
- **Phase 1 is done**: a native Linux binary plays the game, with menus, single-player games, chat, minimap, sound, music and the intro videos (`docs/phase1-bringup.md`).
- **Determinism:** the lifted build matches the original executable. A 403 s game the original recorded under Wine replays with 10 of 10 world checksums matching, and it holds the other way round too (`tools/oracle/`).
- **Phase 2 is done**: the game runs in the browser as WebAssembly, menus and a game at about 66 fps. Chrome, Edge, Firefox and Safari are supported. Auto-load on first visit, save management, audio resume, mute, WASM progress, game timer, wake lock, tab warning, clear cache (`docs/phase2-web.md`).
- **Phase 3 is done**: multiplayer runs through a WebSocket relay server. The web page lists the open games, and hosting or joining one takes a single click. Browser and native games have played each other. Wake lock, reconnect, ping display, join notifications (`docs/phase3-multiplayer.md`, ADR-0006).

The lifter is [pcrecomp](https://github.com/sp00nznet/pcrecomp) (MIT), pinned as a submodule (ADR-0001). Clone with `--recursive`, or run `git submodule update --init`.

| Phase | Content |
|---|---|
| 0 | Recon, tool choice, lifter prototype + differential harness |
| 1 | Full lift, Win32 shim, native Linux binary |
| 2 | WASM: DirectDraw -> Canvas, DirectSound -> WebAudio, files -> OPFS |
| 3 | DirectPlay -> WebSocket relay, game list on the page, invite links |

## You need your own copy of the game

This repository contains tools and runtime code only. No game binaries, no game
data, and nothing generated from the game's code (ADR-0002). Put your
`age2_x1.exe` in `orig/` (git-ignored), or pass `EXE=/path/to/age2_x1.exe`.

## Play it: one command (Docker)

### Turnkey: from a prebuilt bundle (no ISO, no build)

The public repository stays free of copyrighted game assets (ADR-0002), so you host
your own bundle privately (for example S3/R2, a private GitHub Release, or SSH).
On a machine where `build/web/` and `install/` already exist, pack them once:

```bash
./play.sh --pack aoe2-bundle.tar.gz   # also writes aoe2-bundle.tar.gz.sha256
```

Then on any new machine (without `--recursive` submodules, ISOs, or compilers):

```bash
git clone https://github.com/aesaganda/aoe2-on-browser
cd aoe2-on-browser
./play.sh --bundle https://your-private-url/aoe2-bundle.tar.gz --bundle-sha256 <SHA256>
```

Supported `--bundle` sources (also configurable via `AOE2_BUNDLE_URL` and
`AOE2_BUNDLE_SHA256` in `.env`): `https://` / `http://` (set `AOE2_BUNDLE_TOKEN`
in your shell for Bearer auth, never committed to `.env`), `gh://owner/repo[@tag]/asset`,
`ssh://user@host/path`, or a local `.tar.gz`/`.tar` file. Use `./play.sh --refresh-bundle`
to pull a newer bundle later.

### From your own discs and executable

All you need is Docker (Linux, macOS, or Windows with WSL 2), your two disc
images and your unprotected `age2_x1.exe`:

```bash
git clone --recursive https://github.com/aesaganda/aoe2-on-browser
cd aoe2-on-browser
./play.sh --exe /path/to/age2_x1.exe --aok /path/to/AoE2.iso --aoc /path/to/AoE2_Conquerors.iso
```

Then open http://localhost:8642/. Already have an install tree (`age2_x1/age2_x1.exe`,
`data/`, `sound/`, ...)? Use `./play.sh --install ~/aoe2-install` instead.

`play.sh` pulls the toolchain image (`ghcr.io/aesaganda/aoe2-recomp-builder`:
Ghidra, emsdk, the lifter; no game files in it), builds the browser version from
*your* executable into `build/web/`, lays out `install/` from your discs, and starts
the web server, the multiplayer relay and the gateway with Docker Compose. The first
build takes tens of minutes and several GB of RAM (lower `AOE2_JOBS` if it runs out);
after that `./play.sh` just starts it, and `./play.sh --stop` stops it. Your files
stay on your machine (ADR-0002).

The page needs a secure context: `localhost` works, a plain `http://` LAN address
does not. To play from other machines, or online with friends, put it behind HTTPS
(`docs/docker-nginx.md`). On a Linux ARM host (Raspberry Pi) the toolchain image is
built locally the first time.

## Quick start for development (Ubuntu 24.04 or WSL)

```bash
sudo apt install build-essential python3-venv openjdk-25-jdk-headless mingw-w64 gcc-multilib unzip curl
make setup      # Python venv (pefile, LIEF, unicorn, capstone, pytest) + Ghidra
make docs       # Ghidra headless analysis + PE recon -> docs/
make lift       # whole-image lift with pcrecomp -> build/lift/gen
make test       # pcrecomp self-test + synthetic PE + AoE2 prototype differential tests
make sweep      # differential test over every leaf function (~13 min)
```

From Windows: `wsl -d Ubuntu-24.04 make docs`.

### Play it

Lay out an install tree from your own two disc images; it is about 0.9 GB and stays outside the repo:

```bash
sudo apt install p7zip-full cabextract ffmpeg libsdl2-dev libsdl2-ttf-dev zlib1g-dev gdb
tools/install_from_iso.sh <AoE2.iso> <AoE2_Conquerors.iso> orig/age2_x1.exe ~/aoe2-install
```

**Native (Linux):**

```bash
make native
AOE2_ACCEPT_EULA=1 build/native/aoe2 ~/aoe2-install
```

`AOE2_ACCEPT_EULA=1` is your answer to the game's license step.

**Browser:**

```bash
source ~/emsdk/emsdk_env.sh
make web
python3 tools/web/serve.py --install ~/aoe2-install
```

Open http://localhost:8642/ — on first visit the browser automatically downloads and caches the game files in its private storage (OPFS, ADR-0005), then boots straight into the game. Later visits boot directly.

**Online:** run the relay next to the page:

```bash
pip install websockets
python3 tools/net/relay.py
```

Click the **Online** button in the top-right corner while in-game (or on the lobby screen after exiting) to view open games, host a room, or join one:
- The game list shows who is waiting.
- "Host a game" and each game's "Join" open the game straight at its Multiplayer Game setup screen.
- The game's own Multiplayer → Online (aoe2-on-browser) screens work too.

To keep the page and the relay running as services, run `sudo tools/deploy/install-services.sh ~/aoe2-install` (systemd). Native builds reach the relay with `AOE2_RELAY=host:8644`. For players on other machines, put the page and `/relay` behind a proxy, as described in `docs/phase3-multiplayer.md`; that file uses `tailscale serve`.

**Determinism check** (`docs/phase1-bringup.md`):

```bash
tools/oracle/replay_check.sh ~/aoe2-install <recording.mgx>
```

This replays a recorded game with the game's own sync checking. `tools/oracle/wine_*.sh` do the same with the original executable under Wine.

## Layout

```
play.sh          one-command build + run with Docker (see above)
docker/builder/  the toolchain image play.sh builds in
docs/            reports (generated: recon.md, imports.md, ghidra.md) and ADRs
docs/adr/        architecture decision records (0000-template.md)
tools/recon/     PE / import / COM recon (pefile + capstone)
tools/ghidra/    headless Ghidra driver and JSON export script
tools/lift/      lift driver over pcrecomp, prototype picker, leaf sweep
tests/difftest/  our differential harness (Unicorn oracle vs lifted C)
tests/fixtures/  synthetic PE for the binary-free CI path
third_party/     pcrecomp (submodule)
orig/            your game files (ignored)
build/           everything generated from the binary (ignored)
install/         the install tree play.sh lays out from your discs (ignored)
```

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
- **Phase 2 is under way**: the game runs in the browser as WebAssembly, menus and a game at about 66 fps. Chrome, Edge, Firefox and Safari are supported (`docs/phase2-web.md`).
- **Phase 3 is under way**: multiplayer runs through a relay server. The web page lists the open games, and hosting or joining one takes a single click. Browser and native games have played each other (`docs/phase3-multiplayer.md`, ADR-0006).

The lifter is [pcrecomp](https://github.com/sp00nznet/pcrecomp) (MIT), pinned as a submodule (ADR-0001). Clone with `--recursive`, or run `git submodule update --init`.

| Phase | Content |
|---|---|
| 0 | Recon, tool choice, lifter prototype + differential harness |
| 1 | Full lift, Win32 shim, native Linux binary |
| 2 | WASM: DirectDraw -> Canvas, DirectSound -> WebAudio, files -> OPFS |
| 3 | DirectPlay -> relay server (WebRTC later), game list on the page |

## You need your own copy of the game

This repository contains tools and runtime code only. No game binaries, no game
data, and nothing generated from the game's code (ADR-0002). Put your
`age2_x1.exe` in `orig/` (git-ignored), or pass `EXE=/path/to/age2_x1.exe`.

## Quick start (Ubuntu 24.04 or WSL)

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

Open http://localhost:8642/, accept the EULA, copy your files into the browser once, and Play. The browser keeps its copy in this site's private storage (ADR-0005). Don't expose `serve.py --install` to a network.

**Online:** run the relay next to the page:

```bash
pip install websockets
python3 tools/net/relay.py
```

The page then shows an Online section:
- The game list shows who is waiting.
- "Host a game" and each game's "Join" open the game straight at its Multiplayer Game setup screen.
- The game's own Multiplayer → Online (aoe2-on-browser) screens work too.

To keep the page and the relay running as services, run `sudo tools/deploy/install-services.sh ~/aoe2-install` (systemd). Native builds reach the relay with `AOE2_RELAY=host:8644`. For players on other machines, put the page and `/relay` behind a proxy, as described in `docs/phase3-multiplayer.md`; that file uses `tailscale serve`. Each player needs their own copy of the game.

**Determinism check** (`docs/phase1-bringup.md`):

```bash
tools/oracle/replay_check.sh ~/aoe2-install <recording.mgx>
```

This replays a recorded game with the game's own sync checking. `tools/oracle/wine_*.sh` do the same with the original executable under Wine.

## Layout

```
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
```

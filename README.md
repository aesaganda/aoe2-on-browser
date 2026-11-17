# aoe2-recomp

Static recompilation of the original *Age of Empires II: The Conquerors* (2000,
Genie engine, MSVC 6, 32-bit Windows PE) to C, with WebAssembly in the browser as
the goal. The x86 code is translated ahead of time, Win32/DirectX are provided
by a shim, and the result is compiled with Emscripten.

The Definitive Edition is out of scope.

## Status

**Phase 0: recon and lifter prototype.** See `docs/phase0-report.md` once it exists.

| Phase | Content |
|---|---|
| 0 | Recon, tool choice, lifter prototype + differential harness |
| 1 | Full lift, Win32 shim, native Linux binary |
| 2 | WASM: DirectDraw -> Canvas, DirectSound -> WebAudio, files -> OPFS |
| 3 | DirectPlay -> WebRTC, lobby/relay server |

## You need your own copy of the game

This repository contains tools and runtime code only. No game binaries, no game
data, and nothing generated from the game's code (ADR-0002). Put your
`age2_x1.exe` in `orig/` (git-ignored), or pass `EXE=/path/to/age2_x1.exe`.

## Quick start (Ubuntu 24.04 or WSL)

```bash
sudo apt install build-essential python3-venv openjdk-25-jdk-headless mingw-w64 gcc-multilib unzip curl
make setup      # Python venv (pefile, LIEF, unicorn, capstone, pytest) + Ghidra
make docs       # Ghidra headless analysis + PE recon -> docs/
make test       # differential tests
```

From Windows: `wsl -d Ubuntu-24.04 make docs`.

## Layout

```
docs/            reports (generated: recon.md, imports.md, ghidra.md) and ADRs
docs/adr/        architecture decision records (0000-template.md)
tools/recon/     PE / import / COM recon (pefile + capstone)
tools/ghidra/    headless Ghidra driver and JSON export script
orig/            your game files (ignored)
build/           everything generated from the binary (ignored)
```

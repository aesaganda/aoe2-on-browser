# Phase 2: the browser (WebAssembly)

## How to run

```bash
source ~/emsdk/emsdk_env.sh        # emsdk 7.0.0 (git clone emscripten-core/emsdk; ./emsdk install latest)
make web                           # build/web/aoe2.js + aoe2.wasm (43 MB)
python3 tools/web/serve.py --install ~/aoe2-recomp-work/install    # http://localhost:8642/
```

Open `http://localhost:8642/`:
- On the first visit, the page automatically streams the install files from `/install/` into the browser's origin private file system (OPFS, ADR-0005) with a progress bar and boots straight into the game.
- On return visits, the cached OPFS copy boots directly without downloading the install files again.
- Use the **Online** button in the top-right corner of the game (or exit the game) to open the multiplayer room list.

## Build

`tools/web/web.mk` compiles the same lifted C, win32hle (with `patches/pcrecomp`) and
host as the native build, with emcc:
- **Flat memory** (`RECOMP_FLAT_MEMORY`): a guest VA is a wasm address. The PE image
  (0x400000–0x7A4000) sits below 16 MB, which win32hle keeps for fixed reservations,
  and the program's own data starts above (`-sGLOBAL_BASE=16MB`).
- `-mtail-call`, for `RECOMP_ITAIL`'s `musttail`.
- The game on a pthread (`-sPROXY_TO_PTHREAD`), so the page must be cross-origin
  isolated.
- WASMFS with its OPFS backend (`src/web/web_fs.c`).
- Emscripten's SDL2, SDL2_ttf and zlib ports; Liberation fonts embedded at their Linux
  path for win32hle's GDI text.
- 1 GB initial memory, growable to 2 GB.

The whole lift compiled and linked on the first try. A full build takes a few minutes
on 24 cores.

## Log

### 2026-11-18: in game in the browser

| Step | Result | Fix |
|---|---|---|
| First boot in Chromium | The exe maps at 0x400000, binds 255 imports with 0 unresolved, passes the EULA step and the disc check (D: = OPFS `cdrom/`, label AGE2_X1), sets 800×600×8, starts web audio. **Main menu.** History's text (GDI fonts) is right | – |
| Input | Mouse and keyboard reach the game through SDL's canvas. A click from a test tool that sends no mouse move first lands where the pointer last was; real use moves the mouse first | – |
| Frames lag a second or two | win32hle's browser path hands frames to the page only for 16 bpp games; an 8 bpp frame went through SDL's software renderer (scaled to 1280×960, proxied to the page's thread) | Patch 0005: 8 bpp frames take the same path, through a palette-to-RGB565 table, with `hle_screen_rgb_hook` content (the videos) composed first. `web/page.js` draws them on a canvas laid over SDL's (`Module.ra2Present`) |
| A game | Standard Game starts, Town Center selected, its panel and buttons right. **66.7 frames a second presented in game** (Chromium, 24 cores) | – |

## Open

- The intro videos did not start in the browser (no 640×480×16 mode switch was logged);
  `AOE2_TRACE_VIDEO=1` is now on in the browser env — check the console for `[video]` lines.
- Sound: web audio starts, autoplay-resume is patched in; not yet listened to.
- The determinism check in the browser (replay a recorded game and check sync).

## Done (2026-11-18)

- **Firefox and Safari**: the `<input webkitdirectory>` fallback lets those browsers
  pick the install folder. `showDirectoryPicker` is still preferred where it exists
  (Chrome/Edge). The file API's `webkitRelativePath` provides the directory structure.
- **Save games and recordings** persist in OPFS across reloads — the game writes to
  `C:\SaveGame\` which maps to `/opfs/aoe2/SaveGame/`. The page shows the count of
  `.mgl`/`.mgx` files on the lobby screen.
- **Save management**: download saves, import recordings (`.mgx`/`.mgl`/`.mgz`), and
  delete individual saves from the lobby screen.
- **Audio context resume**: patches `AudioContext` to resume all suspended contexts on
  the first user gesture (click / keydown / touchstart).
- **Mute toggle**: M key or HUD button suspends/resumes all audio contexts.
- **WASM download progress**: streams the 42 MB `aoe2.wasm` with a live MB counter
  and progress bar; the SW no longer precaches it on install.
- **Wake lock**: keeps the screen on during gameplay.
- **Tab visibility warning**: red banner during gameplay when the tab is in the
  background.
- **Game timer**: elapsed time counter in the top-right HUD.
- **Multiplayer ping display**: color-coded latency (green/yellow/red) during
  multiplayer games.
- **Join notifications**: browser notification when a player joins a hosted game.
- **Clear cache**: footer link to wipe OPFS and SW cache for recovery.
- **Connection toast**: "Connection lost" / "Connection restored" popups during
  gameplay.
- **PWA**: fullscreen display mode, `launch_handler: navigate-existing` so invite
  links open the existing game window.

# ADR-0004: Cinematics play from MPEG-1 copies made at install time

- **Status:** accepted
- **Date:** 2026-11-18
- **Deciders:** autopilot (by the owner's mandate)

## Context

The game plays four AVI cinematics through MCIWnd (`MSVFW32!MCIWndCreateA`):
`mslogo`, `eslogo`, `age2_x1` (the intro) and `age2`. At start-up it asks
`ICInfo('vidc', 'iv50')` and turns videos off when the Indeo codec is missing.

| File | Video | Audio | Length |
|---|---|---|---|
| mslogo.avi | Indeo 5 (IV50), 320×240, 15 fps | PCM u8 22 kHz mono | 13 s |
| eslogo.avi | same | PCM s16 44.1 kHz stereo | 17 s |
| age2_x1.avi | same | PCM s16 44.1 kHz stereo | 60 s |
| age2.avi | same | PCM s16 22 kHz stereo | 106 s |

The host has to decode Indeo 5 somewhere, natively now and in the browser in
Phase 2. Options:

1. Link libavcodec (LGPL) at run time. That is a large dependency and an
   Emscripten port of FFmpeg for four files, with LGPL relinking duties in a
   bundled WASM build.
2. Write an Indeo 5 decoder. It is a wavelet codec with no free spec; FFmpeg's
   is about 3,000 lines.
3. **Transcode at install time** to a format with a small, permissive, WASM-proven
   decoder, and play that.

## Decision

Option 3:
- `tools/transcode_videos.sh`, run by `install_from_iso.sh`, has ffmpeg write an
  MPEG-1 + MP2 copy (`.mpg`) beside each `.avi`. MPEG-1 has no 15 fps, so frames
  are doubled to 30.
- At run time `src/linux/mciwnd.c` decodes the copy with **pl_mpeg** (MIT, one
  header; submodule `third_party/pl_mpeg`). The library ships an SDL player and is
  used on the web.
- ffmpeg runs only on the owner's machine, at install. It is not linked or
  shipped.
- The copies are derived game data, so they stay in the install tree, never the
  repo (ADR-0002).

The MCIWnd window itself is emulated, only as far as the game uses it:
- a real win32hle child window with a host window procedure;
- `MCI_PLAY`, `MCI_STOP`, `MCI_PAUSE`, `MCI_RESUME`, `MCIWNDM_GETMODEA`, `WM_CLOSE`;
- `ICInfo` answers yes for IV50.

Frames are painted in true colour over the presented picture through a new
win32hle hook (patch 0004, `hle_screen_rgb_hook`), since the game shows videos in
a 640×480×16 mode. Sound goes through win32hle's host audio streams, mixed on
DirectSound's one device: a browser has only one.

## Consequences

- Runtime cost is one header-only decoder; the same code should build for WASM.
- Installing needs `ffmpeg` (Ubuntu package). Without the copies, MCIWndCreateA
  fails and the game skips the videos, as it would without the codec.
- Quality loss from a second lossy encode (`-q:v 2`) is not visible at 320×240.
- `AOE2_SKIP_VIDEOS=1` turns them off; scripted sessions (`tools/native/session.sh`)
  do so by default, since the intro would take their first clicks.
- The game plays a video only if its `.avi` exists. The browser copy leaves the AVIs
  out (ADR-0005), so `web/page.js` writes an empty `.avi` beside each MPEG-1 copy, and
  `mciwnd.c` looks the copy up by its own path. Until 2026-11-18 the browser played no
  videos at all. A lobby launch skips them.

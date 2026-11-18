# ADR-0005: In the browser, the game's files come from the owner, kept in OPFS

- **Status:** accepted
- **Date:** 2026-11-18
- **Deciders:** autopilot (by the owner's mandate)

## Context

The WebAssembly build needs the game's data: the executable, DRS archives, sounds,
campaigns and the language DLLs. Read through C: by win32hle, that is about 0.5 GB. As on
Linux (ADR-0002), none of it may come from this project. So the question is how the
owner's own copy reaches their browser, and where it lives between visits.

Options:
1. **Preloaded package** (`--preload-file`): one 0.5 GB download built from the
   owner's files. It would be served from somewhere and re-fetched on every visit, and
   whoever hosts it is redistributing the game. No.
2. **Fetched on demand** (WASMFS fetch backend from a URL): the same hosting
   problem. Also a network round trip for each DRS read, and nowhere to write saves.
3. **Copied once into the origin private file system (OPFS)** from a folder the
   owner picks, then read and written in place through WASMFS's OPFS backend.

## Decision

Option 3:
- `web/page.js` copies the owner's install tree into OPFS `aoe2/`, from a folder they
  pick (File System Access API, Chrome/Edge). On a development server,
  `tools/web/serve.py --install` serves the local install with a manifest, and
  "Copy from this server" does the same; that must never face the internet.
- Not copied:
  - the disc tree: the game only checks that a CD-ROM with label `AGE2_X1` is there
    (an empty `cdrom/` plus `cdrom.label` stands for it; checked natively: a game
    starts);
  - the AVIs: the MPEG-1 copies are what plays (ADR-0004);
  - `.cab` archives.
  What remains is about 540 MB and 1,300 files.
- `src/web/web_fs.c` mounts OPFS at `/opfs`; `main.c` runs the game from
  `/opfs/aoe2`, mapped to C: as on Linux. Saved games, recordings and the player
  profile are written in place and persist.
- The game's EULA step is a checkbox on the page; it sets `AOE2_ACCEPT_EULA` for the
  module, as the environment variable does natively.

## Consequences

- The first visit costs one copy: 541 MB took about 15 s from a local server. Later
  visits start at once. "Remove the copy" deletes it.
- Needs a browser with OPFS and, for the folder picker, `showDirectoryPicker`
  (Chromium). Firefox/Safari could use a `webkitdirectory` input later.
- The page must be cross-origin isolated for the build's pthreads (COOP/COEP); any
  host serving it has to send those headers (serve.py does).
- Nothing about the game leaves the owner's machine; the site serves only the
  recompiled code (`aoe2.js`, `aoe2.wasm`), which has no game data in it.
  Distributing that code is itself derived-code territory (ADR-0002). Whether it may
  be published at all is the owner's call, and it is not made here.

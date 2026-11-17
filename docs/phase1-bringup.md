# Phase 1 bring-up log

Milestone 1: the native Linux build reaches the main menu (phase0-report §8, the ~4-week checkpoint).

## How to run

```bash
make native                                   # lift (if needed) + build/native/aoe2
build/native/aoe2 <install-root>              # <install-root>/age2_x1/age2_x1.exe, data/, sound/, ...
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy build/native/aoe2 <root>   # headless
```

The host (`src/linux/`) does the following:
- maps `<install-root>` as `C:`;
- seeds the registry keys the installer would have written;
- registers the AoC-specific shims first (`aoc_shims.c`) and pcrecomp's win32hle modules after them;
- boots permissively, so an unanswered import becomes a self-naming stub;
- runs the CRT entry point.

## Log

### 2026-11-17: first boot

| Step | Result | Fix |
|---|---|---|
| Link | 2 missing symbols: `recomp_eh_push`/`recomp_eh_pop` (pcrecomp has them only in `native32/eh32.c`, which includes `<windows.h>`) | `src/linux/runtime_glue.c`, a portable rewrite of the EH frame registry |
| Boot 1 | CRT `_initterm` hit 18 dispatch misses: the C++ static initialisers in the `__xc_a` table, all in code no Ghidra function owns | `lift_image.py` seeds an entry from every code pointer in data (1,145) |
| Boot 2 | `EBUEulaX.dll` not found: the game quits | `aoc_shims.c` answers the EULA DLL (see below) |
| Boot 2 | `_doexit` hit 6 misses: `atexit` handlers, held as `push offset fn` immediates in code | Also seed code addresses held as immediates in code (+837; 1,982 seeds in total) |
| Boot 3 | **No dispatch misses.** CRT start-up and static initialisers run. The game asks for `language_x1.dll` (its string-resource DLL, part of the game data) and quits | **Needs the game data**: see the open question below |

The lift now has 9,674 entries (was 7,672), still 0 errors, and 65 MB of C. The extra "unimplemented" instructions (`bound`, `arpl`, `cli`, …) come from seeds that point at data rather than code. They are never called, and a call would fail loudly.

### 2026-11-17: game data from the owner's disc images

**Install tree.** `tools/install_from_iso.sh <AoE2.iso> <AoE2_Conquerors.iso> orig/age2_x1.exe <root>` lays out the install the way the two setup programs would. It only unpacks:
- `7z` for the ISO images and `cabextract` for `EMPIRES2.CAB` / `MSGAME.CAB`;
- plus the loose files on the discs, some of them under 8.3 names on disc: `EMPIRE_1.DAT` → `data/empires2_x1.dat`, `LANGUA_1.DLL` → `language_x1.dll`.

The resulting tree is 539 MB. It lives outside the repo (`~/aoe2-recomp-work/install` in WSL; ADR-0002).

**Version correction.** The exe asks for `data\empires2_x1.dat`, `language_x1.dll` and `gamedata_x1.drs`, and never for the 1.0c patch's `_p1` files. So it is **AoC 1.0**, unpatched. The 1.0c patch package is therefore not used.

| Step | Result | Fix |
|---|---|---|
| Boot 4 | `language_x1.dll` not found: the guest's current directory was the host's, not the install root | The host `chdir`s to the install root, as the AoC shortcut does (working directory = root, exe in `age2_x1\`) |
| Boot 5 | `language_x1.dll` loads as a resource module; first unanswered import: `IsDBCSLeadByte` | `src/linux/win32_gaps.c`: the 24 imports from the Phase 0 inventory that win32hle lacks. They are registered *after* win32hle, so an upstream implementation wins. MCI/MCIWnd/DirectPlay report "unavailable" for now |
| Boot 6 | **Reaches the EULA gate.** The game calls `EBUEula`; without the player's acceptance it quits | Waiting for the owner: accepting the licence is their decision (`AOE2_ACCEPT_EULA=1`) |

### 2026-11-17: the owner accepted the EULA → main menu

The owner accepted the EULA in the session ("I accept the EULA"). From here on, runs set `AOE2_ACCEPT_EULA=1`.

| Step | Result | Fix |
|---|---|---|
| Boot 7 | "Age of Empires II not detected": AoC looks for `empires2.exe` in the install root (`FindFirstFileA`) and never runs it | The install script copies the base disc's `EMPIRES2.EXE` there, as the base installer does |
| Boot 8 | "requires DirectX 6.1a or higher". The check is `DirectPlayCreate` → `QueryInterface(IID_IDirectPlay4)` → Release, then the file version of `dplayx.dll` (≥ 4.7.0.178 or 4.6.3.516) | `src/linux/dplay_stub.c`: a DirectPlay 4 / Lobby 3 with no service providers (empty enumerations; every other method `E_NOTIMPL`, each popping its own arity from mingw's `dplay.h`), the CLSIDs for `CoCreateInstance`, and version info for `dplayx.dll` = 4.07.00.0700 (DX7a) |
| Boot 9 | Dispatch miss 0x0041E760, a window procedure stored by code that no Ghidra function owns (`mov [esp+0x10], offset WndProc`) | Seeds now come from immediates in a linear sweep of all code, and from data pointers into the middle of Ghidra bodies (MSVC6 keeps switch tables in `.text`, so those are real entries) |
| Boot 10 | `SetDisplayMode(800x600x8)`, primary surface created; then a miss at 0x00628006 in `this_cod` | `this_cod` is a hand-written **threaded-code sprite blitter**: fragments end in `jmp [0x794044]` and branch through `jmp [ebx*4 + 0x62F700]` tables kept inside the section. Seeds now include code pointers held in code sections other than `.text`. **Lift: 10,632 entries, 0 errors** |
| Boot 11 | No crash: the game runs its main loop (DirectSound up; `DirectInputCreateEx` unanswered, so the game falls back to window messages). Black screen | The host opens pcrecomp's SDL screen (`hle_screen_open`); `tools/native/screenshot.sh` runs the game under Xvfb and captures it |
| Boot 12 | **Main menu drawn, without its text.** pcrecomp's `TextOutA` handled only 16/32-bpp DCs, and AoE2 draws its UI text through `GetDC` on 8-bpp DirectDraw surfaces | `patches/pcrecomp/0001-…8bpp…patch` (for upstream): 8-bpp surface DCs map a COLORREF to the nearest entry of the surface's (or the primary's) DirectDraw palette, take `PALETTEINDEX` as is, and draw text unblended. `native.mk` builds from a patched copy; the submodule stays at the pinned commit |
| Boot 13 | **Main menu complete** (buttons, rollover text, copyright). A click on *Single Player* highlights it, updates the description and opens the first-run *New Player* dialog | – |

**Milestone 1 (phase0-report §8, "boots to the main menu natively") is reached.** That was the ~4-week checkpoint.

Verified so far: start-up, DirectDraw 8-bit presentation, GDI text, mouse input on the menu.

Not verified:
- sound (runs used the dummy driver);
- typing into the name field;
- starting a game; scenario load and the simulation;
- MCI music, AVI videos;
- SEH paths.

`DirectInputCreateEx` is still unanswered. The game falls back to window messages, but the in-game mouse and edge scrolling still need to be checked.

### EULA

The game shows Microsoft's EULA through `EBUEulaX.dll` and exits unless the player accepts it. Accepting it is the player's decision, so the shim never accepts on its own:
- it returns "accepted" only when `AOE2_ACCEPT_EULA=1` is set;
- otherwise it points to `EULAX.RTF` on the Conquerors disc, and the game quits just as it does when the dialog is declined.

## Open

- **SEH/C++ EH dispatch:** win32hle ends the run on `RaiseException`. The frame registry is in place; the guest-chain walk is not.
- **DirectInput 7** (`DirectInputCreateEx`): win32hle has DirectInput 3/5 devices, but not the v7 entry point.
- **MCI:** mp3 music. **MCIWnd:** AVI cinematics.
- **Real DirectPlay:** Phase 3.
- **Sound:** run with a real audio driver and listen.
- **Determinism oracle:** recorded games.

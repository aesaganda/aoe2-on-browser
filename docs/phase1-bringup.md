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

### EULA

The game shows Microsoft's EULA through `EBUEulaX.dll` and exits unless the player accepts it. Accepting it is the player's decision, so the shim never accepts on its own:
- it returns "accepted" only when `AOE2_ACCEPT_EULA=1` is set;
- otherwise it points to `EULAX.RTF` on the Conquerors disc, and the game quits just as it does when the dialog is declined.

## Open

- **Game data source:** see the note in the session summary (2026-11-17). Not extracted yet.
- **SEH/C++ EH dispatch:** win32hle ends the run on `RaiseException`. The frame registry is in place; the guest-chain walk is not.
- **DirectInput 7, DirectPlay, MCI/MCIWnd:** not reached yet.

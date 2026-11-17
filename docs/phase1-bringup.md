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

### 2026-11-17: towards a game

Tool: `tools/native/session.sh` keeps a game session alive under Xvfb (`start … --hold`) and drives it step by step (`click` in game coordinates, `type`, `key`, `shot`).

| Step | Result | Fix |
|---|---|---|
| New Player | Typed text reaches win32hle's EDIT control (EN_CHANGE, GetWindowTextA) and the profile is created, but the text is **not drawn while typing** | Open |
| Single Player | "You must insert the … Conquerors Expansion CD". The game's own disc check: registry `CDPath` → `GetDriveTypeA` == CDROM → `GetVolumeInformationA` label | `src/linux/cdrom.c` presents the owner's disc image as D: (contents in `<root>/cdrom`, label `AGE2_X1` read from the image's ISO 9660 descriptor). This is the equivalent of mounting the image; nothing in the game is patched |
| Standard Game | Setup screen correct: Random Map, Coastal, 2 AI players | – |
| Start Game 1 | Miss 0x0060739E: a hand-written **inflate** routine (0x606xxx–0x607xxx, deflate table sizes 288/32/19) dispatches through `jmp dword ptr cs:[reg*4 + table]` | Lift seeds now include targets of `jmp/call [reg*4 + table]` in code. Segment-prefixed operands are always scanned, because pcrecomp's table recogniser misses the `cs:` prefix (to report upstream). Lift: 12,459 entries, 0 errors |
| Start Game 2 | **The process exits silently**: no dispatch miss, no unimplemented import, nothing in the log. Most likely a host signal (SIGSEGV) | Next: a crash handler that prints the guest EIP and pcrecomp's enter-trace, or run under gdb |

### 2026-11-17: in game

**D: failed.** While this step was being built, the drive holding the repo (Kioxia Exceria SSD) failed. Windows logged Ntfs 50 "Delayed Write Failed … data has been lost" and disk 51 paging errors, writes stopped and reads started to fail. The repo was cloned read-only, all 37 commits with a clean `git fsck`, into WSL's own disk (`~/aoe2-recomp-backup`), where work continues. The uncommitted work lost on D: was written again. The old remote is renamed `d-drive-failing`.

`tools/native/session.sh`:
- `AOE2_GDB=1` runs the game under gdb, which prints the host backtrace (lifted `sub_XXXXXXXX` frames), the guest registers and `g_cur_func` on a fatal signal;
- `to-game` clicks from the main menu to Start Game.

| Step | Result | Fix |
|---|---|---|
| Start Game 2 (gdb) | SIGSEGV in `sub_006070A6` at 0x6070FB `mov edi, [esi+0x8200]` with `esi = 8`, inside the hand-written inflate called from the compressed-file reader 0x004C5A20 | – |
| Diagnosis | The inflate is a **stack-copying coroutine**. When its window is full, 0x606E0D saves the registers, pops the guest stack down to its entry frame (return addresses included) into its state and `jmp`s on. The next call, 0x606E5D, pushes it back and resumes mid-call-chain. Its "done" exit (0x60702D → `jmp 0x60756F`) pops the outer function's frame from deep inside. In lifted code each guest function is a C function, so these exits return only from the innermost C frame, and the caller resumes with garbage registers | Not liftable as-is |
| Fix | HLE: `src/linux/aoc_inflate.c` replaces 0x006074CB (`inflate_step(in, in_off, &in_len, out, out_off, &out_len, state, flag)`, cdecl; `state+0xA958 == 0` starts a stream; returns the consumed and produced counts) with zlib raw inflate (`-15`). It is general: `tools/lift/overrides.txt` lists VAs whose lifted body is renamed `sub_X_lifted`, so the host's `sub_X` takes the direct calls and the dispatch entry. The binary links `-lz` | – |
| Start Game 3 | **A random-map game runs.** Town Center, villagers, resource bar (200/200/100/200), population 3/5, Dark Age, Celts. Selecting the Town Center and pressing `C` queues a villager: food 200 → 150, "Creating – 9%", and about 35 s later a fourth villager walks out (4/5) | – |

So the simulation, its timer, keyboard and mouse input in game, and sprite/terrain/UI drawing all work in the lifted build.

Also: lift + full native build on WSL's ext4 takes 2 min 24 s, much faster than over drvfs.

Seen but not fixed:
- the **minimap stays black**;
- **typed text does not show** in the New Player edit box (it reaches the control: the profile gets the name);
- the Town Center command panel shows some odd icons (to be checked against the original).

### 2026-11-18: text, minimap, chat

Tools added for this step:
- `make native-trace` builds the same binary with every lifted function's entry counted. `kill -USR2` once resets the counts; a second time writes `/tmp/aoe2-funcs.txt`. Two such windows, diffed, show which guest functions one action ran.
- `src/linux/ddraw_debug.c` adds a blit trace, surface dumps, and a page-protection watch on one back-buffer pixel.

| Problem | Cause | Fix |
|---|---|---|
| Typed text not drawn in edit boxes | The game uses real Win32 EDIT child windows over its DirectDraw picture. win32hle keeps their state but nothing draws them on the primary | `src/linux/edit_overlay.c` draws each visible EDIT's text and caret on the primary (SDL_ttf). Patch 0002 adds the window queries it needs |
| Minimap black (not even its frame) | The minimap panel (draw at 0x4484B0) left early because its diamond mask had not loaded. The mask is `map800.bmp`, resource 50411 in `interfac.drs` 'bina'. 0x4B03F0 copies it to `GetTempFileNameA(GetTempPathA())` and opens it with `OpenFile`. win32hle's temp path, `C:\WINDOWS\TEMP\`, did not exist under the install root | `main.c` creates `windows/temp` under the install root. Checked with the `marco` cheat: the minimap draws the whole map (terrain, water, forest, resources). Before that, black is just unexplored land |
| Chat: after Return the game stays "Game Paused" and the picture freezes | Single-player chat pauses the game (comm state `+0x1E3C` 4). Return does unpause it (0x4309D0 → state 5). But the chat EDIT is destroyed while it has focus, and win32hle set the focus to none. The game's key dispatch (0x420BF0) only takes keys when `GetFocus()` is its window or a child, so input was dead, and the picture stayed frozen even with the game unpaused (not traced further: the fix below cured both) | Patch 0003: hiding or destroying the focus window moves the focus to its parent, with WM_KILLFOCUS/WM_SETFOCUS, as Windows does. Chat, Return and cheats work and the game resumes |

**Sound and music.** The game decodes its mp3 music (`sound\stream\*.mp3`) itself, in its own stream code around 0x52EC10, and plays it through DirectSound. MCI is only asked to open the CD-audio device by type id (`MCI_OPEN_TYPE_ID`), which a data-only disc does not have either. Checked with SDL's `disk` audio driver (`SDL_AUDIODRIVER=disk SDL_DISKAUDIOFILE=…`): 31 s at the main menu recorded 44.1 kHz stereo with signal in 300 of 308 tenth-second windows. That is the menu theme.

The command panels match the original. Town Center: Create Villager, Loom, Feudal Age, Ungarrison, Town Bell. Villager: Build Economic, Build Military, Repair, Garrison.

## Open

- **SEH/C++ EH dispatch:** win32hle ends the run on `RaiseException`. The frame registry is in place; the guest-chain walk is not.
- **DirectInput 7** (`DirectInputCreateEx`): win32hle has DirectInput 3/5 devices, but not the v7 entry point.
- **MCIWnd:** AVI cinematics.
- **Real DirectPlay:** Phase 3.
- **Determinism oracle:** recorded games.

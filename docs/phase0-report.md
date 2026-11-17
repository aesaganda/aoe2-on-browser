# Phase 0 report: recon, lifter choice, prototype

- **Date:** 2026-11-17
- **Branch:** `phase0`
- **Input:** `age2_x1.exe` (AoC), SHA-256 `29341dbe…88ca8`, built by the owner from their own disc with the CD check removed (ADR-0002)
- **Recommendation: GO for Phase 1**, with a checkpoint at "boots to the main menu natively" (see §6).

## 1. What was done

| Task | Result | Where |
|---|---|---|
| Repo skeleton, ADR format | ✔ | `README.md`, `docs/adr/0000-template.md`, ADR-0001, ADR-0002 |
| Binary recon (pefile, capstone) | ✔ one command: `make recon` | `docs/recon.md`, `docs/imports.md` |
| Ghidra headless (12.2, JDK 25) | ✔ one command: `make ghidra` (≈3.5 min) | `docs/ghidra.md` |
| Lifter evaluation | ✔ pcrecomp chosen by the owner | `docs/adr/0001-lifter.md` |
| Whole-image lift | ✔ `make lift` (40 s) | `build/lift/gen` (ignored) |
| Independent differential harness | ✔ `make test` | `tests/difftest/` |
| 10-20 leaf prototype | ✔ **20/20 pass** | `tests/aoe2_prototype.txt` |
| Sweep over all 1,349 leaves | ✔ **1,229 pass, 0 failures in game code** (§4.3) | `make sweep` |
| CI | ✔ workflow (synthetic PE + pcrecomp self-test; no binary in CI) | `.github/workflows/ci.yml` |
| Optional: Wine + Xvfb screenshot | ✘ not done (§7) | |

## 2. Binary findings

**Shape.**
- MSVC 6: cl 12.00.8168/8447, link 6.00.8034.
- Statically linked CRT and **no RTTI**.
- Image base 0x400000 with relocations stripped, so it must load at that base. That is good for static recompilation: every absolute address is final.
- No TLS, no debug directory.
- Sections: `.text` (2.2 MB), plus a second code section `this_cod` (47 KB) with RW companions `this_dat`/`inf32dat`. These come from a library linked as its own segment and are not yet identified.
- A 45-byte overlay, most likely left by the no-CD patching tool and harmless.

**Protection.**
- No packer, no SafeDisc/SecuROM sections or markers.
- `.text` entropy is 6.5, which is ordinary code.
- The import table is complete, and Ghidra found **no writes into code**.
- The CD-check APIs are still referenced (`GetDriveTypeA` ×2, `GetVolumeInformationA` ×1). The shim should answer them benignly.

**Version.**
- Version resource: "Age of Empires II Expansion", FileVersion 00.07.22.0627, link date 2000-06-27.
- Registry root: `Software\Microsoft\Microsoft Games\Age of Empires II: The Conquerors Expansion\1.0`.
- The exe carries the strings 1.0a–1.0d (multiplayer version compatibility), so the exact patch letter is not confirmed from the bytes.

**Code.**
- 7,456 functions and 556k instructions; median size 73 bytes, largest 13.8 KB.
- **Indirect calls: 7,466.** 81% go through vtables or struct function pointers, 14% through the IAT, and 5% through registers. A guest-address → function dispatch is mandatory.
- **Indirect jumps: 572.** Ghidra recovered tables for 556 of them. The other 16 are virtual tail calls (`jmp [reg+disp]`) and CRT unwind code.
- **18.2% of the code bytes belong to no Ghidra function** (1,986 runs). This is code reached only through pointers. pcrecomp's driver found 216 extra entries by following direct branches, but code reached only through vtables needs runtime miss-logging plus re-lift (§6).

**FPU and SIMD.**
- **x87 in 963 functions (13%); MMX: none; SSE: none.**
- All 20 `fldcw` sites are inside the CRT (`_ftol`, `_controlfp`). The game therefore runs at the default control word 0x027F: 53-bit mantissa, round to nearest.
- Unit coordinates are `float`, converted to integers through `_ftol` at 341 call sites.
- **Consequence:** modelling x87 as IEEE `double` (what pcrecomp does) is exact for this binary, except for extended-exponent edge cases and 18 transcendental/rounding instructions (`fsin` ×4, `fcos` ×4, `fpatan` ×3, `fprem` ×4, `fscale` ×2, `frndint` ×1).

**Exceptions.**
- 624 functions install SEH frames (`_except_handler3` or `__CxxFrameHandler`), and there are 714 C++ EH FuncInfo records.
- Only one `RaiseException` reference exists, so exceptions are probably rare at runtime. The unwinding model still has to be right.

**Threads and timing.**
- **No `CreateThread` import.** There is one mutex, one semaphore and two `WaitForSingleObject` calls; the remaining threads live inside DirectX.
- **`timeGetTime` is the only clock** (222 references): no QPC, no GetTickCount. That gives the lockstep simulation a single time source to virtualise.

**DirectX/COM** (from GUID cross-references in code, which are reliable):

| API | Interfaces actually used | Notes |
|---|---|---|
| DirectDraw | **IDirectDraw v1**, IDirectDrawSurface v1, Palette, Clipper | Never queries DDraw2/4/7. 8-bit, CPU-drawn |
| DirectSound | **IDirectSound v1**, IDirectSoundBuffer | No 3D sound, no capture |
| DirectPlay | **IDirectPlay4** (CoCreateInstance and DirectPlayCreate+QI), Lobby 2A/3A | Service providers: TCP/IP, IPX, modem |
| DirectInput | **IDirectInput7A** via `LoadLibrary("dinput.dll")` + `DirectInputCreateEx` | Not in the import table |
| Other dynamic loads | `EBUEulaX.dll!EBUEula` (EULA dialog), `GetDiskFreeSpaceEx`, `IsProcessorFeaturePresent` | |

Recon lists the method-level usage as a **lower bound**. Static attribution through struct fields is noisy, so the Phase 1 shim should log every vtable slot it is asked for.

**Media.**
- `mciSendCommandA` ×12 plays music: `%s*.mp3` (AoC) and `music%d.mid`.
- `MCIWndCreateA` plays the AVI cinematics.
- `mixer*` handles volume.

## 3. Lifter choice (ADR-0001)

**pcrecomp** (MIT, pinned at `a9fe1aa`, git submodule) is the lifter and runtime base. Our own harness checks it.

**Alternatives.**
- **Our own lifter in the mp2.dk style** is the documented fallback.
- **Theseus** was rejected because it is Rust with no Emscripten path.
- **M-HT SRW** was rejected: its output is asm/LLVM IR, not C, and its symbolisation needs relocations.
- **remill** was rejected because it is only a library and its CFG tools are AGPL or archived.
- **NFSU2Recomp** was rejected because its licence is now "all rights reserved".

What decided it:
- pcrecomp already lifts 32-bit MSVC PE files to C.
- It models x87 as `double` under PC=53, which matches this binary (§2).
- It handles SEH and C++ EH funclets and has jump-table recovery.
- Its `win32hle` implements **234 of AoE2's 255 imports by name**, including DirectDraw 1, DirectSound, DirectInput devices, GDI, registry and COM.
- It already has an Emscripten path (`guest_mem.c`, `musttail` for wasm tail calls).

## 4. Prototype results

### 4.1 Whole-image lift

The lift took 40 s:

| Metric | Value |
|---|---|
| Lifted entries | **7,672** (7,456 from Ghidra + 216 found by the driver) |
| Lift errors | **0** |
| Unimplemented instructions | **2** (`fnstenv`, `fldenv`, once each, in a CPU/FPU probe) |
| Bodies without a terminator | 4 |
| C++ catch funclets | 13 |
| Output | 53.6 MB C in 20 files |
| Compiles (gcc 13, x86-64, -O0, 20 jobs) | **20/20 files, 0 errors**, 41 s |

Not attempted yet: linking a whole program (needs the shims) and an Emscripten compile.

### 4.2 Differential harness (ours)

`tests/difftest/` is ours, independent of pcrecomp's tester; only the C ABI is shared.

**How a test runs.**
- The oracle is Unicorn running the original bytes, with the whole PE image mapped, a 256 KB heap and a 64 KB stack.
- The FPU control word is set to the Windows default, 0x027F.
- Both machines start from the same random state, drawn from two alternating profiles:
  - **mixed:** pointers into the heap, globals and stack; edge integers; floats including halves, ±0, NaN, ±inf and denormals; noise;
  - **dense:** pointer-dense memory and small indices, for object graphs and tables.
- Stack arguments are biased toward pointers and small indices, and sometimes alias each other.

**What is compared.**
- The 8 GPRs, including `esp`, so `ret N` is checked.
- Every byte written anywhere in mapped memory.
- The x87 stack, depth and values, **bit for bit**.
- EFLAGS are not compared at function exit, because MSVC code never reads flags across `ret`. pcrecomp's own suite checks flags per instruction (380/380 pass on Linux).

**Verdicts.**
- A case that faults or times out on *both* sides is not a verdict.
- A fault on only one side is a failure.
- A function needs ≥30 inputs that completed on both sides. If it falls short, further batches run with new seeds; the bar is never lowered.

**The harness is itself tested.**
- On a synthetic PE (mingw, `-march=pentium -mfpmath=387`): 14 leaves pass.
- Three mutation tests plant a bug in lifted code (integer, x87 and memory) and must be caught. **All 3 are caught.**
- This binary-free path is what CI runs.

**Accepted divergences** are counted, not failed:

| Name | What | Why acceptable |
|---|---|---|
| `fnstsw-top` | pcrecomp's `fnstsw` leaves the x87 TOP field (status bits 11-13) at 0 | Documented by pcrecomp. Every MSVC compare idiom tests only C0-C3 |
| `nan-payload` | With two NaN operands, the x87 keeps the larger significand while host SSE keeps the first operand | Only the payload differs, and the game cannot observe it without bit-casting a NaN |
| `div-no-trap` | x86 raises #DE when a quotient overflows; the lifted division wraps | Reachable only by inputs on which the original would crash |

### 4.3 Results

**The 20 prototype functions:** 5 x87, 3 string, 3 loop, 3 mul/div, 3 bitwise and 3 plain, chosen by `tools/lift/pick_leaves.py`, which spreads them over the address space.

**20/20 pass**, 600 random states each:

| Function | Kind | Inputs agreeing | Divergent | Accepted divergences (this run) |
|---|---|---|---|---|
| 0x0040BDF0 | x87 | 172/600 | 0 | – |
| 0x00494BC0 | x87 | 39/600 | 0 | fnstsw-top ×3 |
| 0x004B2C70 | x87 | 345/600 | 0 | fnstsw-top ×1 |
| 0x0055A990 | x87 | 72/600 | 0 | – |
| 0x005E8590 | x87 | 99/600 | 0 | – |
| 0x0040A720 | string | 306/600 | 0 | – |
| 0x004AA100 | string | 179/600 | 0 | – |
| 0x00531F60 | string | 154/600 | 0 | – |
| 0x0040EE00 | loop | 169/600 | 0 | – |
| 0x004A04F0 | loop | 294/600 | 0 | – |
| 0x00593860 | loop | 81/600 | 0 | – |
| 0x00425BC0 | mul/div | 172/600 | 0 | – |
| 0x00576680 | mul/div | 600/600 | 0 | – |
| 0x005ECA20 | mul/div | 304/600 | 0 | – |
| 0x004067F0 | bitwise | 93/600 | 0 | – |
| 0x00487E80 | bitwise | 43/1800 (3 batches) | 0 | – |
| 0x005A8D20 | bitwise | 171/600 | 0 | – |
| 0x00405DD0 | plain | 315/600 | 0 | – |
| 0x00478B80 | plain | 322/600 | 0 | – |
| 0x005792C0 | plain | 553/600 | 0 | – |

**Reading the table.**
- "Inputs agreeing" counts the random states that ran to completion on both machines with identical results.
- The other states were refused by *both*, mostly because a random pointer chain reached an unmapped address.
- Which accepted divergences show up depends on the random seed. Across runs we also saw `nan-payload` in 0x005E8590 and `div-no-trap` in 0x005ECA20.

**Sweep over every leaf** (`make sweep`: 1,349 functions, 150 random states each, about 13 min on 20 cores):

| Verdict | Functions | Meaning |
|---|---|---|
| **pass** | **1,229 (91.1%)** | ≥10 states completed on both sides, and every one matched |
| thin | 112 (8.3%) | No divergence, but fewer than 10 states completed (they need specific object graphs) |
| fail | 7 (0.5%) | Explained below. **None of them is game code** |
| error | 1 | 0x61DD3C has an SEH frame; the harness does not link the EH runtime yet |

Accepted divergences were seen in 15 functions (`fnstsw-top`), 13 (`nan-payload`) and 3 (`div-no-trap`).

The 7 failures:

| Function | What | Class |
|---|---|---|
| 0x0048547C | Ghidra "function" that starts mid-body: it uses the parent's `ebp` and frame | Not a function, so a harness artifact. In the whole-image lift it is an alias inside its parent |
| 0x0060756F | A shared epilogue (`pop ×6; ret`) | Same: not a function |
| 0x006134A0 | CRT; jumps into code outside its Ghidra range (0x613F66) | Harness range artifact |
| 0x00618397, 0x006183A5, 0x006183D7 | CRT `_statusfp`/`_clearfp`-style: `fnstsw` with no preceding compare | **Real lifter limit:** pcrecomp builds the status word from the last compare and does not model the exception flags (IE/ZE/OE/UE/PE) |
| 0x0061CD96 | CRT `fmod` helper: 80-bit (`tbyte`) loads/stores and `fprem` | **Real lifter limit:** the `double` model cannot round-trip 80-bit values |

**Scope of the real limits.** Only 5 functions in the whole binary use 80-bit memory operands, and only 5 store the FPU status word to memory. **All of them are CRT code** (0x61xxxx). In Phase 1 these CRT routines get host implementations instead of lifted bodies. pcrecomp has hooks for this: `recomp_lookup_manual` and the `runtime/hybrid` boundary for keeping the CRT real.

Fixing the harness while the sweep ran mattered. The first sweep reported 13 failures, and 6 of them were harness artifacts:
- a fixed instruction budget that the oracle's and the lifted side's timeouts did not share;
- x87 arguments that the CRT helpers (`_ftol`) never received;
- x87 overflow caused by pre-loading a caller value under functions that use all 8 registers. QEMU does not model x87 stack faults, so the two sides compared garbage with garbage;
- NaN payloads copied through integer registers.

During development the harness found bugs **of its own**, and each was fixed:
- the x87 read-back ran under the escape hook;
- the input distribution was too poor for argument pointers;
- NaN checks were blind to bytes the CPU left unchanged;
- x87 inputs ignored the function's x87 shape (see the sweep notes).

Each one was diagnosed before any difference was attributed to the lifter.

## 5. Shim inventory for Phase 1

Columns:
- **win32hle** says how much of each API family pcrecomp already provides, matched by name only.
- **Effort** is person-weeks for one engineer who knows Win32 and this codebase. It covers integration and verification, not just writing code.

| Area | APIs (imports) | win32hle | Work in Phase 1 | Effort |
|---|---|---|---|---|
| Runtime core: memory at 0x400000, dispatch, loader, SEH/C++ EH unwinding | n/a | recomp32 | Integrate. Test SEH/EH paths with targeted cases. Add dispatch-miss logging and re-lift seeding for the 18% unowned code | 3-4 |
| KERNEL32: heap, VirtualAlloc, files/paths, sync, locale | 110 | 107 | Case-insensitive paths over the game folder; fill 3 gaps | 1-2 |
| USER32: window, message pump, input, timers | 76 | 67 | Single window and message loop; mouse capture and edge scroll; fill 9 small gaps | 2 |
| GDI32: text into 8-bit surfaces, palettes | 28 | 27 | Font metrics and glyph fidelity (UI layout depends on `GetTextExtentPoint32`) | 2-3 |
| DirectDraw 1: surfaces, palette, clipper | 1 + COM | yes | Verify Lock/Blt/BltFast/Flip semantics on AoE2; 8-bit present path | 1-2 |
| DirectSound 1 | 1 + COM | yes | Verify buffer Lock/Play/position; mixing | 1 |
| DirectInput 7 (dynamic) | `DirectInputCreateEx` | devices yes, `Ex`/v7 no | Add the v7 entry point and interface | 0.5-1 |
| WINMM MCI: mp3 music, MIDI | 12 | 11 | Implement `mciSendCommandA` (mp3 through a decoder such as minimp3); mixer | 1-2 |
| MSVFW32 `MCIWndCreateA`: AVI cinematics | 2 | 0 | Phase 1: skip videos. Later: decode AVI (codec to be identified) | 0.5 (skip) / 3+ |
| DirectPlay 4 + Lobby | 3 + COM | 0 | Phase 1: report "no providers" so single-player works. The real one is Phase 3 | 0.5 |
| ADVAPI32 registry, IMM32, VERSION, ole32, WSOCK32, EBUEula | 26 | 24 | File-backed registry; stubs | 0.5-1 |
| Game data and boot | n/a | n/a | Install layout from the owner's ISO (DRS/DAT); boot to the main menu; start a scenario | 1-2 |
| Determinism oracle | n/a | n/a | Recorded-game (`.mgx`) replay comparison against the original under Wine; x87 transcendentals (§6) | 2-3 |

**Phase 1 total: about 16-24 person-weeks** for a native Linux build that plays single-player and replays recorded games without desync. The confidence is low (±50%). The biggest unknowns are runtime dispatch misses, SEH behaviour, and GDI text fidelity.

For context only, very rough:
- **Phase 2** (Emscripten, Canvas/WebAudio/OPFS, Asyncify or worker) is 6-10 weeks.
- **Phase 3** (DirectPlay → WebRTC plus relay) is 4-8 weeks.

## 6. Risks

| # | Risk | Likelihood / impact | Mitigation |
|---|---|---|---|
| R1 | **Distributing a browser build means distributing code derived from the game** (lifted C / wasm). ADR-0002 keeps it out of the repo, but a *hosted* browser port is a distribution question, not a technical one | certain / high | **Owner's decision needed before Phase 2.** Options: personal use only (each user builds from their own exe), a local build pipeline, or rights from the rights holder. *This is the licensing uncertainty to flag, per the brief.* |
| R2 | pcrecomp: single maintainer, commits daily; some code still says "XWA recompiler" | high / medium | Pin, fork when needed, upstream fixes. Our harness and the synthetic CI do not depend on its tests |
| R3 | Desync from x87 transcendentals. **Unicorn (QEMU softfloat) is not a bit-exact oracle for them** | medium / high for multiplayer | See the breakdown below this table |
| R4 | Dispatch misses: 6,029 vtable calls, and 18% of the code is reached only through pointers | high / medium | Seed entries from the 6,808 code pointers in data. Log misses at runtime and re-lift. Consider an interpreter fallback, as ps2-web-recomp does |
| R5 | SEH and C++ EH (624 frames, 714 FuncInfo) wrong on rare paths | medium / medium | Targeted tests on the `_except_handler3` and `__CxxFrameHandler` paths; crash-time tracing |
| R6 | Code size: 53.6 MB of C means slow builds and a large wasm | high / low-medium | Split compilation, -O2 on hot paths only, dead-code reach analysis |
| R7 | Browser model: blocking `GetMessage`, `WaitForSingleObject` and synchronous file I/O | certain / medium (Phase 2) | Asyncify or a worker with SharedArrayBuffer (needs COOP/COEP headers); OPFS sync access handles |
| R8 | Input binary is a user-patched no-CD build; the bytes differ from retail at unknown places | low / low | The exe hash is recorded. Multiplayer compatibility with stock 1.0c clients is not a goal here |
| R9 | Method-level DirectX usage is only a lower bound (static analysis) | medium / low | The shim logs every vtable slot hit and fails loudly on unimplemented ones |

**R3 in detail.**
- **Where the instructions are.** Game code: `fsin`/`fcos` in 4 functions (0x438930, 0x438AF0, 0x48DE00, 0x5438E0) and `fpatan` in 2 (0x4934A0, 0x545130). Three of the six have no direct caller, so they are virtual methods. The rest are in the CRT (`atan`, `fmod`, `pow`/`exp`).
- **Mitigation.**
  - Trace at runtime whether these run in the simulation or only in rendering.
  - If they run in the simulation, emulate the x87 algorithms or tabulate the results.
  - Validate with recorded games against real hardware (Wine on x86).

## 7. Not done / open

- **Wine + Xvfb screenshot (optional).** Not attempted: it needs Wine 32-bit in WSL plus installing the game data from the owner's ISO under Wine (running the installer). It is cheap to do first thing in Phase 1, where it doubles as the runtime/determinism oracle (R3).
- **Not yet attempted:** an Emscripten compile of the lift, and a full link.
- **Leaves only.** The prototype and the sweep cover leaf functions. Calls between lifted functions, dispatch, and SEH unwinding are first exercised in Phase 1.
- **Unicorn is not a perfect oracle.** It is QEMU-based: no x87 stack faults, and transcendental instructions go through host libm. Hardware-exact checks for those need real x86 execution (R3).
- **Unknown:** the identity of the `this_cod`/`inf32dat` library.

## 8. Go / no-go

**GO**, because:
- the binary is clean (no protection, no SMC, no relocations, no MMX/SSE);
- the API surface is the "easy" DirectX: DDraw 1, DSound 1, a CPU-drawn 8-bit renderer;
- the chosen lifter lifts the whole image with 0 errors into C that compiles;
- 20/20 sampled functions match the CPU bit for bit, apart from three explained divergences;
- across all 1,349 leaves, 91% pass and the rest are either "no counterexample, thin coverage" or explained CRT/fragment cases. No game-code divergence was found.

**Conditions:**
1. **Checkpoint after about 4 weeks:** the native build reaches the main menu. If it doesn't, revisit ADR-0001.
2. Settle R1 (distribution model) before Phase 2 work starts.
3. Stand up the recorded-game determinism oracle early in Phase 1 (R3).

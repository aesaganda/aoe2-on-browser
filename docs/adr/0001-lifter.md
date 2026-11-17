# ADR-0001: x86 → C lifter

- **Status:** proposed (needs the owner's decision; see "Decision")
- **Date:** 2026-11-17
- **Deciders:** project owner

## Context

What the lifter has to handle, from `docs/recon.md` and `docs/ghidra.md`:

- **Binary shape.** MSVC 6 (cl 12.00.8168/8447), statically linked CRT, image base 0x400000. No relocations, no packer, no self-modifying code found.
- **Size.** 7,456 functions and 556k instructions.
- **Indirect calls.** 7,466 in total: 81% vtable or struct function pointers, 14% IAT, 5% register.
- **Indirect jumps.** 572; Ghidra recovered switch tables for 556 of them.
- **Data in code.** 54 KB of jump tables inside `.text`.
- **Unowned code.** 18% of the executable bytes belong to no Ghidra function. These are entry points reached only through pointers, and the lifter must find them itself.
- **x87.** 963 functions (13%) use it. There is **no MMX and no SSE**. All 20 `fldcw` sites are in the CRT (`_ftol` and `_controlfp` internals), so the game runs with the default precision-control word 0x027F (53-bit mantissa). The simulation stores positions as `float` and converts them with `_ftol` (341 call sites). A small number of `fsin`/`fcos`/`fpatan`/`fprem` instructions remain; their hardware approximations differ from libm.
- **Exceptions.** 624 functions install SEH frames, and there are 714 C++ EH FuncInfo records.
- **APIs.** DirectDraw 1 and DirectSound 1 only: the code never references IID_IDirectDraw2/4 or the DirectSound3D GUIDs. Also DirectPlay4 plus Lobby2A/3A, and DirectInput 7 loaded at runtime.
- **Target.** C that Emscripten compiles (Phase 2), and a native Linux build first (Phase 1).

Hard requirements: the lifted output is C; it compiles with Emscripten (32-bit guest address space, tail calls or a dispatcher instead of unbounded recursion); x87 results match the hardware bit for bit (lockstep multiplayer and recorded games must not desync); indirect calls go through a guest-address → host-function map with a defined behaviour for misses; and the licence lets us ship the runtime.

## Options considered

Activity dates were checked on 2026-11-17. ✔ = verified in the source, ~ = from the author's docs, not verified by us.

| | Own Python lifter (mp2.dk style) | **pcrecomp** (sp00nznet) | Theseus (evmar) | M-HT SRW | remill (+ own CFG) | NFSU2Recomp |
|---|---|---|---|---|---|---|
| Output | C | C ✔ | **Rust** | asm / llasm → LLVM IR | LLVM IR | C |
| PE32 support | to build | yes ✔ (also NE/LE/Mach-O) | yes | yes (SRW) | instruction semantics only | one game |
| EFLAGS | to build (liveness) | lazy, setter→consumer pattern match ✔ | eager per insn | liveness pass | per insn in IR | lazy + liveness ~ |
| x87 | to build | `double` model with PC=53 ✔; 80-bit claimed for `lift32_cpu` ~ | `f64`, PC/RC "not honoured" ~ | `double _st[8]` ~ | not checked | `double` ~ |
| MMX / SSE | n/a (AoE2 uses none) | yes ✔ | MMX ~ | no MMX ~ | yes | yes ~ |
| Indirect calls | to build | sorted dispatch table + `recomp_lookup` ~ | cache + binary search; **panics** on a miss, needs reseeding | labels from relocs (AoE2 has none) | to build | O(1) table + interpreter fallback ~ |
| SEH / C++ EH | to build | `eh32.py` (funclets, `__except`) ✔ exists | ? | manual | to build | ? |
| Win32 / DirectX runtime | to build | `win32hle`: GDI, **DirectDraw 1/2/4**, DirectSound, COM, registry ✔ (≈16.5k lines of C) | Rust crates | per game, by hand | none | D3D9 → Metal only |
| Emscripten / wasm | ours | `__EMSCRIPTEN__` path in guest_mem ✔; `musttail` for wasm tail calls ✔; Theme Park "plays in Chrome" ~ | wasm32 via Rust, no Emscripten; author calls it buggy ~ | possible in principle | elfconv does AArch64 only | wasm fork unpublished |
| Differential test | ours | Unicorn-based `difftest.py`, per instruction and per function ✔ | ? | no | no | ? |
| License | ours | **MIT** ✔ (Unicorn GPL-2, test-time only) | Apache-2.0 | MIT / GPLv2+ / LGPL | Apache-2.0 (McSema/Anvill AGPL) | now "all rights reserved"; earlier MIT snapshot |
| Maintenance | us | very active (last commit 2026-11-16), single maintainer | very active, single maintainer | active, single maintainer | active | n/a |

Notes:

- **mp2.dk** ("Practical and Portable X86 Recompilation", Mathias Kærlev, 2014). This is a design reference only. It handled one small module (Cube World's terrain generator), tracked only a subset of flags, and listed dynamic targets by hand. Its code lives in `cuwo` (GPL-3.0).
- **Theseus** is the closest finished PE-to-web pipeline. It would mean switching the whole project from C/Emscripten to Rust and its wasm toolchain, and it has no fallback for unknown indirect targets.
- **M-HT SRW** is proven on PE games (Septerra Core, Battle Isle 3). Its symbolisation relies on relocations, which AoE2 has stripped. It needs heavy per-game `.sci` annotation and does not produce C.
- **remill** gives instruction semantics only. Control-flow recovery, dispatch, the runtime and an Emscripten recipe would all be ours. The CFG-recovery tools around it are AGPL and archived or unmaintained.
- **NFSU2Recomp** has changed licence. Its MIT snapshot is fine to read but is not a base to build on, and its runtime is D3D9/macOS.

### What we could not verify in this session

We read pcrecomp's source, but it was not run on AoE2: running a freshly cloned third-party tool was blocked by the session's permission policy. Its accuracy on this binary is therefore unproven. The per-claim marks above say what was checked by reading.

## Decision (proposed)

**Use pcrecomp (MIT) as the lifter and runtime base, pinned to a reviewed commit and vendored as a fork.** On top of it, keep:

1. our own AoE2 layer: the address map and overrides, plus the shims win32hle lacks (DirectPlay 4 and Lobby, DirectInput 7, MCIWnd video, MCI mp3 music), and the Emscripten presentation layer;
2. **our own independent differential harness** (`tests/`): Unicorn runs the original bytes as the oracle, and its random-state generator is ours, so the lifter's correctness is not judged only by its author's tests;
3. a **determinism suite** for x87: `_ftol` and the float-heavy sim functions compared bit for bit, plus a decision on how to reproduce `fsin`/`fcos`/`fpatan`.

**Fallback:** if pcrecomp fails the Phase 0 prototype on AoE2 leaf functions in ways that are not quick to fix, write our own lifter in the mp2.dk style and use pcrecomp only as a reference. AoE2's instruction mix is plain integer + x87 with no MMX/SSE, so this is feasible, but the work moves from integration to building.

## Consequences

- Phase 1 becomes mostly **integration and shim work**, not lifter construction. The effort estimate in `docs/phase0-report.md` assumes this.
- We depend on a single-maintainer project that changes every day. Pin a commit, upstream fixes, and be ready to own the fork.
- pcrecomp's x87 `double` model is correct for AoE2 given PC=53, except for extended-exponent edge cases and the transcendental instructions. These need dedicated tests.
- Unicorn (GPL-2) stays a test-time dependency and never ships.
- Revisit if the prototype shows systematic semantic bugs, or if pcrecomp's licence or maintenance changes.

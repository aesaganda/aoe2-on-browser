# ADR-0003: No guest exception dispatch for AoC (for now)

- **Status:** accepted
- **Date:** 2026-11-18
- **Deciders:** autopilot (by the owner's mandate); revisit if the trigger below fires

## Context

Phase 1 listed "SEH/C++ EH dispatch" as open. On a Linux host, win32hle's
`RaiseException` prints the code and ends the run, and its `RtlUnwind` does
nothing. pcrecomp has the missing half for its Windows host:
- `runtime/native32/eh32.c`: C++ throws dispatched from the guest's FuncInfo;
- `runtime/native32/seh32.c`: RaiseException walks the guest's fs:[0] chain.

Porting both is real work: guest/host pointer translation, no `IsBadReadPtr`, and
EXCEPTION_RECORD/CONTEXT built by hand. So first, what does `age2_x1.exe` raise?

Measured on the binary and the lift (2026-11-18):

| Question | Answer |
|---|---|
| Functions that install an exception frame | 724 (`mov fs:[0], esp`); 714 C++ FuncInfos |
| Functions with a `catch` / `__except` landing pad (lift's `RECOMP_EH_FRAME`) | 13, **all in the statically linked CRT** (0x613000+). Example: the `__try/__except` around WinMain in the startup code. The game's own C++ frames only run destructors on unwind |
| `RaiseException` call sites | **one**, 0x617E8E in 0x617C77. That is the CRT's floating-point exception raiser (0xC000008F…), reached from its math-error paths (0x6131DB, 0x615BA5), and only when an FP exception is unmasked |
| C++ `throw` (`_CxxThrowException` → RaiseException 0xE06D7363) | none. 0xE06D7363 appears only where CRT handler code compares against it (0x6137EE, 0x616E35, …) |
| `RtlUnwind` | one site, the CRT's `_global_unwind2`, reached only while dispatching an exception |

So the game never throws. The only way to raise is an unmasked FP exception, and
the game runs with them masked (the default). Hardware faults (access violations)
are host signals in a lifted program and would not reach guest handlers even with
seh32 ported (native32 does not offer them either).

## Decision

Do not port eh32/seh32 for AoC now. Keep win32hle's behaviour: `RaiseException`
reports the code and the calling guest function, then ends the run. Phase 1's
"SEH/C++ EH" item is closed as not needed.

## Consequences

- No dispatcher code to maintain or carry into the WASM build (Phase 2).
- If the FP path ever fires (an FP control word that unmasks exceptions, e.g.
  from a driver or a mod), the run ends with
  `[win32hle] RaiseException(0xC00000xx) from guest 0x…` instead of the CRT's
  math-error handling.
- Revisit trigger: any `RaiseException` report in a session log. The port is
  then a straight copy of native32's two files, with `ADDR()` for guest pointers
  and a range check for `IsBadReadPtr`. The registry half is already in
  `src/linux/runtime_glue.c`, and the lift already emits the 13 landing pads.

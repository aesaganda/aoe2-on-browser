#!/usr/bin/env python3
"""Lift the whole executable with pcrecomp (third_party/pcrecomp, pinned) into build/lift/gen.

    lift_image.py <exe> <ghidra.json> <outdir> [--stats build/lift/stats.json]

The function catalog comes from our Ghidra export. pcrecomp's driver adds every
direct branch/call target it finds that the catalog lacks, in rounds.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PC = ROOT / "third_party" / "pcrecomp" / "tools"
for d in ("lift", "pe", "disasm"):
    sys.path.insert(0, str(PC / d))

from drive32 import lift_image  # noqa: E402


def data_pointer_seeds(exe: str, funcs: list) -> list[int]:
    """Aligned dwords in data sections that point into code no catalogued function owns.

    Ghidra leaves 18% of age2_x1's code bytes outside any function: code reached only
    through pointers (C++ static initialisers in the CRT's __xc_a table, vtables,
    callbacks). Each such pointer becomes a lift entry.

    Pointers into the middle of a known body count too: MSVC6 keeps switch tables in
    .text, so a pointer in .rdata/.data to mid-body is a real entry Ghidra merged into a
    neighbour (pcrecomp's driver keeps both dispatchable, see find_splits). Immediates
    are taken from a linear sweep of all code, not only Ghidra's bodies: the WndProc of
    a window class (0x41E760) is stored by code that no Ghidra function owns."""
    import bisect
    import struct

    import pefile
    pe = pefile.PE(exe, fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    code = [(base + s.VirtualAddress, base + s.VirtualAddress + s.Misc_VirtualSize)
            for s in pe.sections if s.Characteristics & 0x20000000]
    entries = {int(f["entry"], 16) for f in funcs}
    bodies = sorted((int(lo, 16), int(hi, 16)) for f in funcs for lo, hi in f["ranges"])
    starts = [lo for lo, _ in bodies]

    def inside(a):
        i = bisect.bisect_right(starts, a) - 1
        return i >= 0 and bodies[i][0] < a <= bodies[i][1]
    def wanted(v):
        return v not in entries and any(lo <= v < hi for lo, hi in code)
    seeds = set()
    for s in pe.sections:
        name = s.Name.rstrip(b"\0").decode(errors="replace")
        # Data sections, plus code sections other than .text: age2_x1's `this_cod` is a
        # hand-written threaded-code sprite blitter whose dispatch tables (`jmp [ebx*4 +
        # 0x62F700]`) and fragment addresses live inside it. .text's own tables are
        # MSVC switch tables, which pcrecomp resolves inside their bodies.
        if (s.Characteristics & 0x20000000 and name == ".text") or not s.SizeOfRawData:
            continue
        data = s.get_data()
        for o in range(0, len(data) - 3, 4):
            v = struct.unpack_from("<I", data, o)[0]
            if wanted(v):
                seeds.add(v)
    # Code addresses held as immediates in code: `push offset fn` before _atexit,
    # RegisterClass's WndProc, enumeration callbacks, qsort comparators.
    import capstone
    from capstone import x86 as X
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    md.detail = True
    md.skipdata = True                        # keep going over bytes that do not decode
    mem = pe.get_memory_mapped_image()
    for lo, hi in code:
        for ins in md.disasm(bytes(mem[lo - base:hi - base]), lo):
            if not ins.id or ins.mnemonic.startswith(("j", "call", "loop")):
                continue                      # data bytes; branch targets are the driver's job
            if ins.mnemonic not in ("push", "mov"):
                continue                      # where code stores a function's address
            for op in ins.operands:
                if op.type == X.X86_OP_IMM and wanted(op.imm & 0xFFFFFFFF):
                    seeds.add(op.imm & 0xFFFFFFFF)
    resolved = {int(j["at"], 16) for f in funcs for j in f.get("ijmps", []) if j.get("refs")}
    seeds |= dispatch_table_targets(md, mem, base, code, wanted, owned=lambda a: a in resolved)
    return sorted(seeds)


def dispatch_table_targets(md, mem, base, code, wanted, owned=lambda a: False) -> set[int]:
    """Targets of `jmp/call [reg*4 + table]` anywhere in code, read from the table.

    Hand-written assembly in age2_x1 (an inflate routine at 0x606xxx, the sprite
    blitter) dispatches through tables Ghidra never resolved because no function owns
    that code. A table is read until an entry stops pointing into code (or 256 entries).
    Jumps whose table Ghidra resolved are skipped: those are MSVC switches, which
    pcrecomp resolves within the body (seeding their arms only adds alias stubs). An
    unresolved one inside a Ghidra body is scanned like any other (0x6074CB's table
    at 0x606C4C)."""
    from capstone import x86 as X
    out = set()

    def in_code(v):
        return any(lo <= v < hi for lo, hi in code)
    for lo, hi in code:
        for ins in md.disasm(bytes(mem[lo - base:hi - base]), lo):
            if not ins.id or ins.mnemonic not in ("jmp", "call") or len(ins.operands) != 1:
                continue
            # pcrecomp's table recogniser (disasm32._TABLE_JMP) misses a segment-prefixed
            # operand (`jmp dword ptr cs:[ebx*4 + 0x606c54]`, hand-written asm), so those are
            # seeded even when Ghidra resolved them
            if owned(ins.address) and ":" not in ins.op_str:
                continue
            op = ins.operands[0]
            if op.type != X.X86_OP_MEM or op.mem.index == 0 or op.mem.scale != 4 or op.mem.base != 0:
                continue
            table = op.mem.disp & 0xFFFFFFFF
            for k in range(256):
                off = table + 4 * k - base
                if not (0 <= off < len(mem) - 3):
                    break
                v = int.from_bytes(mem[off:off + 4], "little")
                if not in_code(v):
                    break
                if wanted(v):
                    out.add(v)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exe")
    ap.add_argument("ghidra_json")
    ap.add_argument("out")
    ap.add_argument("--stats")
    ap.add_argument("--only", help="comma-separated VAs: lift just these (prototype mode)")
    ap.add_argument("--no-data-seeds", action="store_true", help="do not add code pointers found in data as entries")
    a = ap.parse_args()

    g = json.loads(Path(a.ghidra_json).read_text())
    funcs = [f for f in g["functions"] if not f["external"] and not f["thunk"]]
    if a.only:
        want = {int(x, 16) for x in a.only.split(",")}
        funcs = [f for f in funcs if int(f["entry"], 16) in want]
    cat = {"functions": [{"address": int(f["entry"], 16), "name": f["name"], "entry_kind": "start"} for f in funcs]}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    catf = Path(a.out).with_suffix(".catalog.json")
    catf.write_text(json.dumps(cat))
    seeds = [] if (a.only or a.no_data_seeds) else data_pointer_seeds(a.exe, funcs)
    t = time.time()
    stats, entries = lift_image(a.exe, str(catf), a.out, seeds=seeds, log=lambda *x: print(*x, flush=True))
    stats["data_seeds"] = len(seeds)
    # lift_image leaves the header and the dispatch table to the caller (one table may span
    # several modules); with one module we write both here, in generate.py's format.
    out = Path(a.out)
    with open(out / "recomp_funcs.h", "w") as f:
        f.write("#pragma once\n#include <stdint.h>\n\n")
        f.write("".join(f"void sub_{va:08X}(void);\n" for va in entries))
    with open(out / "recomp_dispatch.c", "w") as f:
        f.write('#include "recomp_types.h"\n#include "recomp_funcs.h"\n\n')
        f.write("const recomp_dispatch_entry_t recomp_dispatch_table[] = {\n")
        f.write("".join(f"    {{ 0x{va:08X}u, sub_{va:08X} }},\n" for va in sorted(entries)))
        f.write(f"}};\nconst uint32_t recomp_dispatch_count = {len(entries)};\n")
    stats = dict(stats)
    stats["seconds"] = round(time.time() - t, 1)
    stats["entries"] = len(entries)
    stats["catalog"] = len(cat["functions"])
    out_bytes = sum(p.stat().st_size for p in Path(a.out).glob("*.c"))
    stats["c_bytes"] = out_bytes
    print(json.dumps({k: v for k, v in stats.items() if k != "unimplemented"}, indent=1))
    unimpl = stats.get("unimplemented") or {}
    if unimpl:
        print("unimplemented:", sorted(unimpl.items(), key=lambda kv: -kv[1] if isinstance(kv[1], int) else 0)[:30])
    if a.stats:
        Path(a.stats).write_text(json.dumps(stats, indent=1, default=str))


if __name__ == "__main__":
    main()

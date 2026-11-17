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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exe")
    ap.add_argument("ghidra_json")
    ap.add_argument("out")
    ap.add_argument("--stats")
    ap.add_argument("--only", help="comma-separated VAs: lift just these (prototype mode)")
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
    t = time.time()
    stats, entries = lift_image(a.exe, str(catf), a.out, log=lambda *x: print(*x, flush=True))
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

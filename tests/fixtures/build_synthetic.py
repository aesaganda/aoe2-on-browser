#!/usr/bin/env python3
"""Build tests/fixtures/synthetic.c into a 32-bit PE and describe its functions in the
same JSON shape as our Ghidra export, so lift_image.py and the harness take it as-is.

    build_synthetic.py <outdir>   -> <outdir>/synthetic.exe, <outdir>/synthetic.json
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CC = "i686-w64-mingw32-gcc"
NM = "i686-w64-mingw32-nm"
FLAGS = ["-O2", "-march=pentium", "-mno-sse", "-mfpmath=387", "-fno-builtin",
         "-fno-tree-loop-distribute-patterns", "-fno-asynchronous-unwind-tables", "-s"]
LEAVES = ["sum_array", "clamp", "hash_str", "fill_bytes", "mul_wide", "div_round", "rotl_mix", "bit_count",
          "lerp", "dot3", "float_to_int", "float_less", "move_unit", "dist2"]


def main(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    exe, unstripped = out / "synthetic.exe", out / "synthetic.sym.exe"
    flags = [f for f in FLAGS if f != "-s"]
    subprocess.run([CC, *flags, str(HERE / "synthetic.c"), "-o", str(unstripped)], check=True)
    subprocess.run([CC, *FLAGS, str(HERE / "synthetic.c"), "-o", str(exe)], check=True)
    # function bounds from the unstripped twin (identical code layout; -s only drops symbols)
    syms = []
    for line in subprocess.run([NM, "-n", str(unstripped)], capture_output=True, text=True, check=True).stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] in "tT":
            syms.append((int(parts[0], 16), parts[2].lstrip("_")))
    funcs = []
    for i, (a, name) in enumerate(syms):
        if name in LEAVES:
            end = syms[i + 1][0] if i + 1 < len(syms) else a + 0x100
            funcs.append({"entry": f"0x{a:08x}", "name": name, "external": False, "thunk": False,
                          "ranges": [[f"0x{a:08x}", f"0x{end - 1:08x}"]]})
    missing = set(LEAVES) - {f["name"] for f in funcs}
    if missing:
        raise SystemExit(f"symbols not found: {missing}")
    (out / "synthetic.json").write_text(json.dumps({"program": "synthetic.exe", "functions": funcs}, indent=1))
    print(f"synthetic: {len(funcs)} functions -> {exe}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

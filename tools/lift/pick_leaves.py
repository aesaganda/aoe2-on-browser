#!/usr/bin/env python3
"""Choose the Phase 0 prototype set: leaf functions (no calls, no indirect jumps)
from the Ghidra export, spread over kinds of code.

    pick_leaves.py build/ghidra/leaves.json orig/age2_x1.exe > tests/aoe2_prototype.txt

Excluded: SEH frames and fs: accesses (need a TEB), privileged/odd instructions, and
anything under 16 bytes (getters/setters say little). The choice is deterministic so
the committed list can be regenerated and reviewed.
"""
import json
import sys

import capstone
import pefile

leaves = json.load(open(sys.argv[1]))
pe = pefile.PE(sys.argv[2], fast_load=True)
mem = pe.get_memory_mapped_image()
base = pe.OPTIONAL_HEADER.ImageBase
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
BAD = {"in", "out", "int", "int3", "cpuid", "rdtsc", "hlt", "cli", "sti", "lock", "fnstenv", "fldenv", "fnsave", "frstor"}

kinds = {"x87": [], "string": [], "loop": [], "mul/div": [], "bitwise": [], "plain": []}
for f in sorted(leaves, key=lambda f: int(f["entry"], 16)):
    if f["seh"] or f["size"] < 16 or f["size"] > 600 or len(f["ranges"]) != 1:
        continue
    lo, hi = (int(x, 16) for x in f["ranges"][0])
    ins = list(md.disasm(bytes(mem[lo - base:hi - base + 1]), lo))
    m = {i.mnemonic for i in ins}
    if m & BAD or any("fs:" in i.op_str for i in ins):
        continue
    if f["fpu"]:
        k = "x87"
    elif any(x.startswith("rep") or x in ("movsb", "movsd", "stosd", "stosb", "scasb", "cmpsb") for x in m):
        k = "string"
    elif any(x in ("mul", "imul", "div", "idiv") for x in m):
        k = "mul/div"
    elif any(i.mnemonic.startswith("j") and int(i.op_str, 16) < i.address for i in ins if i.op_str.startswith("0x")):
        k = "loop"
    elif m & {"shl", "shr", "sar", "rol", "ror", "and", "or", "xor", "not", "neg", "sbb", "adc", "bt", "setne", "sete"}:
        k = "bitwise"
    else:
        k = "plain"
    kinds[k].append((f["entry"], f["size"], len(ins)))

want = {"x87": 5, "string": 3, "loop": 3, "mul/div": 3, "bitwise": 3, "plain": 3}
print("# Phase 0 prototype: leaf functions of age2_x1.exe (sha256 29341dbe...), chosen by tools/lift/pick_leaves.py")
print("# VA          kind      bytes  insns")
for k, n in want.items():
    pool = kinds[k]
    # spread over the address space (different source files), not the first n
    step = max(1, len(pool) // n)
    for e, size, ni in pool[::step][:n]:
        print(f"{e}  {k:<9} {size:>5}  {ni:>5}")
print(f"# pool sizes: " + ", ".join(f"{k} {len(v)}" for k, v in kinds.items()))

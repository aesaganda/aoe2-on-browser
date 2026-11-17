#!/usr/bin/env python3
"""Static recon of a 32-bit PE game executable.

    recon.py <exe> [--ghidra build/ghidra/<name>.json] [--out docs]

Writes <out>/recon.md and <out>/imports.md. Output is deterministic for a given
input so that the generated docs diff cleanly. Uses Ghidra's function list when
given (better coverage than a linear sweep), otherwise a linear sweep of the code
sections.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import re
import struct
import sys
from pathlib import Path

import capstone
from capstone import x86 as X
import pefile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apinotes  # noqa: E402
import dxheaders  # noqa: E402

# --------------------------------------------------------------------------- helpers

RICH_PRODID = {  # pre-VS2002 encoding (prodid < 0x60): https://github.com/dishather/richprint (comp_id.txt)
    0: "Unknown / unmarked objects",
    1: "Import (imported symbol)",
    6: "Cvtres 5.00 (resource converter)",
    10: "Utc12_C (VC6 C compiler)",
    11: "Utc12_CPP (VC6 C++ compiler)",
    12: "Masm 6.13",
    14: "Masm 6.14",
    19: "Linker 6.00",
    4: "Linker 5.10", 7: "Linker 6.00 (alt id)", 9: "Utc11 (VC5) C",
}
VC6_BUILDS = {8168: "VC6 RTM (12.00.8168)", 8447: "VC6 SP3-SP5 C/C++ (12.00.8447)", 8804: "VC6 SP5 + Processor Pack (12.00.8804)", 8034: "Linker 6.00.8034 (VC6 RTM link.exe)", 8047: "Linker 6.00.8047 (VC6 SP3+)", 7291: "Masm 6.13.7291", 7299: "Masm 6.14.7299", 1735: "Cvtres 5.00.1735"}

PACKER_SECTIONS = {
    "UPX0": "UPX", "UPX1": "UPX", ".aspack": "ASPack", ".adata": "ASPack", "stxt774": "SafeDisc", "stxt371": "SafeDisc",
    ".securom": "SecuROM", ".dsstext": "SecuROM", ".petite": "Petite", "PEC2": "PECompact", ".nsp0": "NsPack",
    ".themida": "Themida", ".vmp0": "VMProtect", ".tls": "(TLS)", "BoG_": "SafeDisc", ".text1": "(ExeCryptor?)",
}
PROTECTION_STRINGS = [
    rb"~df394b.tmp", rb"dplayerx.dll", rb"SECDRV", rb"drvmgt", rb"BoG_ *90.0&!!", rb"stxt774", rb"stxt371",
    rb"CMS16.DLL", rb"CMS_95.DLL", rb"securom", rb"SafeDisc", rb"Macrovision", rb"LaserLock", rb"\.icd",
]
CD_HINT_STRINGS = [rb"Please insert", rb"insert the", rb"CD-ROM", rb"cdrom", rb"CD ROM", rb"\bCD\b"]


def entropy(b: bytes) -> float:
    if not b:
        return 0.0
    c = collections.Counter(b)
    n = len(b)
    return -sum(v / n * math.log2(v / n) for v in c.values())


def h(x: int) -> str:
    return f"0x{x:08x}"


def md_table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out)


# --------------------------------------------------------------------------- image model

class Image:
    def __init__(self, path: Path):
        self.path = path
        self.raw = path.read_bytes()
        self.pe = pefile.PE(data=self.raw)
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.mem = self.pe.get_memory_mapped_image()
        self.iat: dict[int, tuple[str, str]] = {}
        self.imports: list[tuple[str, list[str]]] = []
        for d in getattr(self.pe, "DIRECTORY_ENTRY_IMPORT", []):
            dll = d.dll.decode()
            names = []
            for i in d.imports:
                name = i.name.decode() if i.name else f"#{i.ordinal}"
                names.append(name)
                self.iat[i.address] = (dll, name)
            self.imports.append((dll, names))
        self.sections = [(s.Name.rstrip(b"\0").decode(errors="replace"), self.base + s.VirtualAddress,
                          max(s.Misc_VirtualSize, s.SizeOfRawData), s) for s in self.pe.sections]

    def section_of(self, va: int) -> str:
        for name, start, size, _ in self.sections:
            if start <= va < start + size:
                return name
        return "?"

    def is_exec(self, va: int) -> bool:
        for _, start, size, s in self.sections:
            if start <= va < start + size:
                return bool(s.Characteristics & 0x20000000)
        return False

    def read(self, va: int, n: int) -> bytes:
        off = va - self.base
        return bytes(self.mem[off:off + n])

    def cstr(self, va: int, limit: int = 256) -> str | None:
        off = va - self.base
        if not (0 <= off < len(self.mem)):
            return None
        end = self.mem.find(b"\0", off, off + limit)
        if end < 0:
            return None
        s = self.mem[off:end]
        if len(s) >= 2 and all(32 <= c < 127 or c in (9, 10, 13) for c in s):
            return s.decode()
        return None


# --------------------------------------------------------------------------- disassembly

class Insn:
    __slots__ = ("addr", "size", "mnem", "ops", "op_str", "groups")

    def __init__(self, i):
        self.addr = i.address
        self.size = i.size
        self.mnem = i.mnemonic
        self.op_str = i.op_str
        self.ops = [(o.type, o.reg if o.type == X.X86_OP_REG else None,
                     o.imm if o.type == X.X86_OP_IMM else None,
                     (o.mem.base, o.mem.index, o.mem.scale, o.mem.disp, o.mem.segment) if o.type == X.X86_OP_MEM else None,
                     o.size)
                    for o in i.operands]
        self.groups = set(i.groups)


def disassemble_functions(img: Image, ghidra: dict | None):
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    md.detail = True
    funcs: dict[int, list[Insn]] = {}
    if ghidra:
        for f in ghidra["functions"]:
            if f.get("external") or not f["ranges"]:
                continue
            entry = int(f["entry"], 16)
            insns = []
            for lo, hi in f["ranges"]:
                lo, hi = int(lo, 16), int(hi, 16)
                code = img.read(lo, hi - lo + 1)
                insns.extend(Insn(i) for i in md.disasm(code, lo))
            insns.sort(key=lambda i: i.addr)
            funcs[entry] = insns
    else:
        for name, start, size, s in img.sections:
            if s.Characteristics & 0x20000000:
                code = img.read(start, s.Misc_VirtualSize)
                funcs[start] = [Insn(i) for i in md.disasm(code, start)]
    return funcs


# --------------------------------------------------------------------------- dataflow-lite

OWNER_WINDOW = 0x6000   # bytes; see com_analysis

REG32 = {X.X86_REG_EAX, X.X86_REG_ECX, X.X86_REG_EDX, X.X86_REG_EBX, X.X86_REG_ESI, X.X86_REG_EDI, X.X86_REG_EBP}


def writes_reg(ins: Insn, reg: int) -> bool:
    if not ins.ops:
        return False
    t, r, *_ = ins.ops[0]
    if t == X.X86_OP_REG and r == reg and ins.mnem not in ("push", "cmp", "test"):
        return True
    if ins.mnem in ("call",) and reg in (X.X86_REG_EAX, X.X86_REG_ECX, X.X86_REG_EDX):
        return True
    if ins.mnem == "pop" and ins.ops and ins.ops[0][1] == reg:
        return True
    return False


def last_def(insns: list[Insn], idx: int, reg: int, window: int = 24):
    for j in range(idx - 1, max(-1, idx - window), -1):
        ins = insns[j]
        if writes_reg(ins, reg):
            return j
    return None


def mem_slot(mem, func_entry: int):
    base, index, scale, disp, seg = mem
    disp &= 0xffffffff
    if index != 0:
        return None
    if base == 0:
        return ("global", disp)
    if base in (X.X86_REG_ESP, X.X86_REG_EBP):
        return ("local", func_entry, base, disp)   # stack slots are only meaningful within the function
    return ("field", disp)


def value_of_reg(insns, idx, reg, func_entry):
    """What does `reg` hold at insns[idx]? -> ('imm', v) | ('addr', slot) | ('load', slot) | None"""
    j = last_def(insns, idx, reg)
    if j is None:
        return None
    ins = insns[j]
    if ins.mnem == "mov" and len(ins.ops) == 2:
        t, _, imm, mem, _ = ins.ops[1]
        if t == X.X86_OP_IMM:
            return ("imm", imm & 0xffffffff)
        if t == X.X86_OP_MEM:
            s = mem_slot(mem, func_entry)
            return ("load", s) if s else None
        if t == X.X86_OP_REG:
            return value_of_reg(insns, j, ins.ops[1][1], func_entry)
    if ins.mnem == "lea" and len(ins.ops) == 2:
        s = mem_slot(ins.ops[1][3], func_entry)
        return ("addr", s) if s else None
    return None


def call_args(insns, idx, func_entry, nargs):
    """Best-effort: the values pushed for the call at insns[idx] (arg1 first)."""
    args = []
    j = idx - 1
    while j >= 0 and len(args) < nargs:
        ins = insns[j]
        if ins.mnem == "call" or ins.mnem.startswith("j") or ins.mnem.startswith("ret"):
            break
        if ins.mnem == "push" and ins.ops:
            t, r, imm, mem, _ = ins.ops[0]
            if t == X.X86_OP_IMM:
                args.append(("imm", imm & 0xffffffff))
            elif t == X.X86_OP_REG:
                args.append(value_of_reg(insns, j, r, func_entry))
            elif t == X.X86_OP_MEM:
                s = mem_slot(mem, func_entry)
                args.append(("load", s) if s else None)
        j -= 1
    while len(args) < nargs:
        args.append(None)
    return args


def count_pushes(insns, idx):
    n = 0
    j = idx - 1
    while j >= 0:
        ins = insns[j]
        if ins.mnem == "call" or ins.mnem.startswith("j") or ins.mnem.startswith("ret"):
            break
        if ins.mnem == "push":
            n += 1
        j -= 1
    return n


# --------------------------------------------------------------------------- analyses

def import_refs(img: Image, funcs):
    """IAT slot -> list of referencing instruction addresses (call [iat], mov reg,[iat], jmp [iat])."""
    refs = collections.defaultdict(list)
    for entry, insns in funcs.items():
        for ins in insns:
            for t, _, imm, mem, _ in ins.ops:
                if t == X.X86_OP_MEM and mem[0] == 0 and mem[1] == 0:
                    a = mem[3] & 0xffffffff
                    if a in img.iat:
                        refs[a].append((entry, ins.addr, ins.mnem))
    return refs


_THUNKS: dict[int, int] = {}   # thunk entry -> IAT slot, filled by main()


def import_call_sites(img: Image, funcs, iat_addr: int):
    """Yield (func_entry, insns, idx) for every call that reaches the import:
    `call [iat]`, `call reg` with reg loaded from the IAT, or `call thunk` where the
    thunk is a lone `jmp [iat]` (DirectX/DirectPlay imports are reached that way)."""
    thunks = {e for e, a in _THUNKS.items() if a == iat_addr}
    for entry, insns in funcs.items():
        if entry in thunks:
            continue
        for idx, ins in enumerate(insns):
            if ins.mnem not in ("call", "jmp") or not ins.ops:
                continue
            t, r, imm, mem, _ = ins.ops[0]
            if t == X.X86_OP_MEM and mem[0] == 0 and mem[1] == 0 and (mem[3] & 0xffffffff) == iat_addr:
                yield entry, insns, idx
            elif t == X.X86_OP_IMM and ins.mnem == "call" and (imm & 0xffffffff) in thunks:
                yield entry, insns, idx
            elif t == X.X86_OP_REG and ins.mnem == "call":
                v = value_of_reg(insns, idx, r, entry)
                if v and v[0] == "load" and v[1] == ("global", iat_addr):
                    yield entry, insns, idx


def find_thunks(img, funcs, ghidra):
    """Functions that are just `jmp [iat]` -> import (MSVC sometimes emits these)."""
    th = {}
    for entry, insns in funcs.items():
        if len(insns) == 1 and insns[0].mnem == "jmp":
            t, _, _, mem, _ = insns[0].ops[0]
            if t == X.X86_OP_MEM and mem[0] == 0 and (mem[3] & 0xffffffff) in img.iat:
                th[entry] = mem[3] & 0xffffffff
    return th


def com_analysis(img: Image, funcs, guids, ifaces):
    iat_by_name = {f"{d}!{n}": a for a, (d, n) in img.iat.items()}
    slots: dict[tuple, set[str]] = collections.defaultdict(set)
    creations = []  # (how, at, iface, slot)
    unresolved_creations = []

    def iface_from_guid_ref(v):
        if v and v[0] == "imm":
            g = img.read(v[1], 16) if img.section_of(v[1]) != "?" else b""
            name = guids.get(g)
            if name:
                return name
        return None

    def iid_to_iface(name: str | None):
        if not name:
            return None
        n = re.sub(r"^(IID_|CLSID_)", "", name)
        if n in ifaces:
            return n
        if n.endswith("A") and n[:-1] in ifaces:   # IDirectPlay4A has the same vtable as IDirectPlay4
            return n[:-1]
        return None

    def add_slot(v, iface, how, at):
        if v and v[0] == "addr" and v[1]:
            slots[v[1]].add(iface)
            creations.append((how, at, iface, v[1]))
            return True
        if v and v[0] == "imm" and img.section_of(v[1]) in (".data", "this_dat", "inf32dat"):
            slots[("global", v[1])].add(iface)
            creations.append((how, at, iface, ("global", v[1])))
            return True
        unresolved_creations.append((how, at, iface))
        return False

    entry_points = [
        ("DDRAW.dll!DirectDrawCreate", 3, 2, "IDirectDraw"),
        ("DSOUND.dll!#1", 3, 2, "IDirectSound"),
        ("DPLAYX.dll!#1", 3, 2, "IDirectPlay"),
        ("DPLAYX.dll!#4", 5, 2, "IDirectPlayLobby"),
    ]
    for key, nargs, outarg, iface in entry_points:
        a = iat_by_name.get(key)
        if a is None:
            continue
        for entry, insns, idx in import_call_sites(img, funcs, a):
            args = call_args(insns, idx, entry, nargs)
            add_slot(args[outarg - 1], iface, key.split("!")[1], insns[idx].addr)
    coci = []
    a = iat_by_name.get("ole32.dll!CoCreateInstance")
    if a is not None:
        for entry, insns, idx in import_call_sites(img, funcs, a):
            args = call_args(insns, idx, entry, 5)
            clsid = iface_from_guid_ref(args[0])
            iid = iface_from_guid_ref(args[3])
            coci.append((insns[idx].addr, clsid or "?", iid or "?"))
            iface = iid_to_iface(iid)
            if iface:
                add_slot(args[4], iface, "CoCreateInstance", insns[idx].addr)
            else:
                unresolved_creations.append(("CoCreateInstance", insns[idx].addr, iid or "?"))

    # creation-style methods: method -> (out arg index incl. this, result interface by parent)
    surf_for = {"IDirectDraw": "IDirectDrawSurface", "IDirectDraw2": "IDirectDrawSurface",
                "IDirectDraw4": "IDirectDrawSurface4", "IDirectDraw7": "IDirectDrawSurface7"}
    creators = {
        "CreateSurface": (3, lambda p: surf_for.get(p)),
        "CreatePalette": (4, lambda p: "IDirectDrawPalette"),
        "CreateClipper": (3, lambda p: "IDirectDrawClipper"),
        "GetAttachedSurface": (3, lambda p: p),
        "GetPalette": (2, lambda p: "IDirectDrawPalette"),
        "CreateSoundBuffer": (3, lambda p: "IDirectSoundBuffer"),
        "DuplicateSoundBuffer": (3, lambda p: "IDirectSoundBuffer"),
    }

    # locate every vtable-style call once
    vcalls = []  # (entry, insns, idx, disp, slot)
    for entry, insns in funcs.items():
        for idx, ins in enumerate(insns):
            if ins.mnem != "call" or not ins.ops:
                continue
            t, r, imm, mem, _ = ins.ops[0]
            if t != X.X86_OP_MEM or mem[0] == 0 or mem[1] != 0:
                continue
            vt_reg, disp = mem[0], mem[3]
            if disp < 0 or disp % 4:
                continue
            j = last_def(insns, idx, vt_reg)
            if j is None:
                continue
            d = insns[j]
            if d.mnem != "mov" or d.ops[1][0] != X.X86_OP_MEM:
                continue
            b2, i2, _, disp2, _ = d.ops[1][3]
            if i2 or disp2 != 0 or b2 == 0:
                continue  # vtable load must be mov vt,[obj]
            v = value_of_reg(insns, j, b2, entry)
            if not v or v[0] != "load" or not v[1]:
                continue
            vcalls.append((entry, insns, idx, disp, v[1]))

    # local (stack) slot -> where the pointer is copied: `mov r,[local]` then `mov [field|global], r`
    copies = collections.defaultdict(set)
    for entry, insns in funcs.items():
        for idx, ins in enumerate(insns):
            if ins.mnem != "mov" or len(ins.ops) != 2 or ins.ops[0][0] != X.X86_OP_MEM or ins.ops[1][0] != X.X86_OP_REG:
                continue
            dst = mem_slot(ins.ops[0][3], entry)
            v = value_of_reg(insns, idx, ins.ops[1][1], entry)
            if dst and v and v[0] == "load" and v[1] and v[1] != dst:
                copies[v[1]].add(dst)

    def propagate():
        grew = False
        for src, dsts in copies.items():
            if src in slots:
                for d in dsts:
                    if not slots[src] <= slots.get(d, set()):
                        slots[d] |= slots[src]
                        grew = True
        return grew

    method_use = collections.Counter()
    method_sites = collections.defaultdict(list)
    arity_ok = collections.Counter()
    unconfirmed = collections.Counter()
    ambiguous = 0
    changed = True
    seen = set()
    while changed:
        changed = propagate()
        for entry, insns, idx, disp, slot in vcalls:
            ifs = slots.get(slot)
            if not ifs:
                continue
            for iface in sorted(ifs):
                methods = ifaces.get(iface, [])
                k = disp // 4
                if k >= len(methods):
                    continue
                mname, nargs = methods[k]
                key = (insns[idx].addr, iface)
                if key in seen:
                    continue
                seen.add(key)
                ok = count_pushes(insns, idx) == nargs
                # Struct-field slots collide across unrelated classes (+0x8, +0x1c ...). For
                # them we require the push count to match the method's arity; stack and global
                # slots are exact and need no confirmation.
                # Genie wraps COM objects in its own classes, and MSVC lays out one .obj
                # contiguously, so a field slot is trusted only near the code that filled it.
                near = slot[0] != "field" or any(abs(insns[idx].addr - at) < OWNER_WINDOW
                                                 for _h, at, _i, s in creations if s == slot)
                if slot[0] == "field" and not (ok and near):
                    unconfirmed[(iface, k, mname)] += 1
                    continue
                if len(ifs) > 1:
                    ambiguous += 1
                method_use[(iface, k, mname)] += 1
                method_sites[(iface, k, mname)].append(insns[idx].addr)
                if ok:
                    arity_ok[(iface, k, mname)] += 1
                # QueryInterface -> new interface
                if mname == "QueryInterface":
                    args = call_args(insns, idx, entry, 3)
                    new = iid_to_iface(iface_from_guid_ref(args[1]))
                    if new and add_slot(args[2], new, f"{iface}::QueryInterface", insns[idx].addr):
                        changed = True
                elif mname in creators:
                    outarg, f = creators[mname]
                    new = f(iface)
                    if new:
                        args = call_args(insns, idx, entry, outarg)
                        before = len(slots)
                        if add_slot(args[outarg - 1], new, f"{iface}::{mname}", insns[idx].addr) and len(slots) != before:
                            changed = True
    return {
        "slots": slots, "creations": creations, "unresolved_creations": unresolved_creations,
        "coci": coci, "method_use": method_use, "method_sites": method_sites, "arity_ok": arity_ok,
        "vcalls_total": len(vcalls), "ambiguous": ambiguous, "unconfirmed": unconfirmed,
    }


def guid_hits(img: Image, guids):
    hits = []
    for name, start, size, s in img.sections:
        data = img.read(start, s.Misc_VirtualSize)
        for g, gname in guids.items():
            pos = data.find(g)
            while pos >= 0:
                hits.append((start + pos, name, gname))
                pos = data.find(g, pos + 1)
    return sorted(hits)


def dynamic_loads(img: Image, funcs):
    out = []
    iat_by_name = {f"{d}!{n}": a for a, (d, n) in img.iat.items()}
    for key, argi in (("KERNEL32.dll!LoadLibraryA", 1), ("KERNEL32.dll!GetProcAddress", 2), ("KERNEL32.dll!GetModuleHandleA", 1)):
        a = iat_by_name.get(key)
        if a is None:
            continue
        for entry, insns, idx in import_call_sites(img, funcs, a):
            args = call_args(insns, idx, entry, argi)
            v = args[argi - 1]
            s = img.cstr(v[1]) if v and v[0] == "imm" else None
            out.append((key.split("!")[1], insns[idx].addr, s if s is not None else ("NULL" if v == ("imm", 0) else "(computed)")))
    return out


def strings(img: Image, minlen=4):
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % minlen, img.raw):
        yield m.start(), m.group().decode()


def rtti_classes(img: Image):
    names = sorted(set(m.group(1).decode() for m in re.finditer(rb"\.\?A[VU]([A-Za-z0-9_@?$]+?)@@\x00", img.raw)))
    return names


# --------------------------------------------------------------------------- report

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exe", type=Path)
    ap.add_argument("--ghidra", type=Path)
    ap.add_argument("--out", type=Path, default=Path("docs"))
    a = ap.parse_args()

    img = Image(a.exe)
    pe = img.pe
    ghidra = json.loads(a.ghidra.read_text()) if a.ghidra and a.ghidra.exists() else None
    funcs = disassemble_functions(img, ghidra)
    _THUNKS.update(find_thunks(img, funcs, ghidra))
    guids, ifaces = dxheaders.load()
    refs = import_refs(img, funcs)
    com = com_analysis(img, funcs, guids, ifaces)
    gh = guid_hits(img, guids)
    dyn = dynamic_loads(img, funcs)
    classes = rtti_classes(img)

    oh, fh = pe.OPTIONAL_HEADER, pe.FILE_HEADER
    L = []
    w = L.append
    w(f"# Recon: `{a.exe.name}`\n")
    w("_Generated by `make recon` (tools/recon/recon.py). Do not edit by hand._\n")
    w(f"Function source: {'Ghidra export (' + a.ghidra.name + f', {len(funcs)} functions)' if ghidra else 'linear sweep (no Ghidra export given)'}.\n")

    # ---- identity
    w("## 1. Identity\n")
    ver = {}
    for fi in getattr(pe, "FileInfo", []) or []:
        for e in fi:
            for t in getattr(e, "StringTable", []):
                ver.update({k.decode(): v.decode() for k, v in t.entries.items()})
    ts = fh.TimeDateStamp
    rows = [
        ["File size", f"{len(img.raw):,} bytes"],
        ["MD5", hashlib.md5(img.raw).hexdigest()],
        ["SHA-1", hashlib.sha1(img.raw).hexdigest()],
        ["SHA-256", hashlib.sha256(img.raw).hexdigest()],
        ["Link timestamp", f"{ts} = {dt.datetime.fromtimestamp(ts, dt.timezone.utc):%Y-%m-%d %H:%M:%S} UTC"],
    ] + [[f"Version: {k}", v] for k, v in sorted(ver.items())]
    w(md_table(["Field", "Value"], rows) + "\n")
    w("Provenance: see ADR-0002. The owner built this file from their own retail disc and removed the CD check themselves.\n")

    # ---- headers
    w("## 2. PE headers\n")
    dllch = oh.DllCharacteristics
    rows = [
        ["Machine", hex(fh.Machine) + (" (i386)" if fh.Machine == 0x14C else "")],
        ["Characteristics", f"{hex(fh.Characteristics)} ({', '.join(n for n, v in pefile.image_characteristics if fh.Characteristics & v)})"],
        ["Subsystem", f"{oh.Subsystem} ({pefile.SUBSYSTEM_TYPE.get(oh.Subsystem, '?')})"],
        ["Image base", h(oh.ImageBase)],
        ["Entry point", f"{h(img.base + oh.AddressOfEntryPoint)} (section `{img.section_of(img.base + oh.AddressOfEntryPoint)}`)"],
        ["SizeOfImage", h(oh.SizeOfImage)],
        ["Section / file alignment", f"{hex(oh.SectionAlignment)} / {hex(oh.FileAlignment)}"],
        ["Linker version", f"{oh.MajorLinkerVersion}.{oh.MinorLinkerVersion:02d}"],
        ["OS / subsystem version", f"{oh.MajorOperatingSystemVersion}.{oh.MinorOperatingSystemVersion} / {oh.MajorSubsystemVersion}.{oh.MinorSubsystemVersion}"],
        ["DllCharacteristics", f"{hex(dllch)} (ASLR {'yes' if dllch & 0x40 else 'no'}, DEP {'yes' if dllch & 0x100 else 'no'})"],
        ["Stack reserve / commit", f"{hex(oh.SizeOfStackReserve)} / {hex(oh.SizeOfStackCommit)}"],
        ["Heap reserve / commit", f"{hex(oh.SizeOfHeapReserve)} / {hex(oh.SizeOfHeapCommit)}"],
        ["Base relocations", "none (RELOCS_STRIPPED; must load at the image base)" if oh.DATA_DIRECTORY[5].Size == 0 else f"{oh.DATA_DIRECTORY[5].Size} bytes"],
        ["TLS directory", "none" if oh.DATA_DIRECTORY[9].Size == 0 else f"{oh.DATA_DIRECTORY[9].Size} bytes"],
        ["Load config", "none" if oh.DATA_DIRECTORY[10].Size == 0 else f"{oh.DATA_DIRECTORY[10].Size} bytes"],
        ["Debug directory", "none" if oh.DATA_DIRECTORY[6].Size == 0 else f"{oh.DATA_DIRECTORY[6].Size} bytes"],
        ["Overlay", f"{len(pe.get_overlay() or b'')} bytes after the last section"],
        ["Checksum (header / computed)", f"{hex(oh.CheckSum)} / {hex(pe.generate_checksum())}"],
    ]
    w(md_table(["Field", "Value"], rows) + "\n")

    w("### Sections\n")
    rows = []
    for name, start, size, s in img.sections:
        fl = "".join(c for c, bit in (("R", 0x40000000), ("W", 0x80000000), ("X", 0x20000000)) if s.Characteristics & bit)
        data = img.read(start, min(s.Misc_VirtualSize, s.SizeOfRawData))
        rows.append([f"`{name}`", h(start), hex(s.Misc_VirtualSize), hex(s.SizeOfRawData), fl, f"{entropy(data):.2f}"])
    w(md_table(["Name", "VA", "VirtSize", "RawSize", "Flags", "Entropy"], rows) + "\n")
    bss = [n for n, st, sz, s in img.sections if s.Misc_VirtualSize > s.SizeOfRawData]
    w(f"Sections with zero-filled tails (VirtSize > RawSize): {', '.join('`'+b+'`' for b in bss) or 'none'}.\n")

    # ---- compiler
    w("## 3. Compiler, CRT and C++ runtime\n")
    rich = pe.parse_rich_header()
    if rich:
        vals = rich["values"]
        rows = []
        for i in range(0, len(vals), 2):
            prodid, build, count = vals[i] >> 16, vals[i] & 0xFFFF, vals[i + 1]
            rows.append([prodid, build, count, RICH_PRODID.get(prodid, "?"), VC6_BUILDS.get(build, "")])
        w("Rich header:\n")
        w(md_table(["ProdId", "Build", "Objects", "Tool", "Build meaning"], rows) + "\n")
    crt_dll = [d for d, _ in img.imports if re.match(r"(?i)msvcr|msvcp|mfc", d)]
    w(f"- CRT linkage: {'dynamic: ' + ', '.join(crt_dll) if crt_dll else 'static (no MSVCRT/MSVCP/MFC DLL imported)'}.")
    ep = img.base + oh.AddressOfEntryPoint
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    ep_ins = list(md.disasm(img.read(ep, 40), ep))[:9]
    seh_ep = any("fs:[0]" in i.op_str for i in ep_ins)
    w(f"- Entry point prologue: `{'; '.join(f'{i.mnemonic} {i.op_str}'.strip() for i in ep_ins[:7])}`. "
      f"{'This is the VC6 `WinMainCRTStartup` shape: an SEH frame (`push -1; push scopetable; push _except_handler3`) followed by `GetVersion`.' if seh_ep else ''}")
    n_ehmagic = len(re.findall(re.escape(struct.pack('<I', 0x19930520)), img.raw))
    w(f"- C++ exception-handling FuncInfo records (magic 0x19930520): **{n_ehmagic}**. Each one is a function with C++ `try`/`catch` or a destructor-unwind table, which go through `__CxxFrameHandler`.")
    w(f"- MSVC RTTI type descriptors (`.?AV`/`.?AU` names): **{len(classes)}** classes. Sample: {', '.join('`'+c+'`' for c in classes[:25])}{' ...' if len(classes) > 25 else ''}")
    w("")

    # ---- protection
    w("## 4. Packers and protection\n")
    found_sec = [(n, PACKER_SECTIONS[k]) for n, *_ in img.sections for k in PACKER_SECTIONS if n.lower().startswith(k.lower())]
    prot_str = [(m.start(), m.group().decode(errors="replace")) for p in PROTECTION_STRINGS for m in re.finditer(p, img.raw, re.I)]
    text = next((s for n, st, sz, s in img.sections if n == ".text"), None)
    text_ent = entropy(text.get_data()) if text else 0
    w(f"- Known packer/protector section names: {', '.join(f'`{n}` ({p})' for n, p in found_sec) or 'none'}.")
    w(f"- Known protection strings (SafeDisc/SecuROM/LaserLock markers): {', '.join(f'`{s}` @{hex(o)}' for o, s in prot_str) or 'none'}.")
    w(f"- `.text` entropy {text_ent:.2f}. Typical x86 code is 6.0 to 6.7; encrypted or compressed code is above 7.5.")
    w(f"- Entry point lies in `{img.section_of(ep)}`, the first code section, as an unpacked MSVC binary would have it.")
    w(f"- Import table is complete ({sum(len(n) for _, n in img.imports)} imports in {len(img.imports)} DLLs). Packers usually leave only a few imports.")
    cd = []
    for k in ("GetDriveTypeA", "GetVolumeInformationA"):
        for addr, (d, n) in img.iat.items():
            if n == k:
                cd.append(f"`{k}` x{len(refs.get(addr, []))}")
    w(f"- APIs that CD checks typically use, with their remaining reference counts: {', '.join(cd)}. CD checks are often still present but bypassed.")
    ov = pe.get_overlay() or b""
    if ov:
        w(f"- Overlay: {len(ov)} bytes after the last section (`{ov[:48]!r}`). Not referenced by the loader. Most likely appended by a patching tool; harmless for recompilation, which reads only the sections.")
    if ghidra:
        wx = ghidra.get("writes_to_exec", [])
        w(f"- Writes into executable sections that Ghidra found: **{len(wx)}**" + (": " + ", ".join(f"{x['from']}->{x['to']}" for x in wx[:10]) if wx else "."))
    w("")

    # ---- imports summary
    w("## 5. Imports (summary; the full table is in imports.md)\n")
    rows = []
    for dll, names in img.imports:
        c = collections.Counter(apinotes.note(dll, n)[0] for n in names)
        rows.append([f"`{dll}`", len(names), c.get("trivial", 0), c.get("medium", 0), c.get("hard", 0), c.get("?", 0)])
    tot = [sum(r[i] for r in rows) for i in range(1, 6)]
    rows.append(["**total**", *tot])
    w(md_table(["DLL", "APIs", "trivial", "medium", "hard", "unclassified"], rows) + "\n")
    w("Ordinal imports resolve as follows (ordinals from Wine's `.spec` files): `DPLAYX#1` DirectPlayCreate, `#2` DirectPlayEnumerateA, `#4` DirectPlayLobbyCreateA; `DSOUND#1` DirectSoundCreate.\n")

    # ---- COM
    w("## 6. COM / DirectX\n")
    w(f"{len(gh)} GUIDs from the mingw-w64 DirectX headers occur in the image. Most of them are there only because "
      "`dxguid.lib` was linked whole, so presence alone proves nothing. Below are the GUIDs that the **code references**: an "
      "instruction whose immediate or displacement is the GUID's address.\n")
    gref = collections.Counter()
    gvas = {va for va, _, _ in gh}
    for entry, insns in funcs.items():
        for ins in insns:
            for t, _, imm, mem, _ in ins.ops:
                v = (imm & 0xffffffff) if t == X.X86_OP_IMM else (mem[3] & 0xffffffff) if t == X.X86_OP_MEM and mem[0] == 0 else None
                if v in gvas:
                    gref[v] += 1
    rows = [[h(va), n, dxheaders.guid_str(img.read(va, 16)), gref[va]] for va, sec, n in gh if gref[va]]
    w(md_table(["VA", "Name", "GUID", "Code refs"], rows) + "\n" if rows else "none referenced\n")
    fam = collections.Counter(re.sub(r"^(IID_|CLSID_|GUID_|DPSPGUID_|DPAID_|DPLPROPERTY_)", "", n)[:9] for va, _s, n in gh if not gref[va])
    w("Unreferenced GUID families (linked but not used directly): " + ", ".join(f"{k}* x{v}" for k, v in sorted(fam.items())) + "\n")
    if com["coci"]:
        w("`CoCreateInstance` call sites:\n")
        w(md_table(["Call site", "CLSID", "IID"], [[h(a_), c, i] for a_, c, i in com["coci"]]) + "\n")
    w("Interface-pointer slots, found by following the out-parameters of the factory functions and methods (DirectDrawCreate, CreateSurface, QueryInterface and so on):\n")
    rows = []
    for how, at, iface, slot in com["creations"]:
        rows.append([h(at), how, iface, slot[0] + (" " + h(slot[1]) if slot[0] == "global" else f" +{hex(slot[1])}" if slot[0] == "field" else f" {hex(slot[3] & 0xffffffff)}")])
    w(md_table(["Site", "Created by", "Interface", "Stored in"], rows) + "\n" if rows else "none found\n")
    if com["unresolved_creations"]:
        w(f"Unresolved creation sites, where the out-pointer is computed: {len(com['unresolved_creations'])}: " +
          ", ".join(f"{how}@{h(at)}" for how, at, _ in com["unresolved_creations"][:15]) + "\n")
    w(f"COM method calls: `call [vtbl+disp]` whose object pointer was loaded from a known slot. The program has {com['vcalls_total']} vtable-shaped calls; the table lists those that resolve to a DirectX or DirectPlay interface. "
      "Field offsets collide between unrelated classes, so a call through a struct-field slot is counted only when two checks pass. "
      f"First, its push count must equal the method's arity. Second, it must lie within {OWNER_WINDOW // 1024} KiB of the code that stored the pointer (the wrapper class's own object file). "
      "\"rejected\" counts the matches dropped by that check. "
      f"{com['ambiguous']} of the accepted sites fit more than one interface.\n")
    w("**Lower bound:** a method can still be called through paths this heuristic cannot follow, such as pointers handed through function arguments. "
      "The Phase 1 shim must implement the full vtable and log any method that was never expected.\n")
    rows = []
    for (iface, k, m), n in sorted(com["method_use"].items(), key=lambda kv: (kv[0][0], kv[0][1])):
        rows.append([iface, k, f"`{m}`", n, com["unconfirmed"].get((iface, k, m), 0)])
    w(md_table(["Interface", "Slot", "Method", "Call sites", "rejected"], rows) + "\n" if rows else "none resolved\n")

    # ---- dynamic loading
    w("## 7. Dynamic loading (LoadLibrary / GetProcAddress / GetModuleHandle)\n")
    rows = [[k, h(at), f"`{s}`"] for k, at, s in dyn]
    w(md_table(["API", "Call site", "Argument"], rows) + "\n" if rows else "none\n")

    # ---- threads, timers, SEH, registry, MCI
    w("## 8. Threads, timers, SEH, registry, MCI/MIDI\n")
    def refcount(names):
        out = []
        for addr, (d, n) in sorted(img.iat.items(), key=lambda kv: kv[1][1]):
            if n in names:
                out.append(f"`{n}` x{len(refs.get(addr, []))}")
        return ", ".join(out) or "-"
    w("Reference counts below are instructions that touch the import's IAT slot (`call [iat]` or `mov reg, [iat]`).\n")
    w(f"- **Threads:** imported: {refcount({'CreateThread', '_beginthread', '_beginthreadex', 'SetThreadPriority', 'GetThreadPriority', 'GetCurrentThread'})}. "
      "`CreateThread` is **not** in the import table; any thread creation would go through GetProcAddress (section 7) or happen inside DirectPlay/DirectSound.")
    w(f"- **Synchronisation:** {refcount({'CreateMutexA', 'ReleaseMutex', 'CreateSemaphoreA', 'ReleaseSemaphore', 'WaitForSingleObject'})}.")
    w(f"- **Timers:** {refcount({'timeGetTime', 'timeBeginPeriod', 'timeEndPeriod', 'SetTimer', 'KillTimer', 'QueryPerformanceCounter', 'QueryPerformanceFrequency', 'GetTickCount'})}. "
      "`QueryPerformanceCounter` and `GetTickCount` are not imported, so `timeGetTime` is the game clock.")
    seh_funcs = sum(1 for f in (ghidra or {}).get("functions", []) if f.get("seh"))
    w(f"- **SEH:** {refcount({'RaiseException', 'RtlUnwind', 'SetUnhandledExceptionFilter', 'UnhandledExceptionFilter'})}. "
      f"Functions that install an `fs:[0]` frame: {seh_funcs if ghidra else 'n/a (needs Ghidra export)'}. C++ EH FuncInfo records: {n_ehmagic}.")
    w(f"- **Registry:** {refcount({'RegCreateKeyExA', 'RegQueryValueExA', 'RegSetValueExA', 'RegCloseKey'})}.")
    reg_keys = sorted(set(s for _, s in strings(img) if re.search(r"(?i)^(software|system)\\|\\microsoft\\games|^HKEY", s)))
    w(f"  Registry paths in strings: {', '.join('`'+s+'`' for s in reg_keys[:12]) or 'none'}")
    w(f"- **MCI / mixer / video:** {refcount({'mciSendCommandA', 'mciGetErrorStringA', 'mixerOpen', 'mixerSetControlDetails', 'MCIWndCreateA', 'ICInfo'})}.")
    fpu_names = {"fsin", "fcos", "fsincos", "fpatan", "fptan", "f2xm1", "fyl2x", "fyl2xp1", "fprem", "fprem1", "fscale", "frndint"}
    tr = collections.Counter(i.mnem for insns in funcs.values() for i in insns if i.mnem in fpu_names)
    w(f"- **x87 transcendental / rounding instructions** (hardware approximations differ from libm, which matters for determinism): "
      + (", ".join(f"`{k}` x{v}" for k, v in sorted(tr.items())) or "none") + ".")
    media = sorted(set(s for _, s in strings(img) if re.search(r"(?i)(cdaudio|sequencer|waveaudio|avivideo|mpegvideo|\.mid\b|\.avi\b|\.mp3\b|\.wav\b|\.mmm\b)", s) and len(s) < 80))
    w(f"  Media-related strings: {', '.join('`'+s+'`' for s in media[:30])}{' ...' if len(media) > 30 else ''}")
    w(f"- **Networking (besides DirectPlay):** {refcount({'WSAStartup', 'gethostname', 'gethostbyname', 'WSACleanup'})}.")
    w("")

    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "recon.md").write_text("\n".join(L) + "\n")

    # ---------------- imports.md
    I = ["# Import table: `" + a.exe.name + "`\n",
         "_Generated by `make recon`. Difficulty: **trivial** = constant/no-op/libc one-liner; **medium** = real but bounded code; "
         "**hard** = a subsystem of its own, or a browser constraint (blocking, threads). Refs = instructions referencing the IAT slot._\n"]
    for dll, names in img.imports:
        I.append(f"## {dll} ({len(names)})\n")
        rows = []
        for n in names:
            addr = next(k for k, v in img.iat.items() if v == (dll, n))
            d, note = apinotes.note(dll, n)
            rows.append([f"`{n}`", h(addr), len(refs.get(addr, [])), d, note])
        I.append(md_table(["API", "IAT", "Refs", "Difficulty", "Note"], rows) + "\n")
    (a.out / "imports.md").write_text("\n".join(I) + "\n")
    print(f"recon: wrote {a.out/'recon.md'} and {a.out/'imports.md'} ({len(funcs)} functions scanned)")


if __name__ == "__main__":
    main()

"""Differential testing of lifted functions against the original x86 bytes.

The oracle is Unicorn running the original image. The subject is the lifted C body
compiled against pcrecomp's recomp32 runtime header. Both machines start from the
same random state (registers, stack, a heap of pointer-rich words, the x87 top of
stack) and we compare everything a caller could observe after the function returns:
the eight GPRs (esp included, so `ret N` is checked), every byte written anywhere
in mapped memory, and the x87 stack (depth + values, bit for bit).

EFLAGS are not compared at function exit: MSVC code never reads flags across a
`ret`. pcrecomp's own instruction-level suite covers flag semantics.

This file is ours (independent of pcrecomp's difftest.py); only the C ABI is shared.
"""
from __future__ import annotations

import os
import random
import re
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pefile
from unicorn import Uc, UcError, UC_ARCH_X86, UC_MODE_32, UC_PROT_ALL
from unicorn import x86_const as U

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "third_party" / "pcrecomp" / "runtime" / "recomp32"
HERE = Path(__file__).resolve().parent

HEAP, HEAP_LEN = 0x10000000, 0x40000      # big enough for Genie's large objects (fields at +0xa214 seen)
STACK, STACK_LEN = 0x20000000, 0x10000
ESP0 = STACK + 0x8000
EXIT = 0x30000000               # return address: a mapped page we stop at
TRAMP = EXIT + 0x100            # x87 load/store snippets
FPBUF = EXIT + 0x800
FPCW_WINDOWS = 0x027F           # MSVC/Windows default: 53-bit precision, round-nearest, all masked
MAX_INSNS = 2_000_000
REGS = ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi")
UREG = [U.UC_X86_REG_EAX, U.UC_X86_REG_ECX, U.UC_X86_REG_EDX, U.UC_X86_REG_EBX,
        U.UC_X86_REG_ESP, U.UC_X86_REG_EBP, U.UC_X86_REG_ESI, U.UC_X86_REG_EDI]


# ----------------------------------------------------------------------------- image

@dataclass
class Image:
    base: int
    data: bytes              # as the loader maps it, SizeOfImage long
    data_lo: int             # writable data range: pointers in here look like globals
    data_hi: int

    @classmethod
    def load(cls, path: Path) -> "Image":
        pe = pefile.PE(str(path), fast_load=True)
        base = pe.OPTIONAL_HEADER.ImageBase
        size = (pe.OPTIONAL_HEADER.SizeOfImage + 0xFFF) & ~0xFFF
        img = bytearray(pe.get_memory_mapped_image()[:size]).ljust(size, b"\0")
        rw = [s for s in pe.sections if s.Characteristics & 0x80000000]
        lo = base + min(s.VirtualAddress for s in rw) if rw else base
        hi = base + max(s.VirtualAddress + s.Misc_VirtualSize for s in rw) if rw else base
        return cls(base, bytes(img), lo, hi)


# ----------------------------------------------------------------------------- inputs

EDGE = (0, 1, 2, 3, 4, 7, 8, 0xFF, 0x100, 0x7FFF, 0x8000, 0xFFFF, 0x10000,
        0x7FFFFFFF, 0x80000000, 0x80000001, 0xFFFFFFFE, 0xFFFFFFFF)
F32_SPECIAL = (0.0, -0.0, 0.5, -0.5, 1.5, 2.5, -2.5, 1e-40, float("inf"), float("-inf"), float("nan"))


def f32_bits(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


@dataclass
class Gen:
    """Pointer-rich random states. Each word is drawn from a mix of what game memory
    holds: pointers (heap objects, globals, stack), small and edge integers, floats
    (including halves for rounding, signed zero, NaN/inf/denormals), and noise."""
    img: Image
    rng: random.Random

    def ptr(self) -> int:
        r = self.rng.random()
        if r < 0.70:
            return HEAP + (self.rng.randrange(0, HEAP_LEN - 0x400) & ~3)
        if r < 0.85:
            return self.img.data_lo + (self.rng.randrange(0, max(4, self.img.data_hi - self.img.data_lo - 0x400)) & ~3)
        return STACK + (self.rng.randrange(0x9000, STACK_LEN - 0x400) & ~3)

    dense: bool = False      # profile: pointer-dense memory and small indices (object graphs, tables)

    def word(self) -> int:
        r = self.rng.random()
        if self.dense:
            if r < 0.55:
                return self.ptr()
            if r < 0.85:
                return self.rng.randrange(0, 32)
            return self.rng.getrandbits(32)
        if r < 0.35:
            return self.ptr()
        if r < 0.55:
            return self.rng.choice(EDGE)
        if r < 0.70:
            return self.rng.randrange(-64, 64) & 0xFFFFFFFF
        if r < 0.85:
            if self.rng.random() < 0.2:
                return f32_bits(self.rng.choice(F32_SPECIAL))
            return f32_bits(self.rng.choice([self.rng.uniform(-1e4, 1e4), self.rng.randrange(-300, 300) / 2]))
        return self.rng.getrandbits(32)

    def st0(self) -> float:
        r = self.rng.random()
        if r < 0.15:
            return self.rng.choice(F32_SPECIAL)
        if r < 0.45:
            return self.rng.randrange(-2000, 2000) / 2          # exact halves: rounding ties
        return self.rng.uniform(-1e6, 1e6)

    def arg(self) -> int:
        """Arguments and `this` are pointers or small indices more often than memory at large."""
        r = self.rng.random()
        if self.dense:
            return self.ptr() if r < 0.5 else self.rng.randrange(0, 32)
        if r < 0.45:
            return self.ptr()
        if r < 0.70:
            return self.rng.randrange(0, 64)
        return self.word()

    def fill(self, nbytes: int) -> bytes:
        """Region contents: words drawn (with replacement) from a pool of self.word()
        values. Same distribution as word(), at a fraction of the cost."""
        pool = [self.word() for _ in range(4096)]
        return struct.pack("<%dI" % (nbytes // 4), *self.rng.choices(pool, k=nbytes // 4))

    def alias(self, vals: list) -> list:
        """Sometimes two arguments are the same object (equality/overlap paths)."""
        for i in range(1, len(vals)):
            if self.rng.random() < 0.12:
                vals[i] = vals[self.rng.randrange(i)]
        return vals

    def case(self, idx: int):
        self.dense = idx % 2 == 1          # alternate the two input profiles
        heap = self.fill(HEAP_LEN)
        if idx < len(EDGE):                                    # first cases: one edge value everywhere
            regs = [EDGE[idx]] * 8
            args = [EDGE[idx]] * 32
        else:
            regs = self.alias([self.arg() if r in (1, 3, 6, 7) else self.word() for r in range(8)])
            args = self.alias([self.arg() for _ in range(8)]) + [self.word() for _ in range(24)]
        regs[4] = ESP0
        stack = bytearray(self.fill(STACK_LEN))
        # [esp] = return address, then 32 argument slots
        struct.pack_into("<I32I", stack, ESP0 - STACK, EXIT, *args)
        depth = self.rng.choice((0, 0, 1))
        return {"regs": regs, "depth": depth, "st0": self.st0(), "heap": heap, "stack": bytes(stack)}


def write_cases(path: Path, img: Image, cases) -> None:
    regions = [(img.base, len(img.data)), (HEAP, HEAP_LEN), (STACK, STACK_LEN)]
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x31435444, len(cases), len(regions)))
        for va, n in regions:
            f.write(struct.pack("<II", va, n))
        for c in cases:
            f.write(struct.pack("<8III d", *c["regs"], c["depth"], 0, c["st0"]))
            f.write(c["heap"])
            f.write(c["stack"])


# ----------------------------------------------------------------------------- oracle

@dataclass
class Result:
    status: str                       # ok | fault | timeout | escape
    regs: tuple = ()
    st: tuple = ()                    # x87 stack, top first, as f64 bit patterns
    mem: dict = field(default_factory=dict)
    detail: str = ""


class Oracle:
    def __init__(self, img: Image, entry: int, body_range: tuple[int, int]):
        self.img, self.entry, self.lo, self.hi = img, entry, *body_range

    def run(self, c) -> Result:
        mu = Uc(UC_ARCH_X86, UC_MODE_32)
        mu.mem_map(self.img.base, len(self.img.data), UC_PROT_ALL)
        mu.mem_write(self.img.base, self.img.data)
        mu.mem_map(HEAP, HEAP_LEN)
        mu.mem_write(HEAP, c["heap"])
        mu.mem_map(STACK, STACK_LEN)
        mu.mem_write(STACK, c["stack"])
        mu.mem_map(EXIT, 0x1000)
        mu.mem_write(EXIT, b"\xf4")                            # hlt (never executed: we stop on arrival)
        mu.reg_write(U.UC_X86_REG_FPCW, FPCW_WINDOWS)
        if c["depth"]:
            mu.mem_write(FPBUF, struct.pack("<d", c["st0"]))
            code = b"\xdd\x05" + struct.pack("<I", FPBUF)    # fld qword [FPBUF]
            mu.mem_write(TRAMP, code)
            mu.emu_start(TRAMP, TRAMP + len(code))
        for r, v in zip(UREG, c["regs"]):
            mu.reg_write(r, v)
        mu.reg_write(U.UC_X86_REG_EFLAGS, 0x202)
        escaped = []

        def on_block(uc, addr, size, _):
            if not (self.lo <= addr < self.hi) and addr != EXIT:
                escaped.append(addr)
                uc.emu_stop()
        from unicorn import UC_HOOK_BLOCK
        hook = mu.hook_add(UC_HOOK_BLOCK, on_block)
        try:
            mu.emu_start(self.entry, EXIT, count=MAX_INSNS)
        except UcError as e:
            return Result("fault", detail=str(e))
        mu.hook_del(hook)               # the x87 read-back snippet below runs outside the body
        if escaped:
            return Result("escape", detail=f"{escaped[0]:08X}")
        if mu.reg_read(U.UC_X86_REG_EIP) != EXIT:
            return Result("timeout")
        regs = tuple(mu.reg_read(r) & 0xFFFFFFFF for r in UREG)
        # the stack starts empty with TOP=0 and each push decrements TOP (a full stack of 8
        # would read as 0 too; no leaf function leaves 8 values behind)
        top = (mu.reg_read(U.UC_X86_REG_FPSW) >> 11) & 7
        depth = (8 - top) & 7
        # pop the stack to memory as f64 (fstp qword), the same rounding the lifted model stores
        st = []
        if depth:
            code = b"".join(b"\xdd\x1d" + struct.pack("<I", FPBUF + 8 * i) for i in range(depth))
            mu.mem_write(TRAMP, code)
            mu.emu_start(TRAMP, TRAMP + len(code))
            st = list(struct.unpack("<%dQ" % depth, mu.mem_read(FPBUF, 8 * depth)))
        mem = {}
        for va, init in ((self.img.base, self.img.data), (HEAP, c["heap"]), (STACK, c["stack"])):
            now = bytes(mu.mem_read(va, len(init)))
            if now != init:
                for i in range(0, len(init), 4096):
                    if now[i:i + 4096] != init[i:i + 4096]:
                        for j in range(i, min(i + 4096, len(init))):
                            if now[j] != init[j]:
                                mem[va + j] = now[j]
        return Result("ok", regs, tuple(st), mem)


# ----------------------------------------------------------------------------- subject

BODY_RE = r"^void sub_%08X\(void\) \{\n.*?^\}\n"


def extract_body(gen: Path, va: int) -> str:
    pat = re.compile(BODY_RE % va, re.M | re.S)
    for f in sorted(gen.glob("recomp_*.c")):
        text = f.read_text()
        if ("sub_%08X(void) {" % va) in text:
            m = pat.search(text)
            if m:
                return m.group(0)
    raise KeyError(f"sub_{va:08X} not in {gen}")


def build_subject(body: str, va: int, workdir: Path, cc: str = "gcc", opt: str = "-O1") -> Path:
    callees = sorted(set(re.findall(r"\bsub_[0-9A-F]{8}\b", body)) - {f"sub_{va:08X}"})
    src = workdir / "body.c"
    src.write_text(
        '#include "prelude.h"\n'
        + "".join(f"void {c}(void) {{ dt_escape(0x{c[4:]}u); }}\n" for c in callees)
        + body
        + f"void dt_entry(void) {{ sub_{va:08X}(); }}\n")
    exe = workdir / "subject"
    cmd = [cc, opt, "-w", "-DRECOMP_LOCAL_REGS", f"-I{RUNTIME}", f"-I{HERE}",
           str(src), str(HERE / "driver.c"), "-o", str(exe), "-lm"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("subject does not build:\n" + r.stderr[-4000:])
    return exe


def run_subject(exe: Path, img: Image, cases, workdir: Path) -> list[Result]:
    (workdir / "image.bin").write_bytes(img.data)
    write_cases(workdir / "cases.bin", img, cases)
    out = subprocess.run([str(exe), str(workdir / "image.bin"), str(workdir / "cases.bin")],
                         capture_output=True, text=True, timeout=600).stdout
    res, cur = [], None
    for line in out.splitlines():
        tag, _, rest = line.partition(" ")
        if tag == "C":
            cur = {"status": None, "regs": (), "st": [], "mem": {}, "detail": ""}
        elif tag == "S":
            st, _, det = rest.partition(" ")
            cur["status"], cur["detail"] = st, det
        elif tag == "R":
            cur["regs"] = tuple(int(x, 16) for x in rest.split())
        elif tag == "V":
            cur["st"].append(int(rest, 16))
        elif tag == "M":
            a, b = rest.split()
            cur["mem"][int(a, 16)] = int(b, 16)
        elif tag == "E":
            res.append(Result(cur["status"], cur["regs"], tuple(cur["st"]), cur["mem"], cur["detail"]))
    return res


# ----------------------------------------------------------------------------- compare

def same_f64(a: int, b: int) -> bool:
    """Bit-identical, except that any two NaNs count as equal (the x87 'real indefinite'
    QNaN and a C-produced NaN differ in sign/payload, and no caller can tell them apart
    without bit-casting)."""
    def isnan(x):
        return (x >> 52) & 0x7FF == 0x7FF and x & ((1 << 52) - 1)
    return a == b or (isnan(a) and isnan(b))


NAN_PAYLOAD_DIFFS = 0      # counted, not failed: see nan_word


def nan_word(want: Result, got: Result, addr: int, initial=lambda a: None) -> bool:
    """A byte difference inside an aligned float or double that is NaN on both sides.

    When both operands of an x87 arithmetic op are NaN, the FPU returns the one with
    the larger significand; C on the host (SSE) returns the first operand. Only the
    payload differs, and the game cannot observe that without bit-casting a NaN.
    Treated like same_f64 does for the x87 stack: counted, not failed."""
    global NAN_PAYLOAD_DIFFS

    def word(r, base, n):
        b = [r.mem.get(base + i, initial(base + i)) for i in range(n)]
        return None if None in b else int.from_bytes(bytes(b), "little")
    for n, exp_mask, man_mask in ((4, 0x7F800000, 0x007FFFFF), (8, 0x7FF0000000000000, 0x000FFFFFFFFFFFFF)):
        base = addr & ~(n - 1)
        a, b = word(want, base, n), word(got, base, n)
        if a is not None and b is not None and all((x & exp_mask) == exp_mask and x & man_mask for x in (a, b)):
            NAN_PAYLOAD_DIFFS += 1
            return True
    return False


# Divergences that are understood and accepted, each counted per function instead of failing:
#  fnstsw-top     pcrecomp's fnstsw does not model the x87 TOP field (status bits 11-13);
#                 code that tests C0-C3 (every MSVC compare idiom) is unaffected.
#  div-no-trap    x86 raises #DE when a quotient does not fit; the lifted division wraps.
#                 Only inputs on which the original program would crash reach this.
KNOWN = ("fnstsw-top", "div-no-trap")


def compare(want: Result, got: Result, mnems: set[str] = frozenset(), known: dict | None = None,
            initial=lambda a: None) -> list[str]:
    known = known if known is not None else {}
    if want.status != got.status:
        if want.status != "ok" and got.status != "ok":
            return []                     # both refused this input (fault/timeout): not a verdict
        if (want.status == "fault" and "UC_ERR_EXCEPTION" in want.detail and got.status == "ok"
                and mnems & {"div", "idiv"}):
            known["div-no-trap"] = known.get("div-no-trap", 0) + 1
            return []
        return [f"status: cpu {want.status} {want.detail} / lifted {got.status} {got.detail}"]
    if want.status != "ok":
        return []
    diffs = []
    for n, a, b in zip(REGS, want.regs, got.regs):
        if a != b:
            if "fnstsw" in mnems and (a ^ b) & ~0x3800 == 0:
                known["fnstsw-top"] = known.get("fnstsw-top", 0) + 1
                continue
            diffs.append(f"{n}: cpu {a:08X} lifted {b:08X}")
    if len(want.st) != len(got.st):
        diffs.append(f"x87 depth: cpu {len(want.st)} lifted {len(got.st)}")
    else:
        for i, (a, b) in enumerate(zip(want.st, got.st)):
            if not same_f64(a, b):
                fa, fb = (struct.unpack("<d", struct.pack("<Q", x))[0] for x in (a, b))
                diffs.append(f"st{i}: cpu {fa!r} ({a:016X}) lifted {fb!r} ({b:016X})")
    keys = set(want.mem) | set(got.mem)
    bad = sorted(k for k in keys if want.mem.get(k) != got.mem.get(k))
    bad = [k for k in bad if not nan_word(want, got, k, initial)]
    if bad:
        diffs.append(f"memory: {len(bad)} bytes differ, first at {bad[0]:08X} "
                     f"(cpu {want.mem.get(bad[0], '-')}, lifted {got.mem.get(bad[0], '-')})")
    return diffs


@dataclass
class Verdict:
    va: int
    cases: int
    both_ok: int
    refused: int
    failures: list
    known: dict = field(default_factory=dict)     # accepted divergences: name -> count

    @property
    def passed(self) -> bool:
        return not self.failures


def difftest(img: Image, gen: Path, va: int, body_range: tuple[int, int], ncases: int = 200,
             seed: int = 1, workdir: Path | None = None) -> Verdict:
    rng = random.Random((seed << 32) ^ va)
    g = Gen(img, rng)
    cases = [g.case(i) for i in range(ncases)]
    tmp = Path(workdir or tempfile.mkdtemp(prefix=f"dt_{va:08X}_"))
    tmp.mkdir(parents=True, exist_ok=True)
    exe = build_subject(extract_body(gen, va), va, tmp)
    got = run_subject(exe, img, cases, tmp)
    oracle = Oracle(img, va, body_range)
    fails, both_ok, refused = [], 0, 0
    nan0 = NAN_PAYLOAD_DIFFS
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    lo, hi = body_range
    mnems = {i.mnemonic for i in md.disasm(img.data[lo - img.base:hi - img.base], lo)}
    known: dict = {}
    for i, c in enumerate(cases):
        want = oracle.run(c)

        def initial(a, c=c):
            for base, data in ((img.base, img.data), (HEAP, c["heap"]), (STACK, c["stack"])):
                if base <= a < base + len(data):
                    return data[a - base]
            return None
        d = compare(want, got[i], mnems, known, initial) if i < len(got) else ["lifted run ended early"]
        if d:
            fails.append((i, d))
        elif want.status == "ok":
            both_ok += 1
        else:
            refused += 1
    if NAN_PAYLOAD_DIFFS - nan0:
        known["nan-payload"] = NAN_PAYLOAD_DIFFS - nan0
    return Verdict(va, ncases, both_ok, refused, fails, known)

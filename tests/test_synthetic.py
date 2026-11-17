"""Binary-free path (runs in CI): a synthetic PE built from tests/fixtures/synthetic.c is
lifted with pcrecomp and every leaf is checked against Unicorn. The mutation tests prove
the harness itself catches wrong code: a lifted body with a planted bug must fail."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from difftest.harness import Image, difftest, extract_body, build_subject, run_subject, Oracle, Gen, compare

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "synthetic"
PY = sys.executable


@pytest.fixture(scope="session")
def synth():
    if not shutil.which("i686-w64-mingw32-gcc"):
        pytest.skip("mingw-w64 (i686) not installed")
    import fcntl
    BUILD.mkdir(parents=True, exist_ok=True)
    with open(BUILD / ".lock", "w") as lock:          # xdist workers share one build
        fcntl.flock(lock, fcntl.LOCK_EX)
        src = ROOT / "tests" / "fixtures" / "synthetic.c"
        stamp = BUILD / "gen" / "recomp_funcs.h"
        if not stamp.exists() or stamp.stat().st_mtime < src.stat().st_mtime:
            subprocess.run([PY, str(ROOT / "tests" / "fixtures" / "build_synthetic.py"), str(BUILD)], check=True)
            subprocess.run([PY, str(ROOT / "tools" / "lift" / "lift_image.py"), str(BUILD / "synthetic.exe"),
                            str(BUILD / "synthetic.json"), str(BUILD / "gen")], check=True, capture_output=True)
    funcs = json.loads((BUILD / "synthetic.json").read_text())["functions"]
    return Image.load(BUILD / "synthetic.exe"), {f["name"]: f for f in funcs}


NAMES = ["sum_array", "clamp", "hash_str", "fill_bytes", "mul_wide", "div_round", "rotl_mix", "bit_count",
         "lerp", "dot3", "float_to_int", "float_less", "move_unit", "dist2"]


def rng_of(f):
    lo, hi = f["ranges"][0]
    return int(f["entry"], 16), (int(lo, 16), int(hi, 16) + 1)


@pytest.mark.parametrize("name", NAMES)
def test_synthetic_leaf(synth, name, tmp_path):
    img, funcs = synth
    va, body = rng_of(funcs[name])
    v = difftest(img, BUILD / "gen", va, body, ncases=300, workdir=tmp_path)
    msg = "\n".join(f"case {i}: " + "; ".join(d) for i, d in v.failures[:10])
    assert v.passed, f"{name}: {len(v.failures)}/{v.cases} cases differ\n{msg}"
    assert v.both_ok >= 30, f"{name}: only {v.both_ok} inputs ran to completion"


def _mutated_failures(synth, name, mutate, tmp_path):
    img, funcs = synth
    va, body = rng_of(funcs[name])
    src = extract_body(BUILD / "gen", va)
    bad = mutate(src)
    assert bad != src, "mutation did not apply"
    g = Gen(img, __import__("random").Random(7))
    cases = [g.case(i) for i in range(120)]
    exe = build_subject(bad, va, tmp_path)
    got = run_subject(exe, img, cases, tmp_path)
    o = Oracle(img, va, body)
    return sum(1 for c, r in zip(cases, got) if compare(o.run(c), r))


def test_harness_catches_integer_bug(synth, tmp_path):
    """Flip one bit of eax on every return of clamp(): every completed case must differ."""
    n = _mutated_failures(synth, "clamp", lambda s: s.replace("RECOMP_FLAGS_OUT();", "eax ^= 1; RECOMP_FLAGS_OUT();"), tmp_path)
    assert n > 30


def test_harness_catches_x87_bug(synth, tmp_path):
    """Turn the multiply in lerp() into an add: the returned st0 must differ."""
    def mut(s):
        return re.sub(r"_ST\(0\) \*=", "_ST(0) +=", s, count=1) if "_ST(0) *=" in s else s.replace("*", "+", 1)
    n = _mutated_failures(synth, "lerp", mut, tmp_path)
    assert n > 30


def test_harness_catches_memory_bug(synth, tmp_path):
    """Make fill_bytes() write c+1: memory diffs must be reported."""
    def mut(s):
        return re.sub(r"(MEM8\([^)]*\)\s*=\s*)", r"\1 1 + ", s, count=1)
    n = _mutated_failures(synth, "fill_bytes", mut, tmp_path)
    assert n > 10

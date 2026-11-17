"""Phase 0 prototype: the lifted leaf functions listed in aoe2_prototype.txt must behave
exactly like the original bytes under Unicorn. Needs the user's own age2_x1.exe and the
lift (`make lift`); skips cleanly without them (ADR-0002)."""
import json
import os
from pathlib import Path

import pytest

from difftest.harness import Image, difftest

ROOT = Path(__file__).resolve().parents[1]
EXE = Path(os.environ.get("AOE2_EXE", ROOT / "orig" / "age2_x1.exe"))
GEN = ROOT / "build" / "lift" / "gen"
GHIDRA = ROOT / "build" / "ghidra" / "age2_x1.json"
NCASES = int(os.environ.get("DT_CASES", "600"))
MIN_OK = 30          # a verdict needs this many inputs that both machines ran to completion
MAX_BATCHES = 4


def prototype():
    for line in (ROOT / "tests" / "aoe2_prototype.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            va, kind, *_ = line.split()
            yield pytest.param(int(va, 16), id=f"{va}-{kind}")


@pytest.fixture(scope="module")
def ctx():
    missing = [p for p in (EXE, GEN, GHIDRA) if not p.exists()]
    if missing:
        pytest.skip("needs the game binary and the lift: " + ", ".join(map(str, missing)))
    ranges = {int(f["entry"], 16): f["ranges"] for f in json.loads(GHIDRA.read_text())["functions"]}
    return Image.load(EXE), ranges


@pytest.mark.parametrize("va", list(prototype()))
def test_leaf(ctx, va, tmp_path):
    img, ranges = ctx
    r = ranges[va]
    body = (min(int(lo, 16) for lo, _ in r), max(int(hi, 16) for _, hi in r) + 1)
    # Functions that need very specific object graphs get further batches (new seeds)
    # until enough inputs run to completion on both sides; the bar itself never drops.
    both_ok, fails, known, cases = 0, [], {}, 0
    for seed in range(1, 1 + MAX_BATCHES):
        v = difftest(img, GEN, va, body, ncases=NCASES, seed=seed, workdir=tmp_path / str(seed))
        both_ok, cases = both_ok + v.both_ok, cases + v.cases
        fails += [(f"{seed}:{i}", d) for i, d in v.failures]
        for k, n in v.known.items():
            known[k] = known.get(k, 0) + n
        if fails or both_ok >= MIN_OK:
            break
    v.both_ok, v.cases, v.failures, v.known = both_ok, cases, fails, known
    out = ROOT / "build" / "test-results" / "aoe2_prototype"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{va:08X}.json").write_text(json.dumps(
        {"va": f"0x{va:08X}", "agree": both_ok, "cases": cases, "failures": len(fails), "known": known}))
    msg = "\n".join(f"case {i}: " + "; ".join(d) for i, d in v.failures[:10])
    assert v.passed, f"sub_{va:08X}: {len(v.failures)}/{v.cases} cases differ\n{msg}"
    assert v.both_ok >= MIN_OK, f"sub_{va:08X}: only {v.both_ok} of {cases} inputs ran to completion on both sides"
    known = ", ".join(f"{k} x{n}" for k, n in sorted(v.known.items())) or "none"
    print(f"sub_{va:08X}: {v.both_ok}/{v.cases} inputs agree (rest refused by both), known divergences: {known}")

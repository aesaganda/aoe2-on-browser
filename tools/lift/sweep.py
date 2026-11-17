#!/usr/bin/env python3
"""Differential sweep over every leaf function (not only the 20 in the prototype list).

    sweep.py <exe> <gen> <leaves.json> --out build/lift/sweep.json [--cases 150] [--jobs 20]

Each function gets a verdict: pass, fail (a real divergence), thin (no divergence, but too few
inputs completed on both sides to call it), or error (the harness could not build/run it).
"""
from __future__ import annotations

import argparse
import collections
import json
import multiprocessing as mp
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from difftest.harness import Image, difftest  # noqa: E402

G = {}


def init(exe, gen):
    G["img"], G["gen"] = Image.load(Path(exe)), Path(gen)


def one(args):
    f, ncases, min_ok = args
    va = int(f["entry"], 16)
    lo = min(int(a, 16) for a, _ in f["ranges"])
    hi = max(int(b, 16) for _, b in f["ranges"]) + 1
    try:
        with tempfile.TemporaryDirectory(prefix="sweep_") as d:
            v = difftest(G["img"], G["gen"], va, (lo, hi), ncases=ncases, workdir=Path(d))
        if v.failures:
            kinds = collections.Counter(d.split(":")[0] for _, ds in v.failures for d in ds)
            return {"va": f["entry"], "verdict": "fail", "kinds": dict(kinds), "first": v.failures[0][1][:3],
                    "fpu": f["fpu"], "seh": f["seh"], "known": v.known}
        return {"va": f["entry"], "verdict": "pass" if v.both_ok >= min_ok else "thin", "ok": v.both_ok,
                "fpu": f["fpu"], "seh": f["seh"], "known": v.known}
    except Exception as e:  # noqa: BLE001
        return {"va": f["entry"], "verdict": "error", "error": "".join(traceback.format_exception_only(e))[-300:],
                "fpu": f["fpu"], "seh": f["seh"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exe")
    ap.add_argument("gen")
    ap.add_argument("leaves")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cases", type=int, default=150)
    ap.add_argument("--min-ok", type=int, default=10)
    ap.add_argument("--jobs", type=int, default=mp.cpu_count())
    a = ap.parse_args()
    leaves = json.loads(Path(a.leaves).read_text())
    with mp.Pool(a.jobs, initializer=init, initargs=(a.exe, a.gen)) as pool:
        res = pool.map(one, [(f, a.cases, a.min_ok) for f in leaves], chunksize=4)
    c = collections.Counter(r["verdict"] for r in res)
    known = collections.Counter()
    for r in res:
        for k, n in (r.get("known") or {}).items():
            known[k] += 1
    summary = {"functions": len(res), "verdicts": dict(c), "functions_with_known_divergence": dict(known),
               "cases_per_function": a.cases, "min_ok": a.min_ok}
    Path(a.out).write_text(json.dumps({"summary": summary, "results": res}, indent=1))
    print(json.dumps(summary, indent=1))
    for r in res:
        if r["verdict"] in ("fail", "error"):
            print(r["va"], r["verdict"], r.get("kinds") or r.get("error", "")[:160], r.get("first", ""))


if __name__ == "__main__":
    main()

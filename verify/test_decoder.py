#!/usr/bin/env python3
"""
test_decoder.py -- end-to-end: decode bytes the REAL encoder produced, check the samples.

The companion to test_encoder.py, in the other direction. Take output from MAKEVCC.EXE running
under Unicorn, decode it with this package, and require the recovered samples to match.

    pip install unicorn
    python verify/test_decoder.py --exe /path/to/MAKEVCC.EXE

NOTE: ACMP is LOSSY for att > 0 -- the encoder quantises the delta, so a decoder can only return
the ENCODER'S RECONSTRUCTION, never the source samples. The expectation is rebuilt through the
same quantisation below. A test that compares against the source will fail on quantisation error
alone, which is a very easy mistake to make.
"""
import argparse
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import acmp
from oracle import Oracle


def make_samples(n, seed, shape):
    r = random.Random(seed)
    v, s = 0x80, []
    for _ in range(n):
        if shape == "sparse":
            step = r.choice([0, 0, 0, 1, -1, 1, -1, 4, -5, 9])
        elif shape == "speech":
            step = r.randint(-9, 9)
        else:
            step = r.randint(-2, 2)
        v = max(30, min(225, v + step))
        s.append(v)
    return s


def expected(src, mode, att):
    """What a correct decoder must return: the encoder's own reconstruction."""
    if mode == 6:
        return [min(255, (min(x + (1 << (att - 1)), 0xFF) >> att) << att) if att else x
                for x in src]
    mask = (1 << att) - 1
    pred, out = 0x80, []
    for x in src:
        d = x - pred
        v = (((mask + d) if d < 0 else d) >> att) if att else d
        pred = pred + (v << att)
        out.append(pred & 0xFF)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="path to MAKEVCC.EXE")
    ap.add_argument("--n", type=int, default=64)
    a = ap.parse_args()

    try:
        o = Oracle(exe=a.exe)
    except FileNotFoundError:
        sys.exit("MAKEVCC.EXE not found -- pass --exe /path/to/MAKEVCC.EXE")
    o.init_filter()
    acmp.COUNT = a.n            # these test blocks are `n` samples, not the usual 256

    rows = []
    for mode in range(7):
        for att in (0, 1, 2):
            for shape in ("sparse", "speech", "flat"):
                for seed in (3, 11):
                    src = make_samples(a.n, seed, shape)
                    blob = o.encode_mode(src, mode, att=att)
                    try:
                        got, _nxt, _m, _a, _q = acmp.decode_block(blob, 0)
                    except Exception as e:
                        rows.append((mode, att, shape, seed, False, type(e).__name__))
                        continue
                    if mode == 0:
                        ok = bool(got) and got[0] == src[0]
                        rows.append((mode, att, shape, seed, ok, ""))
                        continue
                    exp = expected(src, mode, att)
                    if got == exp:
                        rows.append((mode, att, shape, seed, True, ""))
                    elif len(got) != len(exp):
                        rows.append((mode, att, shape, seed, False,
                                     "len %d vs %d" % (len(got), len(exp))))
                    else:
                        d = next(i for i in range(len(exp)) if got[i] != exp[i])
                        rows.append((mode, att, shape, seed, False,
                                     "@%d got %d want %d" % (d, got[d], exp[d])))

    tally = {}
    for m, at, _sh, _sd, ok, _info in rows:
        t = tally.setdefault((m, at), [0, 0])
        t[0 if ok else 1] += 1

    print("mode att   exact  fail")
    for k in sorted(tally):
        e, f = tally[k]
        print("  %d   %d     %-6d %d%s" % (k[0], k[1], e, f, "" if f == 0 else "   <-- FAIL"))

    bad = [r for r in rows if not r[4]]
    print()
    print("total %d cases, %d exact, %d not" % (len(rows), len(rows) - len(bad), len(bad)))
    for m, at, sh, sd, _ok, info in bad[:15]:
        print("   mode%d att%d %-7s seed%-3d %s" % (m, at, sh, sd, info))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
test_encoder.py -- differential: this package's bit packer vs the REAL MAKEVCC encoder.

For every (mode, att) and a spread of input shapes, encode the same samples twice -- once with
`acmp.encode_block` and once by running MAKEVCC.EXE under Unicorn -- and require byte equality.

    pip install unicorn
    python verify/test_encoder.py --exe /path/to/MAKEVCC.EXE

MAKEVCC.EXE is Interplay's copyrighted software and is NOT distributed here; it ships on the
Judgment Rites CD-ROM. Supply your own copy.
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


def corpora(n, seed=1):
    """Input shapes chosen to exercise different mode/escape paths."""
    r = random.Random(seed)
    out = [("silence", [0x80] * n),
           ("step", [0x80 if i < n // 2 else 0x90 for i in range(n)]),
           ("ramp", [(0x60 + (i * 3) % 0x40) & 0xFF for i in range(n)]),
           ("small", [max(0, min(255, 0x80 + r.randint(-3, 3))) for _ in range(n)])]
    v, s = 0x80, []
    for _ in range(n):
        v = max(20, min(235, v + r.randint(-9, 9)))
        s.append(v)
    out.append(("speech", s))
    # 'wild' matters: |d| exceeds 127, which a delta-wrapping implementation gets wrong
    out.append(("wild", [r.randint(0, 255) for _ in range(n)]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="path to MAKEVCC.EXE")
    ap.add_argument("--n", type=int, default=64, help="samples per block")
    a = ap.parse_args()

    try:
        o = Oracle(exe=a.exe)
    except FileNotFoundError:
        sys.exit("MAKEVCC.EXE not found -- pass --exe /path/to/MAKEVCC.EXE")
    o.init_filter()

    rows = []
    for mode in range(7):
        for att in (0, 1, 2):
            for name, samples in corpora(a.n):
                hdr = ((att & 3) << 5) | mode
                truth = o.encode_mode(samples, mode, att=att)
                mine = acmp.encode_block(hdr, samples, mode, att)
                rows.append((mode, att, name, mine == truth,
                             "" if mine == truth else "%dB vs %dB" % (len(mine), len(truth))))

    tally = {}
    for m, at, _nm, ok, _info in rows:
        t = tally.setdefault((m, at), [0, 0])
        t[0 if ok else 1] += 1

    print("mode att   exact  fail")
    for k in sorted(tally):
        e, f = tally[k]
        print("  %d   %d     %-6d %d%s" % (k[0], k[1], e, f, "" if f == 0 else "   <-- MISMATCH"))

    bad = [r for r in rows if not r[3]]
    print()
    print("total %d cases, %d exact, %d not" % (len(rows), len(rows) - len(bad), len(bad)))
    for m, at, nm, _ok, info in bad[:15]:
        print("   mode%d att%d %-8s %s" % (m, at, nm, info))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())

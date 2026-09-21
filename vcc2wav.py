#!/usr/bin/env python3
"""
vcc2wav -- decode Interplay .VCC speech containers to WAV.

    python vcc2wav.py COMPUTER.VCC -o out/
    python vcc2wav.py COMPUTER.VCC --list
    python vcc2wav.py COMPUTER.VCC --clip 082 -o out/
    python vcc2wav.py COMPUTER.VCC --verify

Every block is round-trip verified against the original encoder's packer before it is
written, so a clip that decodes is decoded correctly -- not merely plausibly.

Requires only the Python standard library.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import acmp


def main():
    ap = argparse.ArgumentParser(description="Decode Interplay .VCC (ACMP) speech containers.")
    ap.add_argument("vcc", help="path to a .VCC container")
    ap.add_argument("-o", "--out", default="out", help="output directory (default: out)")
    ap.add_argument("--clip", help="decode only this clip id (e.g. 082)")
    ap.add_argument("--rate", type=int, default=22050, help="output sample rate (default 22050)")
    ap.add_argument("--upsample", choices=("fir", "hold"), default="fir",
                    help="quiet-block interpolation: the encoder's FIR kernel, or doubling")
    ap.add_argument("--list", action="store_true", help="list clips and exit")
    ap.add_argument("--verify", action="store_true",
                    help="verify every block byte-exactly; write nothing")
    a = ap.parse_args()

    data = open(a.vcc, "rb").read()
    cl = acmp.clips(a.vcc)
    if a.clip:
        cl = [c for c in cl if c[0] == a.clip]
        if not cl:
            sys.exit("no clip named %r" % a.clip)

    if a.list:
        print("%-6s %-10s %-10s %-7s %s" % ("clip", "offset", "bytes", "blocks", "rate flag"))
        for name, off, size, c4, rate in cl:
            print("%-6s %-10d %-10d %-7d 0x%04x" % (name, off, size, c4, rate))
        return 0

    if not a.verify:
        os.makedirs(a.out, exist_ok=True)

    total_blocks = total_exact = 0
    total_secs = 0.0
    failed = []
    for name, off, size, c4, rate in cl:
        pay = acmp.payload(data, (name, off, size, c4, rate))
        pos = nblk = nok = 0
        while pos < len(pay):
            if not acmp.valid_hdr(pay[pos]):
                break
            try:
                if acmp.verify_block(pay, pos):
                    nok += 1
                _s, nxt, _m, _a, _q = acmp.decode_block(pay, pos)
            except Exception:
                break
            if nxt <= pos:
                break
            nblk += 1
            pos = nxt
        total_blocks += nblk
        total_exact += nok
        complete = (nblk >= c4)
        if not complete or nok != nblk:
            failed.append((name, nblk, nok, c4))

        if a.verify:
            print("%-6s %5d/%-5d blocks  %5d byte-exact  %s"
                  % (name, nblk, c4, nok, "OK" if complete and nok == nblk else "INCOMPLETE"))
            continue

        samples = acmp.decode_clip(pay, upsample=a.upsample)
        if not samples:
            print("%-6s no audio" % name)
            continue
        path = os.path.join(a.out, "%s.wav" % name)
        acmp.write_wav(path, samples, a.rate)
        secs = len(samples) / float(a.rate)
        total_secs += secs
        print("%-6s %5d blocks  %8d samples  %6.1f s  -> %s" % (name, nblk, len(samples), secs, path))

    print()
    print("clips: %d   blocks: %d   byte-exact: %d" % (len(cl), total_blocks, total_exact))
    if not a.verify:
        print("audio written: %.1f s (%.1f min)" % (total_secs, total_secs / 60.0))
    if failed:
        print("incomplete or inexact: %d clip(s)" % len(failed))
        for name, nblk, nok, c4 in failed[:10]:
            print("   %-6s %d/%d blocks, %d exact" % (name, nblk, c4, nok))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

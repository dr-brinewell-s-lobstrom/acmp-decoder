#!/usr/bin/env python3
"""
vcc2wav -- decode Interplay .VCC speech containers to WAV.

    python vcc2wav.py COMPUTER.VCC -o out/
    python vcc2wav.py COMPUTER.VCC --list
    python vcc2wav.py COMPUTER.VCC --clip 082 -o out/
    python vcc2wav.py COMPUTER.VCC --verify

Every block is round-trip verified against the original encoder's packer before it is
written, so a clip's bitstream that decodes is decoded correctly -- not merely plausibly.

Each clip is written at the sample rate in its own header and trimmed to the header's
sample count, which is what the game plays. A container entry that is a plain Creative
Voice file rather than ACMP is copied out unchanged as .voc.

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
    ap.add_argument("--rate", type=int, default=None,
                    help="force an output sample rate (default: each clip's header rate)")
    ap.add_argument("--upsample", choices=("fir", "hold"), default="fir",
                    help="quiet-block interpolation: the encoder's FIR kernel, or doubling")
    ap.add_argument("--list", action="store_true", help="list clips and exit")
    ap.add_argument("--verify", action="store_true",
                    help="verify every block byte-exactly; write nothing")
    a = ap.parse_args()

    data = open(a.vcc, "rb").read()
    allc = acmp.clips(a.vcc)
    cl = list(zip(allc, acmp.output_names(allc)))
    if a.clip:
        cl = [(c, stem) for c, stem in cl if c[0] == a.clip]
        if not cl:
            sys.exit("no clip named %r" % a.clip)

    if a.list:
        print("%-8s %-10s %-10s %-6s %-7s %-6s %s" % ("clip", "offset", "bytes", "kind", "rate",
                                                      "blocks", "samples"))
        for c, stem in cl:
            name, off, size, c4, _flag = c
            kind, rate, n = acmp.header(data, c)
            extra = "   (duplicate id; written as %s)" % stem if stem != name else ""
            print("%-8s %-10d %-10d %-6s %-7s %-6s %s%s"
                  % (name, off, size, kind, rate or "-", c4 if kind == "acmp" else "-",
                     n if n is not None else "-", extra))
        return 0

    if not a.verify:
        os.makedirs(a.out, exist_ok=True)

    total_blocks = total_exact = 0
    total_secs = 0.0
    failed = []
    for c, stem in cl:
        name, off, size, c4, _flag = c
        kind, hdr_rate, hdr_n = acmp.header(data, c)
        if kind != "acmp":
            if kind == "voc" and not a.verify:
                path = os.path.join(a.out, "%s.voc" % stem)
                with open(path, "wb") as f:
                    f.write(data[off:off + size])
                print("%-6s Creative Voice file, copied unchanged -> %s" % (name, path))
            elif kind == "voc":
                print("%-6s Creative Voice file (not ACMP) -- nothing to verify" % name)
            else:
                print("%-6s unrecognised entry, skipped" % name)
                failed.append((name, 0, 0, c4))
            continue

        pay = acmp.payload(data, c)
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
        # The game plays exactly the header's sample count. Decoding to the end of the payload
        # can run past it (a trailing silent block); trim to match. A decode that comes up
        # SHORT is left as is -- see FORMAT.md section 1.
        if hdr_n and len(samples) > hdr_n:
            samples = samples[:hdr_n]
        rate = a.rate or hdr_rate
        path = os.path.join(a.out, "%s.wav" % stem)
        acmp.write_wav(path, samples, rate)
        secs = len(samples) / float(rate)
        total_secs += secs
        print("%-6s %5d blocks  %8d samples  %5d Hz  %6.1f s  -> %s"
              % (name, nblk, len(samples), rate, secs, path))

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

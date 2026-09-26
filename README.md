# acmp-decoder

A decoder for **ACMP**, the undocumented speech codec in Interplay's `.VCC` containers — the voice
audio of *Star Trek: Judgment Rites* (1993/1995).

As far as we can tell this is the first working ACMP decoder. The later Interplay **ACM** codec
(Fallout, Baldur's Gate) has been decodable for years via `libacm` and FFmpeg, but that is a
different format; see [FORMAT.md](FORMAT.md).

Pure Python 3, standard library only. No dependencies for decoding.

## Quick start

```bash
python vcc2wav.py COMPUTER.VCC --list          # list clips
python vcc2wav.py COMPUTER.VCC -o out/         # decode all clips to out/
python vcc2wav.py COMPUTER.VCC --clip 082 -o out/
python vcc2wav.py COMPUTER.VCC --verify        # verify every block, write nothing
```

Output is 8-bit unsigned PCM mono WAV at each clip's own sample rate (22050 or 11025 Hz),
trimmed to the sample count in the clip header.

As a library:

```python
import acmp

data  = open("COMPUTER.VCC", "rb").read()
clips = acmp.clips("COMPUTER.VCC")             # [(name, offset, size, blocks, rate_flag), ...]

for clip in clips:
    kind, rate, count = acmp.header(data, clip)
    if kind != "acmp":
        continue                                   # e.g. a plain Creative Voice file
    samples = acmp.decode_clip(acmp.payload(data, clip))[:count]
    acmp.write_wav("%s.wav" % clip[0], samples, rate)
```

## What "verified" means here

The decoder is not judged by whether its output sounds right. Every block is checked by
**round-trip**: decode the block, re-encode the recovered samples through a reimplementation of
the original encoder's bit packer, and require the emitted bytes to equal the file bytes exactly.
`--verify` runs this over a whole container.

Results on the *Judgment Rites* containers decoded so far:

```
COMPUTER.VCC   ship's computer voice   245 clips    325,290 blocks   byte-exact: all
FED.VCC        episode 1 dialogue    1,489 clips    526,631 blocks   byte-exact: all
NOMAN.VCC      episode 3 dialogue    2,616 clips    942,639 blocks   byte-exact: all
SCOTTY.VCC     episode 6 dialogue      797 clips    318,997 blocks   byte-exact: all
MADNESS.VCC    episode 7 dialogue    1,554 clips    976,920 blocks   byte-exact: all
```

(Block counts are from the clip headers. `--verify` reports slightly more for some containers
— see the note on trailing blocks in FORMAT.md.)

`FED.VCC`, `NOMAN.VCC`, `SCOTTY.VCC` and `MADNESS.VCC` are worth noting: they were decoded with no changes to the code,
after the decoder had been built and tested only against `COMPUTER.VCC`.

The implementation was additionally differentially tested against the **original encoder**.
`MAKEVCC.EXE` — shipped on the game CD — is 32-bit flat x86 inside a DOS/4GW LE image, and can be
run under emulation. Calling each mode's encoder directly and hooking `ftell`/`fseek`/`fputc`
yields byte-exact ground truth, including code paths that no real clip happens to exercise:

```
encoder mirror vs MAKEVCC   126/126 cases exact   (7 modes x 3 attenuations x 6 input shapes)
decoder round-trip          126/126 cases exact
```

That matters because this codec has several traps where a wrong implementation still produces
smooth, plausible, speech-like audio. Statistics and listening tests both passed for
implementations that were badly wrong. See FORMAT.md §8.

**What the round-trip does not cover.** "Byte-exact" is a claim about the bitstream: every block
and every sample it carries. It does not extend to the clip header, which earlier versions of
this decoder misread (corrected with thanks to count023, below), or to the interpolation of
"quiet" blocks. Those are coded at half rate, and the in-between samples are rebuilt with the
encoder's own decimation filter, a reconstruction the round-trip cannot check (FORMAT.md §7).
Decoded lengths also still come up 2 or 130 samples short of the header count on many clips
(FORMAT.md §1). Both are open until the output is compared against the game's own decoder.

## Why this was hard

ACMP interleaves **two bit planes** — self-delimiting "comma" codes carrying code lengths, and a
payload stream — into a single byte stream, governed by a small state machine. Worse, when a
plane would overflow 32 pending bits the encoder writes a **zero placeholder byte, continues, then
seeks back and overwrites it**. So the order of bytes in the file is not the order of bits in the
stream, and a decoder that reads strictly forward and assigns planes on demand desynchronises.

The saving grace is that the tally plane is genuinely self-delimiting: given the bits and the
alternating phase, the split into codes is **unique**. No search is needed to recover code
lengths — which is what makes a linear-time decoder possible at all.

Full details, including the exact per-mode encodings and the reconstruction rules, are in
[FORMAT.md](FORMAT.md).

## Layout

```
acmp/__init__.py    the decoder: container parsing, bit packer, block layer, WAV output
vcc2wav.py          command-line tool
FORMAT.md           complete format specification
verify/             the differential harness (requires your own MAKEVCC.EXE -- see below)
```

## Reproducing the differential tests

`verify/` runs the original encoder under [Unicorn](https://www.unicorn-engine.org/)
(`pip install unicorn`) and diffs it against this implementation.

**`MAKEVCC.EXE` is not included** — it is Interplay's copyrighted software. It ships on the
*Judgment Rites* CD-ROM (`/MAKEVCC.EXE`); point the harness at your own copy.

```bash
pip install unicorn
python verify/test_encoder.py --exe /path/to/MAKEVCC.EXE
python verify/test_decoder.py --exe /path/to/MAKEVCC.EXE
```

## Scope and limits

- Decoding is **exact** where `att == 0` and **exact to the encoder's own reconstruction**
  otherwise — ACMP is lossy for `att > 0` by design, so the original samples are not recoverable
  even in principle.
- Clips are **22050 or 11025 Hz**, read from each clip's header. Dialogue is 22050; some sound
  effects and backing clips are 11025 (one in `NOMAN.VCC`, 222 in `DATA.VCC`). An earlier
  version wrote every clip at 22050 Hz, which played the 11025 Hz clips at double speed.
- A container entry can be a plain **Creative Voice** `.VOC` file instead of ACMP (four in
  `DATA.VCC`). `vcc2wav.py` copies these out unchanged.
- Only `.VCC` is implemented. Interplay's `.SND` banks (e.g. *Birth of the Federation*) appear to
  be the same codec family in a different container and are untested here.

## Provenance

Reverse-engineered from `MAKEVCC.EXE` and `TREKJR.EXE`. Static analysis used
[capstone](https://www.capstone-engine.org/) and
[codemap](https://github.com/kachowtowmater/codemap); the decisive step was executing the original
encoder under [Unicorn](https://www.unicorn-engine.org/) to obtain ground truth rather than
continuing to infer the format by reading.

No game audio is included in this repository.

## Credits

**count023** ([u/count023](https://www.reddit.com/user/count023/)) found that the clip
mini-header was being read one byte out of phase. From their own reverse engineering of the game
executable, they identified the u32 sample rate at byte 20 and the u32 sample count at byte 26. That
exposed the 11025 Hz clips this decoder had been writing at double speed. Their extractor, which
runs `TREKJR.EXE`'s own decoder under emulation, also showed that the game plays exactly the
header's sample count and that a VCC entry can be a plain Creative Voice file. FORMAT.md §1 is
now their layout.

[codemap](https://github.com/kachowtowmater/codemap) broke the static-analysis deadlock. Its `ir`
action lifts a single function to typed IR with a structured AST, which made the encoder's
per-mode routines readable as semantics rather than as hand-traced capstone output. Concretely, it
settled in minutes whether mode 1's zero-delta path emits data bits — it does not, `DoComp1_`
reaches a block that calls the tally writer and returns — a question that had survived roughly 26
hours of manual tracing across two earlier sessions and was blocking the whole bit-plane model.
`bin-disasm` was the orientation tool: format, bitness, image base, section layout and a
size-ranked function list.

One caveat for anyone repeating this on DOS-era binaries: codemap reads ELF/PE/Mach-O/APK, and
`MAKEVCC.EXE` is an MZ stub wrapping an **LE** (DOS/4GW) image, so it is rejected outright. The
code inside an LE is ordinary 32-bit flat x86, so transplanting the image into a synthetic PE32
wrapper is enough — after that codemap analysed it cleanly. Mind the segment bases when you do
(CS 0x10000, DS 0); getting that wrong produces a plausible, wrong disassembly rather than an
error.

## License

MIT — see [LICENSE](LICENSE).

This covers the decoder and its documentation only. It does not and cannot grant any rights in
Interplay's game data: `MAKEVCC.EXE`, the `.VCC` containers, and the audio they hold remain the
property of their respective rights holders, and none of them are included here.

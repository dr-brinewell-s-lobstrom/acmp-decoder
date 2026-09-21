# The ACMP / VCC format

A complete specification of Interplay's **ACMP** speech codec and its **VCC** container, as used
by *Star Trek: Judgment Rites* (1993/1995).

Derived by reverse-engineering `MAKEVCC.EXE` — the original encoder, shipped on the game CD — and
verified by re-encoding: a decode is accepted only when feeding the recovered samples back through
a reimplementation of the encoder's packer reproduces the file bytes exactly.

> **ACMP is not ACM.** Interplay's later ACM codec (magic `0x97280301`, used by Fallout 1/2 and
> the Baldur's Gate series, implemented by FFmpeg and `libacm`) is a different design: 32 filler
> modes selected per *column*, a `juggle()` subband transform, no predictor, one bit stream.
> ACMP is a DPCM residual coder with 7 modes selected per *block* and **two interleaved bit
> streams**. Knowledge of ACM does not transfer.

Byte order is little-endian throughout. Samples are 8-bit unsigned PCM, mono.

---

## 1. VCC container

```
offset  size  field
0       8     "VOCFILES"
8       4     payload length  (also the file offset of the footer)
12      4     clip count N
16      ...   clip data (the first clip's mini-header starts here)
EOF-16N 16N   footer: N records
```

**Footer record (16 bytes):**

```
0   8   clip name, ASCII, NUL-padded   e.g. "001"
8   4   offset   -- FILE-ABSOLUTE position of the clip's mini-header
12  4   size     -- bytes from `offset`, mini-header INCLUSIVE
```

The offset is absolute. Do **not** add the header length to it — a mistake that costs a lot of
time, because a wrong base still produces plausible-looking block headers.

**Clip mini-header (30 bytes, at `offset`):**

```
0   19  "Interplay ACMP Data"
19  4   0x0056221A   (constant)
23  2   0x2800       (constant)
25  2   rate / flags -- see below
27  2   block count  -- the number of blocks in this clip
29  1   0x00
```

The clip's ACMP bitstream is `[offset+30, offset+size)`. Clips are contiguous: one clip's
`offset+size` is the next clip's `offset`.

**Clip 1 is special:** its mini-header *is* the container header — the `"Interplay ACMP Data"` tag
at file offset 16 serves both. So clip 1's payload begins at byte 46.

**The `rate/flags` field is not fully understood.** In `COMPUTER.VCC` it is `0x0200` on 119 clips
and `0x8200` on 117. 22050 Hz is correct for the clips that have been checked by ear; whether the
high bit selects a different rate is unverified. If a clip sounds wrong-pitched, look here first.

---

## 2. Block layout

A clip is a sequence of blocks. Each begins with a **1-byte header**:

```
bits 0-4   mode   (0..6)
bits 5-6   att    (0..2)   attenuation / quantisation shift
bit  7     quiet
```

A header is valid iff `(h & 0x18) == 0` and `(h & 0x1F) <= 6`. This is a weak test — roughly 22%
of arbitrary bytes pass it — so it is useful only as a fast reject, never as confirmation that a
position is a real block boundary.

- **Block size is 256 samples.** The encoder (`CompBuf_`) processes the source in chunks of
  `<= 0x100`.
- **A quiet block carries `(256+1)>>1 = 128` samples**, decimated 2:1 by the encoder. The decoder
  must interpolate them back up (§7).
- `att` is not an independent field: it is the encoder's *retry level*. It starts at 0 and is
  incremented (capped at 2) while the block's estimated cost is poor.
- The header byte itself is written **through the bit packer** as an 8-bit data-plane value, not
  as a raw byte. With the packer freshly reset this drains immediately as one byte, so in practice
  it appears literally — but the packer state must be initialised as if it had been written.

Each block ends with a **flush** (§5).

---

## 3. Sample reconstruction

All modes except 6 are DPCM against a running predictor:

```
pred = 0x80                      # at the start of EVERY block
for each sample:
    d = sample - pred            # TRUE 32-bit difference, range -255..255
    if att:
        t = (d < 0) ? (mask + d) : d          # mask = (1 << att) - 1
        v = t >> att                          # arithmetic shift
    else:
        v = d
    pred = pred + (v << att)     # predictor takes the QUANTISED delta
    emit(v)
```

Two details that are easy to get wrong and invisible on ordinary speech:

- **`d` is never wrapped to 8-bit signed**, and **`pred` is never masked to 8 bits.** Both are
  plain 32-bit integers. On speech `|d|` stays small so a wrapped implementation agrees; on sharp
  transients it diverges.
- The predictor is updated with `v << att` — the **quantised** delta — not with `d`.

**ACMP is lossy whenever `att > 0`**, because `v = d >> att` discards the low bits. A decoder can
therefore only reproduce the *encoder's reconstruction*, never the original samples. Any test that
compares a decode against the source audio will fail by the quantisation error alone.

---

## 4. The two bit planes

This is the part that makes ACMP hard. The encoder maintains **two independent bit accumulators**
that are flushed into **one** byte stream:

| plane | accumulator | bit count | purpose |
|---|---|---|---|
| **tally** | `accT` | `bitsT` | self-delimiting "comma" codes: code lengths, prefixes |
| **data** | `accD` | `bitsD` | payload bits |

Both are LSB-first FIFOs: bits are appended at position `bits`, and a byte is emitted as
`acc & 0xFF` followed by `acc >>= 8`.

### 4.1 Tally codes are self-delimiting

`CountBits(k)` writes `n = min(k, 10)` bits, alternating form on every call via a `toggle` flag
that flips at entry:

```
toggle becomes 1:  (n-1) ones  then a 0      terminator = 0
toggle becomes 0:  (n-1) zeros then a 1      terminator = 1
```

So given the bits and the current phase, the split into codes is **unique**: read LSB-first, stop
at this code's expected terminator, `n` = bits consumed. **No search is required to recover the
code lengths** — this is the key to decoding the format at all.

If `k >= 10`, the 10-bit comma code is followed by `k` written as **6 data-plane bits**.

### 4.2 The `lf` interleave state

A single flag (`lf`, initial `-1`) decides which plane may drain:

```
after a tally write:  if lf != 0 and bitsT != 0:  lf = 1
after a data  write:  if lf != 1 and bitsD != 0:  lf = 0
```

- the **tally fire** happens when `lf == 1 and bitsT >= 8`: emit one tally byte, then drain the
  data plane, then set `lf = (bitsD > 0) ? 0 : -1`
- the **data fire** happens when `lf == 0 and bitsD >= 8`: emit one data byte, then drain the
  tally plane, then set `lf = (bitsT > 0) ? 1 : -1`
- a plane **defers** (does not drain) while it holds the other role: data skips its drain while
  `lf == 1 and no overflow pending`; tally skips while `lf == 0 and no overflow pending`

`bitsT` and `bitsD` may never exceed 32 — the encoder treats that as a fatal error and exits.

### 4.3 The retroactive write-back ("the dance")

When a plane is deferring and would exceed 32 pending bits, the encoder:

1. records the current file position (`ftell`),
2. writes a **zero placeholder byte** there,
3. drains the *other* pending bytes normally (so they land at later positions),
4. later, when the deferring plane finally fires, **seeks back and overwrites the placeholder**
   with that byte.

Trigger conditions are asymmetric:

```
data dance:   lf == 1 and no overflow pending and bitsD + n > 32
tally dance:  lf == 0 and no overflow pending and bitsT + n > 32
```

**Consequence: file byte order is not bit order.** The byte at the placeholder position carries
bits that were accumulated *after* the bits in the bytes following it. A decoder must model this
explicitly; reading strictly forward and assigning planes by demand will desynchronise.

Note also that a placeholder opened by one plane is frequently **resolved by the other** — a
data-plane dance is commonly filled with a tally byte. There is only one overflow slot.

### 4.4 What this means for a decoder

A decoder cannot parse code `i` purely from already-emitted bytes. Code `i` needs tally bit
`B = n_1 + ... + n_{i-1}`, which lives in tally byte `B/8`, and the encoder emits that byte only
once cumulative bits reach `8(B/8 + 1) > B` — i.e. during code `i` **or later**, always after
`n_i` is known.

**Read-ahead is therefore mandatory, not an optimisation**, and it must be reconciled against the
encoder's later emissions so that a byte grabbed early is not consumed twice. In particular:

> If a tally byte has already been pulled as read-ahead when a data dance fires, **that byte is
> the placeholder slot.** Its (already patched) content is correct; claim its position rather than
> consuming a fresh byte.

Getting this wrong costs exactly one byte of alignment and then corrupts everything downstream.

---

## 5. Flush

Every block ends with:

```
lf == 0 :  PutBits(0, 7)   then pad-drain the tally plane   (emit while bitsT > 0)
lf == 1 :  CountBits(7)    then pad-drain the data  plane   (emit while bitsD > 0)
lf == -1:  nothing
```

The pad-drain emits partial bytes. A decoder has usually already consumed these as read-ahead
while parsing the block's final codes, so it must account for them rather than fetch again.

---

## 6. The seven modes

`v` is the quantised delta from §3 (except mode 6, which has no predictor).
`z` is the zigzag of `v`: `z = 2|v|` for `v >= 0`, `z = 2|v| + 1` for `v < 0`.

| mode | per sample |
|---|---|
| **0** | the whole block is one repeated sample. Emits a single raw 8-bit value; the decoder fills all `count` samples with it. Block is 2 bytes. |
| **1** | `v == 0` → `CountBits(1)` **only, no data bits**. Otherwise `b = bitlen(\|v\|)`; `CountBits(b+1)`; then the low `b` bits of `z` (its top bit is implicit). |
| **2** | 4 data bits = `v + 8` (biased). The value `0` is an **escape**: emit `0`, then `v` in `8 - att` bits, two's complement. **Mode 2 never touches the tally plane.** |
| **3** | `k = (\|v\| >> 2) + 1`; `CountBits(k)`; then **always 3** data bits = `z & 7`. Reconstruct `z = 8(k-1) + P`. |
| **4** | `k = (\|v\| >> 3) + 1`; `CountBits(k)`; then **always 4** data bits. Reconstruct `z = 16(k-1) + P`. |
| **5** | if `-1 <= v <= 1`: 2 data bits = `v & 3` (so `0`, `1`, `3`; the value `2` is reserved). Otherwise emit `2` in 2 bits as an **escape**, then `b = bitlen(\|v\|)`, `CountBits(b-1)`, then `b` data bits of `z` (top bit implicit). |
| **6** | no predictor, no tally. `v = sample`; if `att`, `v = min(sample + (1 << (att-1)), 0xFF) >> att`. Emit `v` in `8 - att` bits. Decode as `v << att`. |

**Order matters and differs per mode.** Modes 1/3/4 write the tally code first, then data.
Mode 5 writes data first and only reaches the tally plane on an escape. Modes 2 and 6 are
pure data. Because the packer's interleave state is order-sensitive, this cannot be reduced to a
per-mode "payload width" table.

Modes 3 and 4 share one rule: `k = (|v| >> (w-1)) + 1` with payload width `w` (3 or 4), giving
`|v| = (1 << (w-1)) * (k-1) + (P >> 1)` and `sign = P & 1`.

---

## 7. Quiet blocks and interpolation

If the encoder's high-frequency measure for a chunk falls below a threshold (4.0), it marks the
block **quiet**, halves the sample count to `(count+1)>>1`, and encodes a 2:1-decimated copy of
the window. The decoder must interpolate back to full rate.

The encoder builds its filter at runtime; the coefficients are a Hamming-windowed sinc:

```
coef[k>>1] = 0.5 * sin(k*pi/2) * (0.54 - 0.46*cos((k+21)*pi/21)) / (k*pi/2)     k odd, 1..21
```

giving `0.316674, -0.101270, 0.055845, -0.035014, 0.022719, -0.014631, ...` (11 taps).
Upsampling keeps the original samples and synthesises the midpoints:

```
y[2i]   = x[i]
y[2i+1] = 128 + sum_m coef[m] * ((x[i-m] - 128) + (x[i+1+m] - 128))
```

⚠ **The kernel sums to roughly 0.5 per side, so it must be applied to DC-centred samples with the
128 bias restored afterwards.** Filtering raw unsigned values instead drags every synthesised
sample toward 64 and produces a violent sawtooth over otherwise-correct audio.

---

## 8. Verifying an implementation

Statistics and listening tests are not sufficient — a self-consistent but wrong parse can produce
smooth, speech-like output. Two checks are decisive:

1. **Per-block round-trip.** Decode a block, re-encode the recovered samples with a
   reimplementation of the packer, and require the emitted bytes to equal the file bytes. This
   catches sample-value errors and interleave errors together.
2. **Differential against the original encoder.** `MAKEVCC.EXE` is 32-bit flat x86 inside a
   DOS/4GW LE image and can be run under an emulator (we used Unicorn), calling each `DoCompN_`
   directly and hooking `ftell`/`fseek`/`fputc`. That gives byte-exact ground truth for every
   mode and attenuation, including paths no real clip happens to exercise.

Useful invariant while bringing an implementation up: **block extents must chain.** A clip's
blocks are contiguous and their count is in the mini-header, so a correct decoder consumes the
payload exactly and reaches the stated block count.

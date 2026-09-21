"""
acmp -- decoder for Interplay's ACMP speech codec (1993).

ACMP is the undocumented audio codec inside the .VCC containers of Star Trek: Judgment Rites.
It is NOT the later Interplay ACM codec (magic 0x97280301) that FFmpeg and libacm handle --
different structure entirely. See FORMAT.md for the full specification.

Everything here was derived from MAKEVCC.EXE, the original encoder shipped on the game CD, and
verified byte-exactly against it. See the project README for what "verified" means precisely.

Public API
----------
    clips(path)                  -> [(name, offset, size, block_count, rate_flag), ...]
    decode_block(payload, pos)   -> (samples, next_pos, mode, att, quiet)
    encode_block(hdr, samples, mode, att) -> bytes   (the packer mirror, for verification)
    decode_clip(payload)         -> [samples]        (chains every block)
    write_wav(path, samples, rate)
"""
import math
import struct
import wave

# Samples per block. CompBuf_ processes the input in chunks of <= 0x100; a "quiet" block
# (header bit 7) carries (COUNT+1)>>1 samples, decimated 2:1, and is interpolated back up.
COUNT = 256


# ---------------------------------------------------------------- container

def _clips_impl(path):
    data = open(path, "rb").read()
    assert data[:8] == b"VOCFILES", data[:8]
    paylen, n = struct.unpack_from("<II", data, 8)
    foot = paylen
    out = []
    for i in range(n):
        r = data[foot + i * 16: foot + i * 16 + 16]
        name = r[:8].rstrip(b"\x00").decode("ascii", "replace")
        off, size = struct.unpack("<II", r[8:16])
        c4 = struct.unpack_from("<H", data, off + 27)[0] if off + 30 <= len(data) else 0
        rate = struct.unpack_from("<H", data, off + 25)[0] if off + 30 <= len(data) else 0
        out.append((name, off, size, c4, rate))
    return data, out


# ------------------------------------------------------------ bit packer

class Enc:
    """Mirror of MAKEVCC's two-plane packer (CompPutTally_ / CompPutData_ / CompPutFlush_).

    Unchanged from _enc_rt.py -- these primitives already round-tripped the AA region and
    are corroborated by the lifted IR (defer rule, ftell-save overflow, flush semantics).
    Only the per-mode CALLER below was wrong.
    """

    def __init__(s):
        s.accD = s.bitsD = s.accT = s.bitsT = 0
        s.lf = -1
        s.toggle = 0
        s.ov = None
        s.out = bytearray()

    def emit(s, b):
        s.out.append(b & 0xFF)

    def drain_data(s):
        while s.bitsD >= 8:
            s.emit(s.accD & 0xFF); s.accD >>= 8; s.bitsD -= 8

    def drain_tally(s):
        while s.bitsT >= 8:
            s.emit(s.accT & 0xFF); s.accT >>= 8; s.bitsT -= 8

    def countbits(s, k):
        n = min(k, 10)
        if s.lf == 0 and s.ov is None and s.bitsT + n > 32:
            # ftell-save + zero placeholder; the real byte is written back later
            s.emit(0); s.ov = len(s.out) - 1; s.drain_tally()
        s.toggle ^= 1
        if s.toggle == 1:
            s.accT |= ((1 << (n - 1)) - 1) << s.bitsT
        else:
            s.accT |= 1 << (s.bitsT + n - 1)
        s.bitsT += n
        if s.lf == 1 and s.bitsT >= 8:
            if s.ov is not None:
                s.out[s.ov] = s.accT & 0xFF      # retroactive write-back
                s.ov = None; s.accT >>= 8; s.bitsT -= 8
            else:
                s.emit(s.accT & 0xFF); s.accT >>= 8; s.bitsT -= 8
            s.drain_data()
            s.lf = 0 if s.bitsD > 0 else -1
        if s.lf != 0 or s.ov is not None:
            s.drain_tally()
        if s.lf != 0 and s.bitsT != 0:
            s.lf = 1
        if k >= 10:
            s.putbits(6, k & 0x3F)

    def putbits(s, n, v):
        if s.lf == 1 and s.ov is None and s.bitsD + n > 32:
            s.emit(0); s.ov = len(s.out) - 1; s.drain_data()
        s.accD |= (v & ((1 << n) - 1)) << s.bitsD
        s.bitsD += n
        if s.lf == 0 and s.bitsD >= 8:
            if s.ov is not None:
                s.out[s.ov] = s.accD & 0xFF      # retroactive write-back
                s.ov = None; s.accD >>= 8; s.bitsD -= 8
            else:
                s.emit(s.accD & 0xFF); s.accD >>= 8; s.bitsD -= 8
            s.drain_tally()
            s.lf = 1 if s.bitsT > 0 else -1
        if not (s.lf == 1 and s.ov is None):
            s.drain_data()
        if s.lf != 1 and s.bitsD != 0:
            s.lf = 0

    def flush(s):
        if s.lf == 0:
            s.putbits(7, 0)
            while s.bitsT > 0:
                s.emit(s.accT & 0xFF); s.accT >>= 8; s.bitsT -= 8
        elif s.lf == 1:
            s.countbits(7)
            while s.bitsD > 0:
                s.emit(s.accD & 0xFF); s.accD >>= 8; s.bitsD -= 8


# --------------------------------------------------------- forward decoder

class Fwd:
    """Forward decoder: pulls bytes on demand, parses comma codes directly."""

    def __init__(s, data, hdrpos, END):
        s.data = data
        s.hdrpos = hdrpos
        s.END = END
        s.pos = hdrpos + 1
        # encoder-side bookkeeping (mirrors Block)
        s.bitsT = 0
        s.bitsD = 0
        s.lf = -1
        s.toggle = 0
        s.ov = None
        # decoder-side bit queues, fed by bytes as they are routed
        s.tq = []          # tally bits, FIFO, LSB-first within each byte
        s.dq = []          # data bits, FIFO
        s.owed = []        # planes the encoder emitted, not yet physically consumed
        s.ahead = {"T": 0, "D": 0}
        s.ov_done = False  # the pending placeholder's bits have already been queued
        s.ra_tally = []    # positions of TALLY bytes pulled as read-ahead
        s.ks = []
        s.pay = []          # (k, payload) per sample, for value reconstruction
        s.trace = []       # (pos, plane) for every byte consumed
        s.planes = {}      # pos -> FINAL plane (a dance placeholder is re-assigned at resolution)

    # ---- byte routing ---------------------------------------------------
    def _take(s, plane):
        if s.pos >= s.END:
            raise EOFError("overrun at %d" % s.pos)
        b = s.data[s.pos]
        s.trace.append((s.pos, plane))
        s.planes[s.pos] = plane
        s.pos += 1
        bits = [(b >> i) & 1 for i in range(8)]
        if plane == "T":
            s.tq.extend(bits); s.ahead["T"] = s.ahead.get("T", 0) + 1
        elif plane == "D":
            s.dq.extend(bits); s.ahead["D"] = s.ahead.get("D", 0) + 1
        return b

    def _inject(s, plane, b, pos=None):
        """Queue a byte's bits at RESOLUTION time.

        The encoder emits a zero placeholder, drains EARLIER bits into the following
        bytes, then seeks back and overwrites the placeholder with LATER bits. So file
        order is not bit order, and the dance byte must enter the queue here, not when
        its file position was passed.
        """
        bits = [(b >> i) & 1 for i in range(8)]
        (s.tq if plane == "T" else s.dq).extend(bits)
        if pos is not None:
            s.planes[pos] = plane        # the placeholder's TRUE plane, known at resolution

    def _drain_owed(s):
        while s.owed:
            s._take(s.owed.pop(0))

    def _need_tally(s, n):
        while len(s.tq) < n:
            if s.owed:
                s._take(s.owed.pop(0))     # emission order first
            elif s.ov is not None and not s.ov_done:
                # A placeholder is outstanding and the parser needs TALLY bits, so the
                # encoder's next tally emission IS the write-back into that placeholder
                # (Enc: countbits' fire does `out[ov] = accT & 0xFF`). Its bits must be
                # read BEFORE this code can be parsed -- resolution happens afterwards.
                s._inject("T", s.data[s.ov], s.ov)
                s.ov_done = True
            else:
                s._take("T")               # read-ahead: the encoder has not emitted yet
                s.ra_tally.append(s.pos - 1)

    def _need_data(s, n):
        while len(s.dq) < n:
            if s.owed:
                s._take(s.owed.pop(0))
            elif s.ov is not None and not s.ov_done:
                s._inject("D", s.data[s.ov], s.ov)      # symmetric case
                s.ov_done = True
            else:
                s._take("D")

    # ---- the self-delimiting comma-code parse ---------------------------
    def read_code(s):
        """Parse ONE comma code from the tally stream. Returns n. No search."""
        s.toggle ^= 1
        term = 0 if s.toggle == 1 else 1
        n = 0
        while True:
            s._need_tally(n + 1)
            bit = s.tq[n]
            n += 1
            if bit == term:
                break
            if n > 10:            # n is capped at 10 by CountBits
                break
        del s.tq[:n]
        return n

    # ---- state-machine bookkeeping (mirrors Block.countbits/putbits) ----
    def after_countbits(s, n):
        s.bitsT += n
        if s.lf == 1 and s.bitsT >= 8:
            if s.ov is not None:
                if not s.ov_done:
                    s._inject("T", s.data[s.ov], s.ov)
                s.ov = None; s.ov_done = False
            else:
                s._emitted("T")          # the fired tally byte
            s.bitsT -= 8
            s.drain_data()
            s.lf = 0 if s.bitsD > 0 else -1
        if s.lf != 0 or s.ov is not None:
            s.drain_tally()
        if s.lf != 0 and s.bitsT != 0:
            s.lf = 1

    def putbits(s, n):
        if s.lf == 1 and s.ov is None and s.bitsD + n > 32:
            # The encoder writes its placeholder at the CURRENT output position and later
            # patches it with tally content. If we already pulled a tally byte as
            # read-ahead, THAT byte is the placeholder slot -- its (patched) bits are
            # already queued, so claim the position instead of consuming a new byte.
            if s.ra_tally:
                s.ov = s.ra_tally.pop(0); s.ov_done = True
            else:
                s.ov = s.pos; s.ov_done = False
                s._take("X")
            s.drain_data()
        s.bitsD += n
        if s.lf == 0 and s.bitsD >= 8:
            if s.ov is not None:
                if not s.ov_done:
                    s._inject("D", s.data[s.ov], s.ov)
                s.ov = None; s.ov_done = False
            else:
                s._emitted("D")          # the emitted data byte
            s.bitsD -= 8
            s.drain_tally()
            s.lf = 1 if s.bitsT > 0 else -1
        if not (s.lf == 1 and s.ov is None):
            s.drain_data()
        if s.lf != 1 and s.bitsD != 0:
            s.lf = 0

    def drain_data(s):
        while s.bitsD >= 8:
            s._emitted("D")
            s.bitsD -= 8

    def _emitted(s, plane):
        """The simulation says the encoder wrote a byte of `plane` here.

        If the parser already read that byte ahead of the simulation, cancel against it;
        otherwise queue it so the next read consumes it in the right order.
        """
        if s.ahead.get(plane, 0) > 0:
            s.ahead[plane] -= 1
        else:
            s.owed.append(plane)

    def drain_tally(s):
        while s.bitsT >= 8:
            s._emitted("T")
            s.bitsT -= 8

    # ---- main loop ------------------------------------------------------
    # payload width per mode, from the lifted IR (tools/acmp_ir/DoComp*.ir.txt):
    #   mode 1 -> w = k-1, and d==0 (k==1) consumes NO data bits
    #   mode 3 -> rdx=3; CompPutData_()  : ALWAYS 3 bits
    #   mode 4 -> rdx=4; CompPutData_()  : ALWAYS 4 bits
    def payload_width(s, mode, k):
        if mode == 1:
            return 0 if k <= 1 else k - 1
        if mode == 3:
            return 3
        if mode == 4:
            return 4
        return 0 if k <= 1 else k - 1

    def flush(s):
        """Mirror of CompPutFlush_ (0x11e88) / Block.flush -- consumes the block tail.

        lf == 0  -> PutBits(0,7) then pad-drain the tally plane
        lf == 1  -> CountBits(7) then pad-drain the data plane
        lf == -1 -> nothing

        The CountBits(7) arm must run the real fire/drain bookkeeping (after_countbits),
        not just `bitsT += 7`; faking it mis-routes the tail bytes.
        """
        # The pad-drain must go through _emitted(), NOT _take(): the encoder emits these
        # bytes, but the decoder has usually already pulled them as read-ahead while
        # parsing the final codes. Taking again runs off the end of the block -- which is
        # exactly the EOFError that dominated acmp_decdiff's failures.
        if s.lf == 0:
            s.putbits(7)
            while s.bitsT > 0:
                s._emitted("T"); s.bitsT -= 8
        elif s.lf == 1:
            s.toggle ^= 1
            s.after_countbits(7)
            while s.bitsD > 0:
                s._emitted("D"); s.bitsD -= 8

    def read_data(s, w):
        """Consume w data-plane bits (LSB-first) and run the packer bookkeeping."""
        if w <= 0:
            return 0
        s._need_data(w)
        v = 0
        for i in range(w):
            v |= s.dq[i] << i
        del s.dq[:w]
        s.putbits(w)
        return v

    def read_k(s):
        """One comma code, plus CountBits' 6-bit escape when k >= 10."""
        n = s.read_code()
        s.after_countbits(n)
        if n >= 10:
            v = s.read_data(6)
            return v if v else 64
        return n

    def step(s, mode, att=0):
        """Consume exactly one sample's bits for `mode`. Returns the logged code.

        All seven encoders read from MAKEVCC machine code (TRUE symbol addresses).
        ORDER MATTERS and differs per mode -- the packer state machine is order-sensitive,
        so this cannot be reduced to a per-mode payload width:

          mode 1  DoComp1_ 0x1242c  tally k, then (k-1) data bits; k==1 (d==0) -> NO data
          mode 3  DoComp3_ 0x125a4  tally k = (|v|>>2)+1, then ALWAYS 3 data bits
          mode 4  DoComp4_ 0x12644  tally k = (|v|>>3)+1, then ALWAYS 4 data bits
          mode 2  DoComp2_ 0x12500  4 data bits (v+8 biased); 0 == escape -> (8-att) more
                                     NO tally codes at all -- pure data plane
          mode 5  DoComp5_ 0x126e4  2 data bits; 2 == escape -> tally (b-1), then b bits
                                     DATA FIRST, then tally (opposite of 1/3/4)
          mode 6  DoComp6_ 0x127c0  (8-att) data bits, raw bit-plane truncation.
                                     No predictor, no tally.
          mode 0  DoComp0_ 0x123a8  one raw 8-bit sample for the whole block
        """
        if mode in (1, 3, 4):
            k = s.read_k()
            w = 3 if mode == 3 else 4 if mode == 4 else (0 if k <= 1 else k - 1)
            s.pay.append((k, s.read_data(w)))
            return k
        if mode == 2:
            c = s.read_data(4)          # v + 8, biased; 0 == escape
            P = s.read_data(8 - att) if c == 0 else 0
            s.pay.append((c, P))
            return c
        if mode == 5:
            c = s.read_data(2)          # direct code, or 2 == escape
            if c != 2:
                s.pay.append(("d", c, 0))
                return 0
            t = s.read_k()              # CompPutTally_(b-1)
            P = s.read_data(t + 1)      # b = t+1 bits, implicit MSB
            s.pay.append(("e", t, P))
            return t
        if mode == 6:
            s.pay.append((0, s.read_data(8 - att)))
            return 0
        if mode == 0:
            return 0
        raise NotImplementedError("mode %d" % mode)

    def run(s, max_codes=100000, mode=1, att=0):
        # `or s.dq` matters: modes 2 and 6 never touch the tally plane, so tq is always
        # empty and the loop used to exit the instant pos hit END -- discarding data bits
        # still buffered and reporting a short block.
        while s.pos < s.END or s.tq or s.dq:
            if len(s.ks) >= max_codes:
                break
            try:
                k = s.step(mode, att)
            except EOFError:
                break
            s.ks.append(k)
        return s.ks


# ------------------------------------------------------------ block layer

def valid_hdr(h):
    return (h & 0x18) == 0 and (h & 0x1F) <= 6


def decode_block(data, pos):
    """Forward-decode one block. Returns (samples, next_pos, mode, att, quiet)."""
    h = data[pos]
    mode, att, quiet = h & 0x1F, (h >> 5) & 3, h >> 7
    if mode == 0:
        return [data[pos + 1]], pos + 2, mode, att, quiet
    nc = ((COUNT + 1) >> 1) if quiet else COUNT
    f = Fwd(data, pos, min(pos + 9000, len(data)))
    ks = f.run(max_codes=nc, mode=mode, att=att)
    if len(ks) < nc:
        raise ValueError("short block: %d < %d" % (len(ks), nc))
    f.flush()
    samples, pred = [], 0x80
    if mode == 6:
        # no predictor: the payload IS the sample, bit-plane truncated
        for (_k, P) in f.pay:
            samples.append(min(255, P << att))
        return samples, f.pos, mode, att, quiet
    if mode == 5:
        for tag, a, b_ in f.pay:
            if tag == "d":
                v = 0 if a == 0 else (1 if a == 1 else -1)   # 2-bit signed: 0,1,3->-1
            else:
                b = a + 1                     # tally carried b-1
                z = (1 << b) | b_             # implicit MSB
                v = -(z >> 1) if (z & 1) else (z >> 1)
            pred = pred + (v << att)
            samples.append(pred & 0xFF)
        return samples, f.pos, mode, att, quiet
    for (k, P) in f.pay:
        if mode == 1:
            if k <= 1:
                v = 0
            else:
                b = k - 1
                z = (1 << b) | P            # implicit MSB
                v = -(z >> 1) if (z & 1) else (z >> 1)
        elif mode in (3, 4):
            w = 3 if mode == 3 else 4
            z = (1 << w) * (k - 1) + P
            v = -(z >> 1) if (z & 1) else (z >> 1)
        elif mode == 2:
            if k != 0:
                v = k - 8                          # 4-bit direct, biased +8
            else:
                w = 8 - att                        # escape: full value, sign-extended
                v = P - (1 << w) if P >= (1 << (w - 1)) else P
        else:
            raise NotImplementedError("value recon for mode %d" % mode)
        pred = pred + (v << att)
        samples.append(pred & 0xFF)
    return samples, f.pos, mode, att, quiet


def encode_block(hdr, samples, mode, att):
    """Re-encode through the packer mirror. Returns the block's bytes including header."""
    e = Enc()
    if mode == 0:
        e.putbits(8, samples[0])
        e.flush()
        return bytes([hdr]) + bytes(e.out)

    if mode == 6:
        # DoComp6_ 0x127c0: raw bit-plane truncation of the SAMPLE. No predictor,
        # no tally.  v = min(sample + (1<<(att-1)), 0xff) >> att, written in 8-att bits.
        for s in samples:
            v = s
            if att:
                v = min(v + (1 << (att - 1)), 0xFF) >> att
            e.putbits(8 - att, v)
        e.flush()
        return bytes([hdr]) + bytes(e.out)

    mask = (1 << att) - 1
    pred = 0x80
    for s in samples:
        # TRUE 32-bit difference -- the encoder does `xor ecx,ecx; mov cl,[esi];
        # sub ecx,edi`, so d spans -255..255 and is NEVER wrapped to 8-bit signed.
        # Wrapping is invisible on speech (|d| stays small) but wrong on transients.
        d = s - pred
        if att:
            v = ((mask + d) if d < 0 else d) >> att
        else:
            v = d
        pred = pred + (v << att)        # 32-bit accumulator, not masked
        m = abs(v)
        if mode == 1:
            if v == 0:
                e.countbits(1)
            else:
                b = m.bit_length()
                z = 2 * m + (1 if v < 0 else 0)
                e.countbits(b + 1)
                e.putbits(b, z)             # low b bits; MSB implicit
        elif mode in (3, 4):
            w = 3 if mode == 3 else 4
            k = (m >> (w - 1)) + 1
            z = 2 * m + (1 if v < 0 else 0)
            e.countbits(k)
            e.putbits(w, z & ((1 << w) - 1))
        elif mode == 5:
            if -1 <= v <= 1:
                e.putbits(2, v & 3)               # 2-bit direct
            else:
                e.putbits(2, 2)                   # escape marker
                b = m.bit_length()
                z = 2 * m + (1 if v < 0 else 0)
                e.countbits(b - 1)
                e.putbits(b, z)                   # low b bits; MSB implicit
        elif mode == 2:
            if -7 <= v <= 7:
                e.putbits(4, v + 8)               # 4-bit direct, biased +8
            else:
                e.putbits(4, 0)                   # 0 == escape
                e.putbits(8 - att, v)             # full value, (8-att) bits
        else:
            raise NotImplementedError("encode for mode %d" % mode)
    e.flush()
    return bytes([hdr]) + bytes(e.out)


# ------------------------------------------------- quiet-block upsampling

def fir_taps():
    """Half-band interpolation kernel from InitFilter_ (see module docstring)."""
    taps = []
    for k in range(1, 22, 2):
        x = k * math.pi / 2.0
        taps.append(0.5 * math.sin(x) * (0.54 - 0.46 * math.cos((k + 21) * math.pi / 21.0)) / x)
    return taps


TAPS = fir_taps()


def upsample_fir(x):
    """2x upsample: keep originals, synthesise the in-between samples with the kernel."""
    n = len(x)
    out = []
    for i in range(n):
        out.append(x[i])
        # The kernel sums to ~0.5 per side, so it must be applied to DC-CENTRED samples
        # and the 128 bias restored afterwards -- exactly as CompBuf_ does with
        # `0.5*(raw-128) + FIR_acc + 128.5`. Filtering raw unsigned values instead drags
        # every synthesised sample toward 64 and produces a violent sawtooth.
        acc = 0.0
        for m, c in enumerate(TAPS):
            a = x[i - m] if i - m >= 0 else x[0]
            b = x[i + 1 + m] if i + 1 + m < n else x[-1]
            acc += c * ((a - 128) + (b - 128))
        out.append(max(0, min(255, int(round(acc + 128.5)))))
    return out


def upsample_hold(x):
    out = []
    for s in x:
        out.append(s); out.append(s)
    return out


def write_wav(path, samples, rate):
    w = wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(1)              # 8-bit unsigned, matching the source
    w.setframerate(rate)
    w.writeframes(bytes(bytearray(samples)))
    w.close()
    return len(samples) / float(rate)


def decode_clip(payload, count=COUNT, upsample="fir"):
    """Decode every block in a clip payload. Returns 8-bit unsigned samples.

    Quiet blocks are interpolated back to full rate with the encoder's own half-band kernel
    ("fir", default) or by plain sample doubling ("hold").
    """
    global COUNT
    prev, COUNT = COUNT, count
    try:
        up = upsample_fir if upsample == "fir" else upsample_hold
        pos, out = 0, []
        while pos < len(payload):
            if not valid_hdr(payload[pos]):
                break
            try:
                samples, nxt, mode, att, quiet = decode_block(payload, pos)
            except Exception:
                break
            if nxt <= pos:
                break
            if mode == 0:
                samples = samples * (((COUNT + 1) >> 1) if quiet else COUNT)
            out.extend(up(samples) if quiet else samples)
            pos = nxt
        return out
    finally:
        COUNT = prev


def verify_block(payload, pos):
    """Round-trip one block through the packer mirror. True iff byte-exact.

    This is the correctness gate: decode a block, re-encode the recovered samples, and require
    the emitted bytes to equal the file bytes. It does not rely on statistics or listening.
    """
    h = payload[pos]
    samples, nxt, mode, att, quiet = decode_block(payload, pos)
    return encode_block(h, samples, mode, att) == payload[pos:nxt]


def clips(path):
    """List every clip in a VCC container.

    Returns [(name, offset, size, block_count, rate_flag), ...] where `offset` is the
    FILE-ABSOLUTE position of that clip's 30-byte mini-header.
    """
    return _clips_impl(path)[1]


def payload(data, clip):
    """The raw ACMP bitstream of one clip: everything after its 30-byte mini-header."""
    off, size = clip[1], clip[2]
    return data[off + 30: off + size]

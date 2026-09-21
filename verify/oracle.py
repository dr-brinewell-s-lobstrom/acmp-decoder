#!/usr/bin/env python3
"""
oracle.py -- run MAKEVCC's REAL encoder under Unicorn and capture its byte stream.

Why: every mode/attenuation combination has been inferred one path at a time from whichever
blocks happened to appear in clip001. That produced a decoder correct on 35 blocks and wrong
almost everywhere else. Executing the original encoder turns "derive the format" into
"observe the format" -- and gives a byte-exact oracle for all seven modes at once.

How it works
------------
MAKEVCC is a 32-bit flat DOS/4GW image. Loaded at its link addresses (code VA 0x10000 from
file 0x5400, data VA 0x20000 from file 0x13400) every internal address resolves as-is.

The encoder emits bytes through a FILE* held in the global 0x1468. Its writer has a fast
path that pokes the stdio buffer directly, guarded by:

    mov edx,[0x1468] ; test byte ptr [edx+0xd],4 ; jne <fputc path>

so setting bit 2 of FILE+0xd forces EVERY byte through fputc. That reduces the C runtime
surface to three functions, which are hooked in Python rather than emulated:

    0x1405b  ftell(FILE* in eax)                    -> position in eax
    0x13a15  __update_buffer_ / fseek(eax=FILE*, edx=offset, ebx=whence)
    0x148e8  fputc(eax=char, edx=FILE*)

The virtual file is a bytearray with a position, so the retroactive "dance"
(ftell -> seek back -> overwrite -> seek forward) is reproduced exactly.

Entry point (from the disassembly of CompBuf_ @0x128dc):

    CompBuf_(eax = sample buffer, edx = decimated buffer, ebx = count, ecx = FILE*)
"""
import sys, os, struct, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from unicorn import *
from unicorn.x86_const import *

EXE = os.environ.get("MAKEVCC", "MAKEVCC.EXE")   # override with --exe or $MAKEVCC

# The real program runs CS base = 0x10000, DS base = 0:
#   * near calls resolve as 0x1xxxx, so code sits at linear 0x10000
#   * data displacements are ZERO-based -- [0x763] is the 4.0 quiet threshold at
#     file 0x13400+0x763, so obj2 maps at linear 0 (NOT its LE relocbase 0x20000)
# Unicorn's flat mode gives both segments base 0, so the one cs:-relative access
# (the mode dispatch table) is patched instead of emulating a GDT. See _patch_dispatch.
CODE_FILE, CODE_VA, CODE_LEN = 0x5400, 0x10000, 14 * 0x1000
DATA_FILE, DATA_VA, DATA_LEN = 0x13400, 0x00000, 2 * 0x1000
DISPATCH_JMP = 0x12ad6          # jmp dword ptr cs:[ecx*4 + 0x28c0]
DISPATCH_TAB = 0x28c0           # CS-relative

BSS = 0x00040000          # writable slack for globals above the data object
STACK = 0x00200000
HEAP = 0x00300000
MEMTOP = 0x00400000

FILEP = HEAP + 0x0000     # fake FILE struct
BUF_S = HEAP + 0x1000     # sample buffer
BUF_D = HEAP + 0x2000     # decimated buffer

FTELL = 0x1405b
FSEEK = 0x13a15
FPUTC = 0x148e8

CompBuf_ = 0x128dc
InitFilter_ = 0x12b68
CompPutData_ = 0x123b4
CompPutFlush_ = 0x11e88
# TRUE DoComp addresses (see MAJEL.md Session 8b -- makevcc_syms.json names are shifted)
DOCOMP = {0: 0x123a8, 1: 0x1242c, 2: 0x12500, 3: 0x125a4,
          4: 0x12644, 5: 0x126e4, 6: 0x127c0}

RET_MAGIC = 0x00111000


class Oracle:
    def __init__(s, trace=False, exe=None):
        s.trace = trace
        s.exe = exe or EXE
        s.vf = bytearray()
        s.pos = 0
        s.events = []
        img = open(s.exe, "rb").read()
        s.mu = Uc(UC_ARCH_X86, UC_MODE_32)
        s.mu.mem_map(0, MEMTOP)
        s.mu.mem_write(CODE_VA, img[CODE_FILE:CODE_FILE + CODE_LEN])
        s.mu.mem_write(DATA_VA, img[DATA_FILE:DATA_FILE + DATA_LEN])
        s._patch_dispatch()
        s.mu.mem_write(RET_MAGIC, b"\xF4")          # HLT sentinel
        # FILE struct: only +0xd matters (force fputc), rest zero
        s.mu.mem_write(FILEP, b"\x00" * 0x40)
        s.mu.mem_write(FILEP + 0x0d, bytes([0x04]))
        s.mu.hook_add(UC_HOOK_CODE, s._hook)

    def _patch_dispatch(s):
        """Make the mode-dispatch jump work under a flat CS base of 0.

        Real: CS base 0x10000, so `jmp cs:[ecx*4+0x28c0]` reads the table at linear
        0x128c0 and its entries (0x2ade ...) are CS-relative. Flat-base Unicorn would read
        linear 0x28c0 -- which collides with the data object -- and jump to 0x2ade.
        Fix: point the displacement at 0x128c0 and rebase the 7 entries to linear.
        """
        s.mu.mem_write(DISPATCH_JMP, bytes([0x2E, 0xFF, 0x24, 0x8D]) +
                       struct.pack("<I", CODE_VA + DISPATCH_TAB))
        tab = CODE_VA + DISPATCH_TAB
        ents = struct.unpack("<7I", s.mu.mem_read(tab, 28))
        s.mu.mem_write(tab, struct.pack("<7I", *[e + CODE_VA for e in ents]))

    # ---- hooked C runtime -------------------------------------------------
    def _hook(s, uc, addr, size, ud):
        if addr == FTELL:
            uc.reg_write(UC_X86_REG_EAX, s.pos)
            s._ret(uc)
        elif addr == FSEEK:
            off = uc.reg_read(UC_X86_REG_EDX)
            s.pos = off
            uc.reg_write(UC_X86_REG_EAX, 0)
            s._ret(uc)
        elif addr == FPUTC:
            ch = uc.reg_read(UC_X86_REG_EAX) & 0xFF
            while len(s.vf) < s.pos:
                s.vf.append(0)
            if s.pos < len(s.vf):
                s.vf[s.pos] = ch                     # retroactive overwrite (the dance)
                kind = "PATCH"
            else:
                s.vf.append(ch)
                kind = "write"
            if s.trace:
                esp = uc.reg_read(UC_X86_REG_ESP)
                caller = struct.unpack("<I", uc.mem_read(esp, 4))[0]
                s.events.append((kind, s.pos, ch, caller))
            s.pos += 1
            uc.reg_write(UC_X86_REG_EAX, ch)
            s._ret(uc)

    def _ret(s, uc):
        esp = uc.reg_read(UC_X86_REG_ESP)
        ret = struct.unpack("<I", uc.mem_read(esp, 4))[0]
        uc.reg_write(UC_X86_REG_ESP, esp + 4)
        uc.reg_write(UC_X86_REG_EIP, ret)

    # ---- driving the encoder ---------------------------------------------
    def reset_globals(s):
        for g in (0x1468, 0x146c, 0x1470, 0x1474, 0x1478, 0x147c, 0x1484, 0x1488):
            s.mu.mem_write(g, struct.pack("<I", 0))
        s.mu.mem_write(0x1480, struct.pack("<i", -1))

    def call(s, fn, eax=0, edx=0, ebx=0, ecx=0, budget=8_000_000):
        s.mu.reg_write(UC_X86_REG_EAX, eax)
        s.mu.reg_write(UC_X86_REG_EDX, edx)
        s.mu.reg_write(UC_X86_REG_EBX, ebx)
        s.mu.reg_write(UC_X86_REG_ECX, ecx)
        s.mu.reg_write(UC_X86_REG_ESP, STACK)
        s.mu.mem_write(STACK, struct.pack("<I", RET_MAGIC))
        s.mu.emu_start(fn, RET_MAGIC, 0, budget)

    def init_filter(s):
        """Populate the FIR coefficient table (data 0x1410, 21 doubles) -- it is BSS.

        HiFreqVar_ reads those coefficients to decide the quiet flag, so without this the
        quiet decision is made against zeroed taps and every block comes out non-quiet.
        """
        s.call(InitFilter_)

    def encode_mode(s, samples, mode, att=0, quiet=0):
        """Encode `samples` with ONE chosen mode, bypassing CompBuf_'s mode selection.

        Replicates CompBuf_'s emission sequence exactly:
            reset packer globals -> CompPutData_(hdr, 8) -> DoCompN_(buf, count, att)
            -> CompPutFlush_()
        This sidesteps HiFreqVar_ (an x87 routine Unicorn does not evaluate), which is only
        used to pick the quiet flag -- it has no role in how a block is encoded once the
        mode, att and count are fixed.

        Returns the bytes the REAL encoder emitted, header included.
        """
        s.vf = bytearray(); s.pos = 0; s.events = []
        s.reset_globals()
        s.mu.mem_write(0x1468, struct.pack("<I", FILEP))
        s.mu.mem_write(BUF_S, bytes(bytearray(samples)) + b"\x00" * 64)
        hdr = ((quiet & 1) << 7) | ((att & 3) << 5) | (mode & 0x1F)
        s.call(CompPutData_, eax=hdr, edx=8)
        s.call(DOCOMP[mode], eax=BUF_S, edx=len(samples), ebx=att)
        s.call(CompPutFlush_)
        return bytes(s.vf)

    def encode_block(s, samples, decimated=None, count=None):
        """Run CompBuf_ over `samples`; returns the bytes the real encoder emitted."""
        s.vf = bytearray(); s.pos = 0; s.events = []
        s.reset_globals()
        buf = bytes(bytearray(samples))
        dec = bytes(bytearray(decimated if decimated is not None else samples))
        s.mu.mem_write(BUF_S, buf + b"\x00" * 64)
        s.mu.mem_write(BUF_D, dec + b"\x00" * 64)
        s.mu.mem_write(0x1468, struct.pack("<I", FILEP))
        s.call(CompBuf_, eax=BUF_S, edx=BUF_D, ebx=count or len(samples), ecx=FILEP)
        return bytes(s.vf)

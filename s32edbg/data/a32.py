"""Just enough A32/T32 decoding to step over a call.

Scope is deliberately tiny. Step-over needs two facts about the instruction at
the PC and nothing else:

  * **How long is it?** T32 mixes 16- and 32-bit instructions, so the return
    address of a call is not a fixed distance away.
  * **Is it a call?** Only a call needs stepping *over*; everything else is a
    plain single step. Getting this wrong in the safe direction (calling a
    non-call a call) sets a breakpoint at the next instruction and resumes,
    which lands in the same place a single step would -- just more slowly. The
    unsafe direction (missing a call) steps *into* the callee, which is
    surprising but not damaging.

Deliberately not a disassembler. Anything beyond "how far to the next
instruction, and is this a branch-with-link" belongs in a real one.

Citations: Armv8-A Architecture Reference Manual, A32/T32 instruction set
encoding. The T32 32-bit-instruction rule is F3.1: a halfword whose bits [15:11]
are 0b11101, 0b11110 or 0b11111 begins a 32-bit instruction.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instruction:
    """What step-over needs to know about one instruction."""

    address: int
    width: int  #: bytes, 2 or 4
    is_call: bool
    mnemonic: str  #: best-effort label, for reporting only

    @property
    def next_address(self) -> int:
        return self.address + self.width


def t32_is_32bit(halfword: int) -> bool:
    """Whether ``halfword`` begins a 32-bit T32 instruction (Armv8 ARM F3.1)."""
    return (halfword >> 11) & 0b11111 in (0b11101, 0b11110, 0b11111)


def decode_t32(address: int, first: int, second: int) -> Instruction:
    """Decode a T32 instruction from its two halfwords.

    ``second`` is only meaningful when the instruction is 32 bits wide; callers
    that read four bytes always have it, and it is ignored otherwise.
    """
    if not t32_is_32bit(first):
        # BLX (register), T1: 0100 0111 1 Rm 000
        if (first & 0xFF87) == 0x4780:
            return Instruction(address, 2, True, "BLX (register)")
        return Instruction(address, 2, False, "")

    # 32-bit. BL and BLX (immediate) share the 0b11110 prefix and differ in the
    # second halfword: bits [15:14] are 0b11 for both, and bit [12] selects BL
    # (1) from BLX immediate (0). Both are calls.
    if (first >> 11) & 0b11111 == 0b11110 and (second >> 14) & 0b11 == 0b11:
        return Instruction(address, 4, True, "BL" if (second >> 12) & 1 else "BLX (immediate)")
    return Instruction(address, 4, False, "")


def decode_a32(address: int, word: int) -> Instruction:
    """Decode an A32 instruction. Always four bytes."""
    # BL/B with link: cond != 0b1111 and bits [27:25] == 0b101 with the L bit
    # (bit 24) set.
    if (word >> 28) & 0xF != 0xF:
        if (word >> 25) & 0b111 == 0b101 and (word >> 24) & 1:
            return Instruction(address, 4, True, "BL")
        # BLX (register): cond 0001 0010 1111 1111 1111 0011 Rm
        if (word & 0x0FFF_FFF0) == 0x012F_FF30:
            return Instruction(address, 4, True, "BLX (register)")
    elif (word >> 25) & 0b111 == 0b101:
        # cond == 0b1111 with 101 is BLX (immediate)
        return Instruction(address, 4, True, "BLX (immediate)")
    return Instruction(address, 4, False, "")


def decode(address: int, word: int, *, thumb: bool) -> Instruction:
    """Decode the instruction at ``address`` from the 32 bits read there.

    ``word`` is the little-endian word at ``address & ~3``-relative position --
    callers pass the four bytes starting *at* the instruction, so the low
    halfword is the first one.
    """
    # Strip the Thumb state bit here, once, so no caller has to remember. A PC
    # taken from a function pointer carries it, and leaving it on puts the
    # return address of a call one byte out -- a breakpoint there lands
    # mid-instruction and never fires.
    address &= ~1
    if not thumb:
        return decode_a32(address, word)
    return decode_t32(address, word & 0xFFFF, (word >> 16) & 0xFFFF)

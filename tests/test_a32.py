"""A32/T32 decoding, only as far as step-over needs. No hardware required.

Two facts matter and nothing else: how long the instruction at the PC is, and
whether it is a call. The asymmetry is worth stating -- calling a non-call a call
just makes step-over take the slow route to the same place, while missing a call
steps *into* the callee. So the tests lean on not missing calls.
"""

from __future__ import annotations

import pytest

from s32edbg.data import a32


# ---------------------------------------------------------------------------
# T32 instruction width
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("halfword", [0xE800, 0xF000, 0xF800, 0xFFFF, 0xEFFF])
def test_the_three_prefixes_that_begin_a_32_bit_t32_instruction(halfword: int) -> None:
    """Armv8 ARM F3.1: bits [15:11] of 0b11101, 0b11110 or 0b11111. Getting the
    width wrong puts the return address of a call two bytes out, so step-over
    breakpoints land mid-instruction and never fire."""
    assert a32.t32_is_32bit(halfword) is True


@pytest.mark.parametrize("halfword", [0x0000, 0x4780, 0xBF00, 0xE7FE, 0xDFFF])
def test_everything_else_is_a_16_bit_t32_instruction(halfword: int) -> None:
    assert a32.t32_is_32bit(halfword) is False


def test_a_16_bit_instruction_advances_two_bytes() -> None:
    instruction = a32.decode(0x3210_4A80, 0x0000_BF00, thumb=True)  # NOP
    assert instruction.width == 2
    assert instruction.next_address == 0x3210_4A82


def test_a_32_bit_instruction_advances_four_bytes() -> None:
    instruction = a32.decode(0x3210_4A80, 0x8000_F3AF, thumb=True)  # NOP.W
    assert instruction.width == 4
    assert instruction.next_address == 0x3210_4A84


# ---------------------------------------------------------------------------
# T32 calls
# ---------------------------------------------------------------------------


def test_t32_bl_is_recognised_as_a_call() -> None:
    """BL is the common case by far -- the whole point of step-over."""
    # first halfword 0xF000 (11110 prefix), second 0xF800 (bits [15:14]=11, [12]=1)
    instruction = a32.decode(0x3210_4A80, (0xF800 << 16) | 0xF000, thumb=True)
    assert instruction.is_call is True
    assert instruction.width == 4
    assert instruction.mnemonic == "BL"


def test_t32_blx_immediate_is_recognised_as_a_call() -> None:
    """Same prefix as BL, distinguished by bit [12] of the second halfword. It
    still needs stepping over."""
    instruction = a32.decode(0x3210_4A80, (0xE800 << 16) | 0xF000, thumb=True)
    assert instruction.is_call is True
    assert "BLX" in instruction.mnemonic


def test_t32_blx_register_is_recognised_as_a_call() -> None:
    """The 16-bit form: BLX r3 = 0x4798. Missing it steps into the callee."""
    instruction = a32.decode(0x3210_4A80, 0x0000_4798, thumb=True)
    assert instruction.is_call is True
    assert instruction.width == 2


def test_bx_is_not_a_call() -> None:
    """BX branches without linking -- it is a return or a tail call, not
    something to step over. Treating it as a call would set a breakpoint at an
    address the core never comes back to, and step-over would hang until it
    timed out."""
    instruction = a32.decode(0x3210_4A80, 0x0000_4718, thumb=True)  # BX r3
    assert instruction.is_call is False


def test_an_ordinary_t32_instruction_is_not_a_call() -> None:
    for word in (0x0000_BF00, 0x0000_4668, 0x8000_F3AF, 0x0000_2000):
        assert a32.decode(0x1000, word, thumb=True).is_call is False


# ---------------------------------------------------------------------------
# A32
# ---------------------------------------------------------------------------


def test_a32_instructions_are_always_four_bytes() -> None:
    assert a32.decode(0x1000, 0xE320_F000, thumb=False).width == 4


def test_a32_bl_is_recognised_as_a_call() -> None:
    assert a32.decode(0x1000, 0xEB00_0000, thumb=False).is_call is True  # BL
    assert a32.decode(0x1000, 0x0B00_0000, thumb=False).is_call is True  # BLEQ


def test_a32_b_without_link_is_not_a_call() -> None:
    """B and BL differ only in bit 24. Confusing them makes every plain branch
    take the slow step-over path and, worse, sets a breakpoint at a fall-through
    address the branch skips."""
    assert a32.decode(0x1000, 0xEA00_0000, thumb=False).is_call is False


def test_a32_blx_register_is_recognised_as_a_call() -> None:
    assert a32.decode(0x1000, 0xE12F_FF33, thumb=False).is_call is True  # BLX r3


def test_a32_blx_immediate_is_recognised_as_a_call() -> None:
    """Encoded with cond == 0b1111, which is otherwise the unconditional-space
    escape -- so it needs its own branch in the decoder."""
    assert a32.decode(0x1000, 0xFA00_0000, thumb=False).is_call is True


def test_an_ordinary_a32_instruction_is_not_a_call() -> None:
    for word in (0xE320_F000, 0xE1A0_0000, 0xE580_0000):
        assert a32.decode(0x1000, word, thumb=False).is_call is False


# ---------------------------------------------------------------------------
# The Thumb bit
# ---------------------------------------------------------------------------


def test_the_decoded_address_has_the_thumb_bit_stripped() -> None:
    """A PC value taken from a function pointer may carry bit 0. It is not part
    of the address, and leaving it on would put the return address one byte
    out."""
    instruction = a32.decode(0x3210_4A81, 0x0000_BF00, thumb=True)
    assert instruction.address == 0x3210_4A80
    assert instruction.next_address == 0x3210_4A82

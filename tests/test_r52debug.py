"""Cortex-R52 external debug register decoders. No hardware required.

These tests pin the *decode*, not the offsets. The offsets in
``data/r52debug.py`` are marked VERIFY because no Arm document was available to
check them against, and only silicon can settle them -- so the honest thing to
test offline is that the arithmetic and the bit conventions are self-consistent,
and that a wrong-looking answer is reported as wrong rather than returned as
fact.
"""

from __future__ import annotations

import pytest

from s32edbg.data import coresight as cs
from s32edbg.data import r52debug as r52


# ---------------------------------------------------------------------------
# Comparator addressing
# ---------------------------------------------------------------------------


def test_comparators_are_sixteen_bytes_apart() -> None:
    """Each comparator slot holds a 64-bit value register and a 32-bit control
    register, so the stride is 16 and not 8. Getting this wrong aliases
    comparator n onto n/2 and silently overwrites a neighbour."""
    assert r52.dbgbvr(0) == 0x400
    assert r52.dbgbvr(1) == 0x410
    assert r52.dbgbcr(0) == 0x408
    assert r52.dbgbcr(1) == 0x418
    assert r52.dbgwvr(1) - r52.dbgwvr(0) == r52.COMPARATOR_STRIDE
    assert r52.dbgwcr(1) - r52.dbgwcr(0) == r52.COMPARATOR_STRIDE


def test_the_value_and_control_banks_do_not_overlap() -> None:
    """Sixteen breakpoints is the architectural ceiling, and the watchpoint bank
    starts at 0x800 -- so the breakpoint bank must not reach it."""
    assert r52.dbgbcr(r52.MAX_COMPARATORS - 1) < r52.DBGWVR_BASE


# ---------------------------------------------------------------------------
# MIDR -- the identity anchor
# ---------------------------------------------------------------------------


def test_midr_recognises_a_cortex_r52() -> None:
    """Implementer 0x41 (Arm) and part 0xD13. This is the strongest check that
    the frame offsets are right: MIDR sits at 0xD00, far from the ID block the
    ROM walk already validated, so a correct decode means the middle of the map
    is addressed correctly too."""
    decoded = r52.decode_midr(0x411F_D131)
    assert decoded["is_cortex_r52"] is True
    assert decoded["implementer_name"] == "Arm"
    assert decoded["partnum"] == "0xD13"


def test_midr_does_not_claim_a_cortex_r52_for_another_part() -> None:
    assert r52.decode_midr(0x410F_C241)["is_cortex_r52"] is False  # a Cortex-M4
    assert r52.decode_midr(0x0000_0000)["is_cortex_r52"] is False
    assert r52.decode_midr(0xFFFF_FFFF)["is_cortex_r52"] is False


# ---------------------------------------------------------------------------
# EDSCR
# ---------------------------------------------------------------------------


def test_a_named_halt_reason_is_reported_in_words() -> None:
    assert "breakpoint" in r52.edscr_status_name(0b000111)
    assert "external debug request" in r52.edscr_status_name(0b010011)
    assert "watchpoint" in r52.edscr_status_name(0b101011)


def test_an_unrecognised_status_says_so_instead_of_guessing() -> None:
    """A wrong halt reason is worse than an honest 'I do not recognise this' --
    it would send someone looking for a breakpoint that never fired."""
    assert "unknown" in r52.edscr_status_name(0b101110)


def test_halted_is_derived_by_exclusion_not_by_listing_halt_reasons() -> None:
    """Only two STATUS values mean not-halted. Deriving 'halted' from those,
    rather than from a list of known halt reasons, means an
    implementation-defined reason this table does not name still reads as halted
    instead of silently as running."""
    assert r52.edscr_halted(r52.EDSCR_STATUS_RUNNING) is False
    assert r52.edscr_halted(r52.EDSCR_STATUS_RESTARTING) is False
    assert r52.edscr_halted(0b000111) is True
    assert r52.edscr_halted(0b101110) is True, "an unnamed status must still read as halted"


def test_edscr_status_ignores_the_flag_bits_above_it() -> None:
    """STATUS is bits [5:0]; the flags above must not leak into it."""
    assert r52.edscr_status(0b000010 | r52.EDSCR_HDE | r52.EDSCR_TXFULL) == 0b000010


def test_edscr_decode_reports_the_flags_run_control_depends_on() -> None:
    raw = 0b000111 | r52.EDSCR_HDE | r52.EDSCR_ITE
    decoded = r52.decode_edscr(raw)
    assert decoded["halted"] is True
    assert decoded["halting_debug_enabled"] is True
    assert decoded["instruction_transfer_empty"] is True
    assert decoded["sticky_error"] is False


# ---------------------------------------------------------------------------
# EDPRSR
# ---------------------------------------------------------------------------


def test_edprsr_reports_power_halt_and_lock_state() -> None:
    decoded = r52.decode_edprsr(r52.EDPRSR_PU | r52.EDPRSR_HALTED | r52.EDPRSR_OSLK)
    assert decoded["powered_up"] is True
    assert decoded["halted"] is True
    assert decoded["os_locked"] is True
    assert decoded["in_reset"] is False


# ---------------------------------------------------------------------------
# EDDFR -- read the comparator count, never assume it
# ---------------------------------------------------------------------------


def test_the_comparator_count_is_the_field_plus_one() -> None:
    """Armv8 stores these as 'number minus one', so a nibble of 7 means eight
    comparators -- which is what the S32E2 breakpoints guide states for the R52."""
    decoded = r52.decode_eddfr((7 << 12) | (7 << 20))
    assert decoded["breakpoints"] == 8
    assert decoded["watchpoints"] == 8
    assert decoded["plausible"] is True


def test_a_dead_bus_reading_is_not_mistaken_for_a_comparator_count() -> None:
    """This is why a range check alone is not enough, and the test that found it.

    The minus-one encoding maps 0xFFFFFFFF onto a perfectly in-range 16/16 and
    0x00000000 onto 1/1. Neither is a comparator count -- they are the two ways
    a wire reads when nothing is driving it, which is exactly the state a wrong
    offset or a gated debug clock produces. `connect` already applies the same
    rule to DPIDR.
    """
    for dead in (0x0000_0000, 0xFFFF_FFFF):
        decoded = r52.decode_eddfr(dead)
        assert decoded["plausible"] is False, f"0x{dead:08X} was accepted as a count"


def test_a_real_looking_count_is_still_accepted() -> None:
    """The dead-bus rule must not reject legitimate values that happen to be
    small -- four breakpoints and two watchpoints is a plausible core."""
    decoded = r52.decode_eddfr((3 << 12) | (1 << 20))
    assert (decoded["breakpoints"], decoded["watchpoints"]) == (4, 2)
    assert decoded["plausible"] is True


# ---------------------------------------------------------------------------
# EDITR instruction encodings -- the highest-risk part of the module
# ---------------------------------------------------------------------------


def test_the_a32_register_field_lands_in_bits_15_to_12() -> None:
    """MCR/MRC put Rt at [15:12] of the A32 encoding."""
    for rt in range(16):
        assert r52.instr_reg_to_dtr(rt, t32=False) == 0xEE00_0E15 | (rt << 12)
        assert r52.instr_dtr_to_reg(rt, t32=False) == 0xEE10_0E15 | (rt << 12)


def test_the_two_transfer_directions_differ_only_in_the_direction_bit() -> None:
    """Bit 20 of the A32 encoding selects MCR from MRC. If these two ever stop
    differing by exactly that bit, one of them has been mistyped."""
    assert r52.instr_reg_to_dtr(3, t32=False) ^ r52.instr_dtr_to_reg(3, t32=False) == 1 << 20


def test_t32_puts_the_first_halfword_in_the_low_bits() -> None:
    """Settled on silicon: EDITR takes a 32-bit T32 instruction with the *first*
    halfword in the low 16 bits -- the opposite order from how the encoding is
    normally written down. Measured on RTU0 core 1: the A32 form of
    `MCR p14,0,r0,c0,c5,0` sets EDSCR.ERR, and 0x0E15EE00 executes and transfers.

    Writing the halfwords the other way round does not fault; it executes some
    other instruction. That is why this is pinned rather than left implicit.
    """
    assert r52.instr_reg_to_dtr(0) == 0x0E15_EE00
    assert r52.instr_dtr_to_reg(0) == 0x0E15_EE10
    assert r52.instr_reg_to_dtr(0) & 0xFFFF == 0xEE00, "first halfword must be low"


def test_the_t32_register_field_moves_with_the_halfword_swap() -> None:
    """Rt lives in the second halfword, so after the swap it sits in the high
    bits -- not where the A32 encoding puts it. Extracting it from the A32
    position is exactly the mistake that would silently read the wrong register.
    """
    for rt in range(16):
        assert r52.instr_reg_to_dtr(rt) == 0x0E15_EE00 | (rt << 28)
        assert r52.instr_dtr_to_reg(rt) == 0x0E15_EE10 | (rt << 28)


def test_every_register_gets_a_distinct_encoding_in_both_instruction_sets() -> None:
    """A collision would mean two different registers being read by the same
    instruction, which no round-trip on a single register would catch."""
    for t32 in (True, False):
        reads = {r52.instr_reg_to_dtr(rt, t32=t32) for rt in range(16)}
        writes = {r52.instr_dtr_to_reg(rt, t32=t32) for rt in range(16)}
        assert len(reads) == 16 and len(writes) == 16
        assert not reads & writes


def test_a_register_number_outside_r0_to_r15_is_rejected() -> None:
    for bad in (-1, 16, 99):
        with pytest.raises(ValueError, match="core register"):
            r52.instr_reg_to_dtr(bad)
        with pytest.raises(ValueError, match="core register"):
            r52.instr_dtr_to_reg(bad)


def test_the_round_trip_probe_pattern_is_asymmetric_in_every_byte() -> None:
    """The pattern proves the transfer path by surviving a round trip, so it
    must not be reproducible by a byte-order or shift error. Same reasoning as
    tapseq.MEASURE_MARKER."""
    value = r52.DTR_PROBE_PATTERN
    octets = [(value >> shift) & 0xFF for shift in (0, 8, 16, 24)]
    assert len(set(octets)) == 4, "a repeated byte would survive a byte-order error"
    assert value != int(f"{value:032b}"[::-1], 2), "must not be a bit palindrome"


# ---------------------------------------------------------------------------
# CTI
# ---------------------------------------------------------------------------


def test_the_cti_sits_one_component_above_its_core_debug_frame() -> None:
    """Proven by the ROM walk on this silicon: 0x00410000 -> 0x00420000, and the
    same stride for all four cores."""
    assert 0x0041_0000 + r52.CTI_OFFSET_FROM_DEBUG == 0x0042_0000
    assert 0x00C1_0000 + r52.CTI_OFFSET_FROM_DEBUG == 0x00C2_0000


def test_cti_enable_registers_are_four_bytes_apart() -> None:
    assert r52.ctiouten(0) == 0x0A0
    assert r52.ctiouten(1) == 0x0A4
    assert r52.ctiinen(1) - r52.ctiinen(0) == 4


def test_halt_and_restart_use_different_channels_and_triggers() -> None:
    """Sharing a channel between halt and restart would make a resume
    indistinguishable from a halt request at the trigger matrix."""
    assert r52.CHANNEL_HALT != r52.CHANNEL_RESTART
    assert r52.TRIGOUT_DEBUG_REQUEST != r52.TRIGOUT_RESTART


# ---------------------------------------------------------------------------
# DBGAUTHSTATUS, which lives in the shared CoreSight module
# ---------------------------------------------------------------------------


def test_authstatus_distinguishes_denied_from_not_implemented() -> None:
    """The encoding is 0b00 not implemented, 0b10 implemented-and-disabled,
    0b11 implemented-and-enabled -- so the *high* bit means implemented. Reading
    it the other way round makes a denied signal look unimplemented, hiding
    exactly the condition the register exists to report."""
    assert cs.decode_authstatus(0x00)["non_secure_invasive"] == "not implemented"
    assert cs.decode_authstatus(0b10)["non_secure_invasive"] == "denied"
    assert cs.decode_authstatus(0b11)["non_secure_invasive"] == "granted"


def test_invasive_debug_needs_only_one_of_the_two_invasive_signals() -> None:
    """Either secure or non-secure invasive debug is enough to halt a core;
    which one decides what the comparator's security qualification must say."""
    assert cs.invasive_debug_granted(0xFF) is True
    assert cs.invasive_debug_granted(0b11) is True  # non-secure only
    assert cs.invasive_debug_granted(0b11 << cs.AUTH_SID_SHIFT) is True  # secure only
    assert cs.invasive_debug_granted(0b11 << cs.AUTH_NSNID_SHIFT) is False  # non-invasive only
    assert cs.invasive_debug_granted(0xAA) is False  # all implemented, all denied


def test_the_coresight_preamble_check_masks_the_class_nibble() -> None:
    """CIDR1's low nibble is preamble, its high nibble is the component class --
    so a class 0x9 component and a class 0x1 ROM table must both pass."""
    assert cs.preamble_ok((0x0D, 0x90, 0x05, 0xB1)) is True
    assert cs.preamble_ok((0x0D, 0x10, 0x05, 0xB1)) is True
    assert cs.preamble_ok((0x0D, 0x09, 0x05, 0xB1)) is False
    assert cs.preamble_ok((0x05, 0x05, 0x05, 0x05)) is False

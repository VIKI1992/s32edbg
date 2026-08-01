"""Cortex-M33 debug: the register map and the decisions built on it.

No hardware required. These pin the decodes and the guards; what only silicon
can settle is recorded in docs/test-plan.md.
"""

from __future__ import annotations

import pytest

from s32edbg.data import m33debug as m33


def test_cpuid_recognises_a_cortex_m33() -> None:
    """The identity anchor. CPUID sits at 0xE000ED00, far from the debug block
    at 0xE000EDF0, so a correct decode means the AP is addressing the
    processor's own bus rather than something else."""
    decoded = m33.decode_cpuid(0x410F_D214)
    assert decoded["is_cortex_m33"] is True
    assert decoded["part_name"] == "Cortex-M33"


def test_cpuid_does_not_claim_a_cortex_m33_for_another_part() -> None:
    assert m33.decode_cpuid(0x411F_D131)["is_cortex_m33"] is False  # a Cortex-R52
    assert m33.decode_cpuid(0x0000_0000)["is_cortex_m33"] is False
    assert m33.decode_cpuid(0xFFFF_FFFF)["is_cortex_m33"] is False


def test_every_dhcsr_write_carries_the_debug_key() -> None:
    """A DHCSR write without 0xA05F in [31:16] is silently discarded -- no fault,
    no error. A halt that forgets it looks like a core that refuses to stop."""
    assert m33.dhcsr_write(m33.DHCSR_C_HALT) >> 16 == 0xA05F
    assert m33.dhcsr_write(m33.DHCSR_C_HALT) & 0xFFFF == m33.DHCSR_C_HALT


def test_the_key_cannot_be_overwritten_by_the_control_bits() -> None:
    assert m33.dhcsr_write(0xFFFF_FFFF) >> 16 == 0xA05F


def test_dhcsr_separates_halted_from_sleeping_and_locked_up() -> None:
    """Three very different conditions that all mean "not making progress"."""
    assert m33.decode_dhcsr(m33.DHCSR_S_HALT)["halted"] is True
    assert m33.decode_dhcsr(m33.DHCSR_S_SLEEP)["sleeping"] is True
    assert m33.decode_dhcsr(m33.DHCSR_S_LOCKUP)["locked_up"] is True
    assert m33.decode_dhcsr(m33.DHCSR_S_SLEEP)["halted"] is False


def test_the_comparator_count_comes_from_fp_ctrl() -> None:
    """TRM 13.1: four or eight, and FP_CTRL's reset value encodes which --
    0x10000040 for four, 0x10000080 for eight. Those two reset values are the
    check that the split NUM_CODE field is being read correctly."""
    assert m33.fp_num_code(0x1000_0040) == 4
    assert m33.fp_num_code(0x1000_0080) == 8


def test_a_breakpoint_strips_the_thumb_bit_and_sets_enable() -> None:
    """M33 is Thumb-only, so a function pointer always has bit 0 set -- and bit 0
    of FP_COMP is the enable, not part of the address. Writing the pointer
    straight in would set the comparator to a misaligned address."""
    value = m33.encode_breakpoint(0x2500_1F71)
    assert value == 0x2500_1F70 | m33.FP_COMP_BE
    assert m33.decode_breakpoint(value)["address"] == "0x25001F70"
    assert m33.decode_breakpoint(value)["enabled"] is True


def test_a_cleared_comparator_reads_as_disabled() -> None:
    assert m33.decode_breakpoint(0)["enabled"] is False


def test_the_pcsr_invalid_marker_is_recognised_in_both_spellings() -> None:
    """Bit 0 of DWT_PCSR is RES0 because a PC is halfword aligned, so the
    all-ones "no sample" marker reads back as 0xFFFFFFFE rather than
    0xFFFFFFFF.

    Measured: both FlexLLCE CE cores sampled 0xFFFFFFFE 150 times out of 150.
    Without this the tool reported that as a hot address -- a made-up answer
    presented as a measurement.
    """
    assert m33.pcsr_is_valid(0xFFFF_FFFF) is False
    assert m33.pcsr_is_valid(0xFFFF_FFFE) is False
    assert m33.pcsr_is_valid(0x2500_1F70) is True


def test_the_dwt_comparator_stride_is_sixteen_bytes() -> None:
    assert m33.dwt_comp(0) == 0xE000_1020
    assert m33.dwt_function(0) == 0xE000_1028
    assert m33.dwt_comp(1) - m33.dwt_comp(0) == 0x10


def test_the_bpu_comparator_stride_is_four_bytes() -> None:
    """Unlike the R52's 16-byte comparator slots -- the BPU has no separate
    value and control registers."""
    assert m33.fp_comp(0) == 0xE000_2008
    assert m33.fp_comp(1) - m33.fp_comp(0) == 4


def test_the_pc_is_an_ordinary_register_selector_here() -> None:
    """No instruction execution and no DLR: on the M33 the PC is selector 15."""
    assert m33.REGISTER_SELECTORS["pc"] == 15
    assert "pc" in m33.REGISTER_ORDER


def test_reads_and_writes_differ_only_in_the_direction_bit() -> None:
    selector = m33.REGISTER_SELECTORS["r5"]
    assert (selector | m33.DCRSR_REGWNR) ^ selector == m33.DCRSR_REGWNR


def test_the_retire_bit_is_what_separates_ran_and_stopped_from_never_started() -> None:
    """Why `resume` cannot simply poll for "not halted".

    A breakpoint on a hot address re-halts faster than a poll can see the core
    running -- measured on the SMU, resuming onto a breakpoint in its idle loop
    came back halted every time. DHCSR.S_RETIRE_ST says an instruction retired
    since the last DHCSR read, which distinguishes a working breakpoint from a
    resume that never took. The R52 path had the identical bug.
    """
    ran_and_stopped = m33.DHCSR_S_HALT | m33.DHCSR_S_RETIRE_ST
    never_started = m33.DHCSR_S_HALT

    assert m33.decode_dhcsr(ran_and_stopped)["halted"] is True
    assert m33.decode_dhcsr(never_started)["halted"] is True
    assert ran_and_stopped & m33.DHCSR_S_RETIRE_ST
    assert not never_started & m33.DHCSR_S_RETIRE_ST


def test_stepping_masks_interrupts_by_default() -> None:
    """Without masking, a step at almost any point in a running system lands in
    an interrupt handler rather than the next instruction, which looks like the
    step going somewhere random."""
    from s32edbg.dap import m33 as m33ctl
    import inspect

    signature = inspect.signature(m33ctl.step)
    assert signature.parameters["mask_interrupts"].default is True

"""TAP state-machine sequencing tests. No hardware required."""

from __future__ import annotations

import pytest

from s32edbg.jtag import tapseq


def tms_bits(seq: tapseq.Sequence) -> list[int]:
    return [(seq.tms >> i) & 1 for i in range(seq.nbits)]


def tdi_bits(seq: tapseq.Sequence) -> list[int]:
    return [(seq.tdi >> i) & 1 for i in range(seq.nbits)]


def test_goto_idle_forces_reset_then_idle() -> None:
    seq = tapseq.goto_idle()
    # RM 136.4.8: five consecutive TMS-high edges guarantee Test-Logic-Reset.
    assert tms_bits(seq) == [1, 1, 1, 1, 1, 0]
    assert seq.data_bits == 0


def test_ir_scan_walks_reset_to_shift_ir_and_back() -> None:
    seq = tapseq.ir_scan(0b1010, 4)
    tms = tms_bits(seq)
    # 6 reset/idle clocks, then Select-DR, Select-IR, Capture-IR, Shift-IR
    assert tms[:6] == [1, 1, 1, 1, 1, 0]
    assert tms[6:10] == [1, 1, 0, 0]
    # four payload bits, the last of which exits to Exit1-IR
    assert tms[10:14] == [0, 0, 0, 1]
    # Update-IR then back to Run-Test/Idle
    assert tms[14:] == [1, 0]
    assert seq.data_start == 10
    assert seq.data_bits == 4


def test_ir_scan_payload_is_lsb_first() -> None:
    seq = tapseq.ir_scan(0b1010, 4)
    payload = tdi_bits(seq)[seq.data_start : seq.data_start + 4]
    assert payload == [0, 1, 0, 1], "WJTAGRaw is documented as LSB-first"


def test_dr_scan_does_not_pass_through_reset() -> None:
    """A DR scan must preserve the instruction a preceding ir_scan loaded."""
    seq = tapseq.dr_scan(0xFF, 8)
    tms = tms_bits(seq)
    # Run-Test/Idle -> Select-DR -> Capture-DR -> Shift-DR, no 5x TMS-high run
    assert tms[:3] == [1, 0, 0]
    assert max(sum(tms[i : i + 5]) for i in range(len(tms) - 4)) < 5


def test_dr_scan_from_reset_prepends_tlr() -> None:
    seq = tapseq.dr_scan(0, 32, from_reset=True)
    assert tms_bits(seq)[:6] == [1, 1, 1, 1, 1, 0]
    assert seq.data_start == 9  # 6 reset/idle + 3 navigation clocks


def test_idcode_scan_is_32_bits_after_reset() -> None:
    seq = tapseq.idcode_scan()
    assert seq.data_bits == 32
    assert tms_bits(seq)[:6] == [1, 1, 1, 1, 1, 0]


def test_extract_pulls_payload_out_of_full_capture() -> None:
    seq = tapseq.dr_scan(0, 8)
    # Place a known byte at the payload offset inside a longer TDO capture.
    tdo = 0xA5 << seq.data_start
    assert seq.extract(tdo) == 0xA5


def test_extract_ignores_navigation_clocks() -> None:
    seq = tapseq.dr_scan(0, 4)
    noise = (1 << seq.nbits) - 1  # every clock high
    assert seq.extract(noise) == 0xF


@pytest.mark.parametrize("nbits", [0, -1])
def test_zero_width_shifts_rejected(nbits: int) -> None:
    with pytest.raises(ValueError):
        tapseq.dr_scan(0, nbits)
    with pytest.raises(ValueError):
        tapseq.ir_scan(0, nbits)


def test_bits_roundtrip() -> None:
    value = 0xDEADBEEF
    assert tapseq.bits_to_int(tapseq.int_to_bits(value, 32)) == value


# --------------------------------------------------------------------------
# Shift-delay detection, which is how register lengths get measured
# --------------------------------------------------------------------------


def test_find_shift_delay_locates_marker() -> None:
    length = 37
    marker_len = 8
    marker = (1 << marker_len) - 1
    tdo = marker << length  # register contents come out first, then the marker
    assert tapseq.find_shift_delay(tdo, length + marker_len + 8, marker_len=marker_len) == length


def test_find_shift_delay_returns_none_when_absent() -> None:
    assert tapseq.find_shift_delay(0, 64, marker_len=8) is None


def test_find_shift_delay_finds_earliest_match() -> None:
    marker_len = 4
    tdo = 0xFF  # two overlapping runs of four ones starting at 0
    assert tapseq.find_shift_delay(tdo, 16, marker_len=marker_len) == 0


def test_find_shift_delay_rejects_bad_marker_len() -> None:
    with pytest.raises(ValueError):
        tapseq.find_shift_delay(0, 8, marker_len=0)


# ---------------------------------------------------------------------------
# Marker choice: why register-length measurement does not use all-ones
# ---------------------------------------------------------------------------


def test_a_distinctive_marker_is_not_faked_by_ones_in_the_captured_contents() -> None:
    """An all-ones marker measures the wrong length on a real register.

    find_marker_delay returns the *earliest* match, so any run of ones already
    present in what the register captured is mistaken for the marker and the
    length comes out too short -- confidently, with no error. That is not
    hypothetical for the boundary-scan register, whose capture is live pin state
    and can easily hold a byte of ones. MEASURE_MARKER has no run longer than
    two, so captured contents do not imitate it.
    """
    length = 24
    contents = 0xFFFF_FF  # 24 bits of ones: exactly what defeats an all-ones marker
    tdo = (tapseq.MEASURE_MARKER << length) | contents
    total = length + tapseq.MEASURE_MARKER_BITS

    assert (
        tapseq.find_marker_delay(
            tdo, total, marker=tapseq.MEASURE_MARKER, marker_len=tapseq.MEASURE_MARKER_BITS
        )
        == length
    )
    # The old approach reports 0 against the same capture.
    assert tapseq.find_shift_delay(tdo, total, marker_len=8) == 0


def test_find_marker_delay_locates_an_arbitrary_marker() -> None:
    length = 11
    marker, marker_len = 0xA5C3, 16
    tdo = marker << length
    assert (
        tapseq.find_marker_delay(tdo, length + marker_len, marker=marker, marker_len=marker_len)
        == length
    )


def test_find_marker_delay_returns_none_when_the_marker_never_arrives() -> None:
    assert tapseq.find_marker_delay(0, 64, marker=0xA5C3, marker_len=16) is None


def test_the_measurement_marker_has_no_long_run_of_identical_bits() -> None:
    """The property that makes it safe against captured pin state."""
    bits = f"{tapseq.MEASURE_MARKER:032b}"
    assert "1111" not in bits and "0000" not in bits

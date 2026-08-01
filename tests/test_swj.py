"""SWJ-DP protocol-switch constants and their wire encoding.

The last test checks our transcription against the byte sequences the J-Link
DLL ships, which is the closest thing to ground truth available here. No probe
required, but it is skipped if the DLL is not installed.

No hardware required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from s32edbg.data import swj

DLL_CANDIDATES = sorted(Path(r"C:\Program Files\SEGGER").glob("JLink*/JLink_x64.dll"))


def test_jtag_to_dormant_is_the_31_bit_adiv6_sequence() -> None:
    """ADIv6 B5.3.2: "The recommended 31-bit JTAG-to-DS select sequence is
    0b010_1110_1110_1110_1110_1110_1110_0110" = 0x2EEEEEE6 MSB first."""
    assert swj.JTAG_TO_DORMANT == (0x2EEE_EEE6, 31)


def test_swd_to_dormant_is_the_16_bit_adiv6_sequence() -> None:
    """ADIv6 B5.3.3: 0b0011_1101_1100_0111 = 0x3DC7 MSB first."""
    assert swj.SWD_TO_DORMANT == (0x3DC7, 16)


def test_the_selection_alert_is_128_bits() -> None:
    """ADIv6 B5.3.4."""
    value, nbits = swj.SELECTION_ALERT
    assert nbits == 128
    assert value == 0x49CF9046_A9B4A161_97F5BBC7_45703D98


def test_the_activation_codes_match_adiv6_table_b5_2() -> None:
    assert swj.ACTIVATION_SWD == (0b0101_1000, 8)
    assert swj.ACTIVATION_JTAG == (0b0101_0000, 8)
    assert swj.ACTIVATION_JTAG_SERIAL == (0, 12)


def test_the_direct_sequences_are_recorded_even_though_they_are_deprecated() -> None:
    """ADIv6 B5.2 deprecates these on parts implementing the dormant state, and
    RM 135.14.1.1 says this part does. Kept as documentation only."""
    assert swj.DIRECT_JTAG_TO_SWD == (0x79E7, 16)
    assert swj.DIRECT_SWD_TO_JTAG == (0x3CE7, 16)


def test_wire_bytes_packs_msb_first_bits_into_lsb_first_bytes() -> None:
    """The sequence goes out most-significant bit first, but each byte is
    clocked least-significant bit first, so the first clock is bit 0 of byte 0.
    """
    # 0b1000_0000 over 8 bits: first bit clocked is 1, so byte 0 bit 0 is set.
    assert swj.wire_bytes(0b1000_0000, 8) == b"\x01"
    assert swj.wire_bytes(0b0000_0001, 8) == b"\x80"


def test_a_zero_length_sequence_is_rejected() -> None:
    with pytest.raises(ValueError):
        swj.wire_bytes(0, 0)


@pytest.mark.parametrize(
    "sequence,expected_hex",
    [
        (swj.JTAG_TO_DORMANT, "BABBBB33"),
        (swj.SWD_TO_DORMANT, "BCE3"),
        (swj.SELECTION_ALERT, "92F30962952D8586E9AFDDE3A20EBC19"),
    ],
)
def test_the_wire_encoding_matches_the_bytes_the_jlink_dll_ships(
    sequence: tuple[int, int], expected_hex: str
) -> None:
    """Cross-check both the transcription and the bit order against the vendor.

    JLink_x64.dll carries these sequences as literal byte blobs. If our ADIv6
    reading or our bit packing were wrong, the bytes would differ -- so this
    validates the two things a datasheet transcription can most easily get
    wrong, without needing a probe.
    """
    encoded = swj.wire_bytes(*sequence)
    assert encoded.hex().upper() == expected_hex

    if not DLL_CANDIDATES:
        pytest.skip("no J-Link DLL installed to cross-check against")
    blob = DLL_CANDIDATES[-1].read_bytes()
    assert encoded in blob, "the DLL does not contain this sequence as encoded"


def test_describe_switch_lists_the_steps_in_order_with_citations() -> None:
    steps = swj.describe_switch("jtag", "swd")
    assert len(steps) == 8
    joined = " ".join(steps)
    assert "Test-Logic-Reset" in joined
    assert "B5.3.2" in joined and "B5.3.4" in joined
    assert "UNKNOWN" in joined, "the post-switch DP requirement must be stated"


def test_describe_switch_refuses_the_direction_that_needs_a_power_cycle() -> None:
    """RM 135.5.1: "Switching back from SWD mode to JTAG mode requires POR.\""""
    with pytest.raises(ValueError):
        swj.describe_switch("swd", "jtag")

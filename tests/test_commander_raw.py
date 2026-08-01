"""Commander raw-JTAG path: command formatting and TDO decoding.

run_script is replaced with a recorder returning canned transcripts, so none of
this needs a probe. That matters because no WJTAG* command has ever been run
against this target -- the 13 prior evidence directories only used
ReadAP/WriteAP/ReadDP/WriteDP -- so these tests pin the syntax read out of
JLink.exe's help table, and the hardware checkpoint validates it.

No hardware required.
"""

from __future__ import annotations

import pytest

from s32edbg.errors import TransportError
from s32edbg.jtag import tapseq
from s32edbg.transport import parsing
from s32edbg.transport.commander import CommanderTransport, _decode_tdo


class ScriptRecorder(CommanderTransport):
    """A CommanderTransport whose script runs are recorded, not executed."""

    def __init__(self, captures: list[str] | None = None) -> None:
        # Bypass __init__'s install discovery; only the command layer is tested.
        self.commands: list[list[str]] = []
        self._captures = captures or []
        self.device = None
        self.interface = None
        self.speed_khz = 4000
        self.capability_warnings = []
        self.last_output = ""
        self.tdo_byte_order = "little"

    def _bringup_commands(self) -> list[str]:
        return ["si JTAG", "speed 4000"]

    def run_script(self, commands: list[str], *, add_quit: bool = True) -> str:
        self.commands.append(list(commands))
        return "\n".join(f" TDO: {c}" for c in self._captures)

    def raw_commands(self) -> list[str]:
        """Just the WJTAG*/ResetTAP lines from the last run."""
        return [c for c in self.commands[-1] if c.startswith(("WJTAG", "ResetTAP"))]


# ---------------------------------------------------------------------------
# Command formatting
# ---------------------------------------------------------------------------


def test_raw_shift_emits_wjtagraw_with_decimal_bits_and_space_separated_hex() -> None:
    """JLink.exe's help gives "WJTAGRaw <NumBits(dec)> <TMS(hex)> <TDI(hex)>" --
    space separated, unlike WJTAGIR/WJTAGDR which take a comma."""
    t = ScriptRecorder(captures=["00"])
    t.raw_shift(12, 0xABC, 0x123)
    assert t.raw_commands() == ["WJTAGRaw 12 ABC 123"]


def test_reset_tap_uses_the_resettap_command() -> None:
    t = ScriptRecorder()
    t.reset_tap()
    assert "ResetTAP" in t.commands[-1]


def test_a_sequence_runs_every_step_inside_one_invocation() -> None:
    """TAP state is scoped to a single JLink.exe run, so a sequence split across
    runs would lose the loaded instruction between the IR and its DR."""
    t = ScriptRecorder(captures=["00"] * 4)
    t.jtag_sequence([(0x0A, 8, 0x1234, 32), (0x21, 8, 0x8000_0001, 32)])
    assert len(t.commands) == 1, "the sequence was split across runs"
    assert len(t.raw_commands()) == 4, "expected an IR and a DR vector per step"


def test_only_the_first_step_of_a_sequence_resets_the_tap() -> None:
    """Re-entering Test-Logic-Reset mid-sequence would reload IDCODE into the IR
    (RM 136.4.8) and discard the instruction the next DR shift depends on."""
    t = ScriptRecorder(captures=["00"] * 4)
    t.jtag_sequence([(0x0A, 8, 0x1234, 32), (0x21, 8, 0x8000_0001, 32)])

    first = tapseq.ir_scan(0x0A, 8, from_reset=True)
    later = tapseq.ir_scan(0x21, 8, from_reset=False)
    issued = t.raw_commands()
    assert issued[0].split()[1] == str(first.nbits)
    assert issued[2].split()[1] == str(later.nbits)


def test_cross_check_dr_refuses_more_than_the_wjtagdr_ceiling() -> None:
    """WJTAGDR takes a Data64, so it cannot carry a wider register."""
    t = ScriptRecorder()
    with pytest.raises(TransportError, match="64"):
        t.cross_check_dr(0, 65)


# ---------------------------------------------------------------------------
# TDO decoding
# ---------------------------------------------------------------------------


def test_a_tdo_capture_is_parsed_out_of_the_transcript() -> None:
    assert parsing.parse_tdo_captures("blah\n TDO: A5C3\nmore") == ["A5C3"]


def test_every_tdo_capture_is_returned_in_order() -> None:
    text = " TDO: 01\nnoise\n TDO: 02\n TDO: 03"
    assert parsing.parse_tdo_captures(text) == ["01", "02", "03"]


def test_a_capture_decodes_first_clocked_bit_into_bit_zero() -> None:
    """tapseq indexes TDO LSB-first, so bit 0 must be the first bit clocked."""
    assert _decode_tdo("01", 8) == 0x01
    assert _decode_tdo("80", 8) == 0x80


def test_a_capture_is_masked_to_the_requested_width() -> None:
    assert _decode_tdo("FFFF", 4) == 0xF


def test_a_trailing_partial_nibble_does_not_break_decoding() -> None:
    """JLink.exe prints whole bytes with '%.2X' and a tail nibble with '%.1X',
    so an odd-length string is expected for a width that is not a byte multiple.
    """
    assert _decode_tdo("A5C", 12) == 0x0CA5


def test_a_missing_capture_is_an_error_rather_than_a_silent_zero() -> None:
    """A capture that never arrived must not be reported as a dead TDO line."""
    t = ScriptRecorder(captures=[])
    with pytest.raises(TransportError, match="TDO capture"):
        t.raw_shift(8, 0, 0)


def test_the_byte_order_assumption_is_switchable() -> None:
    """Which end of the printed string holds the first-clocked byte is unproven
    until calibrate_tdo_order() runs on hardware, so both must be expressible."""
    assert _decode_tdo("0102", 16, order="little") == 0x0201
    assert _decode_tdo("0102", 16, order="big") == 0x0102

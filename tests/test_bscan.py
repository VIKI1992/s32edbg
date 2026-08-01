"""Boundary-scan data and safety-policy tests. No hardware required."""

from __future__ import annotations

import pytest

from s32edbg.data import bscan as data
from s32edbg.errors import UnsafeOperation
from s32edbg.jtag import bscan as bscan_mod


# --------------------------------------------------------------------------
# The RM Table 984 preamble payload
# --------------------------------------------------------------------------


def test_ext_test_ctrl_payload_is_208_bits() -> None:
    """52 hex digits x 4 = 208 bits, derived from the literal RM string."""
    digits = data.EXT_TEST_CTRL_PAYLOAD_HEX.replace("_", "")
    assert len(digits) == 52
    assert data.EXT_TEST_CTRL_PAYLOAD_BITS == 208


def test_ext_test_ctrl_payload_transcription() -> None:
    """Guard the RM Table 984 payload against transcription slips.

    The RM prints it wrapped across two lines as
        1800_0800_000C_0004_0000_07C0_0000_0
        003_3041_1800_01A0_0280_0000h
    so the joined groups are checked individually, plus the head and tail
    nibbles of the assembled integer.
    """
    groups = data.EXT_TEST_CTRL_PAYLOAD_HEX.split("_")
    assert groups == [
        "1800", "0800", "000C", "0004", "0000", "07C0", "0000",
        "0003", "3041", "1800", "01A0", "0280", "0000",
    ]
    value = data.EXT_TEST_CTRL_PAYLOAD_VALUE
    # top 16 bits are the first group, low 16 bits the last
    assert value >> (data.EXT_TEST_CTRL_PAYLOAD_BITS - 16) == 0x1800
    assert value & 0xFFFF == 0x0000
    assert f"{value:052X}" == data.EXT_TEST_CTRL_PAYLOAD_HEX.replace("_", "")


def test_payload_value_fits_its_declared_width() -> None:
    assert data.EXT_TEST_CTRL_PAYLOAD_VALUE < (1 << data.EXT_TEST_CTRL_PAYLOAD_BITS)
    # Leading nibble is 1, so it must not fit in one fewer nibble.
    assert data.EXT_TEST_CTRL_PAYLOAD_VALUE >= (1 << (data.EXT_TEST_CTRL_PAYLOAD_BITS - 4))


def test_preamble_has_three_steps_in_rm_order() -> None:
    steps = data.preamble_steps(8)
    assert [s[0] for s in steps] == ["EXT_TEST_CTRL", "TLM_TDR", "TLM_TAP_SELECT_TDR"]
    # RM Table 984: opcodes 0Ah, 21h, 20h in that order.
    assert [s[1] for s in steps] == [0x0A, 0x21, 0x20]
    # and the DR payloads
    assert steps[1][3] == 0x8000_0001
    assert steps[2][3] == 0x8000_0011


# --------------------------------------------------------------------------
# IR width mapping -- the RM contradiction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "opcode8,expected16",
    [
        (data.JTAGC_IR_IDCODE, 0x0001),
        (data.JTAGC_IR_SAMPLE_PRELOAD, 0x0202),
        (data.JTAGC_IR_EXT_TEST_CTRL, 0x0A00),
        (data.JTAGC_IR_TLM_TDR, 0x2121),
        (data.JTAGC_IR_TLM_TAP_SELECT, 0x2020),
        (data.JTAGC_IR_BYPASS, 0xFFFF),
    ],
)
def test_jtagc_ir_maps_8bit_to_16bit(opcode8: int, expected16: int) -> None:
    assert data.jtagc_ir(opcode8, 8) == opcode8
    assert data.jtagc_ir(opcode8, 16) == expected16


def test_jtagc_ir_rejects_unknown_width() -> None:
    """Better to fail loudly than to invent an opcode the RM does not define."""
    with pytest.raises(ValueError, match="no RM opcode table"):
        data.jtagc_ir(data.JTAGC_IR_IDCODE, 12)


def test_jtagc_ir_rejects_opcode_with_no_16bit_form() -> None:
    with pytest.raises(ValueError, match="Table 959"):
        data.jtagc_ir(0x77, 16)


# --------------------------------------------------------------------------
# Safety policy: drive-pin instructions are not implemented
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["EXTEST", "HIGHZ", "CLAMP", "extest"])
def test_drive_instructions_are_refused_with_a_reason(name: str) -> None:
    with pytest.raises(UnsafeOperation) as exc:
        bscan_mod.reject_drive_instruction(name)
    message = str(exc.value)
    assert "SAMPLE/PRELOAD only" in message
    assert "RM 136.4.4" in message


def test_unknown_instruction_is_also_refused() -> None:
    with pytest.raises(UnsafeOperation):
        bscan_mod.reject_drive_instruction("INTEST")


def test_sample_instruction_is_not_used() -> None:
    """RM Tables 958/959: "do not use the SAMPLE instruction"."""
    steps = data.preamble_steps(8)
    assert data.JTAGC_IR_SAMPLE not in [s[1] for s in steps]


# --------------------------------------------------------------------------
# Sample result handling
# --------------------------------------------------------------------------


def test_sample_result_bits_are_lsb_first() -> None:
    result = bscan_mod.SampleResult(bsr_length=8, bits=[1, 0, 1, 0, 0, 0, 0, 0], raw=0x05)
    assert result.bits[0] == 1 and result.bits[2] == 1
    assert result.as_dict()["bsr_length"] == 8


def test_sample_diff_reports_changed_indices() -> None:
    a = bscan_mod.SampleResult(4, [0, 0, 0, 0], 0x0)
    b = bscan_mod.SampleResult(4, [0, 1, 0, 1], 0xA)
    assert a.diff(b) == [1, 3]


def test_sample_diff_handles_different_lengths() -> None:
    a = bscan_mod.SampleResult(4, [0, 0, 0, 0], 0x0)
    b = bscan_mod.SampleResult(2, [1, 0], 0x1)
    assert a.diff(b) == [0]


def test_bsdl_hint_names_the_rm_file() -> None:
    hint = bscan_mod.bsdl_hint(0x0831_F01D)
    assert hint is not None and "SIP_S32E277n8n9_BGA27_831F01D.bsdl" in hint
    assert bscan_mod.bsdl_hint(0xDEADBEEF) is None
    assert bscan_mod.bsdl_hint(None) is None


# ---------------------------------------------------------------------------
# IR width: settled on silicon, not inferred
# ---------------------------------------------------------------------------


def test_the_default_boundary_scan_ir_width_is_sixteen() -> None:
    """Measured on an S32E277: at SIR 8 the SAMPLE/PRELOAD opcode returns the
    IDCODE, meaning the BSR is never selected and the scan silently reads the
    device ID instead of pin state. At SIR 16 each instruction selects a
    distinct register. RM Table 959 over RM Table 984.
    """
    assert data.BSCAN_DEFAULT_IRLEN == 16


def test_the_preamble_uses_the_16_bit_opcodes_at_that_width() -> None:
    ops = [ir_value for _label, _op8, ir_value, _dr, _bits in data.preamble_steps(16)]
    assert ops == [0x0A00, 0x2121, 0x2020]


def test_sample_preload_has_a_distinct_16_bit_encoding() -> None:
    """0x02 at 8 bits and 0x0202 at 16 -- the widths are not interchangeable."""
    assert data.jtagc_ir(data.JTAGC_IR_SAMPLE_PRELOAD, 8) == 0x02
    assert data.jtagc_ir(data.JTAGC_IR_SAMPLE_PRELOAD, 16) == 0x0202

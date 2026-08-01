"""SVF emitter tests.

The critical property is that generated files never contain a command JTAGLoad
rejects at runtime, because that aborts playback mid-sequence.
"""

from __future__ import annotations

import pytest

from s32edbg.data.bscan import preamble_steps
from s32edbg.transport import svf


def test_never_emits_commands_jtagload_rejects() -> None:
    text = svf.emit_svf_for_preamble(8)
    for command in svf.UNSUPPORTED_SVF_COMMANDS:
        # Allowed inside the explanatory comment, never as a statement.
        for line in text.splitlines():
            if line.lstrip().startswith("!"):
                continue
            assert not line.lstrip().upper().startswith(command), f"{command} emitted as a statement"


def test_sets_end_states_to_idle() -> None:
    text = svf.emit_svf_for_preamble(8)
    assert "ENDIR IDLE;" in text
    assert "ENDDR IDLE;" in text


def test_emits_one_sir_sdr_pair_per_preamble_step() -> None:
    text = svf.emit_svf_for_preamble(8)
    assert text.count("SIR ") == 3
    assert text.count("SDR ") == 3


def test_preamble_dr_widths_match_the_rm() -> None:
    text = svf.emit_svf_for_preamble(8)
    assert "SDR 208 TDI (" in text
    assert text.count("SDR 32 TDI (") == 2


def test_ext_test_ctrl_payload_appears_in_full() -> None:
    """The 208-bit payload must reach the SVF intact, all 52 nibbles of it."""
    from s32edbg.data.bscan import EXT_TEST_CTRL_PAYLOAD_HEX

    expected = EXT_TEST_CTRL_PAYLOAD_HEX.replace("_", "")
    assert len(expected) == 52
    assert f"SDR 208 TDI ({expected});" in svf.emit_svf_for_preamble(8)


def test_8bit_and_16bit_ir_produce_different_sir_widths() -> None:
    eight = svf.emit_svf_for_preamble(8)
    sixteen = svf.emit_svf_for_preamble(16)
    assert "SIR 8 TDI (0A);" in eight
    assert "SIR 16 TDI (0A00);" in sixteen


def test_frequency_is_emitted_when_given() -> None:
    text = svf.emit_svf_for_preamble(8, frequency_hz=4_000_000)
    assert "FREQUENCY 4000000 HZ;" in text
    assert "FREQUENCY" not in svf.emit_svf_for_preamble(8)


def test_resets_then_idles_before_the_first_scan() -> None:
    lines = [l for l in svf.emit_svf_for_preamble(8).splitlines() if not l.startswith("!")]
    reset_idx = lines.index("STATE RESET;")
    idle_idx = lines.index("STATE IDLE;")
    first_scan = next(i for i, l in enumerate(lines) if l.startswith("SIR"))
    assert reset_idx < idle_idx < first_scan


def test_emit_svf_accepts_arbitrary_steps() -> None:
    text = svf.emit_svf(preamble_steps(8), 8, comment="hello")
    assert "! hello" in text
    assert text.endswith("\n")


def test_the_sir_width_follows_the_requested_ir_length_not_the_opcode_value() -> None:
    """A scan of the wrong width does not error -- it selects a different
    register.

    RM Table 959's 16-bit IDCODE opcode is 0x0001. An emitter that sized the SIR
    from the operand would call that 8 bits, load a *different* instruction, and
    return whatever register that selects with no diagnostic anywhere. It is the
    same class of silent failure as the 8-bit preamble, which appeared to work
    while actually reading the device ID.
    """
    from s32edbg.data.bscan import JTAGC_IR16, JTAGC_IR_IDCODE

    opcode = JTAGC_IR16[JTAGC_IR_IDCODE]
    assert opcode <= 0xFF, "this test is only meaningful for a small 16-bit opcode"

    text = svf.emit_svf([("IDCODE", 0x00, opcode, 0, 32)], 16)
    assert "SIR 16 TDI (0001);" in text
    assert "SIR 8" not in text


def test_a_dr_read_masks_exactly_the_byte_it_asks_for() -> None:
    """SVF writes scan values MSB-first across the whole vector, so byte 0 -- the
    first bit shifted out -- is the *rightmost* pair of hex digits. Getting this
    reversed reads the far end of the register and reports it as the near end."""
    text = svf.emit_dr_read(16, 0x0202, 32, 0, with_preamble=False)
    assert "MASK (000000FF);" in text

    text = svf.emit_dr_read(16, 0x0202, 32, 3, with_preamble=False)
    assert "MASK (FF000000);" in text


def test_a_dr_read_expects_zero_so_the_mismatch_report_reveals_the_data() -> None:
    """JTAGLoad has no "print TDO" command. Its only read channel is the
    mismatch line, so a read is a deliberately-failing compare against zero."""
    text = svf.emit_dr_read(16, 0x0202, 16, 0, with_preamble=False)
    assert "TDO (0000)" in text


def test_a_dr_read_refuses_a_scan_that_is_not_a_whole_number_of_bytes() -> None:
    with pytest.raises(ValueError, match="whole number of bytes"):
        svf.emit_dr_read(16, 0x0202, 12, 0)


def test_a_dr_read_refuses_a_byte_index_outside_the_scan() -> None:
    with pytest.raises(ValueError, match="outside"):
        svf.emit_dr_read(16, 0x0202, 32, 4)


def test_the_found_value_is_parsed_out_of_the_mismatch_report() -> None:
    stdout = (
        "Executing file [sample.svf]...\n"
        f"{svf.TDO_MISMATCH} (Expected: 0x00, Found: 0x1D)\n"
    )
    assert svf.parse_found_byte(stdout) == 0x1D


def test_a_byte_that_really_is_zero_reports_no_mismatch_and_reads_back_as_zero() -> None:
    """The read channel is a failing compare, so a genuinely-zero byte produces
    no report at all. That has to be read as zero rather than as an error, or
    every cleared pin looks like a failure."""
    assert svf.parse_found_byte("Download successful.\n") is None


def test_svf_result_ok_requires_success_marker() -> None:
    good = svf.SvfResult(0, "Executing file [x.svf]...\nDownload successful.\n", [])
    assert good.ok
    bad = svf.SvfResult(0, 'Command "TRST" is not supported in this software version !', [])
    assert not bad.ok
    assert bad.failures()


def test_svf_result_reports_nonzero_exit() -> None:
    assert not svf.SvfResult(1, "Download failed", []).ok


@pytest.mark.parametrize("irlen", [8, 16])
def test_generated_svf_is_nonempty_and_terminated(irlen: int) -> None:
    text = svf.emit_svf_for_preamble(irlen)
    statements = [l for l in text.splitlines() if l and not l.startswith("!")]
    assert statements
    assert all(l.endswith(";") for l in statements)

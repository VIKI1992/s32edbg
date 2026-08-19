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


def test_idcode_read_has_no_preamble_and_no_sir() -> None:
    """TLR loads IDCODE by itself (RM 136.3.1), valid at either chain width --
    an SIR would need to know the IR length, which is the very thing an
    un-probed chain cannot tell us."""
    text = svf.emit_idcode_read(0)
    statements = [l for l in text.splitlines() if l and not l.startswith("!")]
    assert not any(l.startswith("SIR") for l in statements)
    assert "SDR 32 TDI (00000000) TDO (00000000) MASK (000000FF);" in statements
    assert "STATE RESET;" in statements
    # exactly one scan
    assert sum(l.startswith("SDR ") for l in statements) == 1


def test_idcode_read_masks_the_requested_byte() -> None:
    assert "MASK (FF000000);" in svf.emit_idcode_read(3)
    with pytest.raises(ValueError, match="outside"):
        svf.emit_idcode_read(4)


def test_marker_verify_places_the_marker_after_the_expected_length() -> None:
    """The §7.3 marker-delay design: the marker goes in TDI's low 32 bits
    (shifted first) and must re-emerge exactly expected_len clocks later."""
    marker = 0x6699_A55A
    text = svf.emit_marker_verify(16, 0x0202, 494, marker)
    # The preamble contributes its own SDRs; the verify scan is the last one.
    scan = [l for l in text.splitlines() if l.startswith("SDR ")][-1]
    assert scan.startswith("SDR 526 ")  # 494 + 32
    nib = (526 + 3) // 4
    assert f"TDI ({marker:0{nib}X})" in scan
    assert f"TDO ({marker << 494:0{nib}X})" in scan
    assert f"MASK ({0xFFFF_FFFF << 494:0{nib}X})" in scan
    # and the instruction that selects the boundary register
    assert "SIR 16 TDI (0202);" in text


def test_marker_verify_includes_the_preamble() -> None:
    text = svf.emit_marker_verify(16, 0x0202, 494, 0x6699_A55A)
    assert "SIR 16 TDI (0A00);" in text  # EXT_TEST_CTRL


def test_dr_verify_compares_only_the_masked_cells() -> None:
    """496 shifted bits, 494 real cells: the two pad bits are unpredictable
    and must not fail the compare."""
    text = svf.emit_dr_verify(16, 0x0202, 496, expect=0x5, mask_bits=494)
    scan = next(l for l in text.splitlines() if l.startswith("SDR 496 "))
    nib = (496 + 3) // 4
    assert f"MASK ({(1 << 494) - 1:0{nib}X})" in scan
    assert f"TDO ({0x5:0{nib}X})" in scan


def test_dr_verify_rejects_mask_wider_than_scan() -> None:
    with pytest.raises(ValueError, match="outside"):
        svf.emit_dr_verify(16, 0x0202, 32, expect=0, mask_bits=33)


def test_drive_orders_preload_vector_then_final_instruction() -> None:
    """RM 136.4.4: fill the update latches under SAMPLE/PRELOAD (system
    unaffected), then one Update-IR of EXTEST flips the pins. The other order
    would drive stale latch contents."""
    text = svf.emit_drive(16, 0x0202, vector=0x3, nbits=494, final_ir=0x0404, final_name="EXTEST")
    lines = [l for l in text.splitlines() if not l.startswith("!")]
    preload_i = lines.index("SIR 16 TDI (0202);")
    vector_i = next(i for i, l in enumerate(lines) if l.startswith("SDR 494 "))
    final_i = lines.index("SIR 16 TDI (0404);")
    assert preload_i < vector_i < final_i
    assert final_i == len(lines) - 1  # nothing after: the drive stays latched
    # write-only: no compares anywhere
    assert "TDO" not in text
    assert "MASK" not in text


def test_drive_includes_the_preamble_and_ends_clean() -> None:
    text = svf.emit_drive(16, 0x0202, vector=0, nbits=494, final_ir=0x0509, final_name="HIGHZ")
    assert "SIR 16 TDI (0A00);" in text
    statements = [l for l in text.splitlines() if l and not l.startswith("!")]
    assert all(l.endswith(";") for l in statements)


def test_release_is_a_bare_tap_reset() -> None:
    text = svf.emit_release()
    assert "STATE RESET;" in text
    assert "SIR" not in text and "SDR" not in text


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

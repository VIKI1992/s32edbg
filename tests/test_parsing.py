"""Commander output parser tests, against representative transcripts."""

from __future__ import annotations

import pytest

from s32edbg.errors import TransportError
from s32edbg.transport import parsing


def test_parse_dp_read() -> None:
    text = "Reading DP register 1 = 0xF0000040 (1 read repetitions needed)"
    assert parsing.parse_reg_value(text) == 0xF0000040


def test_parse_ap_read() -> None:
    assert parsing.parse_reg_value("Read AP register 3 = 0x24770006") == 0x24770006


def test_parse_uses_last_value_when_several_printed() -> None:
    text = "Read AP register 3 = 0x00000000\nReading DP register 3 = 0x001DE040"
    assert parsing.parse_reg_value(text) == 0x001DE040


def test_parse_falls_back_to_bare_assignment() -> None:
    assert parsing.parse_reg_value("value = 0xDEADBEEF") == 0xDEADBEEF


def test_parse_reg_value_raises_with_the_error_line() -> None:
    text = "ERROR: Timeout while reading from DP/AP register!"
    with pytest.raises(TransportError) as exc:
        parsing.parse_reg_value(text)
    assert "Timeout while reading" in str(exc.value)


def test_parse_jtag_id() -> None:
    text = "JTAG Id: 0x0831F01D  Version: 0x0 Part no: 0x831F Man. Id: 000E"
    assert parsing.parse_jtag_id(text) == 0x0831F01D


def test_parse_swd_dp_id() -> None:
    assert parsing.parse_jtag_id("Found SWD-DP with ID 0x4C013477") == 0x4C013477


def test_parse_jtag_id_raises_on_invalid() -> None:
    with pytest.raises(TransportError):
        parsing.parse_jtag_id("JTAG Id: 0x00000000: INVALID\nERROR: no target")


def test_parse_returns() -> None:
    assert parsing.parse_returns("WJTAGDR ... returns 0x1F0A") == 0x1F0A
    assert parsing.parse_returns("nothing here") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("VTarget = 3.300V", 3300),
        ("VTref = 3300mV", 3300),
        ("VTarget = 1.8V", 1800),
        ("no voltage line", None),
    ],
)
def test_parse_vtref(text: str, expected: int | None) -> None:
    assert parsing.parse_vtref_mv(text) == expected


def test_parse_pins() -> None:
    text = "TCK=1 TDI=0 TDO=1 TMS=1 TRES=1 TRST=1"
    pins = parsing.parse_pins(text)
    assert pins == {"TCK": 1, "TDI": 0, "TDO": 1, "TMS": 1, "TRES": 1, "TRST": 1}


def test_parse_pins_skips_unknown() -> None:
    assert parsing.parse_pins("TCK=? TDI=0") == {"TDI": 0}


def test_parse_probe_banner() -> None:
    text = (
        "SEGGER J-Link Commander V9.62\n"
        "DLL version V9.62, compiled Jul 22 2026\n"
        "Hardware version: V13.00\n"
        "S/N: 000603000869\n"
        "Firmware: J-Link V13 compiled Jul 22 2026 13:00:00\n"
    )
    fields = parsing.parse_probe_banner(text)
    assert fields["hardware_version"] == "V13.00"
    assert fields["serial"] == "000603000869"
    assert fields["dll_version"] == "V9.62"
    assert fields["firmware"] is not None and "J-Link V13" in fields["firmware"]


def test_find_error_detects_markers() -> None:
    assert parsing.find_error("ERROR: Read from DP/AP register failed!") is not None
    assert parsing.find_error("all good") is None


# --------------------------------------------------------------------------
# Probe/DLL limitation messages -- these gate this target specifically
# --------------------------------------------------------------------------


def test_detects_soc600_unsupported() -> None:
    text = "The connected probe (S/N: 600300869) does not support ARM CoreSight SoC-600."
    assert parsing.find_capability_warnings(text) == ["does not support ARM CoreSight SoC-600"]


def test_detects_dpv3_ceiling() -> None:
    text = "Detected DPv3 or later but J-Link only supports up to DPv2. Debugging not possible"
    assert "only supports up to DPv2" in parsing.find_capability_warnings(text)


def test_detects_dormant_unsupported() -> None:
    text = "The connected J-Link (S/N: 1) does not support dormant mode handling."
    assert "does not support dormant mode handling" in parsing.find_capability_warnings(text)


def test_no_warnings_on_clean_output() -> None:
    assert parsing.find_capability_warnings("CoreSight SoC-600 or later (DPv3 detected)") == []


def test_parse_scan_length_variants() -> None:
    assert parsing.parse_scan_length("ScanLen = 208") == 208
    assert parsing.parse_scan_length("Length of scan chain: 37") == 37
    assert parsing.parse_scan_length("nothing") is None


# ---------------------------------------------------------------------------
# Batched register reads (the cross-transport DPIDR check)
# ---------------------------------------------------------------------------


def test_iter_reg_values_returns_every_value_in_order() -> None:
    """parse_reg_value takes only the last, which loses a batch's sequence."""
    text = (
        "Write DP register 2 = 0x00000000\n"
        "Reading DP register 0 = 0x00000000\n"
        "Reading DP register 0 = 0x4C013477\n"
    )
    assert parsing.iter_reg_values(text) == ["00000000", "00000000", "4C013477"]


def test_iter_reg_values_is_empty_when_nothing_matched() -> None:
    assert parsing.iter_reg_values("no registers here") == []


def test_parse_command_values_attributes_each_value_to_its_command() -> None:
    """A WriteDP echoes a value too, so counting positions across the whole
    transcript pairs a read with a write. Splitting by command echo is what
    keeps a batched DPIDR read from reporting a CTRL/STAT value."""
    text = "\n".join(
        [
            "J-Link>WriteDP 2 0x00000000",
            "Writing DP register 2 = 0x00000000 (0 write repetitions needed)",
            "J-Link>ReadDP 0",
            "Reading DP register 0 = 0x4BA07477 (0 read repetitions needed)",
            "J-Link>ReadDP 1",
            "Reading DP register 1 = 0xF0000000 (0 read repetitions needed)",
        ]
    )
    assert parsing.parse_command_values(text, "ReadDP") == [0x4BA07477, 0xF0000000]
    assert parsing.parse_command_values(text, "WriteDP") == [0x00000000]


def test_parse_command_values_is_empty_when_the_command_never_ran() -> None:
    assert parsing.parse_command_values("J-Link>qc", "ReadDP") == []


def test_parse_dpidr_reads_the_value_jlink_reports_at_connect() -> None:
    """Over JTAG this is the only correct route to the DPIDR.

    ADIv6 Figure A1-2 lists the JTAG-DP DPACC registers as Reserved /
    CTRL-STAT / SELECT / SELECT1 / RDBUFF -- address 0b00 is Reserved, not
    DPIDR. Reading it anyway returns a stable but meaningless value (0x4BA07477
    on this target, where the real DPIDR is 0x4C013477).
    """
    text = "DPIDR: 0x4C013477\nCoreSight SoC-600 or later (DPv3 detected)"
    assert parsing.parse_dpidr(text) == 0x4C013477


def test_parse_dpidr_is_none_when_jlink_did_not_report_one() -> None:
    assert parsing.parse_dpidr("Connecting to target via JTAG") is None


def test_parse_jtag_id_reads_the_chain_detection_line() -> None:
    """V9.62 reports the IDCODE from chain detection as " #0 Id: 0x...", not as
    "JTAG Id:". Matching only the latter makes a perfectly live TAP report as no
    response -- which made a retry loop spin against an already-open window.
    """
    text = (
        "JTAG chain detection found 1 devices:\n"
        " #0 Id: 0x0831F01D, IRLen: 04, JTAG-DP\n"
    )
    assert parsing.parse_jtag_id(text) == 0x0831F01D


def test_parse_jtag_id_still_reads_the_simple_form() -> None:
    text = "JTAG Id: 0x0831F01D  Version: 0x0 Part no: 0x831F Man. Id: 000E"
    assert parsing.parse_jtag_id(text) == 0x0831F01D

"""Pure parsers for J-Link Commander output.

Kept free of subprocess handling so they can be unit-tested against captured
transcripts. The format strings these match were read out of the shipped
JLink.exe binary, since the 36-page UM08001 in the source folder documents no
Commander output at all.
"""

from __future__ import annotations

import re

from ..errors import TransportError

#: "Reading DP register 1 = 0x12345678 (1 read repetitions needed)"
#: "Read AP register 3 = 0x24770006"
#: "Write DP register 2 = 0x01C10000"
_REG_VALUE_RE = re.compile(
    r"(?:read|write|reading|writing)\s+(?:\w+\s+)?register\s+\d+\s*=\s*0x([0-9A-Fa-f]+)",
    re.IGNORECASE,
)

#: Two spellings, and missing the second one makes a live TAP look dead.
#:
#: Older/simple form:
#:   "JTAG Id: 0x0831F01D  Version: 0x0 Part no: 0x831F Man. Id: 000E"
#: Chain-detection form, which is what V9.62 actually prints on this target:
#:   "JTAG chain detection found 1 devices:"
#:   " #0 Id: 0x0831F01D, IRLen: 04, JTAG-DP"
_JTAG_ID_RE = re.compile(
    r"(?:JTAG\s+Id:|#\d+\s+Id:)\s*0x([0-9A-Fa-f]{1,8})", re.IGNORECASE
)

#: "Found SWD-DP with ID 0x4C013477"
_SWD_DP_RE = re.compile(r"SWD-DP\s+with\s+ID\s+0x([0-9A-Fa-f]{1,8})", re.IGNORECASE)

#: "DPIDR: 0x4C013477", printed by J-Link's own connect.
#:
#: This is the only route to a correct DPIDR over JTAG. ADIv6 Figure A1-2 lists
#: the JTAG-DP DPACC registers as Reserved / CTRL-STAT / SELECT / SELECT1 /
#: RDBUFF -- address 0b00 is *Reserved*, not DPIDR. The DP identification lives
#: on the separate IDCODE scan chain, reached with the DAP's IDCODE instruction
#: (IR 0xFE, RM Table 956), not through DPACC. Reading DPACC 0 on JTAG returns a
#: stable but meaningless value: on this target 0x4BA07477 instead of the real
#: 0x4C013477.
_DPIDR_RE = re.compile(r"\bDPIDR:\s*0x([0-9A-Fa-f]{1,8})", re.IGNORECASE)

#: V9.62 actually prints "VTref=3.307V" (no spaces, VTref not VTarget). Older
#: builds and other commands use "VTarget = 3.300V" or "VTref = 3300mV", so all
#: three spellings are accepted. mV is matched first so "3300mV" is not read as
#: 3300 volts.
_VOLT_MV_RE = re.compile(r"(?:VTref|VTarget)\s*=\s*([0-9]+)\s*mV", re.IGNORECASE)
_VOLT_V_RE = re.compile(r"(?:VTref|VTarget)\s*=\s*([0-9]+)(?:\.([0-9]{1,3}))?\s*V(?![a-z])", re.IGNORECASE)

#: "TCK=1 TDI=0 TDO=1 TMS=1 TRES=1 TRST=1"
_PIN_RE = re.compile(r"\b(TCK|TDI|TDO|TMS|TRES|TRST)\s*=\s*([01?])", re.IGNORECASE)

#: "WJTAGDR ... returns 0x1234"
_RETURNS_RE = re.compile(r"returns\s+0x([0-9A-Fa-f]+)", re.IGNORECASE)

#: WJTAGRaw's capture line, " TDO: <hex>". No 0x prefix in the format string.
_TDO_RE = re.compile(r"TDO:\s*([0-9A-Fa-f]+)", re.IGNORECASE)

#: "ScanLen = 416" / "Scan chain length = 416" / "Length of scan chain: 416"
_SCANLEN_RE = re.compile(
    r"(?:scan(?:\s*chain)?\s*(?:len(?:gth)?)?|len)\s*(?:of\s*scan\s*chain)?\s*[:=]\s*(\d+)",
    re.IGNORECASE,
)

#: "IRLen = 8" / "IR length = 8"
_IRLEN_RE = re.compile(r"IR\s*-?\s*len(?:gth)?\s*[:=]\s*(\d+)", re.IGNORECASE)

#: "Hardware version: V13.00" / "Hardware: V13.00"
_HW_RE = re.compile(r"Hardware(?:\s+version)?\s*[:=]\s*(V[0-9.]+)", re.IGNORECASE)

#: "Firmware: J-Link V13 compiled Jul 22 2026 ..."
_FW_RE = re.compile(r"Firmware\s*[:=]\s*(.+)", re.IGNORECASE)

#: "S/N: 000603000869" / "Serial number: 600300869"
_SN_RE = re.compile(r"(?:S/N|Serial\s*number)\s*[:=]\s*(\S+)", re.IGNORECASE)

#: "DLL version V9.62, compiled ..."
_DLL_RE = re.compile(r"DLL\s+version\s*[:=]?\s*(V?[0-9.]+[a-z]?)", re.IGNORECASE)

ERROR_MARKERS = (
    "ERROR:",
    "Error while",
    "Could not",
    "Cannot",
    "not supported by connected",
    "Unknown command",
    "Timeout while",
    "Wrong parity",
    "failed",
)


def find_error(text: str) -> str | None:
    """Return the first line that looks like a J-Link error, if any."""
    for line in text.splitlines():
        stripped = line.strip()
        for marker in ERROR_MARKERS:
            if marker.lower() in stripped.lower():
                return stripped
    return None


def parse_reg_value(text: str, *, what: str = "register") -> int:
    """Extract the hex value from a Read/WriteAP/DP response."""
    err = find_error(text)
    matches = _REG_VALUE_RE.findall(text)
    if not matches:
        # Some builds print only "= 0x...." without the word "register".
        loose = re.findall(r"=\s*0x([0-9A-Fa-f]{1,8})\b", text)
        if loose:
            matches = loose
    if not matches:
        detail = f" ({err})" if err else ""
        raise TransportError(f"could not parse a {what} value from J-Link output{detail}: {text!r}")
    return int(matches[-1], 16)


def parse_jtag_id(text: str) -> int:
    """Extract a JTAG or SWD-DP identification value.

    JLink.exe reports a failed scan as "JTAG Id: 0x%.8X: INVALID", so a matched
    ID is only accepted when that marker is absent -- otherwise a dead chain
    would silently parse as IDCODE 0x00000000.
    """
    m = _JTAG_ID_RE.search(text)
    if m and "invalid" not in text.lower():
        return int(m.group(1), 16)
    m = _SWD_DP_RE.search(text)
    if m:
        return int(m.group(1), 16)
    err = find_error(text)
    if m and "invalid" in text.lower():
        raise TransportError(
            f"J-Link reports the JTAG ID 0x{int(m.group(1), 16):08X} as INVALID. "
            "With JCOMP low the JTAGC and SWJ-DP stay in reset (RM 60.5.4.1), which "
            "looks exactly like this."
        )
    raise TransportError(
        f"could not parse a JTAG/DP ID from J-Link output{f' ({err})' if err else ''}: {text!r}"
    )


def iter_reg_values(text: str) -> list[str]:
    """Every Read/Write AP/DP hex value in the transcript, in order.

    parse_reg_value() returns only the last one, which is right for a
    single-command run but loses the sequence a batch produces.
    """
    return _REG_VALUE_RE.findall(text)


def parse_command_values(text: str, command: str) -> list[int]:
    """Values produced by each occurrence of ``command``, in order.

    JLink.exe echoes every command as "J-Link><command> <args>" and prints the
    result on the following line, so a batch transcript can be split by command
    rather than by position. That matters because reads and writes both print a
    value: counting positions across the whole transcript attributes a
    WriteDP's echo to a ReadDP.
    """
    out: list[int] = []
    for block in text.split(f"J-Link>{command}")[1:]:
        m = _REG_VALUE_RE.search(block)
        if m:
            out.append(int(m.group(1), 16))
    return out


def parse_dpidr(text: str) -> int | None:
    """DPIDR as J-Link's own connect reports it. See _DPIDR_RE for why this
    matters: over JTAG it cannot be read through DPACC at all."""
    m = _DPIDR_RE.search(text)
    return int(m.group(1), 16) if m else None


def parse_returns(text: str) -> int | None:
    m = _RETURNS_RE.search(text)
    return int(m.group(1), 16) if m else None


def parse_tdo_captures(text: str) -> list[str]:
    """Every WJTAGRaw " TDO: <hex>" capture, in order, as raw hex strings.

    Returned unparsed because the caller knows the shift width and therefore how
    to interpret a trailing partial nibble. JLink.exe prints the capture with
    '%.2X' per whole byte plus a '%.1X' tail; those format strings sit
    contiguously with ' TDO: ' and "Expected number of bits" in the binary,
    which is the WJTAGRaw argument-count error.
    """
    return [m.strip() for m in _TDO_RE.findall(text)]


def parse_vtref_mv(text: str) -> int | None:
    """Target reference voltage in millivolts, or None if not reported."""
    m = _VOLT_MV_RE.search(text)
    if m:
        return int(m.group(1))
    m = _VOLT_V_RE.search(text)
    if m:
        whole = int(m.group(1))
        frac = int((m.group(2) or "").ljust(3, "0") or 0)
        return whole * 1000 + frac
    return None


def parse_pins(text: str) -> dict[str, int]:
    pins: dict[str, int] = {}
    for name, value in _PIN_RE.findall(text):
        if value != "?":
            pins[name.upper()] = int(value)
    return pins


def parse_scan_length(text: str) -> int | None:
    m = _SCANLEN_RE.search(text)
    return int(m.group(1)) if m else None


def parse_irlen(text: str) -> int | None:
    m = _IRLEN_RE.search(text)
    return int(m.group(1)) if m else None


def parse_probe_banner(text: str) -> dict[str, str | None]:
    def one(pattern: re.Pattern[str]) -> str | None:
        m = pattern.search(text)
        return m.group(1).strip() if m else None

    return {
        "serial": one(_SN_RE),
        "hardware_version": one(_HW_RE),
        "firmware": one(_FW_RE),
        "dll_version": one(_DLL_RE),
    }


#: Messages that reveal a hard limitation of the connected probe or DLL. These
#: exist as literal strings in JLink_x64.dll and directly gate this target.
CAPABILITY_WARNINGS = (
    "does not support ARM CoreSight SoC-600",
    "does not support CoreSight SoC-600 DAP setups",
    "does not support dormant mode handling",
    "only supports up to DPv2",
)


def find_capability_warnings(text: str) -> list[str]:
    lowered = text.lower()
    return [w for w in CAPABILITY_WARNINGS if w.lower() in lowered]

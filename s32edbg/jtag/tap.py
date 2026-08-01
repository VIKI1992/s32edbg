"""TAP-level operations: IDCODE, IR-width probing, DR-length measurement.

The IR width is measured rather than assumed, because the RM contradicts itself
about it (see data/bscan.py) and the answer determines every JTAGC opcode the
boundary-scan feature sends.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..data.bscan import JTAGC_IR_BYPASS, jtagc_ir
from ..data.known_ids import JTAG_IDCODES, decode_idcode
from ..errors import TransportError
from ..transport.base import Transport
from . import tapseq

#: RM 135.5.3 (L412403): "The IR length is 8-bits." RM Table 959 instead defines
#: 16-bit opcodes for S32E2. Both are probed.
PLAUSIBLE_IR_LENGTHS = (8, 16)


@dataclass
class TapInfo:
    idcode: int | None = None
    idcode_retry: int | None = None
    ir_length: int | None = None
    ir_length_method: str | None = None
    dr_length_bypass: int | None = None
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def part_name(self) -> str | None:
        return JTAG_IDCODES.get(self.idcode) if self.idcode is not None else None

    def as_dict(self) -> dict[str, object]:
        decoded = decode_idcode(self.idcode) if self.idcode is not None else None
        return {
            "idcode": f"0x{self.idcode:08X}" if self.idcode is not None else None,
            "idcode_decoded": decoded,
            "part_name": self.part_name,
            "ir_length": self.ir_length,
            "ir_length_method": self.ir_length_method,
            "dr_length_bypass": self.dr_length_bypass,
            "notes": self.notes,
            "warnings": self.warnings,
        }


def read_idcode(t: Transport) -> int:
    """Reset the TAP and read the device identification register."""
    if t.raw_shift_returns_tdo:
        seq = tapseq.idcode_scan()
        raw = t.raw_shift(seq.nbits, seq.tms, seq.tdi)
        if raw is not None:
            return seq.extract(raw)
    # No raw path. A backend may still know the IDCODE from the chain scan it
    # ran at connect -- the DLL caches one per chain device.
    native = getattr(t, "read_idcode", None)
    if native is not None:
        return int(native())
    t.reset_tap()
    return t.dr_shift(0, 32)


def measure_ir_length(t: Transport, *, max_bits: int = 64, marker_len: int = 4) -> int:
    """Measure the IR width with a walking-ones marker through Shift-IR.

    Leaves the TAP in Test-Logic-Reset afterwards, which RM 136.4.8 says reloads
    the IR with IDCODE -- important, because the measurement itself shifts an
    arbitrary value through the instruction register.
    """
    if not t.raw_shift_returns_tdo:
        raise TransportError(
            f"{t.name} transport cannot measure the IR length: it has no way to "
            "report the TDO capture from a raw shift"
        )

    total = max_bits + marker_len
    pattern = (1 << marker_len) - 1
    seq = tapseq.ir_scan(pattern, total, from_reset=True)
    raw = t.raw_shift(seq.nbits, seq.tms, seq.tdi)
    if raw is None:
        raise TransportError(
            f"{t.name} transport claims raw_shift_returns_tdo but returned no capture"
        )
    tdo = seq.extract(raw)
    t.reset_tap()

    offset = tapseq.find_shift_delay(tdo, total, marker_len=marker_len)
    if offset is None:
        raise TransportError(
            f"walking-ones marker never reached TDO within {max_bits} IR bits. "
            "Check that JCOMP is high -- with JCOMP low the JTAGC and SWJ-DP stay "
            "in reset (RM 60.5.4.1) and TDO will not shift."
        )
    if offset == 0:
        raise TransportError(
            "measured an IR length of 0, which means TDI appears to be wired "
            "straight through to TDO. Check the TDI/TDO path and JCOMP."
        )
    return offset


def measure_dr_length(
    t: Transport,
    ir_value: int,
    ir_bits: int,
    *,
    max_bits: int = 4096,
    marker: int = tapseq.MEASURE_MARKER,
    marker_len: int = tapseq.MEASURE_MARKER_BITS,
) -> int:
    """Length of the DR selected by ``ir_value``, found by marker delay.

    Loads the instruction and shifts its data register in a single
    jtag_sequence() step, because on a batch backend the two would otherwise
    land in different JLink.exe runs and the instruction would be lost.

    The register's own captured contents shift out first, so the marker appears
    after exactly ``length`` bits. The marker is deliberately not all-ones --
    see tapseq.MEASURE_MARKER for why that would under-report the length of a
    register whose capture contains a run of ones, such as the BSR.
    """
    total = max_bits + marker_len
    pattern = marker & ((1 << marker_len) - 1)
    captures = t.jtag_sequence([(ir_value, ir_bits, pattern, total)])
    tdo = captures[0] if captures else None
    if tdo is None:
        raise TransportError(
            f"{t.name} transport shifted the register but cannot report TDO, so the "
            "DR length cannot be measured"
        )
    offset = tapseq.find_marker_delay(tdo, total, marker=pattern, marker_len=marker_len)
    if offset is None:
        raise TransportError(
            f"the marker never reached TDO within {max_bits} DR bits. "
            "Check that JCOMP is high -- with JCOMP low the JTAGC and SWJ-DP stay "
            "in reset (RM 60.5.4.1) and TDO will not shift."
        )
    return offset


def measure_dr_length_bypass(t: Transport, ir_length: int) -> int:
    """Length of the DR while BYPASS is loaded. Should be exactly 1."""
    return measure_dr_length(t, jtagc_ir(JTAGC_IR_BYPASS, ir_length), ir_length, max_bits=64)


def scan_chain_facts(t: Transport) -> dict[str, object]:
    """IDCODE and total IR length as J-Link itself reports them at connect.

    J-Link scans the chain during ``connect`` and prints both, so on a backend
    that can run a batch script this costs one run and needs no raw shifting at
    all. That is what makes ``tap info`` usable on the Commander backend, which
    can drive vectors but cannot report TDO.
    """
    scan = getattr(t, "scan_chain", None)
    if scan is None:
        return {}
    try:
        return scan()
    except (TransportError, ValueError):
        return {}


def probe(t: Transport, *, expect_ir: int | None = None) -> TapInfo:
    """Identify the TAP and settle the IR-width question."""
    info = TapInfo()
    facts = scan_chain_facts(t)
    scanned_idcode = facts.get("idcode")

    if isinstance(scanned_idcode, int):
        info.idcode = scanned_idcode
        info.notes.append(
            "IDCODE taken from J-Link's own chain scan at connect rather than a raw "
            "shift, so it does not depend on the backend reporting TDO."
        )
    else:
        info.idcode = read_idcode(t)
        # RM L412549: JTAGID bits[20:17] come from the secure fuse box and "are
        # not valid immediately out of reset", so a single read can legitimately
        # differ.
        second = read_idcode(t)
        if second != info.idcode:
            info.idcode_retry = second
            info.notes.append(
                f"IDCODE changed between reads (0x{info.idcode:08X} then 0x{second:08X}). "
                "RM L412549 says bits[20:17] come from the secure fuse box and are not "
                "valid immediately out of reset; the later value is used."
            )
            info.idcode = second

    if info.idcode is not None and info.idcode & 1 == 0:
        info.warnings.append(
            f"IDCODE 0x{info.idcode:08X} has bit 0 clear. RM Table 964 says bit 0 is "
            "RAO for the ID register, so this is more likely the bypass register or a "
            "dead TDO than a real ID."
        )
    if info.idcode is not None and info.part_name is None:
        info.warnings.append(
            f"IDCODE 0x{info.idcode:08X} is not one of the values RM Table 961 lists "
            "for S32E2/S32Z2."
        )

    scanned_irlen = facts.get("total_ir_len")
    if expect_ir is not None:
        info.ir_length = expect_ir
        info.ir_length_method = "supplied by --irlen"
    elif isinstance(scanned_irlen, int) and scanned_irlen > 0:
        info.ir_length = scanned_irlen
        info.ir_length_method = "J-Link chain scan (TotalIRLen reported at connect)"
    else:
        info.ir_length = measure_ir_length(t)
        info.ir_length_method = "measured (walking-ones through Shift-IR)"

    if info.ir_length not in PLAUSIBLE_IR_LENGTHS:
        info.warnings.append(
            f"measured an IR length of {info.ir_length}, but the RM only describes 8 "
            "(section 135.5.3) or 16 (Table 959, JTAGC concatenated with JTAGC_AE). "
            "JTAGC opcodes cannot be formed safely at this width."
        )
    else:
        info.notes.append(
            f"IR length {info.ir_length} selects the "
            f"{'8-bit Table 958' if info.ir_length == 8 else '16-bit Table 959'} opcode set."
        )
        if info.ir_length == 16:
            info.notes.append(
                "A 16-bit IR means JTAGC_AE in the Application Extension die is in the "
                "scan chain. Note the board schematic routes no AE JTAG port, so verify "
                "this before relying on it."
            )

    try:
        info.dr_length_bypass = measure_dr_length_bypass(t, info.ir_length)
        if info.dr_length_bypass != 1:
            info.warnings.append(
                f"BYPASS DR measured {info.dr_length_bypass} bits, expected 1. "
                "More than one TAP may be in the chain, which the board schematic "
                "does not show."
            )
    except (TransportError, ValueError, NotImplementedError) as exc:
        info.warnings.append(f"BYPASS DR length not measured: {exc}")
    finally:
        # The measurement leaves an arbitrary instruction loaded, so put the TAP
        # back in Test-Logic-Reset (RM 136.4.8 reloads IDCODE there). A backend
        # that cannot reset the TAP also never loaded one, so nothing to undo.
        try:
            t.reset_tap()
        except (TransportError, NotImplementedError):
            pass

    return info

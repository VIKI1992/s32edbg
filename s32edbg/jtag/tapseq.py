"""IEEE 1149.1 TAP state-machine sequencing, as pure bit vectors.

Building TMS/TDI vectors here (instead of inside a transport) buys two things:
arbitrary shift widths through J-Link's ``WJTAGRaw <NumBits> <TMS> <TDI>``
command -- which sidesteps the 64-bit ceiling on ``WJTAGDR <Data64>,<NumBits>``
-- and unit tests for the state walk without a probe attached.

Bit order is LSB-first throughout, matching WJTAGRaw's documented
"Write Raw JTAG data (LSB first)".

State walk reference (RM 136.4.3 and IEEE 1149.1):
    Test-Logic-Reset --0--> Run-Test/Idle
    Run-Test/Idle    --1--> Select-DR-Scan --1--> Select-IR-Scan
    Select-DR-Scan   --0--> Capture-DR     --0--> Shift-DR
    Select-IR-Scan   --0--> Capture-IR     --0--> Shift-IR
    Shift-x          --1--> Exit1-x        --1--> Update-x --0--> Run-Test/Idle

RM 136.4.8 (L421464): "Holding TMS high for five consecutive rising edges of TCK
guarantees entry into the Test-Logic-Reset state regardless of the current TAP
controller state." Entering TLR reloads the IR with IDCODE, so a DR shift that
must preserve a previously loaded instruction never passes through it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Sequence:
    """A TMS/TDI vector plus where the payload bits sit in the TDO response."""

    nbits: int
    tms: int
    tdi: int
    data_start: int
    data_bits: int

    def extract(self, tdo: int) -> int:
        """Pull the shifted-out payload from a full-length TDO capture."""
        if self.data_bits == 0:
            return 0
        return (tdo >> self.data_start) & ((1 << self.data_bits) - 1)


class _Builder:
    def __init__(self) -> None:
        self.tms = 0
        self.tdi = 0
        self.n = 0

    def clock(self, tms: int, tdi: int = 0) -> None:
        if tms:
            self.tms |= 1 << self.n
        if tdi:
            self.tdi |= 1 << self.n
        self.n += 1

    def clocks(self, tms_bits: list[int]) -> None:
        for bit in tms_bits:
            self.clock(bit)

    def shift(self, value: int, nbits: int) -> tuple[int, int]:
        """Shift ``nbits`` of ``value`` LSB-first; the last bit exits the state.

        Returns (data_start, nbits) so the caller can locate the payload in TDO.
        """
        start = self.n
        for i in range(nbits):
            last = i == nbits - 1
            self.clock(1 if last else 0, (value >> i) & 1)
        return start, nbits

    def done(self, data_start: int, data_bits: int) -> Sequence:
        return Sequence(self.n, self.tms, self.tdi, data_start, data_bits)


#: TMS pattern that forces Test-Logic-Reset from any state, then steps to
#: Run-Test/Idle.
TLR_TO_IDLE = [1, 1, 1, 1, 1, 0]


def goto_idle() -> Sequence:
    """Force Test-Logic-Reset, then land in Run-Test/Idle.

    Resets the IR to IDCODE as a side effect (RM 136.4.8).
    """
    b = _Builder()
    b.clocks(TLR_TO_IDLE)
    return b.done(0, 0)


def ir_scan(value: int, nbits: int, *, from_reset: bool = True) -> Sequence:
    """Shift ``nbits`` into the instruction register, ending in Run-Test/Idle."""
    if nbits <= 0:
        raise ValueError("IR shift needs at least one bit")
    b = _Builder()
    if from_reset:
        b.clocks(TLR_TO_IDLE)
    # Run-Test/Idle -> Select-DR -> Select-IR -> Capture-IR -> Shift-IR
    b.clocks([1, 1, 0, 0])
    start, count = b.shift(value, nbits)  # last bit exits to Exit1-IR
    b.clocks([1, 0])  # Update-IR -> Run-Test/Idle
    return b.done(start, count)


def dr_scan(value: int, nbits: int, *, from_reset: bool = False) -> Sequence:
    """Shift ``nbits`` through the data register, ending in Run-Test/Idle.

    With ``from_reset`` false this never passes through Test-Logic-Reset, so the
    instruction loaded by a preceding ir_scan() stays selected. Pass
    ``from_reset=True`` only when the IR should be reset to IDCODE first.
    """
    if nbits <= 0:
        raise ValueError("DR shift needs at least one bit")
    b = _Builder()
    if from_reset:
        b.clocks(TLR_TO_IDLE)
    # Run-Test/Idle -> Select-DR -> Capture-DR -> Shift-DR
    b.clocks([1, 0, 0])
    start, count = b.shift(value, nbits)  # last bit exits to Exit1-DR
    b.clocks([1, 0])  # Update-DR -> Run-Test/Idle
    return b.done(start, count)


def idcode_scan() -> Sequence:
    """Reset the TAP, then read the 32-bit device identification register.

    RM 136.4.8: entering Test-Logic-Reset loads the IR with IDCODE, so no IR
    shift is needed. RM L412606 notes the first bit shifted out is always 1.
    """
    return dr_scan(0, 32, from_reset=True)


def bits_to_int(bits: list[int]) -> int:
    """LSB-first list of bits -> integer."""
    out = 0
    for i, bit in enumerate(bits):
        if bit:
            out |= 1 << i
    return out


def int_to_bits(value: int, nbits: int) -> list[int]:
    """Integer -> LSB-first list of ``nbits`` bits."""
    return [(value >> i) & 1 for i in range(nbits)]


#: A 32-bit asymmetric marker for register-length measurement.
#:
#: An all-ones marker is unsafe: find_marker_delay returns the *earliest* match,
#: so any run of ones already present in the register's captured contents is
#: mistaken for the marker and the measured length comes out confidently too
#: short. That is a live hazard for the boundary-scan register, whose capture is
#: real pin state and can easily hold eight consecutive ones. This value has no
#: run longer than two and is its own reverse-complement-free pattern, so a
#: chance match costs about 2^-32 per bit position.
#:
#: 0b01100110_10011001_10100101_01011010 -- no run of identical bits longer than
#: two anywhere in it, and its two halves differ so it cannot match a shifted
#: copy of itself.
MEASURE_MARKER = 0x6699_A55A
MEASURE_MARKER_BITS = 32


def find_marker_delay(tdo: int, nbits: int, *, marker: int, marker_len: int) -> int | None:
    """Locate a known marker in a TDO capture to derive register length.

    The caller shifts ``marker`` into a register of unknown length. The
    register's own captured contents come out first; the marker then appears
    after exactly ``length`` bits. Returns that offset, or None if the marker
    never appears within ``nbits``.
    """
    if marker_len <= 0:
        raise ValueError("marker_len must be positive")
    mask = (1 << marker_len) - 1
    want = marker & mask
    for offset in range(nbits - marker_len + 1):
        if ((tdo >> offset) & mask) == want:
            return offset
    return None


def find_shift_delay(tdo: int, nbits: int, *, marker_len: int = 8) -> int | None:
    """find_marker_delay() with an all-ones marker.

    Kept for the walking-ones case; prefer MEASURE_MARKER for anything whose
    captured contents are not known in advance.
    """
    return find_marker_delay(tdo, nbits, marker=(1 << marker_len) - 1, marker_len=marker_len)

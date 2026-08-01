"""SWJ-DP protocol-switch sequences (ADIv6 B5.2 / B5.3).

**Nothing in this package transmits these.** They are here to document what a
protocol switch actually is, to explain the tool's output, and to be checked
against the sequences JLink_x64.dll ships. The switch itself runs on probe
firmware, reached through ``SetDormantModeHandling``; no exported DLL symbol can
drive an SWD bit pattern with a discoverable ABI, so there is no fallback to
write. Recording the constants without a driver is deliberate -- it makes the
tool able to *say* precisely what it asked the probe to do, and precisely what
the probe declined.

Why dormant state at all
------------------------
RM 135.14.1.1: "The CoreSight-600 SWJ-DP component implements the dormant state
of operation and therefore a transition through dormant state is required to
switch protocols." Arm agrees from the other side (ADIv6 B5.2): it "deprecates
use of these [direct 16-bit] sequences on devices where the dormant state of
operation is implemented", which this device is.

The one-way door
----------------
RM 135.5.1: "Switching back from SWD mode to JTAG mode requires POR." The
SWD-to-JTAG constants below are recorded for completeness only; there is no
command that sends them, because on this silicon they would not work anyway.

After any switch
----------------
ADIv6 B5.2: "For ADIv6 or later, the state of the DP registers after a switch is
UNKNOWN, and they must be programmed by the debugger before initiating any AP
transactions. Programming the DP registers might include clearing any sticky
errors." That is why dap.adiv6.Dap.reinitialise_after_protocol_switch() exists
and why it must not read CTRL/STAT before writing it.

All values below are MSB-first, as ADIv6 states them.
"""

from __future__ import annotations

#: (value, nbits) pairs, MSB-first.
#: ADIv6 B5.3.2. Preceded by >=5 clocks with TMS high. The IR must already hold
#: BYPASS or IDCODE, or "the behavior is unpredictable".
JTAG_TO_DORMANT = (0x2EEE_EEE6, 31)

#: ADIv6 B5.3.3. Preceded by >=50 clocks with TMS high (SWD line reset).
SWD_TO_DORMANT = (0x3DC7, 16)

#: ADIv6 B5.3.4. Preceded by >=8 clocks with TMS high, followed by 4 clocks with
#: TMS LOW, then an activation code.
SELECTION_ALERT = (0x49CF9046_A9B4A161_97F5BBC7_45703D98, 128)

#: ADIv6 Table B5-2 activation codes.
ACTIVATION_SWD = (0b0101_1000, 8)
ACTIVATION_JTAG = (0b0101_0000, 8)
ACTIVATION_JTAG_SERIAL = (0b0000_0000_0000, 12)

#: ADIv6 B5.2, deprecated on dormant-capable parts such as this one.
DIRECT_JTAG_TO_SWD = (0x79E7, 16)
DIRECT_SWD_TO_JTAG = (0x3CE7, 16)


def wire_bytes(value: int, nbits: int) -> bytes:
    """Pack an MSB-first sequence the way it goes onto the wire.

    The sequence is transmitted most-significant bit first, but each byte is
    shifted out least-significant bit first, so byte 0 holds the first eight
    clocks with the first clock in bit 0. A trailing partial byte is
    zero-padded in the high bits.

    This is the encoding used by the vendor implementation, which is what
    tests/test_swj.py checks against.
    """
    if nbits <= 0:
        raise ValueError("a sequence needs at least one bit")
    out = bytearray((nbits + 7) // 8)
    for i in range(nbits):
        # Bit i of the transmission is bit (nbits-1-i) of the MSB-first value.
        if (value >> (nbits - 1 - i)) & 1:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def describe_switch(src: str, dst: str) -> list[str]:
    """The ordered steps of a protocol switch, in prose, with citations.

    Used to tell the operator exactly what was requested of the probe -- most
    usefully when the probe refuses and nothing happened at all.
    """
    if (src, dst) != ("jtag", "swd"):
        raise ValueError(f"only the jtag->swd transition is described here, not {src}->{dst}")
    alert_hex = f"0x{SELECTION_ALERT[0]:032X}"
    return [
        "hold TMS high for at least 5 clocks, putting the JTAG TAP in "
        "Test-Logic-Reset (which also loads IDCODE, as the dormant entry requires)",
        f"send the {JTAG_TO_DORMANT[1]}-bit JTAG-to-dormant sequence "
        f"0x{JTAG_TO_DORMANT[0]:08X} (ADIv6 B5.3.2)",
        "hold TMS high for at least 8 clocks",
        f"send the {SELECTION_ALERT[1]}-bit Selection Alert {alert_hex} (ADIv6 B5.3.4)",
        "send 4 clocks with TMS low",
        f"send the {ACTIVATION_SWD[1]}-bit SW-DP activation code "
        f"0b{ACTIVATION_SWD[0]:08b} (ADIv6 Table B5-2)",
        "hold TMS high for at least 50 clocks to put the SWD interface in line reset",
        "reprogram the DP: its register state is UNKNOWN after a switch (ADIv6 B5.2)",
    ]

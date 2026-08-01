"""CoreSight component register offsets, shared by every component type.

Every CoreSight component -- ROM table, MEM-AP, core debug frame, CTI, PMU --
ends with the same identification block, so these offsets are the one part of
the map that never changes. They were previously duplicated three times
(``dap/romtable.py``, ``dap/adiv6.py``, ``data/ladder1.py``) and the lock and
claim offsets existed only inside ``tests/test_hardware.py``. This module is the
single source; the older modules keep their own spellings and import from here.

Citations: CoreSight Architecture Specification v3.0 (IHI0029G) section B2, and
ADIv6.0 (IHI0074B) p280 onwards for the PIDR/CIDR decode.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# The identification block, 0xF00-0xFFF
# --------------------------------------------------------------------------

ITCTRL = 0xF00  #: integration mode control

CLAIMSET = 0xFA0
CLAIMCLR = 0xFA4

#: Affinity of the component's associated processor, when it has one.
#:
#: For a core debug frame this carries the MPIDR affinity of the core it
#: belongs to, which makes it the *authoritative* way to say which core a frame
#: is -- better than inferring identity from address arithmetic. CSv3 B2.3.9.
DEVAFF0 = 0xFA8
DEVAFF1 = 0xFAC

#: Software lock. Writing LAR_KEY to LAR unlocks writes through this interface;
#: any other value locks it. LSR reports whether the lock is implemented (bit 0),
#: currently locked (bit 1) and its width (bit 2).
#:
#: Note ADIv6 p163 says the lock "is not implemented" for an AP, and SoC-600
#: ROM/AP LAR/LSR read RO 0 -- which is why ``romtable.py`` deliberately never
#: unlocks. A *core debug frame* is a different case and may well implement it.
LAR = 0xFB0
LSR = 0xFB4
LAR_KEY = 0xC5AC_CE55

LSR_SLI = 1 << 0  #: software lock implemented
LSR_SLK = 1 << 1  #: software lock currently locked
LSR_NTT = 1 << 2  #: 32-bit (0) or 64-bit (1) lock register

#: Debug authentication status. Which of the four debug-enable signals the
#: component currently sees granted. CSv3 B2.3.11.
AUTHSTATUS = 0xFB8

DEVARCH = 0xFBC
DEVID2 = 0xFC0
DEVID1 = 0xFC4
DEVID = 0xFC8
DEVTYPE = 0xFCC

PIDR4 = 0xFD0
PIDR5 = 0xFD4
PIDR6 = 0xFD8
PIDR7 = 0xFDC
PIDR0 = 0xFE0
PIDR1 = 0xFE4
PIDR2 = 0xFE8
PIDR3 = 0xFEC

CIDR0 = 0xFF0
CIDR1 = 0xFF4
CIDR2 = 0xFF8
CIDR3 = 0xFFC

#: CIDR preamble. CIDR1's low nibble is part of the preamble; its high nibble
#: carries the component class, which is why it is masked rather than compared.
CIDR0_PREAMBLE = 0x0D
CIDR1_PREAMBLE = 0x0
CIDR2_PREAMBLE = 0x05
CIDR3_PREAMBLE = 0xB1


def preamble_ok(cidr: tuple[int, int, int, int]) -> bool:
    """Whether a CIDR0-3 tuple carries a valid CoreSight preamble."""
    return (
        cidr[0] == CIDR0_PREAMBLE
        and (cidr[1] & 0x0F) == CIDR1_PREAMBLE
        and cidr[2] == CIDR2_PREAMBLE
        and cidr[3] == CIDR3_PREAMBLE
    )


# --------------------------------------------------------------------------
# DBGAUTHSTATUS / AUTHSTATUS decode
# --------------------------------------------------------------------------
#
# CSv3 B2.3.11 and Armv8 ARM. Each of the four debug-enable signals is reported
# as a 2-bit field with three defined encodings:
#
#     0b00  not implemented
#     0b10  implemented, disabled
#     0b11  implemented, enabled
#
# so the *high* bit means "implemented" and the low bit means "enabled". Getting
# that the wrong way round makes a denied signal read as not-implemented, which
# hides exactly the condition this register exists to report.

AUTH_NSID_SHIFT = 0  #: non-secure invasive debug
AUTH_NSNID_SHIFT = 2  #: non-secure non-invasive debug
AUTH_SID_SHIFT = 4  #: secure invasive debug
AUTH_SNID_SHIFT = 6  #: secure non-invasive debug

AUTH_FIELDS: tuple[tuple[str, int], ...] = (
    ("non_secure_invasive", AUTH_NSID_SHIFT),
    ("non_secure_non_invasive", AUTH_NSNID_SHIFT),
    ("secure_invasive", AUTH_SID_SHIFT),
    ("secure_non_invasive", AUTH_SNID_SHIFT),
)


def decode_authstatus(raw: int) -> dict[str, str]:
    """Decode DBGAUTHSTATUS into per-signal 'granted'/'denied'/'not implemented'.

    "Granted" is the only state in which invasive debug -- halting, stepping,
    programming a breakpoint comparator -- actually does anything. The classic
    symptom of getting this wrong is comparator writes that read back as zero
    with no error reported anywhere.
    """
    out: dict[str, str] = {}
    for name, shift in AUTH_FIELDS:
        field = (raw >> shift) & 0b11
        if not field & 0b10:
            out[name] = "not implemented"
        else:
            out[name] = "granted" if field & 0b01 else "denied"
    return out


def invasive_debug_granted(raw: int) -> bool:
    """Whether *any* invasive-debug signal is granted.

    Either secure or non-secure invasive debug is enough to halt a core; which
    one you get decides what the comparator's security qualification must say.
    """
    decoded = decode_authstatus(raw)
    return "granted" in (decoded["non_secure_invasive"], decoded["secure_invasive"])

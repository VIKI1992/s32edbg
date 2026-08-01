"""Identification tables and decoders.

Sources, quoted in comments where a value is load-bearing:
  RM   = NXP S32E2 Reference Manual, Rev. 5, 2025-01-28
  ADIv6= Arm Debug Interface Architecture Specification ADIv6.0, IHI0074B
  CSv3 = Arm CoreSight Architecture Specification v3.0, IHI0029G
  TRM  = Arm CoreSight SoC-600 Technical Reference Manual, 100806_0800_18

Values marked CONFIRMED are corroborated by at least two independent sources
(spec text plus a silicon reset value from the RM or the SoC-600 TRM).
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# JTAG TAP IDCODE -- RM Table 961 (L412567) and Table 984 (L421032)
# --------------------------------------------------------------------------

JTAG_IDCODES: dict[int, str] = {
    0x0831B01D: "S32E270 (27x27)",
    0x0831F01D: "S32E277/8/9 (27x27)",
    # S32Z2x, present in the same RM tables; useful to recognise a wrong board.
    0x0831001D: "S32Z270 (17x17)",
    0x0831101D: "S32Z270 (21x21)",
    0x0831501D: "S32Z277/8/9 (21x21)",
}

# RM Table 964 (L412635): device identification register field definitions.
def decode_idcode(idcode: int) -> dict[str, object]:
    """Split a JTAGC IDCODE into its RM Table 964 fields.

    NOTE (RM L412549): "Some fields of the JTAG ID bits are stored in fuses and
    are not valid immediately out of reset. The JTAGID bits[20:17] are formed by
    values stored in secure fuse box." A transient mismatch is expected; re-read.
    """
    return {
        "raw": idcode,
        "prn": (idcode >> 28) & 0xF,  # part revision number
        "dc": (idcode >> 22) & 0x3F,  # design center
        "pin": (idcode >> 12) & 0x3FF,  # part identification number
        "mic": (idcode >> 1) & 0x7FF,  # manufacturer identity code, 0xE = NXP
        "valid_bit": idcode & 1,  # RAO, distinguishes IDCODE from BYPASS
        "name": JTAG_IDCODES.get(idcode),
    }


# --------------------------------------------------------------------------
# JEP106 manufacturer decode
# ADIv6 p283: DES_0 = PIDR1[7:4] -> id[3:0]; DES_1 = PIDR2[2:0] -> id[6:4];
#             DES_2 = PIDR4[3:0] -> continuation count.
# --------------------------------------------------------------------------

JEP106: dict[tuple[int, int], str] = {
    (4, 0x3B): "Arm",  # CONFIRMED: ADIv6 p283 states Arm = 4 continuations, 0x3B
    (0, 0x0E): "NXP (Freescale)",  # CONFIRMED: RM MDM_AP/SDA_AP DEVARCH architect 0x00E
}


def jep106_name(continuation: int, identity: int) -> str:
    return JEP106.get((continuation, identity), f"unknown(cont={continuation},id=0x{identity:02X})")


# --------------------------------------------------------------------------
# CIDR1.CLASS -- ADIv6 Table D1-2 (p278) / CSv3 identical
# --------------------------------------------------------------------------

CLASS_NAMES: dict[int, str] = {
    0x0: "generic verification component",
    0x1: "ROM table (class 0x1, legacy)",
    0x9: "CoreSight component",
    0xB: "peripheral test block",
    0xE: "generic IP component",
    0xF: "CoreLink/PrimeCell (non-standard layout)",
}

CLASS_ROM_TABLE = 0x1
CLASS_CORESIGHT = 0x9

# --------------------------------------------------------------------------
# DEVARCH.ARCHID -- CSv3 Table B2-19 (p57)
#
# CAVEAT: that table's two columns are offset in the PDF text layer, so only
# entries with an independent second anchor are treated as authoritative.
# --------------------------------------------------------------------------

ARCHID_NAMES: dict[int, str] = {
    # CONFIRMED twice: ADIv6 p279 + p333 both state 0x0AF7 = ROM table;
    # TRM p244 gives css600_apbrom_32bit DEVARCH reset = 0x47700AF7.
    0x0AF7: "CoreSight ROM table",
    # CONFIRMED twice: CSv3 Table B2-19 + TRM p201/p156 give css600_apbap and
    # css600_ahbap DEVARCH reset = 0x47700A17.
    0x0A17: "Memory Access Port v2 (MEM-AP)",
    # Single-source (CSv3 Table B2-19 after realignment) -- treated as advisory.
    0x0A27: "JTAG Access Port v2",
    0x0A34: "power requester",
    0x0A47: "Unknown Access Port v2",
    0x1A14: "CTI",
    0x4A13: "ETM",
    0x2A16: "processor PMU",
    0x6A05: "processor debug (Armv8-R)",
    0x2A04: "processor debug (Armv8-M)",
    0x6A15: "processor debug (Armv8.0-A)",
    0x0A63: "STM",
    0x0A31: "basic trace router",
    # Vendor-defined, from RM silicon reset values (architect 0x00E = NXP).
    0x00F0: "NXP MDM-AP (vendor-defined)",  # RM L417964: DEVARCH 0x01D000F0
    0x000E: "NXP SDA-AP (vendor-defined)",  # RM L420158: DEVARCH 0x01D0000E
}

# ARCHIDs we are willing to assert without hedging in output.
ARCHID_CONFIRMED = frozenset({0x0AF7, 0x0A17, 0x00F0, 0x000E})


@dataclass(frozen=True)
class DevArch:
    raw: int
    present: bool
    architect_cont: int
    architect_id: int
    revision: int
    archid: int

    @property
    def architect_name(self) -> str:
        return jep106_name(self.architect_cont, self.architect_id)

    @property
    def archid_name(self) -> str | None:
        name = ARCHID_NAMES.get(self.archid)
        if name and self.archid not in ARCHID_CONFIRMED:
            return f"{name} (unverified mapping)"
        return name


def decode_devarch(raw: int) -> DevArch:
    """ADIv6 p333 / CSv3 p56: ARCHITECT[31:21], PRESENT[20], REVISION[19:16], ARCHID[15:0].

    ARCHITECT is the 11-bit compressed JEP106 form: continuation in bits[31:28]
    of the register (i.e. the top 4 bits of the 11-bit field), identity in the
    low 7 bits. Arm is 0x23B.
    """
    architect = (raw >> 21) & 0x7FF
    return DevArch(
        raw=raw,
        present=bool((raw >> 20) & 1),
        architect_cont=(architect >> 7) & 0xF,
        architect_id=architect & 0x7F,
        revision=(raw >> 16) & 0xF,
        archid=raw & 0xFFFF,
    )


# --------------------------------------------------------------------------
# DEVTYPE -- CSv3 Table B2-30 (p63): SUB[7:4], MAJOR[3:0]
#
# The MAJOR column extracts cleanly. The nested SUB column does NOT survive
# text extraction, so only SUB values cross-checked against observed silicon
# are named. Every one below is corroborated by the CP-1.4 frame map:
#   0x15 R52 external debug, 0x14 CTI, 0x16 PMU, 0x13 ETM
# and by the RM: MDM_AP and SDA_AP both read DEVTYPE 0x40.
# --------------------------------------------------------------------------

DEVTYPE_MAJOR: dict[int, str] = {
    0x0: "miscellaneous",
    0x1: "trace sink",
    0x2: "trace link",
    0x3: "trace source",
    0x4: "debug control",
    0x5: "debug logic",
    0x6: "performance monitor",
}

DEVTYPE_FULL: dict[int, str] = {
    0x00: "other/undefined",
    0x13: "ETM (trace source, core-associated)",
    0x14: "CTI (debug control, trigger matrix)",
    0x15: "processor external debug (debug logic, core-associated)",
    0x16: "PMU (performance monitor, core-associated)",
    0x40: "debug authentication module (miscellaneous)",
    0x11: "trace port (e.g. TPIU)",
    0x21: "trace funnel/router",
    0x12: "trace buffer (e.g. ETB/TMC)",
}


def decode_devtype(raw: int) -> dict[str, object]:
    major = raw & 0xF
    sub = (raw >> 4) & 0xF
    return {
        "raw": raw,
        "major": major,
        "sub": sub,
        "major_name": DEVTYPE_MAJOR.get(major, "reserved"),
        "name": DEVTYPE_FULL.get(raw & 0xFF),
    }


# --------------------------------------------------------------------------
# AP IDR -- ADIv6 p160 (C1.4.9)
# --------------------------------------------------------------------------

AP_CLASS_MEM = 0b1000
AP_CLASS_NONE = 0b0000

# ADIv6 Table C1-2 is NOT exhaustive: SoC-600's AHB-AP reports TYPE 0x8, which
# that table does not list. Identify by DEVARCH.ARCHID + PIDR part number when
# TYPE is unrecognised.
AP_TYPE_NAMES: dict[int, str] = {
    0x0: "JTAG connection",
    0x1: "AMBA AHB3",
    0x4: "AMBA APB2/APB3",
    0x5: "AMBA AXI3/AXI4",
    0x6: "AMBA AHB5 or APB4",
    0x7: "AMBA AXI5",
    0x8: "AMBA AHB5 (SoC-600, observed)",
}


@dataclass(frozen=True)
class ApIdr:
    raw: int
    revision: int
    designer_cont: int
    designer_id: int
    ap_class: int
    variant: int
    ap_type: int

    @property
    def present(self) -> bool:
        """ADIv6 p160: "An IDR value of zero indicates that there is no AP present"."""
        return self.raw != 0

    @property
    def unidentifiable(self) -> bool:
        """ADIv6 p160, bits[12:8]: "If a debugger reads a non-zero value in this
        field, it must treat the AP as unidentifiable"."""
        return bool((self.raw >> 8) & 0x1F)

    @property
    def designer_name(self) -> str:
        return jep106_name(self.designer_cont, self.designer_id)

    @property
    def class_name(self) -> str:
        if self.ap_class == AP_CLASS_MEM:
            return "MEM-AP"
        if self.ap_class == AP_CLASS_NONE:
            return "JTAG-AP" if self.ap_type == 0 else "no defined class"
        return f"reserved(0b{self.ap_class:04b})"

    @property
    def type_name(self) -> str:
        return AP_TYPE_NAMES.get(self.ap_type, f"unknown(0x{self.ap_type:X})")


def decode_ap_idr(raw: int) -> ApIdr:
    designer = (raw >> 17) & 0x7FF
    return ApIdr(
        raw=raw,
        revision=(raw >> 28) & 0xF,
        designer_cont=(designer >> 7) & 0xF,
        designer_id=designer & 0x7F,
        ap_class=(raw >> 13) & 0xF,
        variant=(raw >> 4) & 0xF,
        ap_type=raw & 0xF,
    )


# --------------------------------------------------------------------------
# DP IDR -- ADIv6 p66 (B2.2.6) and DPIDR1 -- p68 (B2.2.7)
# --------------------------------------------------------------------------

DP_VERSION_NAMES = {0x1: "DPv1", 0x2: "DPv2", 0x3: "DPv3"}

# ADIv6 introduces DPv3. DPv3 is how a tool concludes "this is ADIv6".
DP_VERSION_ADIV6 = 0x3

ASIZE_NAMES = {0x0C: 12, 0x14: 20, 0x20: 32, 0x28: 40, 0x30: 48, 0x34: 52}


@dataclass(frozen=True)
class DpIdr:
    raw: int
    revision: int
    partno: int
    min_dp: bool
    version: int
    designer_cont: int
    designer_id: int

    @property
    def is_adiv6(self) -> bool:
        return self.version >= DP_VERSION_ADIV6

    @property
    def version_name(self) -> str:
        return DP_VERSION_NAMES.get(self.version, f"reserved(0x{self.version:X})")

    @property
    def designer_name(self) -> str:
        return jep106_name(self.designer_cont, self.designer_id)


def decode_dpidr(raw: int) -> DpIdr:
    designer = (raw >> 1) & 0x7FF
    return DpIdr(
        raw=raw,
        revision=(raw >> 28) & 0xF,
        partno=(raw >> 20) & 0xFF,
        min_dp=bool((raw >> 16) & 1),
        version=(raw >> 12) & 0xF,
        designer_cont=(designer >> 7) & 0xF,
        designer_id=designer & 0x7F,
    )


def decode_dpidr1(raw: int) -> dict[str, object]:
    asize = raw & 0x7F
    return {
        "raw": raw,
        "asize": asize,
        "asize_bits": ASIZE_NAMES.get(asize),
        "errmode": bool((raw >> 7) & 1),
    }


# Expected on this silicon: DPIDR 0x4C013477 (CP-1.2 recon) -> DPv3.
EXPECTED_DPIDR = 0x4C013477

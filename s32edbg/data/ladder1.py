"""MDM-AP and SDA-AP register offsets, and the "LADDER-1" debug-enable sequence.

Why this exists
---------------
RM section 135.11 (L418643-418653):

    "By default, the clocks to most debug components and memories are disabled.
     The debug clocks are enabled by either:
       - Asserting the Arm DP cdbgpwrupreq debug power up request signal
         (expected external debugger clock gate enable method).
       - Setting any enable bit from the MDM-AP DBGGENCNTRL, DBGGENCNTRL2, and
         DBGGENCNTRL3 registers (expected self-host clock gate enable method).
     NOTE: Trying to access debug components while the clocks are not enabled
     might hang the debug APBIC bus."

On this silicon the power-up request alone is not sufficient to make the debug
APB ROM at 0x01BF_E000 populate. Writing the four words below -- the sequence
recorded as "LADDER-1" in the prior hardware recon
(docs/jlink-migration/evidence/CP-1.4/cmds-ladder1-mdm-enable-apbrom.jlink) --
makes the ROM readable and the cores discoverable. The register offsets match
RM sections 135.10.1.22-.24 exactly, which is what promoted this from an
opaque vendor incantation to a documented mechanism.

The bits are sticky across functional resets and are lost on power loss.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# MDM-AP register offsets -- RM section 135.10.1.1 (L414908-415023)
# --------------------------------------------------------------------------

MDM_STATUS = 0x000
MDM_CONTROL = 0x004
MDM_STATUS2 = 0x040
MDM_CONTROL2 = 0x044
MDM_CONTROL3 = 0x04C
MDM_DBGENCNTRL = 0x0C0
MDM_DBGENCNTRL2 = 0x0C8
MDM_DBGENCNTRL3 = 0x0D0
MDM_IDR = 0xDFC
MDM_DEVARCH = 0xFBC
MDM_DEVTYPE = 0xFCC

#: RM L417910 / L417964: reset values, used as an identity assertion.
MDM_IDR_EXPECTED = 0x001D_E040
MDM_DEVARCH_EXPECTED = 0x01D0_00F0

# MDM_AP.STATUS bits -- RM L415055-415259
MDM_STATUS_SYSTEM_RESET = 1 << 2  # "1b - System is in reset"
MDM_STATUS_CSYSPWRUPREQ = 1 << 6
MDM_STATUS_CDBGPWRUPREQ = 1 << 7
MDM_STATUS_JTAG_ACTIVE = 1 << 8  # TRST of AUX TAPs deasserted

# MDM_AP.CONTROL bits -- RM L415300-415485
MDM_CONTROL_MSK_DBG_FAULT = 1 << 23  # mask debug faults to the FCCU

# --------------------------------------------------------------------------
# SDA-AP register offsets -- RM section 135.13.2.1 (L418722-418814)
# Read-only use here: the challenge/response is one-shot ("HSE allows one only
# attempt for C/R comparison. If it fails, no further attempts are accepted.")
# --------------------------------------------------------------------------

SDA_AUTHSTTS = 0x000
SDA_AUTHCTL = 0x004
SDA_UIDL = 0x070
SDA_UIDH = 0x074
SDA_SYSRSTS = 0x080
SDA_SYSRSTC = 0x088
SDA_RSTCTRL = 0x090
SDA_RSTMASK = 0x094
SDA_IDR = 0xDFC
SDA_DEVARCH = 0xFBC

SDA_IDR_EXPECTED = 0x001D_E001
SDA_DEVARCH_EXPECTED = 0x01D0_000E

# AUTHSTTS bits -- RM L418850-418891
SDA_AUTHSTTS_CHALRDY = 1 << 0
SDA_AUTHSTTS_UIDRDY = 1 << 2
SDA_AUTHSTTS_APPDBGEN = 1 << 30

# SDASYSRSTS bits -- RM L419611-419680
SDA_SYSRSTS_DESTRUCT_RESET = 1 << 1
SDA_SYSRSTS_SYSTEM_RESET = 1 << 2  # functional reset asserted

# SDASYSRSTC bits -- RM L419682-419750 (write path; guarded, not used by default)
SDA_SYSRSTC_SYSRESTREQ = 1 << 4  # destructive reset; debugger loses connection
SDA_SYSRSTC_SYSFUNCREQ = 1 << 5  # hold system in functional reset

#: RM L419752: SDARSTCTRL reset value. R52/CEVA release bits default 1 (write 0
#: to hold in reset); M33 CPUWAIT bits default 0 (write 1 to hold).
SDA_RSTCTRL_RESET_VALUE = 0xF0F3_0000


# --------------------------------------------------------------------------
# The LADDER-1 write list
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GateWrite:
    offset: int
    value: int
    name: str
    why: str


LADDER1: tuple[GateWrite, ...] = (
    GateWrite(
        MDM_CONTROL,
        0x0080_0000,
        "CONTROL[MSK_DBG_FAULT]",
        "RM 135.12: the VFCCU raises a debug fault when debug-enable or halt "
        "signals assert and no debugger is attached. Masking first prevents a "
        "fault storm as the enables below go high.",
    ),
    GateWrite(
        MDM_DBGENCNTRL,
        0x3CF3_CF00,
        "DBGENCNTRL (RTU0 cores)",
        "RM 135.10.1.22: per-core NIDEN/DBGEN/HNIDEN/HIDEN for RTU0 cores 0-3.",
    ),
    GateWrite(
        MDM_DBGENCNTRL2,
        0x3CF3_CF00,
        "DBGENCNTRL2 (RTU1 cores)",
        "RM 135.10.1.23: same field layout as DBGENCNTRL, for RTU1 cores 0-3.",
    ),
    GateWrite(
        MDM_DBGENCNTRL3,
        0xFFF0_3CF0,
        "DBGENCNTRL3 (SMU/CE M33, CEVA, GTM, global)",
        "RM 135.10.1.24: System Manager and both CE Cortex-M33 cores, CEVA_SPF2, "
        "GTM_DBG, plus the global GSPNIDEN/GSPIDEN/GNIDEN/GDBGEN bits.",
    ),
)

#: Registers to read back when reporting gate state.
GATE_READBACK: tuple[tuple[int, str], ...] = (
    (MDM_STATUS, "STATUS"),
    (MDM_CONTROL, "CONTROL"),
    (MDM_STATUS2, "STATUS2"),
    (MDM_CONTROL2, "CONTROL2"),
    (MDM_CONTROL3, "CONTROL3"),
    (MDM_DBGENCNTRL, "DBGENCNTRL"),
    (MDM_DBGENCNTRL2, "DBGENCNTRL2"),
    (MDM_DBGENCNTRL3, "DBGENCNTRL3"),
)

"""RM Table 957 (APBIC1 memory map) and Table 965 ROM-table anchors as data.

RM L412352-412389 (Table 957) and L412684-413075 (Table 965).

Two address views exist for every AP. RM NOTE L412349 says the debug system sits
at 0x4C00_0000 in system memory, yet Table 957's cores column shows 0x4DC1_1000
for an APBIC base of 0x01C1_0000 -- a 0x1000 discrepancy the RM never explains.

SoC-600 TRM section 2.2 (p41-42) resolves it: every MEM-AP is a *twin AP*
occupying 8 KB, presenting two logical views selected by paddr_s[12]:
    AP-L0 at +0x0000  -> the external debugger view
    AP-L1 at +0x1000  -> the self-hosted (core) view
Read-only registers are shared between the views; writable ones are duplicated.
TRM: "An external debugger can only discover one of the twin APs through the
ROM table. The other AP is dedicated for self-hosted debug."

So the tool addresses APs at their AP-L0 base and expects the ROM table to
enumerate exactly those.
"""

from __future__ import annotations

from dataclasses import dataclass

#: RM NOTE L412349: debug system base in system (core) address space.
DEBUG_SYSTEM_CORE_BASE = 0x4C00_0000

#: SoC-600 twin-AP stride: AP-L1 sits this far above AP-L0.
TWIN_AP_L1_OFFSET = 0x1000

#: Each MEM-AP occupies two 4 KB logical APs.
TWIN_AP_SIZE = 0x2000


@dataclass(frozen=True)
class ExpectedAp:
    base: int  #: APBIC base address, debugger view (AP-L0)
    core_base: int | None  #: system-memory address, cores view (Table 957)
    name: str
    idr: int | None  #: Table 957 "Identification register (IDR) value"
    pdac2_slot: int | None  #: XRDC_0 PDAC2 slot guarding this AP


#: The top-level ROM table that describes the AP map (not itself an AP).
APBIC1_ROM_BASE = 0x01C0_0000

#: ROM table reached through the APB-AP's BASE register; holds the CoreSight
#: component tree on APBIC2. Observed on silicon (CP-1.4) and gated by LADDER-1.
APBIC2_ROM_BASE = 0x01BF_E000

EXPECTED_APS: tuple[ExpectedAp, ...] = (
    ExpectedAp(0x01C1_0000, 0x4DC1_1000, "MDM-AP (miscellaneous debug module)", 0x001D_E040, 299),
    ExpectedAp(0x01C2_0000, 0x4DC2_1000, "AXI-AP (system interconnect)", 0x3477_0017, 300),
    ExpectedAp(0x01C3_0000, 0x4DC3_1000, "APB-AP (debug APB -> APBIC2)", 0x2477_0006, 301),
    ExpectedAp(0x01C7_0000, 0x4DC7_1000, "SDA-AP (secure debug authenticate)", 0x001D_E001, 303),
    ExpectedAp(0x01C8_0000, 0x4DC8_1000, "AHB-AP (SMU Cortex-M33)", 0x3477_0008, 304),
    ExpectedAp(0x01C9_0000, 0x4DC9_1000, "AHB-AP (CE Cortex-M33 core 0)", 0x3477_0008, 305),
    ExpectedAp(0x01C9_2000, 0x4DC9_3000, "AHB-AP (CE Cortex-M33 core 1)", 0x3477_0008, 306),
    ExpectedAp(0x01CA_0000, 0x4DCA_1000, "AHB-AP (reserved)", 0x3477_0008, 308),
    ExpectedAp(0x01CA_2000, 0x4DCA_3000, "AHB-AP (reserved)", 0x3477_0008, 309),
)

#: Table 957 lists no entry for these, and no PDAC2 slots 302/307. Flag if seen.
UNDOCUMENTED_AP_WINDOW = (0x01C4_0000, 0x01C6_FFFF)

#: RM L412334: "External tool direct access is limited to only the 4 MB of
#: memory space of APBIC1."
APBIC1_WINDOW = (0x01C0_0000, 0x01FF_FFFF)

EXPECTED_AP_BY_BASE: dict[int, ExpectedAp] = {ap.base: ap for ap in EXPECTED_APS}

#: Well-known AP roles, for commands that need a specific AP.
AP_MDM = 0x01C1_0000
AP_AXI = 0x01C2_0000
AP_APB = 0x01C3_0000
AP_SDA = 0x01C7_0000

#: Short names the CLI accepts for --ap, so every bus in Table 957 is reachable
#: by a name rather than an address the operator has to look up.
AP_ALIASES: dict[str, int] = {
    "mdm": AP_MDM,
    "axi": AP_AXI,
    "apb": AP_APB,
    "sda": AP_SDA,
    "ahb-smu": 0x01C8_0000,
    "ahb-ce0": 0x01C9_0000,
    "ahb-ce1": 0x01C9_2000,
    "ahb-r0": 0x01CA_0000,
    "ahb-r1": 0x01CA_2000,
}

#: APs that master the system interconnect. RM Table 957's note applies to
#: these and not to the debug-APB path: an access while functional reset is
#: asserted can leave peripherals unreachable until a destructive reset.
#: A frozenset rather than a bare comparison so a sibling AP can be added
#: without hunting down every check.
SYSTEM_BUS_APS = frozenset({AP_AXI})


# --------------------------------------------------------------------------
# Table 965 anchors: ROM tables expected on APBIC2, addressed via the APB-AP.
# Used to check romdump coverage. Addresses are APBIC-relative (debugger view).
# --------------------------------------------------------------------------

APBIC2_EXPECTED_ROMS: dict[int, str] = {
    0x0000_0000: "RTU0 subsystem ROM table",
    0x0040_0000: "R52 RTU0 ROM table",
    0x0080_0000: "RTU1 subsystem ROM table",
    0x00C0_0000: "R52 RTU1 ROM table",
    0x0100_0000: "peripheral group 3 ROM table",
    0x0110_0000: "peripheral group 2 ROM table",
    0x0120_0000: "peripheral group 1 ROM table",
    0x0128_0000: "peripheral group 3_1 ROM table",
    0x0130_0000: "peripheral group 4 ROM table",
    0x0138_0000: "peripheral group 5 ROM table",
    0x0140_0000: "peripheral group 0 ROM table",
    0x01BF_E000: "APBIC2 ROM table",
}

#: Per-core external-debug frame bases proven on silicon (CP-1.4, EDDEVAFF0 and
#: DEVTYPE reads). Kept for cross-checking romdump output, not for driving it.
R52_DEBUG_FRAMES: dict[str, dict[str, int]] = {
    "RTU0_core0": {"debug": 0x0041_0000, "cti": 0x0042_0000, "pmu": 0x0043_0000, "etm": 0x0044_0000},
    "RTU0_core1": {"debug": 0x0051_0000, "cti": 0x0052_0000, "pmu": 0x0053_0000, "etm": 0x0054_0000},
    "RTU0_core2": {"debug": 0x0061_0000, "cti": 0x0062_0000, "pmu": 0x0063_0000, "etm": 0x0064_0000},
    "RTU0_core3": {"debug": 0x0071_0000, "cti": 0x0072_0000, "pmu": 0x0073_0000, "etm": 0x0074_0000},
}

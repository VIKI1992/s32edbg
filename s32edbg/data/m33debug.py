"""Cortex-M33 debug register map. A different model from the R52's.

Where the R52 is reached through a CoreSight external-debug *frame* on the APB
(EDSCR, EDITR, CTI), the M33 is reached through its own **AHB-AP** as ordinary
memory: the debug registers live in the processor's own address map, in the
Private Peripheral Bus at 0xE000_0000. So there is no instruction-transfer
dance and no cross-trigger interface to program -- halting is a single write.

Three M33 subsystems on this part, each behind its own AP (RM Table 957):

    0x01C8_0000  SMU Cortex-M33
    0x01C9_0000  FlexLLCE CE Cortex-M33 core 0
    0x01C9_2000  FlexLLCE CE Cortex-M33 core 1

**Sourcing.** Addresses and reset values below are from the **Cortex-M33 TRM**
(100230_0100_08) sections 10-13, read directly: DHCSR/DCRSR/DCRDR/DEMCR from the
debug register summary, the BPU from Table 13-1, the DWT from Table 11-1. The
field layouts are Armv8-M architectural. As with the R52 map, nothing here is
trusted until an identity anchor passes on silicon -- see ``m33.identify``.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# System Control Space -- identity
# --------------------------------------------------------------------------

CPUID = 0xE000_ED00

CPUID_IMPLEMENTER_ARM = 0x41
CPUID_PARTNO_CORTEX_M33 = 0xD21


def decode_cpuid(raw: int) -> dict[str, object]:
    """Decode CPUID. Implementer 0x41 with part 0xD21 is a Cortex-M33.

    The identity anchor for this whole map: CPUID sits at 0xE000ED00, far from
    the debug block at 0xE000EDF0, so a correct decode means the AP is
    addressing the processor's PPB correctly rather than something else.
    """
    implementer = (raw >> 24) & 0xFF
    partno = (raw >> 4) & 0xFFF
    return {
        "raw": f"0x{raw:08X}",
        "implementer": f"0x{implementer:02X}",
        "implementer_name": "Arm" if implementer == CPUID_IMPLEMENTER_ARM else "unknown",
        "variant": (raw >> 20) & 0xF,
        "partno": f"0x{partno:03X}",
        "part_name": "Cortex-M33" if partno == CPUID_PARTNO_CORTEX_M33 else "unknown",
        "revision": raw & 0xF,
        "is_cortex_m33": implementer == CPUID_IMPLEMENTER_ARM
        and partno == CPUID_PARTNO_CORTEX_M33,
    }


# --------------------------------------------------------------------------
# Debug Control Block -- TRM section 10, "Debug register summary"
# --------------------------------------------------------------------------

DHCSR = 0xE000_EDF0  #: Debug Halting Control and Status Register
DCRSR = 0xE000_EDF4  #: Debug Core Register Selector (write-only)
DCRDR = 0xE000_EDF8  #: Debug Core Register Data
DEMCR = 0xE000_EDFC  #: Debug Exception and Monitor Control
DAUTHCTRL = 0xE000_EE04
DAUTHSTATUS = 0xE000_EFB8

#: Non-secure aliases, TRM Table: DHCSR_NS at 0xE002EDF0. Recorded because a
#: read of the secure view from a non-secure debugger returns RAZ/WI, which
#: looks exactly like a dead bus.
DHCSR_NS = 0xE002_EDF0
DCRDR_NS = 0xE002_EDF8

#: **Every DHCSR write must carry this key in [31:16] or it is ignored.**
#: A write without it is silently discarded -- no fault, no error -- so a halt
#: that forgets it looks like a core that refuses to stop.
DHCSR_DBGKEY = 0xA05F_0000

# Control bits, write side
DHCSR_C_DEBUGEN = 1 << 0  #: halting debug enable. Without it, nothing else works
DHCSR_C_HALT = 1 << 1
DHCSR_C_STEP = 1 << 2
DHCSR_C_MASKINTS = 1 << 3  #: mask interrupts while stepping
DHCSR_C_SNAPSTALL = 1 << 5

# Status bits, read side
DHCSR_S_REGRDY = 1 << 16  #: the register transfer completed
DHCSR_S_HALT = 1 << 17
DHCSR_S_SLEEP = 1 << 18
DHCSR_S_LOCKUP = 1 << 19
DHCSR_S_SDE = 1 << 20  #: secure debug enabled
DHCSR_S_RETIRE_ST = 1 << 24
DHCSR_S_RESET_ST = 1 << 25


def decode_dhcsr(raw: int) -> dict[str, object]:
    return {
        "raw": f"0x{raw:08X}",
        "halted": bool(raw & DHCSR_S_HALT),
        "debug_enabled": bool(raw & DHCSR_C_DEBUGEN),
        "sleeping": bool(raw & DHCSR_S_SLEEP),
        "locked_up": bool(raw & DHCSR_S_LOCKUP),
        "secure_debug_enabled": bool(raw & DHCSR_S_SDE),
        "reset_since_last_read": bool(raw & DHCSR_S_RESET_ST),
        "register_ready": bool(raw & DHCSR_S_REGRDY),
    }


def dhcsr_write(bits: int) -> int:
    """A DHCSR value with the debug key applied.

    Always use this rather than writing DHCSR directly: the key is not optional
    and its absence produces no error at all.
    """
    return DHCSR_DBGKEY | (bits & 0xFFFF)


DEMCR_VC_CORERESET = 1 << 0  #: halt on reset -- catch startup code
DEMCR_TRCENA = 1 << 24  #: enables the DWT, including its PC sampler


# --------------------------------------------------------------------------
# Core registers, via DCRSR/DCRDR
# --------------------------------------------------------------------------

DCRSR_REGWNR = 1 << 16  #: set to write, clear to read

#: Armv8-M register selectors. 15 is the debug return address, i.e. the PC --
#: unlike the R52 this needs no special instruction, just a selector.
REGISTER_SELECTORS: dict[str, int] = {
    **{f"r{n}": n for n in range(13)},
    "sp": 13,
    "lr": 14,
    "pc": 15,
    "xpsr": 16,
    "msp": 17,
    "psp": 18,
    "control": 20,
}

#: The order `m33 regs` reports them in.
REGISTER_ORDER: tuple[str, ...] = (
    *(f"r{n}" for n in range(13)),
    "sp",
    "lr",
    "pc",
    "xpsr",
    "msp",
    "psp",
)


# --------------------------------------------------------------------------
# Breakpoint Unit -- TRM Table 13-1
# --------------------------------------------------------------------------
#
# TRM 13.1: "The BPU ... can implement four or eight hardware breakpoints and up
# to eight instruction comparators. The BPU does not support Flash patching."
# FP_CTRL resets to 0x10000040 with four comparators and 0x10000080 with eight,
# so the count is readable and is never assumed.

FP_CTRL = 0xE000_2000
FP_COMP_BASE = 0xE000_2008
FP_COMP_STRIDE = 4

FP_CTRL_ENABLE = 1 << 0
FP_CTRL_KEY = 1 << 1  #: must be written 1 for any FP_CTRL write to take effect

#: FP_COMP bit 0 enables the comparator; the address occupies [31:1].
FP_COMP_BE = 1 << 0


def fp_comp(n: int) -> int:
    return FP_COMP_BASE + n * FP_COMP_STRIDE


def fp_num_code(fp_ctrl: int) -> int:
    """Number of instruction comparators, from FP_CTRL.

    NUM_CODE is split: bits [7:4] are the low part and [14:12] the high part.
    Reset values 0x…40 and 0x…80 give 4 and 8, which is the check that the
    field positions are right.
    """
    return (((fp_ctrl >> 12) & 0b111) << 4) | ((fp_ctrl >> 4) & 0xF)


def encode_breakpoint(address: int) -> int:
    """FP_COMP value for a breakpoint at ``address``.

    The Thumb state bit is not part of the address -- M33 is Thumb-only, so a
    function pointer always has bit 0 set and writing it into the comparator
    would set BE on a misaligned address.
    """
    return (address & 0xFFFF_FFFE) | FP_COMP_BE


def decode_breakpoint(raw: int) -> dict[str, object]:
    return {
        "enabled": bool(raw & FP_COMP_BE),
        "address": f"0x{raw & 0xFFFF_FFFE:08X}",
        "raw": f"0x{raw:08X}",
    }


# --------------------------------------------------------------------------
# Data Watchpoint and Trace -- TRM Table 11-1
# --------------------------------------------------------------------------

DWT_CTRL = 0xE000_1000
DWT_PCSR = 0xE000_101C  #: Program Counter Sample Register
DWT_COMP_BASE = 0xE000_1020
DWT_FUNCTION_BASE = 0xE000_1028
DWT_COMP_STRIDE = 0x10

#: DWT_PCSR reads all-ones when the sample is not valid -- while the core is
#: halted, asleep, or in reset. It is a profiling aid for a *running* core,
#: which is what makes it the safe way to find a hot address.
#:
#: **Bit 0 is RES0**, because a PC is at least halfword aligned. So the invalid
#: marker actually reads back as 0xFFFFFFFE, not 0xFFFFFFFF. Measured on this
#: silicon: both FlexLLCE CE cores sampled 0xFFFFFFFE 150 times out of 150, and
#: without this the tool would have reported that as a hot address -- a
#: made-up answer presented as a measurement.
DWT_PCSR_INVALID = 0xFFFF_FFFF


def pcsr_is_valid(raw: int) -> bool:
    """Whether a DWT_PCSR sample is a real PC.

    Accepts either spelling of the all-ones marker, since bit 0 reads back as
    zero.
    """
    return (raw | 1) != 0xFFFF_FFFF


def dwt_comp(n: int) -> int:
    return DWT_COMP_BASE + n * DWT_COMP_STRIDE


def dwt_function(n: int) -> int:
    return DWT_FUNCTION_BASE + n * DWT_COMP_STRIDE


def dwt_numcomp(dwt_ctrl: int) -> int:
    """How many comparators the DWT implements, from DWT_CTRL[31:28]."""
    return (dwt_ctrl >> 28) & 0xF


#: DWT_FUNCTION.MATCH for a data-address watchpoint, Armv8-M.
DWT_MATCH_DISABLED = 0b0000
DWT_MATCH_ADDR_READ = 0b0110
DWT_MATCH_ADDR_WRITE = 0b0101
DWT_MATCH_ADDR_RW = 0b0100

DWT_MATCH_FOR: dict[str, int] = {
    "load": DWT_MATCH_ADDR_READ,
    "store": DWT_MATCH_ADDR_WRITE,
    "both": DWT_MATCH_ADDR_RW,
}

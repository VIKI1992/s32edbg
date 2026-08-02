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
DHCSR_S_RETIRE_ST = 1 << 24  #: an instruction retired since the last read (clear-on-read)
DHCSR_S_RESET_ST = 1 << 25  #: a reset occurred since the last read (clear-on-read)
DHCSR_S_RESTART_ST = 1 << 26  #: the core left Debug state since the last read (clear-on-read)


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
# DFSR -- why the core halted. Armv8-M D1.2 / guide §6.2
# --------------------------------------------------------------------------
#
# Write-1-to-clear: the bits accumulate across halts, so the cause of *this*
# halt is only readable if the previous one was cleared. The tool clears what
# it reports.

DFSR = 0xE000_ED30

DFSR_HALTED = 1 << 0  #: halt request or step
DFSR_BKPT = 1 << 1  #: FPB comparator or BKPT instruction
DFSR_DWTTRAP = 1 << 2  #: DWT watchpoint
DFSR_VCATCH = 1 << 3  #: vector catch (incl. VC_CORERESET)
DFSR_EXTERNAL = 1 << 4  #: external debug request (MDM-AP EDBGREQ / CTI)

_DFSR_NAMES: tuple[tuple[int, str], ...] = (
    (DFSR_EXTERNAL, "external debug request"),
    (DFSR_VCATCH, "vector catch"),
    (DFSR_DWTTRAP, "watchpoint (DWT)"),
    (DFSR_BKPT, "breakpoint"),
    (DFSR_HALTED, "halt request or step"),
)


def decode_dfsr(raw: int) -> dict[str, object]:
    causes = [name for bit, name in _DFSR_NAMES if raw & bit]
    return {
        "raw": f"0x{raw:08X}",
        "causes": causes,
        "cause": causes[0] if len(causes) == 1 else None,
    }


# --------------------------------------------------------------------------
# AIRCR -- the warm-reset request, for reset-and-halt. Guide §6.8
# --------------------------------------------------------------------------

AIRCR = 0xE000_ED0C

#: VECTKEY (0x05FA in [31:16]) | SYSRESETREQ (bit 2). Guide §6.8: "warm reset
#: request -- not guaranteed to take effect immediately". Like DHCSR, a write
#: without the key is silently ignored.
AIRCR_SYSRESETREQ = 0x05FA_0004


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

# DWT_FUNCTION field layout for a watchpoint, Armv8-M / guide §6.7:
#   MATCH [3:0], ACTION [5:4] (0b01 = generate a debug event, i.e. halt),
#   DATAVSIZE [11:10] (00 byte, 01 halfword, 10 word), MATCHED bit 24
#   (set on match, clears on read).
DWT_FUNCTION_MATCH_MASK = 0xF
DWT_FUNCTION_ACTION_SHIFT = 4
DWT_FUNCTION_ACTION_DEBUG_EVENT = 0b01
DWT_FUNCTION_DATAVSIZE_SHIFT = 10
DWT_FUNCTION_MATCHED = 1 << 24
#: ID [31:27] identifies what this comparator supports. Read-only -- measured
#: on the SMU: FUNCTION written 0x815 reads back 0x58000815, ID = 0b01011. Any
#: read-back comparison must mask it out along with MATCHED.
DWT_FUNCTION_ID_MASK = 0x1F << 27
#: The RO bits to ignore when verifying a FUNCTION write landed.
DWT_FUNCTION_RO_MASK = DWT_FUNCTION_ID_MASK | DWT_FUNCTION_MATCHED

_DATAVSIZE_FOR_SIZE = {1: 0b00, 2: 0b01, 4: 0b10}
_MATCH_NAMES = {v: k for k, v in DWT_MATCH_FOR.items()}


def encode_watchpoint(address: int, *, size: int = 4, on: str = "both") -> tuple[int, int]:
    """Build (DWT_COMP, DWT_FUNCTION) for a data-address watchpoint.

    Guide §6.7: the comparator address must be **naturally aligned** to
    DATAVSIZE, else the behaviour is UNPREDICTABLE -- so misalignment is
    rejected rather than rounded. ACTION = 0b01 makes a match a debug event
    (halt) rather than a trace packet or a bare CMPMATCH trigger.
    """
    datavsize = _DATAVSIZE_FOR_SIZE.get(size)
    if datavsize is None:
        raise ValueError(f"M33 watchpoint size must be 1, 2 or 4 bytes, not {size}")
    if address % size:
        raise ValueError(
            f"a {size}-byte watchpoint must be {size}-byte aligned; 0x{address:08X} "
            "is not (DWT_COMP must be naturally aligned to DATAVSIZE)"
        )
    match = DWT_MATCH_FOR.get(on)
    if match is None:
        raise ValueError(f"watchpoint access must be load, store or both, not {on!r}")
    function = (
        match
        | (DWT_FUNCTION_ACTION_DEBUG_EVENT << DWT_FUNCTION_ACTION_SHIFT)
        | (datavsize << DWT_FUNCTION_DATAVSIZE_SHIFT)
    )
    return address, function


def decode_watchpoint(comp: int, function: int) -> dict[str, object]:
    match = function & DWT_FUNCTION_MATCH_MASK
    datavsize = (function >> DWT_FUNCTION_DATAVSIZE_SHIFT) & 0b11
    return {
        "enabled": match != DWT_MATCH_DISABLED,
        "address": f"0x{comp:08X}",
        "comp": f"0x{comp:08X}",
        "function": f"0x{function:08X}",
        "bytes": {0b00: 1, 0b01: 2, 0b10: 4}.get(datavsize),
        "on": _MATCH_NAMES.get(match, f"0b{match:04b}"),
        "matched": bool(function & DWT_FUNCTION_MATCHED),
    }

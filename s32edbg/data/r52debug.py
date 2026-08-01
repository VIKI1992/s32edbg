"""Cortex-R52 external debug and CTI register maps.

Scope and provenance
--------------------
These are the Armv8 **external debug** register offsets -- the memory-mapped
view a debugger reaches through the APB-AP, not the system-register view a core
sees. On this target each core's debug frame is a 4 KB CoreSight component
(RTU0 core 0 at APBIC2 ``0x0041_0000``), so every offset below is added to that
base and read with an ordinary ``mem_read``.

**Sourcing.** Two tiers, and the difference matters.

*Cited.* ``DLR``/``DSPSR`` come from the **Cortex-R52 TRM section 4.2.31**, which
settles that the AArch32 debug registers are coprocessor registers reached with
``MRC p15`` rather than special registers reached with ``MRS``. That one page
answered in a sentence what a 64-point sweep of the MRS encoding space could not,
because the PC was never in that space.

*Measured, not cited.* The external-debug **offsets** and bit positions are the
architectural Armv8 map. They are marked ``VERIFY`` and treated the way this
project treats every unverified implementation fact: not trusted until a
known-answer oracle passes on silicon. ``runcontrol.identify()`` performs those
checks, and the register-transfer path refuses to report a value until its
round-trip test passes -- the same discipline ``calibrate_tdo_order()`` applies
to raw JTAG. All of them have since passed; see docs/test-plan.md.

Anchors that are *not* in doubt, because the ROM walk already proved them on
this silicon: the frame is a CoreSight component whose ``DEVTYPE`` reads 0x15
("processor external debug") and whose ``DEVARCH`` archid is 0x6A05 ("processor
debug (Armv8-R)"), at the four addresses recorded in ``expected_apmap``.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# External debug register offsets within a core's 4 KB debug frame.
# VERIFY: architectural Armv8 external debug map, unconfirmed against a
# document on this machine. Oracles in runcontrol.identify().
# --------------------------------------------------------------------------

EDESR = 0x020  #: debug status, halting-event pending flags
EDECR = 0x024  #: debug control: halting step, reset catch, OS unlock catch
EDWAR_LO = 0x030
EDWAR_HI = 0x034

DBGDTRRX = 0x080  #: host -> target data transfer
EDITR = 0x084  #: instruction transfer: the core executes what is written here
EDSCR = 0x088  #: the main status/control register
DBGDTRTX = 0x08C  #: target -> host data transfer
EDRCR = 0x090  #: reason clear: sticky error, pipeline advance
EDACR = 0x094
EDECCR = 0x098  #: exception catch control

EDPCSR_LO = 0x0A0  #: sampled PC. Implementation-defined while halted -- see below
EDCIDSR = 0x0A4
EDVIDSR = 0x0A8
EDPCSR_HI = 0x0AC

OSLAR = 0x300  #: OS lock access: write 0 to unlock
EDPRCR = 0x310  #: power/reset control
EDPRSR = 0x314  #: power/reset status, and the halted flag

MIDR = 0xD00  #: main ID -- implementer and part number, our identity anchor
EDPFR_LO = 0xD20
EDPFR_HI = 0xD24
EDDFR_LO = 0xD28  #: debug feature: how many breakpoint/watchpoint comparators
EDDFR_HI = 0xD2C

#: Breakpoint and watchpoint comparator banks. Each comparator is a 16-byte
#: slot holding a 64-bit value register and a 32-bit control register.
DBGBVR_BASE = 0x400
DBGBCR_BASE = 0x408
DBGWVR_BASE = 0x800
DBGWCR_BASE = 0x808
COMPARATOR_STRIDE = 0x10

#: Architectural ceiling. The real count comes from EDDFR and is never assumed;
#: this only bounds a scan so a wrong EDDFR decode cannot run away.
MAX_COMPARATORS = 16


def dbgbvr(n: int) -> int:
    """Offset of breakpoint value register n (64-bit; low word at this offset)."""
    return DBGBVR_BASE + n * COMPARATOR_STRIDE


def dbgbcr(n: int) -> int:
    return DBGBCR_BASE + n * COMPARATOR_STRIDE


def dbgwvr(n: int) -> int:
    return DBGWVR_BASE + n * COMPARATOR_STRIDE


def dbgwcr(n: int) -> int:
    return DBGWCR_BASE + n * COMPARATOR_STRIDE


# --------------------------------------------------------------------------
# EDSCR -- status and control.  VERIFY (bit positions)
# --------------------------------------------------------------------------

EDSCR_STATUS_MASK = 0b11_1111  # [5:0]
EDSCR_ERR = 1 << 6
EDSCR_HDE = 1 << 14  #: halting debug enable: route debug events to Debug state
EDSCR_SDD = 1 << 16  #: secure debug disabled
EDSCR_NS = 1 << 18
EDSCR_MA = 1 << 20
EDSCR_TDA = 1 << 21

#: EDSCR.INTdis, bits [23:22] -- disables taking interrupts in Non-debug state.
#:
#: **A single step with interrupts enabled does not step one instruction of your
#: program.** Measured: stepping from a T32 idle loop at 0x321D2EE6 landed at
#: 0x321DD018 with status "halting step, no syndrome" -- the core executed one
#: instruction, immediately took a pending interrupt, and stopped in the vector
#: table. On a system with a periodic tick that happens on essentially every
#: step, so stepping appears to teleport into exception handlers and never
#: advance through the code being debugged.
#:
#: The M33 path had this from the start (DHCSR.C_MASKINTS); the R52 path did
#: not, which is the same oversight as fixing a bug on one path and not the
#: other.
EDSCR_INTDIS_SHIFT = 22
EDSCR_INTDIS_MASK = 0b11 << EDSCR_INTDIS_SHIFT
EDSCR_INTDIS_OFF = 0b00
EDSCR_INTDIS_ON = 0b11


def edscr_with_intdis(edscr: int, disable: bool) -> int:
    """EDSCR with the interrupt-disable field set or cleared, other bits kept.

    Read-modify-write: EDSCR carries live status in the same word, so writing
    the field alone would clobber it.
    """
    value = edscr & ~EDSCR_INTDIS_MASK
    if disable:
        value |= EDSCR_INTDIS_ON << EDSCR_INTDIS_SHIFT
    return value
EDSCR_ITE = 1 << 24  #: instruction transfer empty -- ready for the next EDITR
EDSCR_PIPEADV = 1 << 25
EDSCR_TXU = 1 << 26  #: TX underrun
EDSCR_RXO = 1 << 27  #: RX overrun
EDSCR_ITO = 1 << 28  #: instruction transfer overrun
EDSCR_TXFULL = 1 << 29
EDSCR_RXFULL = 1 << 30
EDSCR_A = 1 << 31

#: EDSCR.STATUS -- why the core is where it is. Anything not listed here is
#: reported as unknown rather than guessed at, because a wrong halt reason is
#: worse than an honest "I do not recognise this".
EDSCR_STATUS_NAMES: dict[int, str] = {
    0b000001: "restarting (leaving debug state)",
    0b000010: "running (non-debug state)",
    0b000111: "halted: breakpoint",
    0b010011: "halted: external debug request",
    0b011011: "halted: halting step, normal",
    0b011111: "halted: halting step, exclusive",
    0b100011: "halted: OS unlock catch",
    0b100111: "halted: reset catch",
    0b101011: "halted: watchpoint",
    0b101111: "halted: HLT instruction",
    0b110011: "halted: software access to debug register",
    0b110111: "halted: exception catch",
    0b111011: "halted: halting step, no syndrome",
}

#: The two STATUS values that mean the core is *not* in Debug state.
EDSCR_STATUS_RESTARTING = 0b000001
EDSCR_STATUS_RUNNING = 0b000010

#: Statuses that mean a halting step completed. Three of them, because the
#: architecture distinguishes a normal step from one that ended in an exclusive
#: sequence or produced no syndrome.
STEP_COMPLETE_STATUSES = frozenset({0b011011, 0b011111, 0b111011})


def edscr_status(raw: int) -> int:
    return raw & EDSCR_STATUS_MASK


def edscr_status_name(raw: int) -> str:
    status = edscr_status(raw)
    return EDSCR_STATUS_NAMES.get(status, f"unknown status 0b{status:06b}")


def edscr_halted(raw: int) -> bool:
    """Whether EDSCR.STATUS says the core is in Debug state.

    Derived by exclusion rather than by listing halt reasons, so an
    implementation-defined halt reason this table does not name still reads as
    halted instead of silently as running.
    """
    return edscr_status(raw) not in (EDSCR_STATUS_RESTARTING, EDSCR_STATUS_RUNNING)


def decode_edscr(raw: int) -> dict[str, object]:
    return {
        "raw": f"0x{raw:08X}",
        "status": edscr_status_name(raw),
        "halted": edscr_halted(raw),
        "halting_debug_enabled": bool(raw & EDSCR_HDE),
        "sticky_error": bool(raw & EDSCR_ERR),
        "instruction_transfer_empty": bool(raw & EDSCR_ITE),
        "tx_full": bool(raw & EDSCR_TXFULL),
        "rx_full": bool(raw & EDSCR_RXFULL),
        "secure_debug_disabled": bool(raw & EDSCR_SDD),
    }


# --------------------------------------------------------------------------
# EDPRSR -- power and reset status.  VERIFY (bit positions)
# --------------------------------------------------------------------------

EDPRSR_PU = 1 << 0  #: core powered up
EDPRSR_SPD = 1 << 1
EDPRSR_R = 1 << 2  #: core in reset now
EDPRSR_SR = 1 << 3  #: sticky reset since last read
EDPRSR_HALTED = 1 << 4  #: core is in Debug state
EDPRSR_OSLK = 1 << 5  #: OS lock is set
EDPRSR_DLK = 1 << 6  #: double lock
EDPRSR_EDAD = 1 << 7  #: external debug access disabled
EDPRSR_SDAD = 1 << 8  #: secure debug access disabled
EDPRSR_EPMAD = 1 << 9
EDPRSR_SPMAD = 1 << 10
EDPRSR_SDR = 1 << 11


def decode_edprsr(raw: int) -> dict[str, object]:
    return {
        "raw": f"0x{raw:08X}",
        "powered_up": bool(raw & EDPRSR_PU),
        "halted": bool(raw & EDPRSR_HALTED),
        "in_reset": bool(raw & EDPRSR_R),
        "sticky_reset": bool(raw & EDPRSR_SR),
        "os_locked": bool(raw & EDPRSR_OSLK),
        "double_locked": bool(raw & EDPRSR_DLK),
        "external_debug_access_disabled": bool(raw & EDPRSR_EDAD),
    }


def debug_registers_reachable(edprsr: int) -> tuple[bool, str | None]:
    """Whether the core-domain debug registers can be read at all, and why not.

    Measured on this silicon: with the OS lock set or external debug access
    disabled, a read of EDSCR **faults** while MIDR, EDDFR and DBGAUTHSTATUS
    still return correct values -- the ID registers are not gated but the
    core-domain ones are. Checking EDPRSR first turns that from a mysterious
    fault into a stated reason.
    """
    if edprsr & EDPRSR_DLK:
        return False, "the OS double lock is set"
    if edprsr & EDPRSR_OSLK:
        return False, "the OS lock is set (clear OSLAR to release it)"
    if edprsr & EDPRSR_EDAD:
        return False, "external debug access is disabled (EDPRSR.EDAD)"
    if not edprsr & EDPRSR_PU:
        return False, "the core power domain is down"
    return True, None


# --------------------------------------------------------------------------
# EDECR / EDRCR / EDPRCR.  VERIFY (bit positions)
# --------------------------------------------------------------------------

EDECR_OSUCE = 1 << 0  #: OS unlock catch enable
EDECR_RCE = 1 << 1  #: reset catch enable
EDECR_SS = 1 << 2  #: halting step enable

EDRCR_CSE = 1 << 2  #: clear sticky error
EDRCR_CSPA = 1 << 3  #: clear sticky pipeline advance
EDRCR_CBRRQ = 1 << 4  #: cancel bus request

EDPRCR_CORENPDRQ = 1 << 0  #: hold the core power domain up
EDPRCR_CWRR = 1 << 1  #: warm reset request


# --------------------------------------------------------------------------
# MIDR -- the identity anchor
# --------------------------------------------------------------------------

MIDR_IMPLEMENTER_ARM = 0x41
MIDR_PARTNUM_CORTEX_R52 = 0xD13


def decode_midr(raw: int) -> dict[str, object]:
    """Decode MIDR. Implementer 0x41 + part 0xD13 is a Cortex-R52.

    This is the cheapest and strongest check that the frame offsets are right:
    MIDR sits at 0xD00, far from the ID block the ROM walk already validated, so
    a correct decode here means the middle of the map is addressed correctly
    too.
    """
    implementer = (raw >> 24) & 0xFF
    partnum = (raw >> 4) & 0xFFF
    return {
        "raw": f"0x{raw:08X}",
        "implementer": f"0x{implementer:02X}",
        "implementer_name": "Arm" if implementer == MIDR_IMPLEMENTER_ARM else "unknown",
        "variant": (raw >> 20) & 0xF,
        "architecture": (raw >> 16) & 0xF,
        "partnum": f"0x{partnum:03X}",
        "part_name": "Cortex-R52" if partnum == MIDR_PARTNUM_CORTEX_R52 else "unknown",
        "revision": raw & 0xF,
        "is_cortex_r52": implementer == MIDR_IMPLEMENTER_ARM
        and partnum == MIDR_PARTNUM_CORTEX_R52,
    }


# --------------------------------------------------------------------------
# EDDFR -- how many comparators this core actually implements
# --------------------------------------------------------------------------
#
# VERIFY. Armv8 encodes these as "number minus one", so a field of 7 means 8
# comparators -- which matches what the S32E2 breakpoints guide states for the
# R52. Both the field positions and the minus-one convention are checked at
# runtime by requiring the result to land in a plausible range; the count is
# never assumed to be 8.

EDDFR_BRPS_SHIFT = 12
EDDFR_WRPS_SHIFT = 20
EDDFR_CTX_CMPS_SHIFT = 28


def decode_eddfr(raw: int) -> dict[str, object]:
    """Decode the comparator counts, flagging an implausible result rather than
    returning it as fact.

    Reading the count instead of hardcoding 8 matters: the guide says eight, but
    a wrong offset would also produce a number, and programming comparator 7 on
    a core that implements four writes into a reserved region.
    """
    breakpoints = ((raw >> EDDFR_BRPS_SHIFT) & 0xF) + 1
    watchpoints = ((raw >> EDDFR_WRPS_SHIFT) & 0xF) + 1
    context = ((raw >> EDDFR_CTX_CMPS_SHIFT) & 0xF) + 1

    # A range check alone is not enough. The minus-one encoding maps 0xFFFFFFFF
    # onto a perfectly in-range 16/16 and 0x00000000 onto 1/1 -- and those two
    # values are not comparator counts, they are the two ways a wire reads when
    # nothing is driving it. The same reasoning `connect` applies to DPIDR.
    dead_bus = raw in (0x0000_0000, 0xFFFF_FFFF)
    plausible = (
        not dead_bus
        and 1 <= breakpoints <= MAX_COMPARATORS
        and 1 <= watchpoints <= MAX_COMPARATORS
    )
    return {
        "raw": f"0x{raw:08X}",
        "breakpoints": breakpoints,
        "watchpoints": watchpoints,
        "context_comparators": context,
        "plausible": plausible,
    }


# --------------------------------------------------------------------------
# CTI -- how a core is actually halted
# --------------------------------------------------------------------------
#
# Armv8 has no "write 1 to halt" bit. The external debug request is a CTI
# trigger output, so halting means routing a channel to trigger output 0 and
# pulsing it. Corroborated independently by SEGGER's own requirement that a CTI
# base address be configured alongside the core base on Armv8-A/R.
#
# VERIFY: the offsets are the CoreSight CTI programmers' model; the trigger
# assignment (out 0 = debug request, out 1 = restart) is the Armv8 convention.
# Both are self-validating -- if pulsing channel 0 halts the core, they are
# right.

CTICONTROL = 0x000
CTIINTACK = 0x010
CTIAPPSET = 0x014
CTIAPPCLEAR = 0x018
CTIAPPPULSE = 0x01C
CTIINEN_BASE = 0x020
CTIOUTEN_BASE = 0x0A0
CTITRIGINSTATUS = 0x130
CTITRIGOUTSTATUS = 0x134
CTICHINSTATUS = 0x138
CTICHOUTSTATUS = 0x13C
CTIGATE = 0x140
CTIASICCTL = 0x144

CTICONTROL_GLBEN = 1 << 0

#: Offset of the CTI's own base from its core's debug frame base, on this
#: silicon. Proven by the ROM walk: 0x0041_0000 -> 0x0042_0000, and the same
#: stride for all four cores.
CTI_OFFSET_FROM_DEBUG = 0x0001_0000


def ctiinen(n: int) -> int:
    return CTIINEN_BASE + n * 4


def ctiouten(n: int) -> int:
    return CTIOUTEN_BASE + n * 4


#: Armv8 core CTI trigger assignment.
TRIGOUT_DEBUG_REQUEST = 0  #: pulse this to halt
TRIGOUT_RESTART = 1  #: pulse this to resume
TRIGIN_CROSS_HALT = 0  #: asserted when the core enters Debug state

#: Channels this tool uses. Channel 0 carries halt, channel 1 carries restart.
#: Kept low and fixed so cross-trigger configuration stays readable.
CHANNEL_HALT = 0
CHANNEL_RESTART = 1
#: A third channel, used only when cross-triggering is switched on, so the two
#: single-core channels never propagate to the matrix by accident.
CHANNEL_CROSS_HALT = 2


# --------------------------------------------------------------------------
# Instruction encodings for the EDITR register-transfer path
# --------------------------------------------------------------------------
#
# **The highest-risk item in this module.** In Debug state the core executes
# whatever instruction is written to EDITR, which is how a debugger moves a
# core register into a place it can read. The A32 encodings below are derived
# from the MCR/MRC encoding layout:
#
#   [31:28] cond=1110 (AL)   [27:24] 1110   [23:21] opc1   [20] direction
#   [19:16] CRn              [15:12] Rt     [11:8] coproc  [7:5] opc2
#   [4] 1                    [3:0] CRm
#
# with coproc=p14, opc1=0, CRn=c0, CRm=c5, opc2=0 -- the debug DTR.
#
# Derived, not read from a document. ``runcontrol`` therefore round-trips a
# known pattern through a scratch register before any register value is
# reported, and refuses to report anything if that fails.

# **Settled on silicon: this core takes T32, and EDITR wants it halfword-swapped.**
# Measured on RTU0 core 1, halted, EDSCR 0x03054113 (RW[13:10] = 0b0000, so all
# exception levels are AArch32):
#
#     A32  MCR p14,0,r0,c0,c5,0  0xEE000E15  ->  EDSCR.ERR set, nothing transferred
#     T32  same, halfword-swapped 0x0E15EE00 ->  ok, EDSCR.TXfull set, DTRTX = 0x321C1774
#     A32  NOP                   0xE320F000  ->  EDSCR.ERR set
#     T32  NOP                   0x8000F3AF  ->  ok
#
# A 32-bit T32 instruction is two halfwords, and EDITR takes them with the
# *first* halfword in the low 16 bits -- the opposite order from how the
# encoding is normally written down. Both encodings are kept: A32 is what the
# instruction set reference shows, T32 is what this core executes, and
# `runcontrol` picks between them by round-tripping a known pattern rather than
# by assuming either.

#: MCR p14,0,Rt,c0,c5,0 -- Rt -> DBGDTRTX. First/second halfword.
_MCR_DTR_HALFWORDS = (0xEE00, 0x0E15)
#: MRC p14,0,Rt,c0,c5,0 -- DBGDTRRX -> Rt.
_MRC_DTR_HALFWORDS = (0xEE10, 0x0E15)
#: MRS Rd, CPSR, T32 encoding T1.
_MRS_CPSR_HALFWORDS = (0xF3EF, 0x8000)


def _t32(first: int, second: int) -> int:
    """Pack a 32-bit T32 instruction the way EDITR expects it.

    First halfword in the low 16 bits. Writing it the other way round is the
    single easiest mistake here, and it does not fault -- it executes some other
    instruction.
    """
    return ((second & 0xFFFF) << 16) | (first & 0xFFFF)


def _check_reg(rt: int) -> None:
    if not 0 <= rt <= 15:
        raise ValueError(f"r{rt} is not an AArch32 core register")


def instr_reg_to_dtr(rt: int, *, t32: bool = True) -> int:
    """Instruction that moves core register ``rt`` into the target-to-host DTR."""
    _check_reg(rt)
    first, second = _MCR_DTR_HALFWORDS
    if not t32:
        return (first << 16) | second | (rt << 12)
    return _t32(first, second | (rt << 12))


def instr_dtr_to_reg(rt: int, *, t32: bool = True) -> int:
    """Instruction that moves the host-to-target DTR into core register ``rt``."""
    _check_reg(rt)
    first, second = _MRC_DTR_HALFWORDS
    if not t32:
        return (first << 16) | second | (rt << 12)
    return _t32(first, second | (rt << 12))


#: EDPCSR reads this when the sample is not valid, which on this silicon is the
#: case while the core is halted. Measured: 0xFFFFFFFF on a core stopped at an
#: external debug request. So EDPCSR is not a usable PC source here.
EDPCSR_INVALID = 0xFFFF_FFFF


def instr_mrc_p15(opc1: int, crn: int, crm: int, opc2: int, rt: int, *, t32: bool = True) -> int:
    """``MRC p15, opc1, Rt, CRn, CRm, opc2`` -- read a coprocessor register.

    MRC encoding:
        [31:28] cond=1110  [27:24] 1110  [23:21] opc1  [20] 1 (read)
        [19:16] CRn        [15:12] Rt    [11:8] coproc [7:5] opc2
        [4] 1              [3:0] CRm
    """
    _check_reg(rt)
    a32 = (
        0xEE10_0010
        | (opc1 << 21)
        | (crn << 16)
        | (rt << 12)
        | (0xF << 8)  # coproc = p15
        | (opc2 << 5)
        | crm
    )
    if not t32:
        return a32
    return _t32(a32 >> 16, a32 & 0xFFFF)


# --------------------------------------------------------------------------
# DLR and DSPSR -- the PC and PSR of a core in Debug state
# --------------------------------------------------------------------------
#
# **Settled from the Cortex-R52 TRM, section 4.2.31**, "AArch32 debug registers
# ... 32-bit wide debug registers accessed with (coproc==0b1111)":
#
#     DSPSR   CRn=c4  Op1=3  CRm=c5  Op2=0   Debug Saved Program Status Register
#     DLR     CRn=c4  Op1=3  CRm=c5  Op2=1   Debug Link Register
#
# with the TRM footnote on DLR: "The reset value is the value of the PC in
# Halting Debug state."
#
# So these are **coprocessor** registers reached with MRC p15, not special
# registers reached with MRS. That is why every MRS variant failed: a 64-point
# sweep of the MRS banked-register space found the banked SP and LR and no PC,
# because the PC was never in that space to begin with.
#
# Confirmed on silicon, RTU0 core 1 halted:
#     DSPSR  EDITR 0x0F15EE74 -> 0x0000003F   (mode 0x1F System, T set: Thumb)
#     DLR    EDITR 0x0F35EE74 -> 0x321D2EE6   (in the 0x321xxxxx code region)

DLR_OPC1, DLR_CRN, DLR_CRM, DLR_OPC2 = 3, 4, 5, 1
DSPSR_OPC1, DSPSR_CRN, DSPSR_CRM, DSPSR_OPC2 = 3, 4, 5, 0


def instr_dlr_to_reg(rt: int, *, t32: bool = True) -> int:
    """Move DLR -- the Debug-state PC -- into core register ``rt``."""
    return instr_mrc_p15(DLR_OPC1, DLR_CRN, DLR_CRM, DLR_OPC2, rt, t32=t32)


def instr_dspsr_to_reg(rt: int, *, t32: bool = True) -> int:
    """Move DSPSR -- the Debug-state saved PSR -- into core register ``rt``."""
    return instr_mrc_p15(DSPSR_OPC1, DSPSR_CRN, DSPSR_CRM, DSPSR_OPC2, rt, t32=t32)


#: CPSR mode field names, for decoding DSPSR. AArch32 M[4:0].
PSR_MODES: dict[int, str] = {
    0b10000: "User",
    0b10001: "FIQ",
    0b10010: "IRQ",
    0b10011: "Supervisor",
    0b10110: "Monitor",
    0b10111: "Abort",
    0b11010: "Hyp",
    0b11011: "Undefined",
    0b11111: "System",
}

PSR_T = 1 << 5  #: Thumb execution state


def decode_psr(raw: int) -> dict[str, object]:
    mode = raw & 0b11111
    return {
        "raw": f"0x{raw:08X}",
        "mode": PSR_MODES.get(mode, f"reserved(0b{mode:05b})"),
        "thumb": bool(raw & PSR_T),
        "flags": "".join(
            name for bit, name in ((31, "N"), (30, "Z"), (29, "C"), (28, "V")) if raw & (1 << bit)
        )
        or "-",
    }


def instr_cpsr_to_reg(rt: int, *, t32: bool = True) -> int:
    """``MRS Rt, CPSR``.

    A32 encoding A1 is ``cond 00010 R=0 001111 Rd 000000000000``; the T32
    encoding T1 puts Rd in bits [11:8] of the second halfword.

    **Unresolved on this silicon.** The T32 form 0x8000F3EF sets EDSCR.ERR on a
    halted Cortex-R52 while the DTR transfers in the same encoding work
    perfectly. Most likely this is the M-profile MRS encoding rather than the
    A/R-profile one, or CPSR is simply not readable this way in Debug state
    (where the architecture provides DSPSR instead). Callers must treat a
    failure here as expected and carry on -- r0-r14 do not depend on it.
    """
    _check_reg(rt)
    if not t32:
        return 0xE10F_0000 | (rt << 12)
    first, second = _MRS_CPSR_HALFWORDS
    return _t32(first, second | (rt << 8))


# --------------------------------------------------------------------------
# Breakpoint and watchpoint control words
# --------------------------------------------------------------------------
#
# VERIFY. Field positions are the Armv8 DBGBCR/DBGWCR layout. The qualification
# fields (HMC, SSC, PMC) decide which exception levels and security states a
# comparator matches in, and a comparator qualified for the wrong one is
# accepted, reads back correctly, and simply never fires -- which the
# breakpoints guide lists in §7.2 as "breakpoint is accepted but never hits".
# So the settings below aim to match as widely as possible, and the proof is a
# breakpoint that actually fires.

DBGBCR_E = 1 << 0
DBGBCR_PMC_SHIFT = 1  #: privilege mode control [2:1]
DBGBCR_BAS_SHIFT = 5  #: byte address select [8:5]
DBGBCR_HMC = 1 << 13
DBGBCR_SSC_SHIFT = 14  #: security state control [15:14]
DBGBCR_LBN_SHIFT = 16  #: linked breakpoint number [19:16]
DBGBCR_BT_SHIFT = 20  #: breakpoint type [23:20]

DBGBCR_BT_UNLINKED_ADDRESS = 0b0000
DBGBCR_PMC_ANY_PRIVILEGE = 0b11  #: PL1 and PL0

DBGWCR_E = 1 << 0
DBGWCR_PAC_SHIFT = 1  #: privileged access control [2:1]
DBGWCR_LSC_SHIFT = 3  #: load/store control [4:3]
DBGWCR_BAS_SHIFT = 5  #: byte address select [12:5], eight bits
DBGWCR_HMC = 1 << 13
DBGWCR_SSC_SHIFT = 14

DBGWCR_LSC_LOAD = 0b01
DBGWCR_LSC_STORE = 0b10
DBGWCR_LSC_BOTH = 0b11


def encode_breakpoint(address: int, *, thumb: bool = True) -> tuple[int, int]:
    """Build (DBGBVR, DBGBCR) for an unlinked instruction breakpoint.

    Three things the breakpoints guide §2 warns about, handled here:

    * **The Thumb bit is not part of the address.** A function pointer to T32
      code has bit 0 set to mark the instruction set; writing that into DBGBVR
      gives a comparator that can never match, because instruction addresses are
      always at least halfword aligned.
    * **DBGBVR is word-aligned.** Bits [1:0] are RES0, so the halfword within
      the word is selected by BAS, not by the address.
    * **BAS must match the instruction width.** A 16-bit T32 instruction
      occupies one halfword; selecting all four bytes would also cover the
      neighbouring instruction.
    """
    address &= ~1  # drop the Thumb state bit -- it is not part of the address
    if thumb:
        bas = 0b0011 if (address & 0b10) == 0 else 0b1100
    else:
        bas = 0b1111

    control = (
        DBGBCR_E
        | (DBGBCR_PMC_ANY_PRIVILEGE << DBGBCR_PMC_SHIFT)
        | (bas << DBGBCR_BAS_SHIFT)
        | (DBGBCR_BT_UNLINKED_ADDRESS << DBGBCR_BT_SHIFT)
    )
    return address & ~0b11, control


def decode_breakpoint(value: int, control: int) -> dict[str, object]:
    bas = (control >> DBGBCR_BAS_SHIFT) & 0xF
    # BAS says which byte of the word the comparator watches; recover the
    # address the operator actually asked for.
    address = value | (0b10 if bas in (0b1100, 0b1000) else 0)
    return {
        "enabled": bool(control & DBGBCR_E),
        "address": f"0x{address:08X}",
        "dbgbvr": f"0x{value:08X}",
        "dbgbcr": f"0x{control:08X}",
        "bas": f"0b{bas:04b}",
        "type": (control >> DBGBCR_BT_SHIFT) & 0xF,
        "privilege": (control >> DBGBCR_PMC_SHIFT) & 0b11,
        "security": (control >> DBGBCR_SSC_SHIFT) & 0b11,
    }


def encode_watchpoint(
    address: int, *, size: int = 4, on: str = "both"
) -> tuple[int, int]:
    """Build (DBGWVR, DBGWCR) for an unlinked data watchpoint.

    ``size`` is the number of bytes watched from ``address``; BAS selects them
    within the aligned doubleword, so a watch that straddles the boundary is
    rejected rather than silently truncated.
    """
    if size not in (1, 2, 4, 8):
        raise ValueError(f"watchpoint size must be 1, 2, 4 or 8 bytes, not {size}")
    offset = address & 0b111
    if offset + size > 8:
        raise ValueError(
            f"a {size}-byte watch at 0x{address:08X} straddles the 8-byte boundary "
            "a single comparator covers; split it across two watchpoints"
        )
    lsc = {"load": DBGWCR_LSC_LOAD, "store": DBGWCR_LSC_STORE, "both": DBGWCR_LSC_BOTH}
    if on not in lsc:
        raise ValueError(f"watchpoint access must be load, store or both, not {on!r}")

    bas = ((1 << size) - 1) << offset
    control = (
        DBGWCR_E
        | (DBGBCR_PMC_ANY_PRIVILEGE << DBGWCR_PAC_SHIFT)
        | (lsc[on] << DBGWCR_LSC_SHIFT)
        | (bas << DBGWCR_BAS_SHIFT)
    )
    return address & ~0b111, control


def decode_watchpoint(value: int, control: int) -> dict[str, object]:
    bas = (control >> DBGWCR_BAS_SHIFT) & 0xFF
    lsc = (control >> DBGWCR_LSC_SHIFT) & 0b11
    first = (bas & -bas).bit_length() - 1 if bas else 0
    return {
        "enabled": bool(control & DBGWCR_E),
        "address": f"0x{value + first:08X}",
        "dbgwvr": f"0x{value:08X}",
        "dbgwcr": f"0x{control:08X}",
        "bytes": bin(bas).count("1"),
        "on": {0b01: "load", 0b10: "store", 0b11: "both"}.get(lsc, f"0b{lsc:02b}"),
    }


#: A scratch register is unavoidable: every transfer goes through one. R0 is
#: used, and its original value is saved and restored around every access --
#: without that, reading the registers of a halted core would corrupt the
#: context you halted to inspect.
SCRATCH_REG = 0

#: Round-trip probe value. Asymmetric in every byte so a byte-order or
#: shift error cannot reproduce it by chance -- the same reasoning as
#: ``tapseq.MEASURE_MARKER``.
DTR_PROBE_PATTERN = 0x6A3D_1C97

#: The AArch32 core registers this tool reads. PC is deliberately absent: in
#: Debug state the PC is not readable as r15, and the correct source is settled
#: on hardware rather than guessed (see runcontrol.read_pc).
CORE_REGISTERS: tuple[str, ...] = tuple(f"r{i}" for i in range(15))

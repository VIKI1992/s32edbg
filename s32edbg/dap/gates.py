"""Debug-enable gates: the MDM-AP clock/enable gate and the HSE/SDA-AP gate.

Figure 1013 in the RM draws two distinct gate layers on the System Debug
Interface -- "APB security gates" and "HSE Security Gates". This module reports
both and can drive the one that is safe to drive.

What is safe to write
---------------------
MDM-AP DBGENCNTRL{,2,3} plus CONTROL[MSK_DBG_FAULT] -- the "LADDER-1" sequence.
RM 135.11 states the debug clocks are off by default and are enabled by
"setting any enable bit from the MDM-AP DBGGENCNTRL, DBGGENCNTRL2, and
DBGGENCNTRL3 registers", and warns that "Trying to access debug components while
the clocks are not enabled might hang the debug APBIC bus." These bits are
sticky across functional resets and are lost on power loss.

What is never written
---------------------
The SDA-AP *authentication* path -- AUTHCTL and the KEYRESP registers. RM
135.13.4: "HSE allows one only attempt for C/R comparison. If it fails, no
further attempts are accepted." A botched challenge/response is unrecoverable
until reset, and the prior hardware recon established that this silicon needs no
unlock at all. Those registers are read-only here, purely to report whether the
HSE gate is open.

The one exception, and why
-------------------------
SDASYSRSTC. RM 135.11 describes it as the debugger's own reset control:
"System functional reset field SYSFUNCREQ that allows the debugger to hold the
system in functional reset." It is not part of the challenge/response path and
carries none of its one-shot risk.

It is also the only software route onto a board that will not stay still. With
blank flash the BootROM fails, and the failure cascade escalates to a POR --
which RM Table 301 says is one of exactly two things that hold the JTAGC in
reset, so the TAP dies. With software running, RM 135.15.1 warns the debug pads
"operate in JTAG functionality out of reset but can be later reassigned to their
alternate functionalities", so an application that remuxes them also removes the
TAP. Holding functional reset stops both, and RM Table 301 confirms a functional
reset leaves the JTAGC and SWJ-DP alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..data.expected_apmap import AP_MDM, AP_SDA
from ..data.ladder1 import (
    GATE_READBACK,
    LADDER1,
    MDM_CONTROL,
    MDM_CONTROL2,
    MDM_CONTROL3,
    MDM_CONTROL2_EDBGREQ_RTU0_ALL,
    MDM_CONTROL3_EDBGREQ_RTU1_ALL,
    MDM_CONTROL_LOAD_ASSERT,
    MDM_DEVARCH,
    MDM_DEVARCH_EXPECTED,
    MDM_IDR,
    MDM_IDR_EXPECTED,
    MDM_STATUS,
    MDM_STATUS_CDBGPWRUPREQ,
    MDM_STATUS_CSYSPWRUPREQ,
    MDM_STATUS_JTAG_ACTIVE,
    MDM_STATUS_SYSTEM_RESET,
    edbgreq_bit,
    SDA_AUTHSTTS,
    SDA_AUTHSTTS_APPDBGEN,
    SDA_AUTHSTTS_CHALRDY,
    SDA_AUTHSTTS_UIDRDY,
    SDA_IDR,
    SDA_IDR_EXPECTED,
    SDA_RSTCTRL,
    SDA_RSTMASK,
    SDA_SYSRSTC,
    SDA_SYSRSTC_SYSFUNCREQ,
    SDA_SYSRSTS,
    SDA_SYSRSTS_DESTRUCT_RESET,
    SDA_SYSRSTS_SYSTEM_RESET,
    SDA_UIDH,
    SDA_UIDL,
)
from ..errors import DapError, UnsafeOperation
from .adiv6 import Dap


@dataclass
class MdmState:
    idr: int | None = None
    devarch: int | None = None
    registers: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def identity_ok(self) -> bool:
        return self.idr == MDM_IDR_EXPECTED

    @property
    def enables_set(self) -> bool:
        """True when all three DBGENCNTRL registers are non-zero."""
        return all(
            self.registers.get(name, 0) != 0
            for name in ("DBGENCNTRL", "DBGENCNTRL2", "DBGENCNTRL3")
        )

    @property
    def system_reset_asserted(self) -> bool | None:
        status = self.registers.get("STATUS")
        if status is None:
            return None
        return bool(status & MDM_STATUS_SYSTEM_RESET)

    def status_flags(self) -> dict[str, bool]:
        status = self.registers.get("STATUS", 0)
        return {
            "system_reset": bool(status & MDM_STATUS_SYSTEM_RESET),
            "cdbgpwrupreq": bool(status & MDM_STATUS_CDBGPWRUPREQ),
            "csyspwrupreq": bool(status & MDM_STATUS_CSYSPWRUPREQ),
            "aux_tap_trst_released": bool(status & MDM_STATUS_JTAG_ACTIVE),
        }


@dataclass
class HseState:
    idr: int | None = None
    authsts: int | None = None
    sysrsts: int | None = None
    rstctrl: int | None = None
    uid: int | None = None
    error: str | None = None

    @property
    def identity_ok(self) -> bool:
        return self.idr == SDA_IDR_EXPECTED

    @property
    def app_debug_enabled(self) -> bool | None:
        if self.authsts is None:
            return None
        return bool(self.authsts & SDA_AUTHSTTS_APPDBGEN)

    @property
    def challenge_pending(self) -> bool | None:
        if self.authsts is None:
            return None
        return bool(self.authsts & SDA_AUTHSTTS_CHALRDY)

    def reset_flags(self) -> dict[str, bool]:
        v = self.sysrsts or 0
        return {
            "functional_reset_asserted": bool(v & SDA_SYSRSTS_SYSTEM_RESET),
            "destructive_reset_asserted": bool(v & SDA_SYSRSTS_DESTRUCT_RESET),
        }


@dataclass
class GateReport:
    mdm: MdmState
    hse: HseState
    warnings: list[str] = field(default_factory=list)

    @property
    def apb_rom_should_be_readable(self) -> bool:
        return self.mdm.enables_set


def read_mdm(dap: Dap, ap_base: int = AP_MDM) -> MdmState:
    state = MdmState()
    try:
        state.idr = dap.ap_read(ap_base, MDM_IDR)
        state.devarch = dap.ap_read(ap_base, MDM_DEVARCH)
        for offset, name in GATE_READBACK:
            state.registers[name] = dap.ap_read(ap_base, offset)
    except DapError as exc:
        state.error = str(exc)
    return state


def read_hse(dap: Dap, ap_base: int = AP_SDA) -> HseState:
    """Read-only view of the SDA-AP. Never writes."""
    state = HseState()
    try:
        state.idr = dap.ap_read(ap_base, SDA_IDR)
        state.authsts = dap.ap_read(ap_base, SDA_AUTHSTTS)
        state.sysrsts = dap.ap_read(ap_base, SDA_SYSRSTS)
        state.rstctrl = dap.ap_read(ap_base, SDA_RSTCTRL)
        if state.authsts & SDA_AUTHSTTS_UIDRDY:
            lo = dap.ap_read(ap_base, SDA_UIDL)
            hi = dap.ap_read(ap_base, SDA_UIDH)
            state.uid = (hi << 32) | lo
    except DapError as exc:
        state.error = str(exc)
    return state


def status(dap: Dap) -> GateReport:
    mdm = read_mdm(dap)
    hse = read_hse(dap)
    report = GateReport(mdm=mdm, hse=hse)

    if mdm.idr is not None and not mdm.identity_ok:
        report.warnings.append(
            f"MDM-AP IDR is 0x{mdm.idr:08X}, but RM Table 957 documents "
            f"0x{MDM_IDR_EXPECTED:08X}. The AP map may be misaddressed."
        )
    if mdm.devarch is not None and mdm.devarch != MDM_DEVARCH_EXPECTED:
        report.warnings.append(
            f"MDM-AP DEVARCH is 0x{mdm.devarch:08X}, expected 0x{MDM_DEVARCH_EXPECTED:08X}."
        )
    if hse.idr is not None and not hse.identity_ok:
        report.warnings.append(
            f"SDA-AP IDR is 0x{hse.idr:08X}, but RM Table 957 documents 0x{SDA_IDR_EXPECTED:08X}."
        )
    if not mdm.enables_set:
        report.warnings.append(
            "MDM-AP DBGENCNTRL/2/3 read zero, so the debug clocks are still gated. "
            "The APB ROM will not enumerate and RM 135.11 warns that touching debug "
            "components now 'might hang the debug APBIC bus'. Run `gates apply` first."
        )
    if hse.challenge_pending:
        report.warnings.append(
            "SDA-AP AUTHSTTS[CHALRDY] is set: the HSE is offering a debug-authentication "
            "challenge. This tool never answers it -- the RM states only one attempt is "
            "ever accepted."
        )
    if hse.app_debug_enabled is False and not mdm.enables_set:
        report.warnings.append(
            "SDA-AP AUTHSTTS[APPDBGEN] is clear. If MDM-AP writes are rejected, this "
            "part is in a secured lifecycle state and needs an HSE challenge/response "
            "that is out of scope for this tool."
        )
    flags = hse.reset_flags()
    if flags["functional_reset_asserted"]:
        report.warnings.append(
            "Functional reset is asserted. RM Table 957 warns that accessing peripherals "
            "or memory via the AXI-AP now can leave them inaccessible until a destructive "
            "reset; AXI-AP traffic is blocked while this holds."
        )
    return report


def apply_ladder1(dap: Dap, ap_base: int = AP_MDM, *, verify: bool = True) -> GateReport:
    """Write the LADDER-1 debug-enable sequence, then re-read.

    Idempotent: the registers are simply set to the documented values, so
    re-running is harmless. Writes are ordered so that MSK_DBG_FAULT lands first
    -- RM 135.12 says the VFCCU raises a debug fault when debug-enable signals
    assert with no debugger attached.
    """
    before = read_mdm(dap, ap_base)
    if before.error:
        raise DapError(f"cannot reach the MDM-AP at 0x{ap_base:08X}: {before.error}")

    for write in LADDER1:
        dap.ap_write(ap_base, write.offset, write.value)

    report = status(dap)
    if verify:
        expected = {
            "CONTROL": LADDER1[0].value,
            "DBGENCNTRL": LADDER1[1].value,
            "DBGENCNTRL2": LADDER1[2].value,
            "DBGENCNTRL3": LADDER1[3].value,
        }
        for name, want in expected.items():
            got = report.mdm.registers.get(name)
            if got is None:
                continue
            if got != want:
                report.warnings.append(
                    f"{name} reads back 0x{got:08X} after writing 0x{want:08X}. "
                    "If it reads zero, MDM-AP writes are being rejected, which points "
                    "at the HSE gate rather than at this sequence."
                )
    return report


def arm_edbgreq(dap: Dap, ap_base: int = AP_MDM) -> None:
    """Latch external debug requests so every R52 halts on leaving reset.

    The reference plugin's RAM-load values, verbatim (its
    `_reset_to_first_instruction()`): CONTROL gets MSK_DBG_FAULT plus the M33
    request bits, CONTROL2/CONTROL3 get the EDBGREQ nibble for all four cores
    of RTU0/RTU1. Written while reset is asserted -- the MDM-AP is a debug
    component and survives functional reset (guide §3.6), so the requests are
    still pending when the cores come out and they halt before executing
    anything.
    """
    dap.ap_write(ap_base, MDM_CONTROL, MDM_CONTROL_LOAD_ASSERT)
    dap.ap_write(ap_base, MDM_CONTROL2, MDM_CONTROL2_EDBGREQ_RTU0_ALL)
    dap.ap_write(ap_base, MDM_CONTROL3, MDM_CONTROL3_EDBGREQ_RTU1_ALL)


def clear_core_edbgreq(dap: Dap, core: int = 0, ap_base: int = AP_MDM) -> None:
    """De-assert one RTU0 core's external debug request (read-modify-write).

    The plugin's `start_debug_by_core_name` equivalent: once the core is in
    Debug state the request has served its purpose, and leaving it asserted
    would fight every later resume. RMW so the other cores' still-pending
    requests -- and the M33 bits in CONTROL -- stay as the flow left them.
    """
    current = dap.ap_read(ap_base, MDM_CONTROL2)
    dap.ap_write(ap_base, MDM_CONTROL2, current & ~edbgreq_bit(core))


def clear_all_r52_edbgreq(dap: Dap, ap_base: int = AP_MDM) -> None:
    """Drop every latched R52 debug request (RTU0 and RTU1, RMW).

    Once the load flow has its core pinned by a real CTI halt, the latches
    have done their job -- they existed to stop cores running away between
    the reset release and the RGM holds. Leaving them armed is a trap:
    measured 2026-08-04, the application's own core-1 release ran into the
    stale latch and core1 halted instead of booting, so core0 hung at the OS
    boot barrier. The M33 request bits in CONTROL are left exactly as the
    plugin leaves them.
    """
    for reg, mask in (
        (MDM_CONTROL2, MDM_CONTROL2_EDBGREQ_RTU0_ALL),
        (MDM_CONTROL3, MDM_CONTROL3_EDBGREQ_RTU1_ALL),
    ):
        current = dap.ap_read(ap_base, reg)
        dap.ap_write(ap_base, reg, current & ~mask)


def assert_axi_safe(report: GateReport) -> None:
    """Refuse AXI-AP traffic while functional reset is asserted."""
    if report.hse.reset_flags()["functional_reset_asserted"] or report.mdm.system_reset_asserted:
        raise UnsafeOperation(
            "refusing to use the AXI-AP while functional reset is asserted. RM Table 957: "
            "'During reset, do not access peripherals and memory regions via AXI_AP. If "
            "such an access occurs, the peripherals and memory regions might not be "
            "accessible via AXI_AP after reset de-assertion. In that case, you must issue "
            "a destructive reset.'"
        )


#: Offsets that must never be written by this tool.
FORBIDDEN_WRITE_APS = {AP_SDA: "SDA-AP (one-shot HSE challenge/response)"}

#: The one SDA-AP register this tool may write, and why.
#:
#: The blanket "never write the SDA-AP" rule exists because of RM 135.13.4:
#: "HSE allows one only attempt for C/R comparison. If it fails, no further
#: attempts are accepted." That is a property of the *authentication* path --
#: AUTHCTL and the KEYRESP registers -- and a botched attempt there is
#: unrecoverable until reset.
#:
#: SDASYSRSTC is a different register with different consequences. RM 135.11
#: describes it as the debugger's own reset control: "System functional reset
#: field SYSFUNCREQ that allows the debugger to hold the system in functional
#: reset." That is the documented way to stop a target rebooting underneath
#: you, and RM Table 301 confirms a functional reset does not reset the JTAGC,
#: so the TAP survives it. Without this there is no software route onto a board
#: whose flash is blank: the boot-failure cascade escalates to POR, and POR is
#: precisely what holds the JTAGC in reset.
SDA_WRITABLE_OFFSETS = {
    SDA_SYSRSTC: "system reset control (RM 135.11), the only route onto a rebooting target",
    #: SDARSTMASK is documented (guide §3.6/§5.10.1) as masking the MC_RGM
    #: functional reset away from the RTU debug logic so breakpoints and an
    #: armed reset catch survive. **Measured 2026-08-02: it does not deliver
    #: that on this silicon.** The write lands (reads back exactly), but a
    #: functional reset still wiped the R52 debug state (OS lock back, EDECR
    #: cleared) -- and the boot wedged: rtu0.0 stayed in BootROM-range code and
    #: rtu0.1 was never released, until a clean attach/release recovered it.
    #: Plausibly the documented PSTATn-invalidation side effect derails boot
    #: software. Kept writable for experiments; nothing in the tool uses it.
    SDA_RSTMASK: "RTU functional-reset mask (measured ineffective here -- see comment)",
    #: SDARSTCTRL carries the R52 RSTREL release bits (0 = hold the core in
    #: reset) -- the guide §5.10.2 mechanism reset-halt is built on. Reset
    #: machinery, not the one-shot authentication path.
    SDA_RSTCTRL: "R52 reset-release control, the §5.10.2 hold behind `core reset-halt`",
}


def guard_write(ap_base: int, offset: int | None = None) -> None:
    """Refuse writes the safety policy forbids.

    ``offset`` narrows the check to a single register. Omitting it keeps the
    original whole-AP behaviour, so a caller that cannot say which register it
    is about to touch is still refused.
    """
    reason = FORBIDDEN_WRITE_APS.get(ap_base)
    if reason is None:
        return
    if ap_base == AP_SDA and offset in SDA_WRITABLE_OFFSETS:
        return
    raise UnsafeOperation(
        f"writes to {reason} are disabled by policy"
        + (
            ""
            if offset is None
            else f"; offset 0x{offset:03X} is not one of the permitted registers "
            f"({', '.join(f'0x{o:03X}' for o in SDA_WRITABLE_OFFSETS)})"
        )
    )


def hold_in_functional_reset(dap: Dap, *, ap_base: int = AP_SDA) -> None:
    """Hold the system in functional reset so it stops rebooting.

    RM 135.11 names this as the debugger's mechanism, and RM Table 301 is what
    makes it usable: a functional reset does *not* reset the JTAGC or the
    SWJ-DP, so the debug connection survives while the cores stay stopped. On a
    blank-flash board this is what breaks the BootROM failure cascade before it
    escalates to a POR and takes the TAP down with it.

    It also stops application code ever running, which matters on a booting
    board for a second reason: RM 135.15.1 says the debug pads "operate in JTAG
    functionality out of reset but can be later reassigned to their alternate
    functionalities", so software that remuxes them removes the TAP.
    """
    guard_write(ap_base, SDA_SYSRSTC)
    dap.ap_write(ap_base, SDA_SYSRSTC, SDA_SYSRSTC_SYSFUNCREQ)


def set_reset_mask(dap: Dap, value: int, *, ap_base: int = AP_SDA) -> None:
    """Program SDARSTMASK -- which cores' debug state survives functional reset.

    The breakpoints guide §5.10.1: RTU0 cores 0-3 are bits 31:28 and RTU1 cores
    0-3 bits 23:20 (bit 31 = RTU0 core 0, matching the guide's §5.10.2 example
    "clear bit31 for RTU0 core0" for the sibling SDARSTCTRL layout). Write 0 to
    restore the default, in which a functional reset wipes RTU debug state and
    the MC_RGM.PSTATn status bits are valid again.
    """
    guard_write(ap_base, SDA_RSTMASK)
    dap.ap_write(ap_base, SDA_RSTMASK, value)


#: SDARSTMASK bits covering every R52 core: RTU0 cores 0-3 (31:28) and RTU1
#: cores 0-3 (23:20). The POR-mask bits (27 and 19) are deliberately excluded.
RSTMASK_ALL_R52 = (0xF << 28) | (0xF << 20)


def rstrel_bit(rtu: int, core: int) -> int:
    """SDARSTCTRL release-bit position for an R52 core.

    Guide §5.10.1/§5.10.2: RTU0 cores 0-3 are bits 31:28 and RTU1 cores 0-3
    bits 23:20, core 0 highest ("e.g. clear bit31 for RTU0 core0"). The bit is
    a *release*: 0 holds the core in reset, 1 (the reset value) lets it boot.
    """
    if not 0 <= core <= 3:
        raise ValueError(f"R52 core index {core} is not 0-3")
    if rtu == 0:
        return 31 - core
    if rtu == 1:
        return 23 - core
    raise ValueError(f"RTU{rtu} is not a Cortex-R52 cluster on this part")


def read_reset_control(dap: Dap, *, ap_base: int = AP_SDA) -> int:
    return dap.ap_read(ap_base, SDA_RSTCTRL)


def write_reset_control(dap: Dap, value: int, *, ap_base: int = AP_SDA) -> None:
    """Program SDARSTCTRL. Callers read-modify-write one RSTREL bit at a time --
    the register also carries the M33 CPUWAIT bits with the *opposite*
    polarity (guide pitfall #10), so absolute writes are how cores get held by
    accident."""
    guard_write(ap_base, SDA_RSTCTRL)
    dap.ap_write(ap_base, SDA_RSTCTRL, value)


def release_functional_reset(dap: Dap, *, ap_base: int = AP_SDA) -> None:
    """Let the system run again. Never issues a destructive reset."""
    guard_write(ap_base, SDA_SYSRSTC)
    dap.ap_write(ap_base, SDA_SYSRSTC, 0)

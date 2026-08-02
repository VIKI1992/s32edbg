"""Behaviours aligned to the S32E27 breakpoints/run-control guide.

Each test names the guide section it pins. These are the sequences the guide is
explicit about -- the CTI acknowledge poll (§5.5), the EDPRSR.SDR restart
confirmation (§5.5), any-EL comparator qualification (§5.7/§5.8), the EDWAR
watchpoint address (§5.3), comparator initialisation (§5.7), the two-write M33
step (§6.4), DWT watchpoints (§6.7), reset-and-halt (§5.10/§6.8) and the
synchronized restart (§5.9).
"""

from __future__ import annotations

import pytest
from conftest import CORE0_CTI, CORE0_DEBUG, CORE1_DEBUG, SyntheticTarget

from s32edbg.dap import m33 as m33ctl
from s32edbg.dap import runcontrol
from s32edbg.dap.adiv6 import Dap
from s32edbg.data import ladder1 as lad
from s32edbg.data import m33debug as m33
from s32edbg.data import r52debug as r52
from s32edbg.data.expected_apmap import AP_SDA
from s32edbg.errors import UnsafeOperation


def _halted_core(target: SyntheticTarget | None = None):
    target = target or SyntheticTarget()
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]
    runcontrol.halt(dap, core)
    return target, dap, core


# ---------------------------------------------------------------------------
# §5.5 -- acknowledge poll and SDR restart confirmation
# ---------------------------------------------------------------------------


def test_halting_acknowledges_until_the_trigger_reads_deasserted() -> None:
    """§5.5 step 1: CTIINTACK, then poll CTITRIGOUTSTATUS until the
    debug-request trigger deasserts. The fake latches the trigger on the halt
    pulse and drops it only on INTACK, so a halt that skipped the acknowledge
    would leave it set here."""
    target, dap, core = _halted_core()
    status = target.mem.get(CORE0_CTI + r52.CTITRIGOUTSTATUS, 0)
    assert not status & (1 << r52.TRIGOUT_DEBUG_REQUEST)


def test_an_acknowledge_that_never_deasserts_raises_instead_of_restarting() -> None:
    """§5.5: "you must first clear any still-asserted Debug request, or the
    core swallows the restart". A restart issued anyway would look exactly like
    a resume that did nothing, so the poll timing out is an error, not a
    warning."""
    target = SyntheticTarget()
    target.cti_ack_broken = True
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]
    with pytest.raises(runcontrol.RunControlError, match="did not deassert"):
        runcontrol.halt(dap, core, timeout_s=0.2)


def test_resume_confirms_the_restart_through_edprsr_sdr() -> None:
    """§5.5: "Confirm restart with EDPRSR.SDR (clear-on-read), not
    EDSCR.STATUS -- the architecture is explicit about this"."""
    target, dap, core = _halted_core()
    state = runcontrol.resume(dap, core)
    assert state.halted is False
    assert state.restart_confirmed is True
    # Clear-on-read: the confirmation consumed the bit.
    assert not target.mem.get(CORE0_DEBUG + r52.EDPRSR, 0) & r52.EDPRSR_SDR


def test_a_rehalt_on_a_hot_breakpoint_still_confirms_via_sdr() -> None:
    """The guide's §7.5 case: the core resumes onto an armed breakpoint and
    re-halts before any poll can see it running. SDR set proves the core left
    Debug state, so this is a working breakpoint, not a failed resume."""
    target, dap, core = _halted_core()
    pc = target.dlr[CORE0_DEBUG]
    runcontrol.set_breakpoint(dap, core, pc)
    state = runcontrol.resume(dap, core)
    assert state.rehalted is True
    assert state.restart_confirmed is True


# ---------------------------------------------------------------------------
# §5.7 / §5.8 -- comparator control words and initialisation
# ---------------------------------------------------------------------------


def test_breakpoint_control_words_match_the_guides_any_el_recipes() -> None:
    """§5.7's own recipe values: 0x21E7 for an A32 breakpoint and 0x2187 for a
    T32 instruction in the high halfword -- {HMC,SSC,PMC} = {1,00,11}, "the
    default choice for a halting debugger". The earlier words (0x1E7/0x187)
    matched EL1/EL0 only and would silently never fire in hypervisor code."""
    value, control = r52.encode_breakpoint(0x0040_0120, thumb=False)
    assert (value, control) == (0x0040_0120, 0x0000_21E7)
    value, control = r52.encode_breakpoint(0x0800_0232, thumb=True)
    assert (value, control) == (0x0800_0230, 0x0000_2187)
    value, control = r52.encode_breakpoint(0x0800_0230, thumb=True)
    assert (value, control) == (0x0800_0230, 0x0000_2067)


def test_watchpoint_control_words_match_the_guides_any_el_recipe() -> None:
    """§5.8's recipe: DBGWCR 0x21F7 for a 4-byte store watch at a
    doubleword-aligned address."""
    value, control = r52.encode_watchpoint(0x3000_1008, size=4, on="store")
    assert value == 0x3000_1008
    assert control == 0x0000_21F7


def test_sanitising_disables_every_comparator_and_reports_what_was_armed() -> None:
    """§5.7: every comparator field is UNKNOWN after cold reset, including the
    enable bit -- so a session initialises them, and says how many looked armed
    rather than discarding them silently."""
    target, dap, core = _halted_core()
    runcontrol.set_breakpoint(dap, core, 0x0040_0120)
    # A garbage comparator that happens to read as enabled:
    target.mem[CORE0_DEBUG + r52.dbgwcr(3)] = r52.DBGWCR_E | 0xDEAD_0000 & 0x0FFF_0000
    armed = runcontrol.sanitize_comparators(dap, core)
    assert armed == 2
    assert all(not e["enabled"] for e in runcontrol.list_breakpoints(dap, core))
    assert all(not e["enabled"] for e in runcontrol.list_watchpoints(dap, core))


# ---------------------------------------------------------------------------
# §5.3 -- EDWAR on a watchpoint halt
# ---------------------------------------------------------------------------


def test_a_watchpoint_halt_reports_the_data_address_from_edwar() -> None:
    """§5.3: "On a watchpoint halt, the data address is in EDWAR" (high word
    UNKNOWN, ignored)."""
    target, dap, core = _halted_core()
    target.mem[CORE0_DEBUG + r52.EDWAR_LO] = 0x3000_100A
    target._set_status(CORE0_DEBUG, r52.EDSCR_STATUS_WATCHPOINT)
    where = runcontrol.halt_location(dap, core)
    assert where["known"] is True
    assert where["data_address"] == "0x3000100A"
    assert "EDWAR" in str(where["source"])


# ---------------------------------------------------------------------------
# §5.4 / §5.5 -- writing the PC (DLR) and DSPSR
# ---------------------------------------------------------------------------


def test_the_pc_is_written_through_dlr_and_read_back() -> None:
    """§5.5: "On exit the core restarts from DLR; if you changed the PC, write
    DLR first". The Thumb bit is not part of the address."""
    target, dap, core = _halted_core()
    runcontrol.write_pc(dap, core, 0x3200_0101)
    assert runcontrol.read_pc(dap, core) == 0x3200_0100
    assert target.dlr[CORE0_DEBUG] == 0x3200_0100


def test_dspsr_round_trips_and_the_scratch_register_survives() -> None:
    target, dap, core = _halted_core()
    saved_r0 = target.regfile[CORE0_DEBUG][r52.SCRATCH_REG]
    runcontrol.write_dspsr(dap, core, 0x0000_01D3)
    assert runcontrol.read_dspsr(dap, core) == 0x0000_01D3
    assert target.regfile[CORE0_DEBUG][r52.SCRATCH_REG] == saved_r0


def test_the_dlr_write_encoding_is_the_mrc_encoding_with_the_direction_bit_clear() -> None:
    """MCR p15,3,r0,c4,c5,1 differs from the silicon-proven MRC read encoding
    (packed 0x0F35EE74) only in bit 20 of the first halfword."""
    assert r52.instr_dlr_to_reg(0) == 0x0F35_EE74
    assert r52.instr_reg_to_dlr(0) == 0x0F35_EE64
    assert r52.instr_reg_to_dspsr(0) == 0x0F15_EE64


# ---------------------------------------------------------------------------
# §5.9 -- synchronized restart
# ---------------------------------------------------------------------------


def test_a_group_restart_releases_every_halted_core_on_one_pulse() -> None:
    target = SyntheticTarget()
    dap = Dap(target)
    cores = runcontrol.discover(dap)
    for core in cores:
        runcontrol.halt(dap, core)
    results = runcontrol.resume_group(dap, cores)
    assert set(results) == {c.name for c in cores}
    assert all(state.halted is False for state in results.values())
    assert all(state.restart_confirmed for state in results.values())


def test_the_group_restart_closes_the_channel_gates_afterwards() -> None:
    """§5.9 opens CTIGATE for the restart channel so one pulse reaches every
    core; the tool's default posture is local-only, so the gates must close
    again or a later single-core resume would broadcast."""
    target = SyntheticTarget()
    dap = Dap(target)
    cores = runcontrol.discover(dap)
    for core in cores:
        runcontrol.halt(dap, core)
    runcontrol.resume_group(dap, cores)
    for core in cores:
        gate = target.mem.get(core.cti_base + r52.CTIGATE, 0)
        assert not gate & (1 << r52.CHANNEL_RESTART)


def test_preparing_the_cti_preserves_cross_trigger_routing() -> None:
    """CTIOUTEN writes are read-modify-write: an absolute write silently removed
    the cross-halt channel `core xtrigger on` had routed, disarming the group."""
    target = SyntheticTarget()
    dap = Dap(target)
    cores = runcontrol.discover(dap)
    runcontrol.set_cross_trigger(dap, cores, True)
    runcontrol.halt(dap, cores[0])
    outen0 = target.mem.get(
        cores[0].cti_base + r52.ctiouten(r52.TRIGOUT_DEBUG_REQUEST), 0
    )
    assert outen0 & (1 << r52.CHANNEL_CROSS_HALT)


# ---------------------------------------------------------------------------
# §5.10 / §3.6 -- R52 reset-and-halt
# ---------------------------------------------------------------------------


def test_reset_halt_catches_the_core_at_its_reset_vector() -> None:
    """The §5.10.2 hold flow: RSTREL clear -> functional reset -> arm RCE while
    held -> release -> reset catch, then the OS lock (re-set by the cold reset)
    is cleared so the catch status is readable. The fake models the *measured*
    behaviour: the reset wipes debug state and re-locks every core, whatever
    SDARSTMASK says."""
    target = SyntheticTarget()
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]
    state = runcontrol.reset_halt(dap, core)
    assert state.halted is True
    assert r52.edscr_status(state.edscr or 0) == r52.EDSCR_STATUS_RESET_CATCH
    assert runcontrol.read_pc(dap, core) == 0x0040_0000  # the fake's reset vector


def test_reset_halt_restores_the_release_bit_and_catch_bits() -> None:
    """A leaked RSTREL hold leaves a core permanently in reset, and a leaked
    EDECR.RCE halts some future reset unexpectedly -- both are put back
    whatever happens."""
    target = SyntheticTarget()
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]
    runcontrol.reset_halt(dap, core)
    rstctrl = target.ap_space.get(AP_SDA + lad.SDA_RSTCTRL, 0)
    assert rstctrl & (1 << 31)  # rtu0.0's release bit is back
    edecr = target.mem.get(CORE0_DEBUG + r52.EDECR, 0)
    assert not edecr & (r52.EDECR_RCE | r52.EDECR_OSUCE)


# ---------------------------------------------------------------------------
# Pitfall #3 -- the AXI-AP guard applies to step-over's instruction fetch
# ---------------------------------------------------------------------------


def test_reading_an_instruction_refuses_while_functional_reset_is_asserted() -> None:
    """RM Table 957 (guide pitfall #3): AXI-AP traffic during functional reset
    can leave peripherals unreachable until a destructive reset. `mem --ap axi`
    refuses; the step-over fetch must refuse identically, not bypass the guard."""
    target, dap, core = _halted_core()
    target.ap_space[AP_SDA + lad.SDA_SYSRSTS] = lad.SDA_SYSRSTS_SYSTEM_RESET
    with pytest.raises(UnsafeOperation, match="functional reset"):
        runcontrol.read_instruction(dap, core, 0x0040_0120, thumb=True)


# ---------------------------------------------------------------------------
# §6.4 -- the two-write M33 step
# ---------------------------------------------------------------------------


def _m33_core(target: SyntheticTarget):
    dap = Dap(target)
    cores = m33ctl.discover(dap)
    return dap, m33ctl.resolve("smu", cores)


def test_an_m33_step_with_masking_takes_two_writes() -> None:
    """§6.4: "changing C_MASKINTS in the same write that clears C_HALT is
    UNPREDICTABLE -- so 'set MASKINTS' and 'start the step' must be two writes:
    first 0xA05F000F (halted, set step+mask), then 0xA05F000D (release)"."""
    target = SyntheticTarget()
    dap, core = _m33_core(target)
    m33ctl.halt(dap, core)
    target.m33_dhcsr_writes.clear()
    m33ctl.step(dap, core, mask_interrupts=True)
    both = m33.DHCSR_C_DEBUGEN | m33.DHCSR_C_STEP | m33.DHCSR_C_MASKINTS
    assert target.m33_dhcsr_writes[0] == both | m33.DHCSR_C_HALT  # 0x…000F
    assert target.m33_dhcsr_writes[1] == both  # 0x…000D
    assert target.m33_unpredictable == []


def test_an_m33_step_suspends_armed_breakpoints_and_restores_them() -> None:
    """A comparator at the current PC matches the fetch before the instruction
    executes -- the same re-trigger the R52 path fixed. The fake refuses to
    advance the PC when an armed comparator matches, so a step that skipped the
    suspension would report the PC unmoved here."""
    target = SyntheticTarget()
    dap, core = _m33_core(target)
    m33ctl.halt(dap, core)
    pc = target.m33["pc"]
    entry = m33ctl.set_breakpoint(dap, core, pc)
    m33ctl.step(dap, core)
    assert target.m33["pc"] == pc + 2  # the step actually executed
    back = target.mem.get(m33.fp_comp(int(entry["index"])), 0)
    assert back & m33.FP_COMP_BE  # and the comparator came back armed


def test_an_m33_halt_reports_and_clears_its_cause() -> None:
    """§6.2: DFSR carries the halt cause and is write-1-to-clear; what is
    reported is cleared so the next halt's cause is not polluted."""
    target = SyntheticTarget()
    dap, core = _m33_core(target)
    m33ctl.halt(dap, core)
    cause = m33ctl.halt_cause(dap, core)
    assert "halt request or step" in cause["causes"]
    assert target.mem.get(m33.DFSR, 0) == 0


def test_an_m33_resume_onto_a_hot_breakpoint_reports_rehalted() -> None:
    target = SyntheticTarget()
    dap, core = _m33_core(target)
    m33ctl.halt(dap, core)
    m33ctl.set_breakpoint(dap, core, target.m33["pc"])
    dhcsr, rehalted = m33ctl.resume(dap, core)
    assert rehalted is True


# ---------------------------------------------------------------------------
# §6.7 -- M33 DWT watchpoints
# ---------------------------------------------------------------------------


def test_the_dwt_function_word_matches_the_guides_recipe() -> None:
    """§6.7's example: a word-sized write watchpoint is FUNCTION 0x815
    (DATAVSIZE=10, ACTION=01, MATCH=0101)."""
    comp, function = m33.encode_watchpoint(0x3000_1008, size=4, on="store")
    assert comp == 0x3000_1008
    assert function == 0x0000_0815


def test_a_misaligned_dwt_watchpoint_is_rejected_not_rounded() -> None:
    """§6.7: DWT_COMP must be naturally aligned to DATAVSIZE, else
    UNPREDICTABLE."""
    with pytest.raises(ValueError, match="aligned"):
        m33.encode_watchpoint(0x3000_1002, size=4)


def test_setting_an_m33_watchpoint_enables_trcena_first() -> None:
    """§6.7: "DEMCR.TRCENA must be 1 first -- it is the global DWT enable"."""
    target = SyntheticTarget()
    dap, core = _m33_core(target)
    entry = m33ctl.set_watchpoint(dap, core, 0x3000_1008, size=4, on="store")
    assert target.mem.get(m33.DEMCR, 0) & m33.DEMCR_TRCENA
    listed = m33ctl.list_watchpoints(dap, core)
    assert listed[int(entry["index"])]["enabled"] is True
    m33ctl.clear_watchpoint(dap, core, int(entry["index"]))
    assert all(not e["enabled"] for e in m33ctl.list_watchpoints(dap, core))


# ---------------------------------------------------------------------------
# §6.8 -- M33 reset-and-halt
# ---------------------------------------------------------------------------


def test_m33_reset_halt_catches_the_reset_handler_entry() -> None:
    target = SyntheticTarget()
    dap, core = _m33_core(target)
    dhcsr, pc = m33ctl.reset_halt(dap, core)
    assert dhcsr & m33.DHCSR_S_HALT
    assert pc == 0x2500_0000  # the fake's reset handler
    # The vector catch is disarmed again afterwards.
    assert not target.mem.get(m33.DEMCR, 0) & m33.DEMCR_VC_CORERESET
    cause = m33ctl.halt_cause(dap, core)
    assert "vector catch" in cause["causes"]

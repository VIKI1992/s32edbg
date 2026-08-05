"""Core discovery, identity checking and run-control policy. No hardware required.

What these tests can and cannot prove is worth being clear about. They prove the
*logic*: that discovery merges two sources without hiding their disagreement,
that a misidentified frame is refused rather than written to, and that the
advisories fire. They cannot prove the register offsets are right -- only silicon
can, which is what the identity anchors exist for.
"""

from __future__ import annotations

import pytest
from conftest import (
    AUTHSTATUS_ALL_GRANTED,
    CORE0_CTI,
    CORE0_DEBUG,
    CORE1_CTI,
    CORE1_DEBUG,
    MIDR_CORTEX_R52,
    SyntheticTarget,
    put_core_frame,
)

from s32edbg.dap import runcontrol
from s32edbg.dap.adiv6 import Dap
from s32edbg.data import coresight as cs
from s32edbg.data import r52debug as r52
from s32edbg.errors import UnsafeOperation


@pytest.fixture()
def dap() -> Dap:
    return Dap(SyntheticTarget())


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_the_rom_tables_two_cores_are_found(dap: Dap) -> None:
    cores = runcontrol.discover(dap)
    assert [c.debug_base for c in cores] == [CORE0_DEBUG, CORE1_DEBUG]
    assert all(c.found_in_rom for c in cores)


def test_each_core_gets_its_cti_by_stride_not_by_lookup(dap: Dap) -> None:
    """The CTI is not enumerated in the ROM table on this silicon, so its
    address is derived. Halting depends on it -- SEGGER's own device definitions
    require a CTI base alongside the core base on Armv8-A/R."""
    cores = runcontrol.discover(dap)
    assert cores[0].cti_base == CORE0_CTI
    assert cores[0].cti_base - cores[0].debug_base == r52.CTI_OFFSET_FROM_DEBUG


def test_a_frame_present_but_not_enumerated_is_found_and_flagged(dap: Dap) -> None:
    """The reason discovery probes as well as enumerating.

    The datasheet gives four Cortex-R52 cores per RTU; the ROM table on this
    part lists two frames per RTU. Rather than pick a side, a frame that answers
    but is not listed is reported with found_in_rom=False, so the discrepancy
    stays visible instead of being silently resolved either way.
    """
    target = SyntheticTarget()
    unlisted = 0x0061_0000  # RTU0 core 2 by stride, absent from the ROM table
    put_core_frame(target.mem, unlisted, aff0=2, aff1=0)

    cores = runcontrol.discover(Dap(target))
    found = {c.debug_base: c for c in cores}
    assert unlisted in found
    assert found[unlisted].found_in_rom is False
    assert found[CORE0_DEBUG].found_in_rom is True


def test_probing_can_be_turned_off(dap: Dap) -> None:
    """Probing costs five reads per candidate address, and on a target where
    unmapped reads hang rather than fault that is not free."""
    target = SyntheticTarget()
    put_core_frame(target.mem, 0x0061_0000, aff0=2)
    cores = runcontrol.discover(Dap(target), probe_unlisted=False)
    assert [c.debug_base for c in cores] == [CORE0_DEBUG, CORE1_DEBUG]


def test_an_address_with_no_component_is_not_reported_as_a_core(dap: Dap) -> None:
    """Unmapped APBIC2 space reads as zero here, which fails the CoreSight
    preamble check -- so the probe must not invent cores out of empty space."""
    assert runcontrol.looks_like_a_debug_frame(dap, 0x0071_0000) is False


def test_a_component_that_is_not_a_debug_frame_is_not_reported_as_a_core(dap: Dap) -> None:
    """The CTI has a valid CoreSight preamble but DEVTYPE 0x14, not 0x15.
    Matching on the preamble alone would turn every component into a core."""
    assert runcontrol.looks_like_a_debug_frame(dap, CORE0_CTI) is False


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_a_healthy_frame_passes_all_three_identity_anchors(dap: Dap) -> None:
    ident = runcontrol.identify(dap, CORE0_DEBUG)
    assert ident.is_cortex_r52 and ident.devtype_ok and ident.devarch_ok
    assert ident.anchors_agree is True
    assert ident.midr == MIDR_CORTEX_R52


def test_one_anchor_disagreeing_is_enough_to_fail_identity() -> None:
    """Any single anchor could pass by accident on a misaddressed frame. Three
    agreeing is what makes the offset map trustworthy enough to write to, so the
    check is conjunctive."""
    target = SyntheticTarget()
    target.mem[CORE0_DEBUG + r52.MIDR] = 0x410F_C241  # a Cortex-M4
    ident = runcontrol.identify(Dap(target), CORE0_DEBUG)
    assert ident.devtype_ok and ident.devarch_ok
    assert ident.anchors_agree is False


def test_affinity_names_the_core_rather_than_its_address(dap: Dap) -> None:
    """DEVAFF0 carries the associated core's MPIDR. Address arithmetic says
    where a frame is; affinity says which core it belongs to -- the thing that
    actually matters when the enumeration and the datasheet disagree."""
    cores = runcontrol.discover(dap)
    assert cores[0].name == "rtu0.0"
    assert cores[1].name == "rtu0.1"


def test_affinity_zero_is_a_real_identity_not_a_missing_one() -> None:
    """RTU0 core 0 legitimately has affinity 0.0, so an all-zero DEVAFF0 must
    not be read as "unknown" and demoted to an address-based name. This is the
    bug the affinity-naming test caught."""
    core = runcontrol.discover(Dap(SyntheticTarget()))[0]
    assert core.identity.affinity == (0, 0, 0)
    assert core.name == "rtu0.0"


def test_cores_that_all_claim_the_same_affinity_fall_back_to_addresses() -> None:
    """The remaining hazard: a part where DEVAFF0 is not implemented reads zero
    on every frame, so every core would claim to be rtu0.0 and --core would be
    ambiguous. The collision is detected and both names become addresses."""
    target = SyntheticTarget()
    put_core_frame(target.mem, CORE1_DEBUG, aff0=0, aff1=0)  # same affinity as core 0
    cores = runcontrol.discover(Dap(target))
    assert {c.name for c in cores} == {
        f"core@0x{CORE0_DEBUG:08X}",
        f"core@0x{CORE1_DEBUG:08X}",
    }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def test_a_running_core_reads_as_running(dap: Dap) -> None:
    assert runcontrol.discover(dap)[0].state.halted is False


def test_a_halted_core_reads_as_halted() -> None:
    target = SyntheticTarget()
    put_core_frame(
        target.mem,
        CORE0_DEBUG,
        edscr=0b000111,  # halted: breakpoint
        edprsr=r52.EDPRSR_PU | r52.EDPRSR_HALTED,
    )
    assert runcontrol.discover(Dap(target))[0].state.halted is True


def test_the_two_status_registers_disagreeing_reads_as_unknown_not_as_a_verdict() -> None:
    """EDSCR.STATUS and EDPRSR.HALTED are independent reports of the same fact.

    Disagreement means one of the two bit maps in data/r52debug.py is wrong --
    both are marked VERIFY. Picking one and reporting it as the answer would
    hide the very evidence that the map is wrong, so it reports unknown and
    warns.
    """
    target = SyntheticTarget()
    put_core_frame(
        target.mem,
        CORE0_DEBUG,
        edscr=0b000111,  # says halted
        edprsr=r52.EDPRSR_PU,  # says running
    )
    core = runcontrol.discover(Dap(target))[0]
    assert core.state.halted is None
    assert core.state.status_registers_disagree is True
    assert "disagree" in str(core.state.as_dict()["warning"])


def test_a_locked_core_reports_why_its_registers_are_unreadable() -> None:
    """Measured on silicon, and the reason `read_state` reads EDPRSR first.

    Three of the four cores on the bench board fault on EDSCR while MIDR, EDDFR
    and DBGAUTHSTATUS all read correctly -- the ID registers are not gated by
    the OS lock but the core-domain ones are. Without checking EDPRSR first the
    tool reported "registers disagree", which sends someone to debug the bit
    maps instead of the lock.
    """
    target = SyntheticTarget()
    target.lock_core(CORE0_DEBUG)
    core = runcontrol.discover(Dap(target))[0]
    assert core.state.unreachable_reason is not None
    assert "OS lock" in core.state.unreachable_reason
    assert core.state.halted is None
    assert core.state.status_registers_disagree is False, (
        "an unread EDSCR is an absence, not a disagreement -- reporting it as a "
        "bit-map problem would point at the wrong thing"
    )


def test_a_core_in_reset_says_so_rather_than_reporting_a_state() -> None:
    target = SyntheticTarget()
    put_core_frame(
        target.mem, CORE0_DEBUG, edprsr=r52.EDPRSR_PU | r52.EDPRSR_R | r52.EDPRSR_OSLK
    )
    decoded = r52.decode_edprsr(r52.EDPRSR_PU | r52.EDPRSR_R | r52.EDPRSR_OSLK)
    assert decoded["in_reset"] is True
    reachable, reason = r52.debug_registers_reachable(
        r52.EDPRSR_PU | r52.EDPRSR_R | r52.EDPRSR_OSLK
    )
    assert reachable is False and reason is not None


def test_a_powered_down_core_is_reported_as_such() -> None:
    reachable, reason = r52.debug_registers_reachable(0)
    assert reachable is False
    assert "power domain" in (reason or "")


def test_a_healthy_core_reports_its_registers_as_reachable() -> None:
    reachable, reason = r52.debug_registers_reachable(r52.EDPRSR_PU)
    assert reachable is True and reason is None


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def test_a_healthy_core_needs_no_warnings(dap: Dap) -> None:
    core = runcontrol.discover(dap)[0]
    assert runcontrol.preflight(core, write=True) == []


def test_run_control_against_a_misidentified_component_is_refused() -> None:
    """A wrong base address does not fault -- it reads and writes some other
    component. That is the failure this refusal exists to prevent, and it is the
    reason the identity check runs before anything is written."""
    target = SyntheticTarget()
    target.mem[CORE0_DEBUG + r52.MIDR] = 0x410F_C241
    core = runcontrol.discover(Dap(target))[0]
    with pytest.raises(UnsafeOperation, match="does not identify as a Cortex-R52"):
        runcontrol.preflight(core, write=False)


def test_writing_to_a_core_without_invasive_debug_is_refused() -> None:
    """The breakpoints guide section 6.1 gives the symptom: "comparator writes
    fail or read back as zero". The writes are accepted and do nothing, which is
    worse than being refused, because it looks like the breakpoint is armed."""
    target = SyntheticTarget()
    put_core_frame(target.mem, CORE0_DEBUG, authstatus=0xAA)  # implemented, all denied
    core = runcontrol.discover(Dap(target))[0]

    assert runcontrol.preflight(core, write=False), "reads stay allowed, with a warning"
    with pytest.raises(UnsafeOperation, match="invasive debug is not granted"):
        runcontrol.preflight(core, write=True)


def test_a_locked_debug_frame_is_reported_before_writes_are_attempted() -> None:
    target = SyntheticTarget()
    put_core_frame(target.mem, CORE0_DEBUG, lsr=cs.LSR_SLI | cs.LSR_SLK)
    core = runcontrol.discover(Dap(target))[0]
    assert any("software lock" in n for n in runcontrol.preflight_notes(core))


def test_an_implausible_comparator_count_warns_that_indices_are_untrustworthy() -> None:
    target = SyntheticTarget()
    put_core_frame(target.mem, CORE0_DEBUG, eddfr=0xFFFF_FFFF)
    core = runcontrol.discover(Dap(target))[0]
    assert any("not trustworthy" in n for n in runcontrol.preflight_notes(core))


def test_status_reports_problems_instead_of_refusing_to_look() -> None:
    """A `status` command that refused because something looked wrong would
    withhold exactly the information needed to diagnose it. preflight_notes
    reports; only preflight refuses."""
    target = SyntheticTarget()
    target.mem[CORE0_DEBUG + r52.MIDR] = 0x0000_0000
    core = runcontrol.discover(Dap(target))[0]
    notes = runcontrol.preflight_notes(core)
    assert any("does not identify as a Cortex-R52" in n for n in notes)


# ---------------------------------------------------------------------------
# Unlock, halt, resume
# ---------------------------------------------------------------------------


def test_unlocking_clears_the_os_lock_and_makes_edscr_readable() -> None:
    """The whole point of the unlock: while the OS lock is set, EDSCR faults.
    Three of the four cores on the bench board come up in exactly this state."""
    target = SyntheticTarget()
    target.lock_core(CORE0_DEBUG)
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]
    assert core.state.unreachable_reason is not None

    notes = runcontrol.unlock(dap, core)
    assert any("OS lock" in n for n in notes)
    assert runcontrol.read_state(dap, CORE0_DEBUG).edscr is not None


def test_unlocking_an_already_unlocked_core_does_nothing_and_says_so() -> None:
    dap = Dap(SyntheticTarget())
    core = runcontrol.discover(dap)[0]
    assert runcontrol.unlock(dap, core) == []


def test_the_os_double_lock_is_refused_rather_than_written_at() -> None:
    """Only the target's own software can clear the double lock. Writing OSLAR
    at it would be accepted and do nothing, which reads as a successful unlock."""
    target = SyntheticTarget()
    put_core_frame(target.mem, CORE0_DEBUG, edprsr=r52.EDPRSR_PU | r52.EDPRSR_DLK)
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]
    with pytest.raises(runcontrol.RunControlError, match="double lock"):
        runcontrol.unlock(dap, core)


def test_halting_stops_the_core_and_both_status_registers_agree() -> None:
    """Agreement is the pass criterion, not just EDPRSR.HALTED. The two are
    independent reports of the same fact, so agreement is what makes the answer
    trustworthy -- and disagreement means a bit map is wrong."""
    dap = Dap(SyntheticTarget())
    core = runcontrol.discover(dap)[0]

    state = runcontrol.halt(dap, core)
    assert state.halted is True
    assert state.status_registers_disagree is False
    assert r52.edscr_halted(state.edscr or 0)
    assert (state.edprsr or 0) & r52.EDPRSR_HALTED


def test_halting_enables_halting_debug_first() -> None:
    """Guide 5.6: with EDSCR.HDE clear the debug event is routed to self-hosted
    handling instead, so the core never stops and the halt request silently does
    nothing."""
    target = SyntheticTarget()
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]
    runcontrol.halt(dap, core)
    assert target.mem[CORE0_DEBUG + r52.EDSCR] & r52.EDSCR_HDE


def test_halting_routes_the_channel_to_the_debug_request_trigger() -> None:
    """Armv8 has no halt bit -- the request is CTI trigger output 0. If the
    routing is not programmed, the pulse goes nowhere."""
    target = SyntheticTarget()
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]
    runcontrol.halt(dap, core)

    outen = target.mem[CORE0_CTI + r52.ctiouten(r52.TRIGOUT_DEBUG_REQUEST)]
    assert outen == 1 << r52.CHANNEL_HALT
    assert target.mem[CORE0_CTI + r52.CTICONTROL] & r52.CTICONTROL_GLBEN


def test_a_single_core_halt_does_not_propagate_to_the_cross_trigger_matrix() -> None:
    """CTIGATE controls propagation out to the CTM. Leaving it enabled would make
    halting one core disturb every other core sharing the matrix, so cross-core
    halting stays opt-in rather than being a side effect."""
    target = SyntheticTarget()
    target.mem[CORE0_CTI + r52.CTIGATE] = 0xF
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]
    runcontrol.halt(dap, core)

    gate = target.mem[CORE0_CTI + r52.CTIGATE]
    assert not gate & (1 << r52.CHANNEL_HALT)
    assert not gate & (1 << r52.CHANNEL_RESTART)


def test_resuming_acknowledges_the_debug_request_before_restarting() -> None:
    """Without the acknowledge the request is still asserted at the trigger
    matrix, so the core re-enters Debug state the instant it leaves -- which
    looks exactly like a resume that did nothing."""
    target = SyntheticTarget()
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]
    runcontrol.halt(dap, core)
    runcontrol.resume(dap, core)

    assert target.mem[CORE0_CTI + r52.CTIINTACK] == 1 << r52.TRIGOUT_DEBUG_REQUEST


def test_halt_and_resume_round_trip() -> None:
    dap = Dap(SyntheticTarget())
    core = runcontrol.discover(dap)[0]

    assert runcontrol.halt(dap, core).halted is True
    assert runcontrol.resume(dap, core).halted is False
    assert runcontrol.halt(dap, core).halted is True


def test_a_halt_that_does_not_take_raises_rather_than_reporting_success() -> None:
    """A CTI whose pulse goes nowhere is the realistic failure -- wrong trigger
    assignment, or a core that is not listening. Reporting that as a halt would
    make every later register read look like a corrupted context."""
    target = SyntheticTarget()
    target.cti_enabled = False  # the pulse now has no effect, as a mis-wired CTI would
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]
    with pytest.raises(runcontrol.RunControlError, match="did not halt"):
        runcontrol.halt(dap, core, timeout_s=0.05)


# ---------------------------------------------------------------------------
# Registers, through the EDITR instruction-transfer path
# ---------------------------------------------------------------------------


def _halted_core(target: SyntheticTarget | None = None):
    target = target or SyntheticTarget()
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]
    runcontrol.halt(dap, core)
    return target, dap, core


def test_a_register_is_read_by_making_the_core_move_it_into_the_transfer_register() -> None:
    """There is no register file to address. The only way to see r5 is to have
    the core execute "move r5 into DBGDTRTX" and then read that."""
    target, dap, core = _halted_core()
    assert runcontrol.read_reg(dap, core, 5) == target.regfile[CORE0_DEBUG][5]


def test_a_register_write_goes_through_the_transfer_register_too() -> None:
    target, dap, core = _halted_core()
    runcontrol.write_reg(dap, core, 7, 0xDEAD_BEEF)
    assert target.regfile[CORE0_DEBUG][7] == 0xDEAD_BEEF
    assert runcontrol.read_reg(dap, core, 7) == 0xDEAD_BEEF


def test_the_round_trip_oracle_passes_on_a_correct_encoding() -> None:
    _target, dap, core = _halted_core()
    assert runcontrol.verify_register_path(dap, core) is True
    assert runcontrol.instruction_set(core) == "T32"


def test_the_oracle_calibrates_rather_than_assuming_the_instruction_set() -> None:
    """The bench core takes T32, but that is a property of its execution state,
    not of the tool. A core running A32 must work without a code change, so the
    oracle tries both and remembers which one round-tripped."""
    target = SyntheticTarget()
    target.accepts_t32 = False
    _t, dap, core = _halted_core(target)

    assert runcontrol.verify_register_path(dap, core) is True
    assert runcontrol.instruction_set(core) == "A32"
    assert runcontrol.read_reg(dap, core, 5) == target.regfile[CORE0_DEBUG][5]


def test_the_oracle_fails_when_neither_instruction_set_works() -> None:
    """If the encodings are simply wrong, no amount of trying both helps -- and
    reporting a register value anyway is the thing to avoid."""
    target = SyntheticTarget()
    _t, dap, core = _halted_core(target)
    # Break the transfer: the core accepts the instruction but moves nothing.
    target.regfile[CORE0_DEBUG] = {}
    target.mem[CORE0_DEBUG + r52.DBGDTRTX] = 0
    original = target._execute
    target._execute = lambda base, instr: None  # type: ignore[method-assign]
    try:
        assert runcontrol.verify_register_path(dap, core) is False
    finally:
        target._execute = original  # type: ignore[method-assign]


def test_the_round_trip_oracle_restores_the_scratch_register() -> None:
    """An oracle that corrupted the context it was checking would be worse than
    no oracle -- the values reported afterwards would be the probe's leftovers."""
    target, dap, core = _halted_core()
    before = target.regfile[CORE0_DEBUG][r52.SCRATCH_REG]
    runcontrol.verify_register_path(dap, core)
    assert target.regfile[CORE0_DEBUG][r52.SCRATCH_REG] == before


def test_a_wrong_instruction_encoding_is_caught_rather_than_returning_a_number() -> None:
    """The whole reason the oracle exists.

    These encodings were derived from the MCR/MRC layout, not read from an Arm
    document. A wrong one does not necessarily fault -- it can leave a
    plausible-looking value in the transfer register. Here the core rejects the
    instruction, which is what silicon does with something it cannot execute,
    and the failure must surface rather than being reported as a register value.
    """
    _target, dap, core = _halted_core()
    with pytest.raises(runcontrol.RunControlError, match="faulted in Debug state"):
        runcontrol.execute(dap, core, 0xDEAD_BEEF, timeout_s=0.05)


def test_registers_cannot_be_read_from_a_running_core() -> None:
    """While running, the core does not execute what is written to EDITR, so
    there is no transfer path at all. Saying so beats timing out."""
    dap = Dap(SyntheticTarget())
    core = runcontrol.discover(dap)[0]
    with pytest.raises(runcontrol.RunControlError, match="not halted"):
        runcontrol.read_registers(dap, core)


def test_reading_all_registers_reports_the_scratch_registers_original_value() -> None:
    """r0 carries every other value on its way out, so a naive read would report
    whatever the last transfer left in it rather than what the core had."""
    target, dap, core = _halted_core()
    expected = target.regfile[CORE0_DEBUG][r52.SCRATCH_REG]
    regs = runcontrol.read_registers(dap, core)
    assert regs.path_verified is True
    assert regs.values[f"r{r52.SCRATCH_REG}"] == expected
    assert regs.values["r5"] == target.regfile[CORE0_DEBUG][5]
    assert regs.values["dspsr"] == target.dspsr[CORE0_DEBUG]


def test_the_pc_comes_from_dlr_via_a_p15_read() -> None:
    """Cortex-R52 TRM §4.2.31: the AArch32 debug registers are accessed with
    coproc==0b1111, and DLR (CRn=c4, Op1=3, CRm=c5, Op2=1) holds "the value of
    the PC in Halting Debug state".

    They are coprocessor registers, not special registers -- which is why every
    MRS variant failed, including a 64-point sweep of the banked-register space
    that found the banked SP and LR but no PC. The PC was never in that space.
    """
    target, dap, core = _halted_core()
    regs = runcontrol.read_registers(dap, core)
    assert regs.pc == target.dlr[CORE0_DEBUG]
    assert "pc" not in regs.values, "the PC is not a general-purpose register"


def test_the_debug_saved_psr_decodes_to_a_mode_and_instruction_set() -> None:
    """Measured 0x0000003F on silicon: System mode, T bit set. The T bit is the
    same fact that decides which instruction encoding EDITR accepts."""
    decoded = r52.decode_psr(0x0000_003F)
    assert decoded["mode"] == "System"
    assert decoded["thumb"] is True


def test_edpcsr_is_not_offered_as_the_pc_when_it_reads_invalid() -> None:
    """EDPCSR is a profiling aid. Measured 0xFFFFFFFF -- the architectural
    "sample not valid" -- on a halted core, so offering it as a PC would be
    offering a made-up number."""
    target, dap, core = _halted_core()
    target.mem[CORE0_DEBUG + r52.EDPCSR_LO] = r52.EDPCSR_INVALID
    regs = runcontrol.read_registers(dap, core)
    assert regs.pc_sampled is None
    assert regs.pc is not None, "the real PC still comes from DLR"


# ---------------------------------------------------------------------------
# Breakpoints and watchpoints
# ---------------------------------------------------------------------------


def test_the_comparator_count_comes_from_eddfr_not_from_a_constant() -> None:
    """The guide says eight. A wrong EDDFR offset would also produce a number,
    and programming comparator 7 on a core that implements four writes into a
    reserved region."""
    dap = Dap(SyntheticTarget())
    core = runcontrol.discover(dap)[0]
    assert runcontrol.comparator_counts(core) == (8, 8)


def test_comparators_are_refused_when_eddfr_is_not_believable() -> None:
    target = SyntheticTarget()
    put_core_frame(target.mem, CORE0_DEBUG, eddfr=0xFFFF_FFFF)
    core = runcontrol.discover(Dap(target))[0]
    with pytest.raises(runcontrol.RunControlError, match="believable comparator count"):
        runcontrol.comparator_counts(core)


def test_a_breakpoint_lands_and_reads_back(dap: Dap) -> None:
    core = runcontrol.discover(dap)[0]
    entry = runcontrol.set_breakpoint(dap, core, 0x3210_4A80)
    assert entry["index"] == 0
    assert entry["enabled"] is True
    assert entry["address"] == "0x32104A80"


def test_the_thumb_bit_is_stripped_from_a_function_pointer(dap: Dap) -> None:
    """A T32 function pointer has bit 0 set to mark the instruction set. It is
    not part of the address, and a comparator holding it can never match --
    instruction addresses are at least halfword aligned. The breakpoints guide
    calls this out explicitly in §2."""
    core = runcontrol.discover(dap)[0]
    with_bit = runcontrol.set_breakpoint(dap, core, 0x3210_4A81, index=0)
    without = runcontrol.set_breakpoint(dap, core, 0x3210_4A80, index=1)
    assert with_bit["dbgbvr"] == without["dbgbvr"]


def test_a_thumb_breakpoint_selects_the_right_halfword(dap: Dap) -> None:
    """DBGBVR is word-aligned; which halfword within the word is selected by
    BAS. Selecting all four bytes would also cover the neighbouring
    instruction."""
    core = runcontrol.discover(dap)[0]
    low = runcontrol.set_breakpoint(dap, core, 0x3210_4A80, index=0)
    high = runcontrol.set_breakpoint(dap, core, 0x3210_4A82, index=1)
    assert low["bas"] == "0b0011"
    assert high["bas"] == "0b1100"
    assert low["dbgbvr"] == high["dbgbvr"], "same word, different halfword"
    assert high["address"] == "0x32104A82"


def test_an_arm_breakpoint_covers_the_whole_word(dap: Dap) -> None:
    core = runcontrol.discover(dap)[0]
    entry = runcontrol.set_breakpoint(dap, core, 0x3210_4A80, thumb=False)
    assert entry["bas"] == "0b1111"


def test_breakpoints_fill_free_slots_in_order(dap: Dap) -> None:
    core = runcontrol.discover(dap)[0]
    assert runcontrol.set_breakpoint(dap, core, 0x1000)["index"] == 0
    assert runcontrol.set_breakpoint(dap, core, 0x2000)["index"] == 1
    runcontrol.clear_breakpoint(dap, core, 0)
    assert runcontrol.set_breakpoint(dap, core, 0x3000)["index"] == 0


def test_running_out_of_comparators_says_so(dap: Dap) -> None:
    """The breakpoints guide lists a full comparator bank as the first cause to
    check for "cannot insert hardware breakpoint"."""
    core = runcontrol.discover(dap)[0]
    for n in range(8):
        runcontrol.set_breakpoint(dap, core, 0x1000 + n * 4)
    with pytest.raises(runcontrol.RunControlError, match="all 8 breakpoint comparators"):
        runcontrol.set_breakpoint(dap, core, 0x9000)


def test_a_comparator_index_beyond_what_the_core_implements_is_refused(dap: Dap) -> None:
    core = runcontrol.discover(dap)[0]
    with pytest.raises(runcontrol.RunControlError, match="implements 8"):
        runcontrol.set_breakpoint(dap, core, 0x1000, index=9)


def test_a_comparator_that_reads_back_wrong_is_reported_not_assumed(dap: Dap) -> None:
    """"Comparator writes fail or read back as zero" is the guide's §6.1
    signature for incomplete debug authorisation. Silently accepting it would
    leave someone waiting for a breakpoint that was never armed."""
    target = SyntheticTarget()
    d = Dap(target)
    core = runcontrol.discover(d)[0]
    target.faulting.add(CORE0_DEBUG + r52.dbgbvr(0))
    with pytest.raises(runcontrol.RunControlError, match="did not take|faulted"):
        runcontrol.set_breakpoint(d, core, 0x3210_4A80, index=0)


def test_clearing_a_breakpoint_blanks_the_value_too(dap: Dap) -> None:
    """Guide §5.9: clear the value as well as the enable bit before handing the
    target back. A disabled comparator still holding an address is confusing to
    whoever looks next."""
    target = SyntheticTarget()
    d = Dap(target)
    core = runcontrol.discover(d)[0]
    runcontrol.set_breakpoint(d, core, 0x3210_4A80, index=0)
    runcontrol.clear_breakpoint(d, core, 0)
    assert target.mem[CORE0_DEBUG + r52.dbgbvr(0)] == 0
    assert target.mem[CORE0_DEBUG + r52.dbgbcr(0)] == 0


def test_a_watchpoint_lands_with_the_requested_size_and_access(dap: Dap) -> None:
    core = runcontrol.discover(dap)[0]
    entry = runcontrol.set_watchpoint(dap, core, 0x3178_F000, size=4, on="store")
    assert entry["enabled"] is True
    assert entry["bytes"] == 4
    assert entry["on"] == "store"
    assert entry["address"] == "0x3178F000"


def test_a_watchpoint_straddling_the_comparator_boundary_is_refused(dap: Dap) -> None:
    """One comparator covers an aligned doubleword. Truncating silently would
    watch fewer bytes than asked for and never say so."""
    core = runcontrol.discover(dap)[0]
    with pytest.raises(ValueError, match="straddles"):
        runcontrol.set_watchpoint(dap, core, 0x3178_F006, size=4)


def test_breakpoint_and_watchpoint_banks_are_independent(dap: Dap) -> None:
    """Separate resources -- filling one must not consume the other."""
    core = runcontrol.discover(dap)[0]
    for n in range(8):
        runcontrol.set_breakpoint(dap, core, 0x1000 + n * 4)
    assert runcontrol.set_watchpoint(dap, core, 0x3178_F000)["index"] == 0


def test_listing_shows_free_slots_as_well_as_used_ones(dap: Dap) -> None:
    """"How many are left" is the question that matters when a breakpoint
    cannot be inserted."""
    core = runcontrol.discover(dap)[0]
    runcontrol.set_breakpoint(dap, core, 0x1000, index=3)
    entries = runcontrol.list_breakpoints(dap, core)
    assert len(entries) == 8
    assert entries[3]["enabled"] is True
    assert sum(1 for e in entries if e["enabled"]) == 1


# ---------------------------------------------------------------------------
# Stepping and cross-trigger
# ---------------------------------------------------------------------------


def test_stepping_sets_and_then_clears_the_halting_step_bit() -> None:
    """EDECR.SS left set turns the *next* ordinary resume into another single
    step, which looks exactly like a resume that does not work."""
    target, dap, core = _halted_core()
    runcontrol.step(dap, core)
    assert not target.mem[CORE0_DEBUG + r52.EDECR] & r52.EDECR_SS


def test_a_step_on_a_running_core_is_refused() -> None:
    dap = Dap(SyntheticTarget())
    core = runcontrol.discover(dap)[0]
    with pytest.raises(runcontrol.RunControlError, match="not halted"):
        runcontrol.step(dap, core)


def test_step_over_a_plain_instruction_is_an_ordinary_step() -> None:
    target, dap, core = _halted_core()
    target.dlr[CORE0_DEBUG] = 0x3210_4A80
    target.mem[0x3210_4A80] = 0x0000_BF00  # NOP, 16-bit
    _state, info = runcontrol.step_over(dap, core)
    assert info["is_call"] is False
    assert info["action"] == "single step"


def test_step_over_a_call_breakpoints_the_return_address() -> None:
    """The point of step-over: run the callee rather than stepping through it.
    The return address is PC + the instruction's width, which is why the decoder
    has to get the width right."""
    target, dap, core = _halted_core()
    target.dlr[CORE0_DEBUG] = 0x3210_4A80
    target.mem[0x3210_4A80] = (0xF800 << 16) | 0xF000  # BL, 32-bit
    _state, info = runcontrol.step_over(dap, core)
    assert info["is_call"] is True
    assert info["return_address"] == "0x32104A84"


def test_step_over_removes_its_temporary_breakpoint() -> None:
    """A leaked comparator consumes one of the eight and fires later at a
    confusing moment."""
    target, dap, core = _halted_core()
    target.dlr[CORE0_DEBUG] = 0x3210_4A80
    target.mem[0x3210_4A80] = (0xF800 << 16) | 0xF000
    runcontrol.step_over(dap, core)
    assert all(not e["enabled"] for e in runcontrol.list_breakpoints(dap, core))


def test_step_over_removes_its_breakpoint_even_when_the_call_never_returns() -> None:
    target, dap, core = _halted_core()
    target.dlr[CORE0_DEBUG] = 0x3210_4A80
    target.mem[0x3210_4A80] = (0xF800 << 16) | 0xF000
    target.cti_enabled = False  # the restart pulse goes nowhere, so it never returns
    with pytest.raises(runcontrol.RunControlError, match="did not return"):
        runcontrol.step_over(dap, core, timeout_s=0.05)
    assert all(not e["enabled"] for e in runcontrol.list_breakpoints(dap, core))


def test_an_instruction_in_the_high_halfword_is_fetched_correctly() -> None:
    """T32 instructions are halfword-aligned, so one can begin in the *high*
    half of a word -- and then its second halfword is the low half of the next
    word.

    Reading a 32-bit word at the instruction address and slicing it as
    (low, high) is wrong for those, and wrong only on odd halfwords, which is why
    it survived the offline tests. Caught on silicon: an instruction at
    0x3241F522 decoded 2 bytes wide when stepping proved it 4.
    """
    target, dap, core = _halted_core()
    #   word at 0x1000: low half is some other instruction, high half is the
    #   first halfword of a 32-bit BL; its second halfword lives at 0x1004.
    target.mem[0x1000] = (0xF000 << 16) | 0xBF00
    target.mem[0x1004] = 0x0000_F800

    instruction = runcontrol.read_instruction(dap, core, 0x1002, thumb=True)
    assert instruction.width == 4, "the high-halfword instruction is 32 bits wide"
    assert instruction.is_call is True
    assert instruction.next_address == 0x1006


def test_the_instruction_set_is_read_from_dspsr_not_assumed() -> None:
    """Measured on silicon: an interrupt took the core from a T32 idle loop into
    an A32 exception handler. Assuming Thumb decoded 4-byte A32 instructions as
    2-byte and made every correct step look like a decoder error."""
    target, dap, core = _halted_core()
    target.dlr[CORE0_DEBUG] = 0x2000
    target.dspsr[CORE0_DEBUG] = 0x0000_001F  # System mode, T clear -> A32
    target.mem[0x2000] = 0xE320_F000  # A32 NOP

    _state, info = runcontrol.step_over(dap, core)
    assert info["width"] == 4, "an A32 instruction is always four bytes"


def test_an_unreadable_instruction_is_reported_rather_than_guessed() -> None:
    """Step-over cannot work without seeing the instruction. Assuming a width
    would put the breakpoint at the wrong address, where it silently never
    fires."""
    target, dap, core = _halted_core()
    target.dlr[CORE0_DEBUG] = 0x3210_4A80
    target.faulting.add(0x3210_4A80)
    with pytest.raises(runcontrol.RunControlError, match="could not read the instruction"):
        runcontrol.step_over(dap, core)


def test_cross_trigger_off_by_default_keeps_a_halt_to_one_core() -> None:
    """CTIGATE gates propagation to the matrix. Leaving the halt channel open
    would make every single-core halt stop the application core too."""
    target, dap, core = _halted_core()
    gate = target.mem[CORE0_CTI + r52.CTIGATE]
    assert not gate & (1 << r52.CHANNEL_HALT)


def test_enabling_cross_trigger_opens_the_gate_on_every_core() -> None:
    target = SyntheticTarget()
    dap = Dap(target)
    cores = runcontrol.discover(dap)
    runcontrol.set_cross_trigger(dap, cores, True)

    for cti in (CORE0_CTI, CORE1_CTI):
        mask = 1 << r52.CHANNEL_CROSS_HALT
        assert target.mem[cti + r52.CTIGATE] & mask, "channel must reach the matrix"
        assert target.mem[cti + r52.ctiinen(r52.TRIGIN_CROSS_HALT)] & mask
        assert target.mem[cti + r52.ctiouten(r52.TRIGOUT_DEBUG_REQUEST)] & mask


def test_disabling_cross_trigger_puts_it_back() -> None:
    target = SyntheticTarget()
    dap = Dap(target)
    cores = runcontrol.discover(dap)
    runcontrol.set_cross_trigger(dap, cores, True)
    runcontrol.set_cross_trigger(dap, cores, False)

    mask = 1 << r52.CHANNEL_CROSS_HALT
    for cti in (CORE0_CTI, CORE1_CTI):
        assert not target.mem[cti + r52.CTIGATE] & mask


def test_cross_trigger_uses_a_channel_of_its_own() -> None:
    """Sharing the single-core halt channel would make an ordinary halt
    propagate the moment cross-trigger was ever switched on."""
    assert r52.CHANNEL_CROSS_HALT not in (r52.CHANNEL_HALT, r52.CHANNEL_RESTART)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_a_core_can_be_selected_by_index_name_or_address(dap: Dap) -> None:
    cores = runcontrol.discover(dap)
    assert runcontrol.resolve("1", cores).debug_base == CORE1_DEBUG
    assert runcontrol.resolve("rtu0.1", cores).debug_base == CORE1_DEBUG
    assert runcontrol.resolve(hex(CORE1_DEBUG), cores).debug_base == CORE1_DEBUG


def test_an_unknown_core_is_rejected_and_lists_the_real_ones(dap: Dap) -> None:
    cores = runcontrol.discover(dap)
    with pytest.raises(ValueError, match="rtu0.0"):
        runcontrol.resolve("nonsense", cores)
    with pytest.raises(ValueError, match="rtu0.0"):
        runcontrol.resolve("7", cores)


def test_stepping_masks_interrupts_by_default() -> None:
    """The bug behind "stepping did not seem to work".

    Measured on silicon: a step from a T32 idle loop at 0x321D2EE6 landed at
    0x321DD018 with status "halting step, no syndrome" -- the core executed one
    instruction, immediately took a pending interrupt, and stopped in the vector
    table. On a system with a periodic tick that happens on essentially every
    step, so stepping appears to teleport into handlers and never advance
    through the code under test.

    The M33 path masked interrupts from the start; the R52 path did not.
    """
    target, dap, core = _halted_core()
    runcontrol.step(dap, core)
    # INTdis is restored afterwards, so the evidence is that it was written at
    # all -- a resume left with interrupts masked would be its own bug.
    writes = [a for a, _ in target.edscr_writes]
    assert any(
        v & r52.EDSCR_INTDIS_MASK for _, v in target.edscr_writes
    ), "EDSCR.INTdis was never set during the step"
    assert writes


def test_stepping_restores_the_interrupt_mask_afterwards() -> None:
    """Leaving interrupts masked would make the next ordinary resume run with
    them off, and the system would quietly stop servicing anything."""
    target, dap, core = _halted_core()
    runcontrol.step(dap, core)
    final = target.mem[CORE0_DEBUG + r52.EDSCR]
    assert not final & r52.EDSCR_INTDIS_MASK


def test_interrupt_masking_can_be_turned_off() -> None:
    """Sometimes you want to step *into* the handler on purpose."""
    target, dap, core = _halted_core()
    runcontrol.step(dap, core, mask_interrupts=False)
    assert not any(v & r52.EDSCR_INTDIS_MASK for _, v in target.edscr_writes)


def test_the_intdis_helper_preserves_the_rest_of_edscr() -> None:
    """EDSCR carries live status in the same word, so a blind write would
    clobber it."""
    edscr = 0b000111 | r52.EDSCR_HDE | r52.EDSCR_ITE
    masked = r52.edscr_with_intdis(edscr, True)
    assert masked & r52.EDSCR_INTDIS_MASK
    assert masked & r52.EDSCR_HDE and masked & r52.EDSCR_ITE
    assert r52.edscr_status(masked) == 0b000111
    assert r52.edscr_with_intdis(masked, False) == edscr


def test_stepping_suspends_armed_breakpoints_for_the_duration() -> None:
    """The bug behind "step 1  0x321D2EE6 -> 0x321D2EE6".

    A comparator armed at the current PC re-triggers the instant the core is
    restarted, before any instruction executes -- the core comes back halted
    with a breakpoint status and the PC unmoved. The breakpoints guide gives the
    remedy in section 5.8: disable the comparator, step, re-enable it.
    """
    target, dap, core = _halted_core()
    runcontrol.set_breakpoint(dap, core, 0x3210_4A80, index=0)
    before = target.mem[CORE0_DEBUG + r52.dbgbcr(0)]

    runcontrol.step(dap, core)

    disabled = [
        v for a, v in target.dbgbcr_writes if a == CORE0_DEBUG + r52.dbgbcr(0)
        and not v & r52.DBGBCR_E
    ]
    assert disabled, "the armed comparator was never suspended for the step"
    assert target.mem[CORE0_DEBUG + r52.dbgbcr(0)] == before, "and it must be restored"


def test_a_suspended_breakpoint_is_restored_even_when_the_step_fails() -> None:
    """Otherwise a failed step silently loses the breakpoints the operator
    armed, and the next resume runs straight past them."""
    target, dap, core = _halted_core()
    runcontrol.set_breakpoint(dap, core, 0x3210_4A80, index=0)
    before = target.mem[CORE0_DEBUG + r52.dbgbcr(0)]
    target.cti_enabled = False  # the restart goes nowhere, so the step times out

    with pytest.raises(runcontrol.RunControlError):
        runcontrol.step(dap, core, timeout_s=0.05)
    assert target.mem[CORE0_DEBUG + r52.dbgbcr(0)] == before


def test_halting_acknowledges_the_debug_request() -> None:
    """The bug behind "stepping did not seem to work".

    CTIAPPPULSE raises the debug request but nothing lowers it. Left asserted,
    the core is released and re-halted repeatedly -- measured on silicon, the PC
    moved between one CLI command and the next, so a step appeared to do nothing
    and `bp clear` appeared to resume execution. The lingering request also broke
    the register path: reading the PC straight after a halt failed and worked
    immediately after this acknowledgement.
    """
    target, dap, core = _halted_core()
    assert target.mem.get(CORE0_CTI + r52.CTIINTACK) == 1 << r52.TRIGOUT_DEBUG_REQUEST
    assert target.mem.get(CORE0_CTI + r52.CTIAPPCLEAR) == 1 << r52.CHANNEL_HALT


def test_the_halt_is_still_reported_as_halted_after_acknowledging() -> None:
    """Acknowledging lowers the *request*; the core stays in Debug state. If it
    did not, this would trade one bug for a worse one."""
    _target, dap, core = _halted_core()
    assert runcontrol.read_state(dap, core.debug_base).halted is True


def test_arming_a_breakpoint_enables_halting_debug() -> None:
    """The "breakpoint is accepted but never hits" failure (guide §5.2 item 3).

    With EDSCR.HDE clear the debug event goes to self-hosted handling instead,
    so the comparator reads back perfectly and simply never fires. Measured: a
    breakpoint armed on a freshly unlocked core never fired, because only
    `halt()` had been setting HDE.
    """
    target = SyntheticTarget()
    target.mem[CORE0_DEBUG + r52.EDSCR] = r52.EDSCR_STATUS_RUNNING  # HDE clear
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]

    runcontrol.set_breakpoint(dap, core, 0x3210_4A80)
    assert target.mem[CORE0_DEBUG + r52.EDSCR] & r52.EDSCR_HDE


def test_a_group_restart_under_an_armed_group_needs_the_teardown() -> None:
    """The guide gap made executable: with the §5.9 group armed, the halted
    members' DBGTRIGGER levels re-drive the halt channel straight through the
    acknowledge, so a naive group restart is swallowed. `unarm_cross_halt`
    (mask CTIINEN ch2 -> restart -> re-arm) is the beyond-guide teardown that
    makes it work -- and the group is armed again afterwards."""
    target = SyntheticTarget()
    dap = Dap(target)
    cores = runcontrol.discover(dap)
    runcontrol.set_cross_trigger(dap, cores, True)
    runcontrol.halt(dap, cores[0])  # the group stops both

    with pytest.raises(runcontrol.RunControlError):
        runcontrol.resume_group(dap, cores, timeout_s=0.2)  # naive: swallowed

    results = runcontrol.resume_group(
        dap, cores, timeout_s=0.5, unarm_cross_halt=True
    )
    assert set(results) == {"rtu0.0", "rtu0.1"}
    ch2 = 1 << r52.CHANNEL_CROSS_HALT
    assert target.mem[CORE0_CTI + r52.ctiinen(r52.TRIGIN_CROSS_HALT)] & ch2
    assert runcontrol.read_state(dap, cores[0].debug_base).halted is False
    assert runcontrol.read_state(dap, cores[1].debug_base).halted is False


def test_an_armed_cross_halt_group_propagates_a_halt_in_the_model() -> None:
    """The §5.9 input direction, modelled: with the group armed on both cores,
    halting one drives channel 2 and the other enters Debug state with the
    external-debug-request status -- the propagation that has never yet fired
    on silicon, now pinned offline ahead of the bench."""
    target = SyntheticTarget()
    dap = Dap(target)
    cores = runcontrol.discover(dap)
    runcontrol.set_cross_trigger(dap, cores, True)

    runcontrol.halt(dap, cores[0])

    assert runcontrol.read_state(dap, cores[0].debug_base).halted is True
    assert runcontrol.read_state(dap, cores[1].debug_base).halted is True
    assert target.mem[CORE1_CTI + r52.CTITRIGOUTSTATUS] & (1 << r52.TRIGOUT_DEBUG_REQUEST)


def test_arming_a_watchpoint_enables_halting_debug() -> None:
    """Watchpoint parity with the breakpoint fix above (guide §5.2 item 3):
    with HDE clear the comparator arms, reads back, and never halts anything.
    The breakpoint path learned this at the bench; the watchpoint path gets
    the same call here rather than the same bench day."""
    target = SyntheticTarget()
    target.mem[CORE0_DEBUG + r52.EDSCR] = r52.EDSCR_STATUS_RUNNING  # HDE clear
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]

    runcontrol.set_watchpoint(dap, core, 0x3178_0040)
    assert target.mem[CORE0_DEBUG + r52.EDSCR] & r52.EDSCR_HDE


def test_an_all_invalid_sample_says_why_rather_than_reporting_nothing() -> None:
    """"0 distinct addresses" distinguishes none of halted, locked, or not
    executing -- and those need different responses."""
    target = SyntheticTarget()
    target.lock_core(CORE0_DEBUG)
    dap = Dap(target)
    core = runcontrol.discover(dap)[0]
    target.faulting.add(CORE0_DEBUG + r52.EDPCSR_LO)

    result = runcontrol.sample_pc(dap, core, count=5)
    assert result["invalid"] == 5
    assert "no PC was captured" in str(result["note"])
    assert "OS lock" in str(result["note"])

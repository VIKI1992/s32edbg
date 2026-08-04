"""The `load` flow: bring-up tables, SRAMC edge-trigger, segment writing, PC.

Driven against the SyntheticTarget, whose load-flow model reproduces the three
behaviours the flow lives or dies by: the MC_ME key handshake commits pending
updates, RAMCR.INIT is EDGE-triggered (a 1->1 write is a silent no-op), and a
core whose RGM reset lifts while its MDM EDBGREQ is latched halts straight out
of reset -- the park mechanism.
"""

from __future__ import annotations

import struct

import pytest
from conftest import CORE0_DEBUG, CORE1_DEBUG, SyntheticTarget

from s32edbg import elf
from s32edbg.dap import loader, memaccess, runcontrol
from s32edbg.dap.adiv6 import Dap
from s32edbg.data import ladder1 as lad
from s32edbg.data import r52debug as r52
from s32edbg.data import rtuboot
from s32edbg.data.expected_apmap import AP_AXI, AP_MDM
from s32edbg.dap import gates
from s32edbg.errors import LoadError


# ---------------------------------------------------------------------------
# Synthetic ELF helpers (LMAs inside the loadable windows)
# ---------------------------------------------------------------------------


def make_image(
    segments: list[tuple[int, bytes]], entry: int = 0x3210_0021
) -> tuple[elf.Image, bytes]:
    """Build a parsed Image + blob with the given (paddr, data) segments."""
    phoff = 52
    phnum = len(segments)
    data_off = phoff + phnum * elf.PHDR_SIZE
    ident = elf.ELF_MAGIC + bytes([1, 1, 1, 0]) + b"\x00" * 8
    ehdr = struct.pack(
        "<HHIIIIIHHHHHH", 2, elf.EM_ARM, 1, entry, phoff, 0, 0, 52,
        elf.PHDR_SIZE, phnum, 0, 0, 0,
    )
    phdrs = b""
    blobs = b""
    off = data_off
    for paddr, data in segments:
        phdrs += struct.pack(
            "<IIIIIIII", elf.PT_LOAD, off, paddr, paddr, len(data), len(data), 5, 4
        )
        blobs += data
        off += len(data)
    blob = ident + ehdr + phdrs + blobs
    return elf.parse(blob, name="synthetic.elf"), blob


def make_hooks(dap: Dap) -> tuple[loader.LoadHooks, list[str]]:
    notes: list[str] = []

    def attach(core: runcontrol.Core) -> None:
        runcontrol.preflight(core, write=True)
        if core.state.unreachable_reason:
            runcontrol.unlock(dap, core)
            core.state = runcontrol.read_state(dap, core.debug_base)

    return (
        loader.LoadHooks(
            note=notes.append,
            attach_core=attach,
            discover_cores=lambda: runcontrol.discover(dap),
        ),
        notes,
    )


def prepared_dap(device: SyntheticTarget) -> Dap:
    dap = Dap(device)
    dap.power_up()
    gates.apply_ladder1(dap)
    return dap


# ---------------------------------------------------------------------------
# The init-action executor
# ---------------------------------------------------------------------------


def test_the_executor_runs_write_rmw_poll_and_sleep() -> None:
    device = SyntheticTarget()
    dap = prepared_dap(device)
    device.mem[0x4190_0300] = 0xFF00
    actions = (
        rtuboot.InitAction("write32", addr=0x4186_005C, value=0x2),
        rtuboot.InitAction("rmw", addr=0x4190_0300, mask=0x1, value=0x1),
        rtuboot.InitAction("poll", addr=0x4190_0300, mask=0x1, value=0x1, timeout_ms=10),
        rtuboot.InitAction("sleep", timeout_ms=1),
    )
    loader.run_init_actions(dap, actions)
    assert device.mem[0x4186_005C] == 0x2
    assert device.mem[0x4190_0300] == 0xFF01, "rmw must preserve unmasked bits"


def test_a_poll_that_never_matches_is_a_load_error_naming_the_register() -> None:
    device = SyntheticTarget()
    dap = prepared_dap(device)
    action = rtuboot.InitAction(
        "poll", addr=0x4190_0308, mask=0x1, value=0x1, timeout_ms=1, label="PCS set"
    )
    with pytest.raises(LoadError, match="PCS set"):
        loader._poll_word(dap, AP_AXI, action)


# ---------------------------------------------------------------------------
# Segment planning
# ---------------------------------------------------------------------------


def test_a_ragged_tail_is_padded_with_zeros_into_one_final_word() -> None:
    image, blob = make_image([(0x3178_0000, b"\x11\x22\x33\x44\x55\x66")])
    plans = loader.plan_segment_words(image, blob)
    assert plans[0].words == (0x4433_2211, 0x0000_6655)


def test_an_unaligned_lma_is_refused() -> None:
    image, blob = make_image([(0x3178_0002, b"\x11\x22\x33\x44")])
    with pytest.raises(LoadError, match="unaligned LMA"):
        loader.plan_segment_words(image, blob)


def test_a_segment_claiming_bytes_in_anothers_padded_word_is_refused() -> None:
    """Cannot come out of elf.parse (overlap is rejected there, and an aligned
    follower can never start inside the padded word), but the planner refuses
    rather than assumes -- constructed directly to prove the guard."""
    a = elf.Segment(index=0, paddr=0x3178_0000, vaddr=0, offset=0, filesz=6, memsz=6)
    b = elf.Segment(index=1, paddr=0x3178_0006, vaddr=0, offset=6, filesz=2, memsz=2)
    image = elf.Image(name="x", entry=0x3210_0021, segments=(a, b), total_filesz=8)
    with pytest.raises(LoadError, match="padded final word"):
        loader.plan_segment_words(image, b"\x00" * 8)


def test_an_image_outside_every_loadable_window_is_refused() -> None:
    image, _blob = make_image([(0x0040_0000, b"\xde\xad\xbe\xef")])  # flash-ish
    with pytest.raises(LoadError, match="outside every loadable RAM window"):
        loader.check_windows(image)


# ---------------------------------------------------------------------------
# SRAMC: the edge-trigger root cause, reproduced and fixed
# ---------------------------------------------------------------------------


def test_writing_init_while_already_set_is_a_silent_no_op_in_the_model() -> None:
    """Prove the fake models the silicon defect the v2 ladder exists for."""
    device = SyntheticTarget()
    dap = prepared_dap(device)
    device.mem[0x3178_E1A0] = 0xFFFF_0000  # virgin-ECC garbage
    device.mem[0x761D_0000] = 0x1  # INIT already set (NXP scrub left it)
    memaccess.write_word(dap, AP_AXI, 0x761D_0004, 0x3178_0000)
    memaccess.write_word(dap, AP_AXI, 0x761D_0008, 0x317B_FFFF)
    memaccess.write_word(dap, AP_AXI, 0x761D_0000, 0x101)  # 1 -> 1: no edge
    assert device.mem[0x3178_E1A0] == 0xFFFF_0000, "scrub must NOT have run"


def test_the_v2_ladder_produces_the_edge_and_the_witnesses_read_zero() -> None:
    device = SyntheticTarget()
    dap = prepared_dap(device)
    for w in rtuboot.SRAMC_V2_VERIFY_ADDRS:
        device.mem[w] = 0xFFFF_0000
    for base in (0x761D_0000, 0x761E_0000, 0x761F_0000):
        device.mem[base] = 0x1  # what the generic scrub leaves behind
    loader.sramc_init_v2(dap)
    for w in rtuboot.SRAMC_V2_VERIFY_ADDRS:
        assert device.mem[w] == 0


def test_a_ladder_whose_init_did_not_run_fails_loudly() -> None:
    """Kill the edge by pre-setting INIT *and* making the 0-write ineffective:
    model a controller that ignores RAMCR=0 (stuck INIT). The witness check is
    what must catch it."""
    device = SyntheticTarget()
    dap = prepared_dap(device)
    device.mem[rtuboot.SRAMC_V2_VERIFY_ADDRS[0]] = 0xFFFF_0000

    real_on = device.on_mem_write

    def stuck_on(addr: int, value: int) -> bool | None:
        if addr == 0x761D_0000:
            device.mem[addr] = 0x1  # RAMCR stuck at INIT=1: no 0-write, no edge
            return True  # claim the write so the raw store is skipped
        return real_on(addr, value)

    device.on_mem_write = stuck_on  # type: ignore[method-assign]
    # IDONE reads stale-set, exactly as the root-caused wedge presented:
    # perfect-looking registers, no init actually run.
    device.mem[0x761D_000C] = 0x1
    with pytest.raises(LoadError, match="SRAMC init verify FAILED"):
        loader.sramc_init_v2(dap)


# ---------------------------------------------------------------------------
# wait_halted
# ---------------------------------------------------------------------------


def test_wait_halted_times_out_with_the_edprsr_value() -> None:
    device = SyntheticTarget()
    dap = prepared_dap(device)
    cores = runcontrol.discover(dap)
    core0 = runcontrol.resolve("rtu0.0", cores)
    with pytest.raises(LoadError, match="did not enter Debug state"):
        loader.wait_halted(dap, core0, timeout_s=0.05)


# ---------------------------------------------------------------------------
# The whole flow
# ---------------------------------------------------------------------------


def run_full_load(device: SyntheticTarget, **kwargs):
    dap = prepared_dap(device)
    hooks, notes = make_hooks(dap)
    image, blob = make_image(
        [
            (0x3210_0020, b"\x21\x00\x00\xea\x55\xaa\x55\xaa"),  # holds the entry
            (0x3178_0000, b"\x11\x22\x33\x44\x55\x66"),  # ragged tail
        ]
    )
    report = loader.load_image(
        dap,
        device,
        image,
        blob,
        hooks,
        reset=loader.SdaReset(dap),
        settle_timeout_s=0.5,
        **kwargs,
    )
    return dap, device, report, notes


def test_the_full_flow_lands_the_image_and_parks_core0_at_the_entry() -> None:
    _dap, device, report, _notes = run_full_load(device := SyntheticTarget())

    # The image is in memory, ragged tail zero-padded.
    assert device.mem[0x3210_0020] == 0xEA00_0021
    assert device.mem[0x3210_0024] == 0xAA55_AA55
    assert device.mem[0x3178_0000] == 0x4433_2211
    assert device.mem[0x3178_0004] == 0x0000_6655

    # Core0: halted, PC at the entry (Thumb bit stripped), DSPSR.T set.
    assert device.mem[CORE0_DEBUG + r52.EDPRSR] & r52.EDPRSR_HALTED
    assert device.dlr[CORE0_DEBUG] == 0x3210_0020
    assert device.dspsr[CORE0_DEBUG] & r52.PSR_T

    # Every R52 latch is dropped once core0 is pinned: a stale request would
    # halt rtu0.1 the moment the application's own bring-up releases it
    # (measured on target), and core0 would hang at the OS boot barrier.
    assert not device.ap_space[AP_MDM + lad.MDM_CONTROL2] & 0x000F_0000
    assert not device.ap_space[AP_MDM + lad.MDM_CONTROL3] & 0x000F_0000

    # The report tells the story.
    assert report.pc == 0x3210_0020
    assert report.total_bytes == 14
    assert report.fast_path == "unused"  # SyntheticTarget cannot block-write
    assert all(s.path == "adiv6" for s in report.segments)
    assert report.core1 == "leave"

    # Core1 was left entirely alone: still held in reset by RGM.
    assert device.mem[rtuboot.RGM_PRST0_1] & 0x4


def test_the_sramc_witnesses_gate_the_whole_flow() -> None:
    device = SyntheticTarget()
    # Poison a witness and break D1's edge so the scrub never zeroes it.
    device.mem[0x317C_0010] = 0xFFFF_0000
    real = device._loadflow_write

    def no_edge(addr: int, value: int) -> None:
        if addr == 0x761E_0000:
            device.mem[addr] = 0x1  # INIT stuck set; IDONE stays stale
            device.mem[addr + 0xC] = 0x1
            return
        real(addr, value)

    device._loadflow_write = no_edge  # type: ignore[method-assign]
    with pytest.raises(LoadError, match="SRAMC init verify FAILED"):
        run_full_load(device)


def test_core1_park_leaves_both_cores_halted_at_the_entry() -> None:
    _dap, device, report, _notes = run_full_load(
        device := SyntheticTarget(), core1="park"
    )
    assert device.mem[CORE1_DEBUG + r52.EDPRSR] & r52.EDPRSR_HALTED
    assert device.dlr[CORE1_DEBUG] == 0x3210_0020
    assert not device.ap_space[AP_MDM + lad.MDM_CONTROL2] & lad.edbgreq_bit(1)
    assert report.core1 == "park"


def test_core1_release_resumes_it_after_setting_the_pc() -> None:
    _dap, device, _report, _notes = run_full_load(
        device := SyntheticTarget(), core1="release"
    )
    assert not device.mem[CORE1_DEBUG + r52.EDPRSR] & r52.EDPRSR_HALTED
    assert device.dlr[CORE1_DEBUG] == 0x3210_0020


def test_full_verify_reads_every_word_back() -> None:
    _dap, _device, report, _notes = run_full_load(SyntheticTarget(), verify_full=True)
    assert all(s.verified == "full" for s in report.segments)


def test_a_release_that_never_leaves_reset_fails_in_the_settle_gate() -> None:
    """Guide §9#3 ordering: the AXI-AP must not be touched while functional
    reset is asserted, so a stuck release must die in the settle poll --
    before any AXI traffic -- not as a policy refusal halfway through P3."""

    class StuckReset(SyntheticTarget):
        def ap_reg_write(self, index: int, value: int) -> None:
            from s32edbg.data.expected_apmap import AP_SDA
            from s32edbg.data import ladder1

            addr = self._addr(index)
            if addr == AP_SDA + ladder1.SDA_SYSRSTC:
                # Assert works; release is ignored -- the reset stays up.
                if value & ladder1.SDA_SYSRSTC_SYSFUNCREQ:
                    super().ap_reg_write(index, value)
                return
            super().ap_reg_write(index, value)

    device = StuckReset()
    with pytest.raises(LoadError, match="did not leave functional reset"):
        run_full_load(device)
    # And no AXI write ever happened: the MC_ME base saw nothing.
    assert 0x4190_0300 not in device.mem


# ---------------------------------------------------------------------------
# The WriteMem fast path: trust is earned, lies are caught
# ---------------------------------------------------------------------------


class FastTarget(SyntheticTarget):
    """A target whose transport-side WriteMem works correctly."""

    can_block_write = True

    def __init__(self) -> None:
        super().__init__()
        self.block_writes: list[tuple[int, int]] = []

    def write_mem_block(self, addr: int, data: bytes) -> None:
        self.block_writes.append((addr, len(data)))
        for i in range(0, len(data), 4):
            self.mem[addr + i] = int.from_bytes(data[i : i + 4], "little")


class LyingFastTarget(FastTarget):
    """WriteMem that corrupts one word -- the read-back probe must catch it."""

    def write_mem_block(self, addr: int, data: bytes) -> None:
        super().write_mem_block(addr, data)
        self.mem[addr] ^= 0xFF  # first word corrupted


def test_the_fast_path_is_used_when_it_proves_itself() -> None:
    _dap, device, report, notes = run_full_load(FastTarget())
    assert report.fast_path == "used"
    assert device.block_writes, "WriteMem was never called"
    assert any("proven by full read-back" in n for n in notes)
    assert all(s.path == "dll" for s in report.segments)
    assert device.mem[0x3210_0020] == 0xEA00_0021


def test_a_lying_fast_path_is_demoted_and_the_image_still_lands_correctly() -> None:
    _dap, device, report, notes = run_full_load(LyingFastTarget())
    assert report.fast_path == "demoted"
    assert any("fast path demoted" in n for n in notes)
    # The suspect block was rewritten via ADIv6: final memory is correct.
    assert device.mem[0x3210_0020] == 0xEA00_0021
    assert device.mem[0x3178_0000] == 0x4433_2211


# ---------------------------------------------------------------------------
# XRDC tolerance -- measured 2026-08-04: the debugger reaches the SRAMC
# controllers in the 0x76xxxxxx slice but XRDC denies the RTU0 GPR
# ---------------------------------------------------------------------------


def test_an_optional_action_denied_by_xrdc_is_skipped_with_a_note() -> None:
    device = SyntheticTarget()
    dap = prepared_dap(device)
    device.faulting.add(0x7612_0000)
    notes: list[str] = []
    actions = (
        rtuboot.InitAction(
            "rmw", addr=0x7612_0000, mask=0x1, value=0x1,
            label="split-lock", optional=True,
        ),
        rtuboot.InitAction("write32", addr=0x4186_005C, value=0x2),
    )
    loader.run_init_actions(dap, actions, note=notes.append)
    assert any("skipped -- XRDC denies" in n for n in notes)
    assert device.mem[0x4186_005C] == 0x2, "the table continued past the skip"


def test_a_faulted_write_with_a_fallback_lands_through_the_alias() -> None:
    device = SyntheticTarget()
    dap = prepared_dap(device)
    device.faulting.add(0x7990_0000)
    notes: list[str] = []
    actions = (
        rtuboot.InitAction(
            "write32", addr=0x7990_0000, value=0xE7FE_E7FE,
            label="park", fallback_addr=0x3210_0000,
        ),
    )
    loader.run_init_actions(dap, actions, note=notes.append)
    assert device.mem[0x3210_0000] == 0xE7FE_E7FE
    assert any("retrying through the alias" in n for n in notes)


def test_the_full_flow_survives_the_measured_gpr_denial() -> None:
    """The exact 2026-08-04 bench state: 0x76120000 XRDC-denied. The load must
    complete in the lockstep reset default rather than abort."""
    device = SyntheticTarget()
    device.faulting.add(rtuboot.RTU0_GPR_CFG_CORE)
    _dap, device, report, notes = run_full_load(device)
    assert any("skipped -- XRDC denies" in n for n in notes)
    assert device.mem[0x3210_0020] == 0xEA00_0021
    assert device.dlr[CORE0_DEBUG] == 0x3210_0020
    assert report.pc == 0x3210_0020


class ResumingFastTarget(FastTarget):
    """Models the measured J-Link DLL behaviour: WriteMem resumes the core it
    believes it halted (EDPRSR.SDR observed set mid-load, 2026-08-04)."""

    def write_mem_block(self, addr: int, data: bytes) -> None:
        super().write_mem_block(addr, data)
        self._restart(CORE0_DEBUG)


def test_a_writemem_that_resumes_the_core_is_re_halted_before_verify_and_pc() -> None:
    _dap, device, report, notes = run_full_load(ResumingFastTarget())
    assert any("re-halting at the park" in n for n in notes)
    assert device.mem[CORE0_DEBUG + r52.EDPRSR] & r52.EDPRSR_HALTED
    assert device.dlr[CORE0_DEBUG] == 0x3210_0020
    assert report.pc == 0x3210_0020
    assert report.fast_path == "used"


class FlakyFastTarget(FastTarget):
    """WriteMem fails (rc-style TransportError) for every attempt of exactly
    one block, then behaves -- the measured clustered -1 (FETK contention)."""

    def __init__(self) -> None:
        super().__init__()
        self.failures_left = loader.FAST_ATTEMPTS  # every attempt of one block

    def write_mem_block(self, addr: int, data: bytes) -> None:
        if self.failures_left:
            self.failures_left -= 1
            from s32edbg.errors import TransportError

            raise TransportError(f"JLINK_WriteMem(0x{addr:08X}) returned -1")
        super().write_mem_block(addr, data)


class BrokenFastTarget(FastTarget):
    """WriteMem fails every single time (rc-style, never a data lie)."""

    def write_mem_block(self, addr: int, data: bytes) -> None:
        from s32edbg.errors import TransportError

        raise TransportError("returned -1")


def test_a_failing_block_falls_back_alone_and_the_fast_path_stays_on() -> None:
    """The measured FETK-contention shape: one block's attempts all fail. That
    block is rewritten via ADIv6; the NEXT block uses WriteMem again. The old
    demote-forever policy turned three clustered failures into minutes of
    word-writing for the rest of the load."""
    _dap, device, report, notes = run_full_load(FlakyFastTarget())
    assert any("block fallback #1" in n for n in notes)
    assert report.fast_path == "used"
    assert any("fell back to ADIv6 on WriteMem transients" in n for n in report.notes)
    assert device.mem[0x3210_0020] == 0xEA00_0021
    assert device.mem[0x3178_0000] == 0x4433_2211
    assert device.block_writes, "WriteMem was used again after the fallback"


def test_writemem_failing_every_block_never_costs_correctness() -> None:
    """rc failures are never a reason for permanent demotion -- each block just
    falls back individually, and with no block ever landing via the DLL the
    report says the fast path went unused."""
    _dap, device, report, notes = run_full_load(BrokenFastTarget())
    assert report.fast_path == "unused"
    assert not any("staying there" in n for n in notes)
    assert all(s.path == "adiv6" for s in report.segments)
    assert device.mem[0x3210_0020] == 0xEA00_0021
    assert device.mem[0x3178_0000] == 0x4433_2211


class SyncableFastTarget(FastTarget):
    """Tracks the DLL-model sync calls the fast path issues."""

    can_cpu_halt = True

    def __init__(self) -> None:
        super().__init__()
        self.halt_syncs = 0

    def cpu_halt_sync(self) -> bool | None:
        self.halt_syncs += 1
        return True


def test_the_dll_model_is_synced_when_the_fast_path_arms() -> None:
    """The arm-time Halt() sync is what removed the resume-behind-our-back
    side effect on target (the DLL stops wrapping WriteMem in stop-mode
    halt/go once its model says "halted by me")."""
    _dap, device, report, notes = run_full_load(SyncableFastTarget())
    assert device.halt_syncs >= 1
    assert any("synced the DLL's target model (arming" in n for n in notes)
    assert report.fast_path == "used"

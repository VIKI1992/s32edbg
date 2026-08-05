"""The `debug` session: one connection, many commands, restore on close.

Why this command exists at all: every ordinary invocation opens and closes its
own J-Link session, and on this board each connect pulses reset through R439
into the SBC/PMIC (test-plan defect D-2); halts held across invocations
deadlocked the application. These tests pin the session contract -- a single
transport for the whole session, run-control state coherent between commands,
and a close that puts the target back.
"""

from __future__ import annotations

import io
import sys

import pytest
from conftest import CORE0_CTI, CORE0_DEBUG, CORE1_CTI, CORE1_DEBUG, SyntheticTarget
from rich.console import Console

from s32edbg import cli
from s32edbg.data import m33debug as m33d
from s32edbg.data import r52debug as r52

EXIT_OK = 0
EXIT_REFUSED = 3


@pytest.fixture()
def run(monkeypatch, capsys):
    """Invoke `entry()` as the console script would. Same shape as test_cli's."""
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=200, no_color=True))
    monkeypatch.setattr(cli, "err_console", Console(file=buffer, width=200, no_color=True))

    def invoke(*argv: str, stdin: str = "") -> tuple[int, str]:
        monkeypatch.setattr(sys, "argv", ["s32edbg", *argv])
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = cli.entry()
        captured = capsys.readouterr()
        return code, buffer.getvalue() + captured.out + captured.err

    return invoke


@pytest.fixture()
def opened(monkeypatch):
    """Patch the transport factory, counting how often a connection is made."""
    device = SyntheticTarget()
    opens: list[str] = []

    def factory(kind, **kwargs):
        opens.append(kind)
        return device

    monkeypatch.setattr(cli, "discover", lambda *a, **k: object())
    monkeypatch.setattr(cli, "make_transport", factory)
    return device, opens


def _halted(device: SyntheticTarget, debug_base: int) -> bool:
    return bool(device.mem.get(debug_base + r52.EDPRSR, 0) & r52.EDPRSR_HALTED)


def test_a_session_is_one_connection_for_many_commands(run, opened) -> None:
    """The whole point: five commands, one transport. Outside a session each of
    these would have been its own J-Link connect -- and its own reset pulse."""
    device, opens = opened
    code, out = run(
        "debug", "--core", "rtu0.0",
        "-c", "halt",
        "-c", "status",
        "-c", "bp set 0x00400120",
        "-c", "bp list",
        "-c", "regs",
    )
    assert code == EXIT_OK
    assert len(opens) == 1
    assert device.closed is True
    assert "session closed" in out


def test_closing_the_session_restores_the_target(run, opened) -> None:
    """The chosen close contract: breakpoints the session set are cleared and
    cores it halted are resumed -- comparators first, so the resumed core does
    not immediately re-halt on a session breakpoint."""
    device, _ = opened
    code, out = run(
        "debug", "--core", "rtu0.0",
        "-c", "halt",
        "-c", "bp set 0x00400120",
    )
    assert code == EXIT_OK
    assert "restore: cleared breakpoint 0 on rtu0.0" in out
    assert "restore: resumed rtu0.0" in out
    assert not _halted(device, CORE0_DEBUG)
    assert device.mem.get(CORE0_DEBUG + r52.dbgbcr(0), 0) == 0


def test_keep_on_exit_leaves_the_target_as_the_session_left_it(run, opened) -> None:
    device, _ = opened
    code, out = run(
        "debug", "--core", "rtu0.0", "--keep-on-exit",
        "-c", "halt",
        "-c", "bp set 0x00400120",
    )
    assert code == EXIT_OK
    assert "--keep-on-exit" in out
    assert _halted(device, CORE0_DEBUG)
    assert device.mem.get(CORE0_DEBUG + r52.dbgbcr(0), 0) & r52.DBGBCR_E


def test_a_core_resumed_during_the_session_is_not_resumed_again_at_close(run, opened) -> None:
    device, _ = opened
    code, out = run(
        "debug", "--core", "rtu0.0",
        "-c", "halt",
        "-c", "resume",
    )
    assert code == EXIT_OK
    assert "restore: resumed" not in out
    assert not _halted(device, CORE0_DEBUG)


def test_the_focused_core_is_the_default_for_bare_commands(run, opened) -> None:
    device, _ = opened
    code, _ = run("debug", "--core", "rtu0.1", "-c", "halt")
    assert code == EXIT_OK
    # rtu0.1 is the fake's second frame; only it should have been halted.
    assert not _halted(device, CORE0_DEBUG)


def test_use_switches_the_focus_mid_session(run, opened) -> None:
    device, _ = opened
    code, _ = run(
        "debug", "--core", "rtu0.0",
        "-c", "use rtu0.1",
        "-c", "halt",
        "-c", "quit",
    )
    assert code == EXIT_OK
    assert not _halted(device, CORE0_DEBUG)
    assert not _halted(device, CORE1_DEBUG)  # restored on close


def test_session_incompatible_commands_are_refused_but_do_not_end_the_session(run, opened) -> None:
    """`bscan` would change the JTAG chain under the held connection (ordering
    rule 3: that takes the DAP away until a power cycle). The refusal names the
    reason and the session carries on."""
    device, opens = opened
    code, out = run(
        "debug", "--core", "rtu0.0",
        "-c", "bscan preamble",
        "-c", "halt",
    )
    assert code == EXIT_OK
    assert "cannot run inside a debug session" in out
    assert len(opens) == 1
    assert "restore: resumed rtu0.0" in out  # the later halt still ran


def test_a_session_needs_the_dll_backend(run, opened) -> None:
    """The commander backend runs one JLink.exe per operation -- it cannot hold
    a connection, so a commander session would be the per-command behaviour the
    session exists to replace."""
    code, out = run("--transport", "commander", "debug")
    assert code == EXIT_REFUSED
    assert "dll backend" in out


def test_stdin_lines_drive_the_session_when_not_a_tty(run, opened) -> None:
    device, _ = opened
    code, out = run("debug", "--core", "rtu0.0", stdin="halt\nstatus\nquit\n")
    assert code == EXIT_OK
    assert "halted" in out
    assert "session closed" in out
    assert not _halted(device, CORE0_DEBUG)


def test_first_touch_sanitises_stale_comparators(run, opened) -> None:
    """Guide §5.7: comparator state is UNKNOWN after cold reset, enable bit
    included. A session's first write-operation on a core starts it from a
    known state -- and says what it discarded."""
    device, _ = opened
    device.mem[CORE0_DEBUG + r52.dbgbcr(5)] = r52.DBGBCR_E  # stale garbage
    code, out = run("debug", "--core", "rtu0.0", "-c", "halt")
    assert code == EXIT_OK
    assert "disabled 1 pre-existing comparator" in out
    assert device.mem.get(CORE0_DEBUG + r52.dbgbcr(5), 0) == 0


def test_keep_breakpoints_skips_the_sanitation(run, opened) -> None:
    device, _ = opened
    device.mem[CORE0_DEBUG + r52.dbgbcr(5)] = r52.DBGBCR_E
    code, out = run(
        "debug", "--core", "rtu0.0", "--keep-breakpoints", "-c", "halt"
    )
    assert code == EXIT_OK
    assert "pre-existing comparator" not in out
    assert device.mem.get(CORE0_DEBUG + r52.dbgbcr(5), 0) & r52.DBGBCR_E


def test_an_m33_halted_in_the_session_is_resumed_at_close(run, opened) -> None:
    device, _ = opened
    code, out = run("debug", "-c", "m33 halt --core smu")
    assert code == EXIT_OK
    assert "restore: resumed smu" in out
    assert device.m33["halted"] is False


def test_a_failing_command_reports_and_the_session_continues(run, opened) -> None:
    """Stepping a running core fails (its register path needs Debug state).
    The error is printed, the session survives it, and the later halt runs."""
    device, _ = opened
    code, out = run(
        "debug", "--core", "rtu0.0",
        "-c", "step",          # not halted yet: a run-control error
        "-c", "halt",          # still runs
    )
    assert code == EXIT_OK
    assert "error:" in out
    assert "restore: resumed rtu0.0" in out


def test_unknown_session_commands_point_at_help(run, opened) -> None:
    code, out = run("debug", "-c", "frobnicate")
    assert code == EXIT_OK
    assert "unknown command" in out


def test_a_malformed_address_reports_and_the_session_survives(run, opened) -> None:
    """The reported defect: `bp set 32169874` (no 0x) used to raise an uncaught
    ValueError that unwound the REPL and killed the process -- and with it the
    session whose whole purpose is avoiding reconnects (defect D-2). Now it is
    a printed error; the next command still runs and the close is orderly."""
    device, opens = opened
    code, out = run(
        "debug", "--core", "rtu0.0",
        "-c", "bp set 32169874",   # rejected: bare number
        "-c", "bp set zzz",        # rejected: not hex at all
        "-c", "halt",              # still runs
    )
    assert code == EXIT_OK
    assert out.count("0x-prefixed") == 2
    assert "restore: resumed rtu0.0" in out
    assert "session closed" in out
    assert len(opens) == 1


def test_an_internal_error_reports_and_the_session_survives(run, opened, monkeypatch) -> None:
    """Defence in depth: even a genuine bug inside a command must not end the
    session. It is reported as an internal error and the REPL keeps going."""
    device, _ = opened

    real_halt = cli.runcontrol.halt
    calls: list[int] = []

    def flaky_halt(dap, c):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated bug")
        return real_halt(dap, c)

    monkeypatch.setattr(cli.runcontrol, "halt", flaky_halt)
    code, out = run(
        "debug", "--core", "rtu0.0",
        "-c", "halt",   # internal error, survives
        "-c", "halt",   # works
    )
    assert code == EXIT_OK
    assert "internal error" in out
    assert "simulated bug" in out
    assert "still open" in out
    assert "restore: resumed rtu0.0" in out
    assert "session closed" in out


# ---------------------------------------------------------------------------
# load -- the RAM-load flow driven end to end through the session
# ---------------------------------------------------------------------------


def _write_load_elf(tmp_path):
    """A two-segment ELF inside the loadable windows, ragged tail included."""
    import struct

    from s32edbg import elf as elf_mod

    segments = [
        (0x3210_0020, b"\x21\x00\x00\xea\x55\xaa\x55\xaa"),
        (0x3178_0000, b"\x11\x22\x33\x44\x55\x66"),
    ]
    phoff, phnum = 52, len(segments)
    data_off = phoff + phnum * elf_mod.PHDR_SIZE
    ident = elf_mod.ELF_MAGIC + bytes([1, 1, 1, 0]) + b"\x00" * 8
    ehdr = struct.pack(
        "<HHIIIIIHHHHHH", 2, elf_mod.EM_ARM, 1, 0x3210_0021, phoff, 0, 0, 52,
        elf_mod.PHDR_SIZE, phnum, 0, 0, 0,
    )
    phdrs, blobs, off = b"", b"", data_off
    for paddr, data in segments:
        phdrs += struct.pack(
            "<IIIIIIII", elf_mod.PT_LOAD, off, paddr, paddr, len(data), len(data), 5, 4
        )
        blobs += data
        off += len(data)
    p = tmp_path / "app.elf"
    p.write_bytes(ident + ehdr + phdrs + blobs)
    return p


def test_load_in_a_session_lands_the_image_and_parks_the_core(run, opened, tmp_path) -> None:
    device, opens = opened
    p = _write_load_elf(tmp_path)
    code, out = run("debug", "-c", f'load "{p}"')
    assert code == EXIT_OK
    assert len(opens) == 1, "load must reuse the held session connection"
    assert device.mem[0x3210_0020] == 0xEA00_0021
    assert device.mem[0x3178_0004] == 0x0000_6655, "ragged tail zero-padded"
    assert device.dlr[CORE0_DEBUG] == 0x3210_0020
    assert "rtu0.0 halted at the entry" in out
    assert "resets the whole SoC" in out
    # note_halted + restore-on-close: the parked core is resumed at quit.
    assert "restore: resumed rtu0.0" in out


def test_load_keep_on_exit_leaves_the_core_parked(run, opened, tmp_path) -> None:
    device, _ = opened
    p = _write_load_elf(tmp_path)
    code, out = run("debug", "--keep-on-exit", "-c", f'load "{p}"')
    assert code == EXIT_OK
    assert "restore:" not in out
    from s32edbg.data import r52debug as r52mod

    assert device.mem[CORE0_DEBUG + r52mod.EDPRSR] & r52mod.EDPRSR_HALTED


def test_load_argument_errors_never_touch_the_probe_and_never_kill_the_session(
    run, opened, tmp_path
) -> None:
    device, opens = opened
    not_elf = tmp_path / "not.elf"
    not_elf.write_bytes(b"MZ definitely not an ELF")
    code, out = run(
        "debug", "--core", "rtu0.0",
        "-c", "load",                       # missing argument
        "-c", f'load "{tmp_path / "gone.elf"}"',  # missing file
        "-c", f'load "{not_elf}"',          # not an ELF
        "-c", "halt",                       # session still works
    )
    assert code == EXIT_OK
    assert "Missing argument" in out
    assert "cannot read" in out
    assert "not an ELF" in out
    assert "restore: resumed rtu0.0" in out
    assert "session closed" in out


def test_a_one_shot_load_warns_about_the_reconnect_reset(run, opened, tmp_path) -> None:
    p = _write_load_elf(tmp_path)
    code, out = run("load", str(p))
    assert code == EXIT_OK
    assert "one-shot load" in out
    assert "D-2" in out


def test_each_in_session_load_gets_a_fresh_probe_connection(run, opened, tmp_path) -> None:
    """Finding 9: the J-Link DLL's WriteMem wedges after ~4 MB of traffic per
    DLL connection -- instant -1s that neither retries nor the DLL's own
    Halt() cure. Every in-session load therefore reopens the DLL connection
    (its connect pulse lands moments before the load's own SoC reset), so
    back-to-back loads each start with a full WriteMem budget."""
    device, opens = opened
    p = _write_load_elf(tmp_path)
    code, out = run("debug", "-c", f'load "{p}"', "-c", f'load "{p}"')
    assert code == EXIT_OK
    assert out.count("fresh probe connection for this load") == 2
    assert getattr(device, "open_calls", 0) >= 2, "the DLL session was never reopened"
    assert device.mem[0x3210_0020] == 0xEA00_0021
    assert "rtu0.0 halted at the entry" in out
    assert "session closed" in out


def test_a_core_the_app_reset_mid_session_gets_fresh_records(run, opened) -> None:
    """The application's own core-1 cycle (assert its RGM reset, release it)
    under a live session: the cold reset wipes that core's comparators and
    re-sets its OS lock (guide §3.6). The session must notice on its next
    state refresh, drop its records for that core -- a restore that 'cleared'
    a wiped comparator or resumed a rebooted core would be undoing a machine
    that no longer exists -- and re-unlock + re-sanitise on the next touch."""
    device, _ = opened
    prst = f"0x{0x4185_0048:08X}"  # rtuboot.RGM_PRST0_1
    code, out = run(
        "debug", "--core", "rtu0.1",
        "-c", "halt",
        "-c", "bp set 0x00400120",
        # The app's BrsHw sequence for core1, as three bus writes: release
        # (the fake boots with PRST=0x1F, all held), assert, release.
        "-c", f"mem write {prst} 0x1B --yes",
        "-c", f"mem write {prst} 0x1F --yes",
        "-c", f"mem write {prst} 0x1B --yes",
        "-c", "status",
        "-c", "halt",
    )
    assert code == EXIT_OK
    assert "was reset since this session last touched it" in out
    # The dropped breakpoint record is not "restored" at close...
    assert "cleared breakpoint 0 on rtu0.1" not in out
    # ...but the re-halt after the reset is: the session re-owned the core.
    assert "restore: resumed rtu0.1" in out
    from s32edbg.data import r52debug as r52mod

    assert not device.mem.get(CORE1_DEBUG + r52mod.dbgbcr(0), 0), "comparator stayed wiped"


PRST = f"0x{0x4185_0048:08X}"  # rtuboot.RGM_PRST0_1; the fake boots it as 0x1F


def test_catch_halts_core1_at_the_apps_own_release(run, opened) -> None:
    """The whole catch flow against the fake's replay of the application
    releasing core1: arm inside the session, 'run the app' (an RGM write
    clearing core1's reset bit), then --wait converts the EDBGREQ hold into a
    real acknowledged halt -- unlocked, latch dropped, comparators
    re-sanitised -- and a breakpoint can be set at the first instruction."""
    device, _ = opened
    code, out = run(
        "debug", "--core", "rtu0.1",
        "-c", "catch",
        "-c", f"mem write {PRST} 0x1B --yes",  # the app releases core1
        "-c", "catch --wait --timeout 2",
        "-c", "bp set 0x00400120",
    )
    assert code == EXIT_OK
    assert "catch armed" in out
    assert "caught at its first instruction" in out
    assert "cleared the OS lock" in out
    from s32edbg.data import ladder1 as ladmod
    from s32edbg.data.expected_apmap import AP_MDM as APM

    assert not device.ap_space.get(APM + ladmod.MDM_CONTROL2, 0) & ladmod.edbgreq_bit(1), \
        "the latch must be dropped by the conversion"
    # Restore-on-close resumed the caught core and cleared the breakpoint.
    assert "cleared breakpoint 0 on rtu0.1" in out
    assert "restore: resumed rtu0.1" in out
    assert not _halted(device, CORE1_DEBUG)


def test_closing_the_session_drops_an_unfired_catch_latch(run, opened) -> None:
    """Finding 7 as a contract: an armed latch must never outlive the session
    -- left behind, it halts core1 on the application's own release with
    nobody attached."""
    device, _ = opened
    code, out = run("debug", "--core", "rtu0.1", "-c", "catch")
    assert code == EXIT_OK
    assert "dropped the armed catch latch for rtu0.1" in out
    from s32edbg.data import ladder1 as ladmod
    from s32edbg.data.expected_apmap import AP_MDM as APM

    assert not device.ap_space.get(APM + ladmod.MDM_CONTROL2, 0) & ladmod.edbgreq_bit(1)


def test_catch_arm_only_requires_a_session(run, opened) -> None:
    _device, _ = opened
    code, out = run("core", "catch", "--core", "rtu0.1")
    assert code == 1
    assert "needs a debug session" in out


def test_catch_wait_timeout_in_session_keeps_the_latch_for_close(run, opened) -> None:
    device, _ = opened
    code, out = run("debug", "--core", "rtu0.1", "-c", "catch --wait --timeout 0.1")
    assert code == EXIT_OK  # the session survives the command's failure
    assert "did not halt within" in out
    assert "dropped the armed catch latch for rtu0.1" in out  # close owned it


def test_a_write_touch_converts_a_fired_catch(run, opened) -> None:
    """The safety net: a `bp set` against a fired-but-unconverted hold would
    program a core that is not architecturally in Debug state and whose next
    resume would be swallowed -- _attach_core converts first."""
    device, _ = opened
    code, out = run(
        "debug", "--core", "rtu0.1",
        "-c", "catch",
        "-c", f"mem write {PRST} 0x1B --yes",
        "-c", "bp set 0x00400120",
    )
    assert code == EXIT_OK
    assert "converting it into a real acknowledged halt first" in out
    assert "breakpoint 0" in out


def test_load_after_catch_clears_the_bookkeeping(run, opened, tmp_path) -> None:
    """`load` arms and drops every R52 latch itself; stale catch bookkeeping
    surviving it would make restore 'drop' a latch the load already owns."""
    device, _ = opened
    p = _write_load_elf(tmp_path)
    code, out = run("debug", "--core", "rtu0.1", "-c", "catch", "-c", f'load "{p}"')
    assert code == EXIT_OK
    assert "dropped the armed catch latch" not in out


def test_sync_on_makes_a_halt_stop_both_cores(run, opened) -> None:
    """Stop-the-world: with the §5.9 group armed, halting the focused core
    drives channel 2 and the partner enters Debug state too -- both owned by
    the session, both resumed at close."""
    device, _ = opened
    code, out = run(
        "debug", "--core", "rtu0.0",
        "-c", "sync on",
        "-c", "halt",
    )
    assert code == EXIT_OK
    assert "sync on" in out
    assert "sync: rtu0.1 stopped with the group" in out
    assert "disarmed the cross-trigger sync group" in out
    assert "restore: resumed rtu0.0" in out
    assert "restore: resumed rtu0.1" in out
    assert not _halted(device, CORE0_DEBUG)
    assert not _halted(device, CORE1_DEBUG)


def test_bare_continue_under_sync_group_restarts_with_the_teardown(run, opened) -> None:
    """A bare `continue` under sync releases the whole stopped group on one
    pulse. The teardown is what makes that possible -- the halted members'
    DBGTRIGGER levels are masked out of channel 2 for the restart window, then
    re-armed. The re-arm is proven behaviourally: a second halt after the
    continue must stop the partner again. (The post-quit CTI state is the
    close-disarm test's subject.)"""
    device, _ = opened
    code, out = run(
        "debug", "--core", "rtu0.0",
        "-c", "sync on",
        "-c", "halt",
        "-c", "continue",
        "-c", "halt",
    )
    assert code == EXIT_OK
    assert "synchronized restart: one pulse released 2 core(s)" in out
    assert out.count("sync: rtu0.1 stopped with the group") == 2, \
        "the group must still be armed after the restart"
    assert not _halted(device, CORE0_DEBUG)
    assert not _halted(device, CORE1_DEBUG)


def test_step_under_sync_steps_one_core_and_leaves_the_partner_halted(run, opened) -> None:
    device, _ = opened
    code, out = run(
        "debug", "--core", "rtu0.0",
        "-c", "sync on",
        "-c", "halt",
        "-c", "step",
        "-c", "status --core rtu0.1",
    )
    assert code == EXIT_OK
    assert "cross-halt masked for the step" in out
    assert "step 1" in out
    # The partner stayed halted through the step; close resumes both.
    assert "restore: resumed rtu0.1" in out


def test_closing_the_session_disarms_sync(run, opened) -> None:
    """A session must not leave the chip in stop-the-world mode: after quit,
    the channel-2 routing is gone from both CTIs and a later single-core halt
    stays local."""
    device, _ = opened
    from s32edbg.data import r52debug as r52mod

    code, out = run("debug", "--core", "rtu0.0", "-c", "sync on", "-c", "halt")
    assert code == EXIT_OK
    ch2 = 1 << r52mod.CHANNEL_CROSS_HALT
    assert not device.mem.get(CORE0_CTI + r52mod.ctiinen(r52mod.TRIGIN_CROSS_HALT), 0) & ch2
    assert not device.mem.get(CORE1_CTI + r52mod.ctiinen(r52mod.TRIGIN_CROSS_HALT), 0) & ch2
    assert not device.mem.get(CORE0_CTI + r52mod.CTIGATE, 0) & ch2
    assert not device.mem.get(CORE1_CTI + r52mod.CTIGATE, 0) & ch2


def test_a_bp_hit_under_sync_stops_the_group_and_is_recorded_for_restore(run, opened) -> None:
    """The armed group turns a breakpoint hit into a stop-the-world: the
    resumed core re-halts on its comparator, the re-armed channel drags the
    partner in, the session takes ownership of it, and close resumes both."""
    device, _ = opened
    code, out = run(
        "debug", "--core", "rtu0.0",
        "-c", "halt",
        "-c", "bp set 0x00400120",
        "-c", "sync on",
        "-c", "continue",
        "-c", "status",
    )
    assert code == EXIT_OK
    assert "ran and immediately re-halted" in out
    assert "sync: rtu0.1 halted by the cross-trigger group" in out
    assert "restore: resumed rtu0.0" in out
    assert "restore: resumed rtu0.1" in out
    assert not _halted(device, CORE0_DEBUG)
    assert not _halted(device, CORE1_DEBUG)


def test_note_core_reset_drops_only_that_cores_records() -> None:
    """A core-local cold reset (the app cycling one core's RGM reset) wipes
    that core's comparators and re-sets its OS lock (guide §3.6). The session
    must forget its records for that core -- and only that core -- so the next
    touch re-unlocks and re-sanitises."""
    from s32edbg.session import DebugSession

    s = DebugSession(transport=None, dap=None)
    s.halted_r52 = {"rtu0.0", "rtu0.1"}
    s.bp_set = {("rtu0.0", 0), ("rtu0.1", 1)}
    s.wp_set = {("rtu0.1", 0)}
    s.sanitized = {"rtu0.0", "rtu0.1", "m33:smu"}

    s.note_core_reset("rtu0.1")

    assert s.halted_r52 == {"rtu0.0"}
    assert s.bp_set == {("rtu0.0", 0)}
    assert s.wp_set == set()
    assert s.sanitized == {"rtu0.0", "m33:smu"}


def test_load_works_on_a_blank_flash_board_whose_smu_answers_nothing(run, opened, tmp_path) -> None:
    """The RAM-load path after a chip erase (measured 2026-08-05): no
    bootloader, the SoC in the BootROM fail cascade, the SMU AP dead at
    attach. P0's pre-reset M33 quiesce is best-effort -- the load's own
    EDBGREQ-latched reset supplies the quiesce that persists -- so the image
    still lands and rtu0.0 still parks at its entry."""
    device, _opens = opened
    device._m33_dead_reads = 10**6
    device.m33_dead_reads_after_reset = 0
    p = _write_load_elf(tmp_path)
    code, out = run("debug", "-c", f'load "{p}"')
    assert code == EXIT_OK
    assert "could not quiesce before the reset" in out
    assert device.mem[0x3210_0020] == 0xEA00_0021
    assert "rtu0.0 halted at the entry" in out


def test_an_in_session_flash_op_reopens_the_probe_connection(run, opened, tmp_path) -> None:
    """The flash flow stages the applet plus payload through WriteMem, so
    Finding 9 applies to it exactly as to `load`: every in-session flash op
    reopens the DLL connection first, then runs to a verified program. The
    --ahb-base points the read window at where the fake serves it."""
    device, opens = opened
    image = tmp_path / "scratch.bin"
    image.write_bytes(bytes(range(256)))
    code, out = run(
        "debug", "--core", "rtu0.0",
        "-c", f'flash program "{image}" --offset 0x300000 --ahb-base 0x08000000 --yes',
    )
    assert code == EXIT_OK
    assert "fresh probe connection for this flash op" in out
    assert getattr(device, "open_calls", 0) >= 2, "the DLL session was never reopened"
    assert device.flash[0x0030_0000] == 0x0302_0100
    assert "programmed" in out
    assert "session closed" in out

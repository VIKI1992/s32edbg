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
from conftest import CORE0_DEBUG, CORE1_DEBUG, SyntheticTarget
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

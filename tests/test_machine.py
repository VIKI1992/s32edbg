"""`debug --machine`: the NDJSON session protocol for IDE frontends.

The contract these tests pin (s32edbg/machine.py):

- stdout carries only JSON objects, one per line: a `ready` event, then
  exactly one result envelope per non-empty stdin line (progress events may
  precede an envelope), then a `bye` event after restore.
- Envelopes carry the same payloads the `--json` contract emits, captured
  instead of printed; error kinds mirror the exit-code contract.
- All human/Rich output goes to stderr, never stdout.
- No input may end the session other than `quit` or EOF, and both restore
  the target exactly like the interactive REPL does.
"""

from __future__ import annotations

import io
import json
import sys
import time

import pytest
from conftest import CORE0_DEBUG, SyntheticTarget
from rich.console import Console
from test_session import _write_load_elf

from s32edbg import cli
from s32edbg.data import r52debug as r52


@pytest.fixture()
def opened(monkeypatch):
    device = SyntheticTarget()

    def factory(kind, **kwargs):
        return device

    monkeypatch.setattr(cli, "discover", lambda *a, **k: object())
    monkeypatch.setattr(cli, "make_transport", factory)
    return device


@pytest.fixture()
def machine(monkeypatch):
    """Run `s32edbg <argv>` with a piped stdin and a captured protocol stdout.

    Returns (exit code, parsed JSON lines, human text). A stdout line that is
    not valid JSON fails the test right here -- that is the core promise.
    """
    human = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=human, width=200, no_color=True))
    monkeypatch.setattr(cli, "err_console", Console(file=human, width=200, no_color=True))

    def invoke(*argv: str, stdin: str = ""):
        proto = io.StringIO()
        monkeypatch.setattr(sys, "argv", ["s32edbg", *argv])
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        monkeypatch.setattr(sys, "stdout", proto)
        code = cli.entry()
        lines = [
            json.loads(line)
            for line in proto.getvalue().splitlines()
            if line.strip()
        ]
        return code, lines, human.getvalue()

    return invoke


def envelopes(lines: list[dict]) -> list[dict]:
    return [l for l in lines if "event" not in l]


def events(lines: list[dict], name: str) -> list[dict]:
    return [l for l in lines if l.get("event") == name]


# ---------------------------------------------------------------------------
# The stream shape
# ---------------------------------------------------------------------------


def test_ready_then_one_envelope_per_line_then_bye(machine, opened) -> None:
    code, lines, _ = machine(
        "debug", "--machine", "--core", "rtu0.0",
        stdin="halt\nregs\nquit\n",
    )
    assert code == 0
    ready = lines[0]
    assert ready["event"] == "ready"
    assert ready["protocol"] == 1
    assert ready["version"]
    assert "rtu0.0" in ready["cores"] and "rtu0.1" in ready["cores"]
    assert ready["m33"] == ["smu", "ce0", "ce1"]
    assert ready["focus"] == "rtu0.0"
    assert ready["discovery_error"] is None

    envs = envelopes(lines)
    assert [e["seq"] for e in envs] == [1, 2, 3]
    halt_env = envs[0]
    assert halt_env["ok"] is True
    assert halt_env["cmd"] == "core halt"
    assert halt_env["result"]["halt"][0]["core"] == "rtu0.0"
    regs_env = envs[1]
    assert regs_env["ok"] is True
    assert regs_env["cmd"] == "core regs"
    assert "registers" in regs_env["result"]
    assert envs[2]["cmd"] == "quit"

    bye = lines[-1]
    assert bye["event"] == "bye"
    assert bye["kept"] is False
    assert any("resumed rtu0.0" in n for n in bye["restore_notes"])


def test_envelopes_carry_cost_fields(machine, opened) -> None:
    """Result envelopes report wall ms and the DP/AP transaction count.

    `ops` is the cost model an IDE can reason with: one transaction is one USB
    round trip on the real transport, so a slow command's envelope names its
    own cause (900 ms / 850 ops is USB-bound; 900 ms / 40 ops is a wait).
    """
    code, lines, _ = machine(
        "debug", "--machine", "--core", "rtu0.0",
        stdin="halt\nregs\nquit\n",
    )
    assert code == 0
    halt_env, regs_env, _quit_env = envelopes(lines)
    for env in (halt_env, regs_env):
        assert isinstance(env["ms"], (int, float)) and env["ms"] >= 0
        assert isinstance(env["ops"], int)
    # Halting and reading registers both talk to the target.
    assert halt_env["ops"] > 0
    assert regs_env["ops"] > 0


def test_read_caches_collapse_repeat_stop_traffic(machine, opened) -> None:
    """Within one stop, repeated register reads reuse one hardware read.

    The IDE asks for registers up to five times per stop (regs, stack,
    locals, every watch). The session caches the first read keyed on its
    run/data generations; a step moves the generation and forces a re-read.
    The DP/AP `ops` count in the envelopes is the proof.
    """
    code, lines, _ = machine(
        "debug", "--machine", "--core", "rtu0.0",
        stdin="halt\nregs\nregs\nstep\nregs\nquit\n",
    )
    assert code == 0
    _halt, regs1, regs2, _step, regs3, _quit = envelopes(lines)
    assert regs1["ok"] and regs2["ok"] and regs3["ok"]
    # First read pays the full register-transfer traffic; the repeat pays
    # only the selected core's state refresh.
    assert regs2["ops"] < regs1["ops"] // 4
    # The step bumped the generation: the next read is hardware again.
    assert regs3["ops"] > regs1["ops"] // 2


def test_stdout_is_json_only_and_humans_get_stderr(machine, opened) -> None:
    # The fixture already json.loads()es every stdout line; what remains is to
    # show the human chatter really went to the console stream instead.
    code, lines, human = machine(
        "debug", "--machine", "--core", "rtu0.0", stdin="halt\nquit\n"
    )
    assert code == 0
    assert "session open" in human
    assert "session closed" in human
    assert all(isinstance(l, dict) for l in lines)


def test_monitor_pushes_stopped_event_when_a_resumed_core_halts(
    opened, monkeypatch
) -> None:
    """A breakpoint hit is pushed, not polled for.

    After `resume`, the halt monitor watches the core; when it re-enters
    Debug state with a breakpoint status on its own, an asynchronous
    {"event":"stopped",...} line appears between envelopes within one 50 ms
    sweep -- the frontend no longer needs a 1 Hz poll to notice.

    (The synthetic target models resume-with-an-armed-comparator as an
    immediate re-halt reported by the resume itself, so this test resumes
    clean and then flips the halt bits directly -- the spontaneous stop the
    monitor exists to catch.)
    """
    device = opened
    human = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=human, width=200, no_color=True))
    monkeypatch.setattr(cli, "err_console", Console(file=human, width=200, no_color=True))
    proto = io.StringIO()

    class SlowStdin:
        """Feeds the session, then simulates the silicon hitting a
        breakpoint and holds the session open until the event lands."""

        def isatty(self) -> bool:
            return False

        def __iter__(self):
            yield "halt\n"
            yield "resume\n"
            edscr = device.mem.get(CORE0_DEBUG + r52.EDSCR, 0)
            device.mem[CORE0_DEBUG + r52.EDSCR] = (
                edscr & ~r52.EDSCR_STATUS_MASK
            ) | r52.EDSCR_STATUS_BREAKPOINT
            device.mem[CORE0_DEBUG + r52.EDPRSR] = (
                device.mem.get(CORE0_DEBUG + r52.EDPRSR, 0) | r52.EDPRSR_HALTED
            )
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and '"stopped"' not in proto.getvalue():
                time.sleep(0.02)
            yield "quit\n"

    monkeypatch.setattr(
        sys, "argv", ["s32edbg", "debug", "--machine", "--core", "rtu0.0"]
    )
    monkeypatch.setattr(sys, "stdin", SlowStdin())
    monkeypatch.setattr(sys, "stdout", proto)
    code = cli.entry()
    assert code == 0

    lines = [json.loads(l) for l in proto.getvalue().splitlines() if l.strip()]
    stopped = [l for l in lines if l.get("event") == "stopped"]
    assert len(stopped) == 1
    ev = stopped[0]
    assert ev["family"] == "r52"
    assert ev["core"] == "rtu0.0"
    assert "breakpoint" in ev["status"]
    # No comparator is armed, and the shadow-served attribution says so
    # honestly instead of inventing an address.
    assert ev["location"]["known"] is False
    # The event line must sit between the resume envelope and quit's.
    kinds = [
        ("event", l["event"]) if "event" in l else ("seq", l["seq"]) for l in lines
    ]
    resume_at = kinds.index(("seq", 2))
    stopped_at = kinds.index(("event", "stopped"))
    quit_at = kinds.index(("seq", 3))
    assert resume_at < stopped_at < quit_at


def test_eof_without_quit_still_restores(machine, opened) -> None:
    device = opened
    code, lines, _ = machine(
        "debug", "--machine", "--core", "rtu0.0", stdin="halt\n"
    )
    assert code == 0
    bye = events(lines, "bye")
    assert len(bye) == 1
    assert any("resumed rtu0.0" in n for n in bye[0]["restore_notes"])
    assert not device.mem.get(CORE0_DEBUG + r52.EDPRSR, 0) & r52.EDPRSR_HALTED


# ---------------------------------------------------------------------------
# Error kinds mirror the exit-code contract
# ---------------------------------------------------------------------------


def test_error_kinds(machine, opened) -> None:
    code, lines, _ = machine(
        "debug", "--machine", "--core", "rtu0.0",
        stdin=(
            "wat\n"                       # unknown command
            "doctor\n"                    # blocked inside a session
            "bp set 12345\n"              # bare number: usage
            "mem write 0x00410000 0x1\n"  # no --yes in machine mode: usage
            "quit\n"
        ),
    )
    assert code == 0
    envs = envelopes(lines)
    kinds = [e.get("error", {}).get("kind") for e in envs[:4]]
    assert kinds == ["unknown-command", "refused", "usage", "usage"]
    assert all(e["ok"] is False for e in envs[:4])
    assert "cannot run inside a debug session" in envs[1]["error"]["message"]
    assert "0x-prefixed" in envs[2]["error"]["message"]
    assert "--yes" in envs[3]["error"]["message"]


def test_machine_requires_piped_stdin(machine, opened, monkeypatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:  # pragma: no cover - trivial
            return True

    human = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=human, width=200, no_color=True))
    monkeypatch.setattr(cli, "err_console", Console(file=human, width=200, no_color=True))
    monkeypatch.setattr(sys, "argv", ["s32edbg", "debug", "--machine"])
    monkeypatch.setattr(sys, "stdin", Tty("quit\n"))
    assert cli.entry() == 1


def test_machine_refuses_batch_commands(machine, opened) -> None:
    code, lines, _ = machine(
        "debug", "--machine", "-c", "halt", stdin="quit\n"
    )
    assert code == 1
    assert lines == []


# ---------------------------------------------------------------------------
# Builtins and payload capture
# ---------------------------------------------------------------------------


def test_use_and_sync_report_structured_state(machine, opened) -> None:
    code, lines, _ = machine(
        "debug", "--machine",
        stdin="use rtu0.1\nuse\nsync status\nquit\n",
    )
    assert code == 0
    envs = envelopes(lines)
    assert envs[0]["cmd"] == "use"
    assert envs[0]["result"] == {"focus": "rtu0.1", "m33_focus": None}
    assert envs[1]["result"] == {"focus": "rtu0.1", "m33_focus": None}
    assert envs[2]["cmd"] == "sync"
    assert envs[2]["result"] == {"sync": False}


def test_bp_set_and_clear_carry_payloads(machine, opened) -> None:
    code, lines, _ = machine(
        "debug", "--machine", "--core", "rtu0.0",
        stdin="halt\nbp set 0x00400120\nbp clear 0\nquit\n",
    )
    assert code == 0
    envs = envelopes(lines)
    bp_env = envs[1]
    assert bp_env["ok"] is True
    assert bp_env["result"]["breakpoint"]["index"] == 0
    clear_env = envs[2]
    assert clear_env["result"] == {"core": "rtu0.0", "cleared": [0], "all": False}


def test_poll_reports_every_core_without_touching_run_state(machine, opened) -> None:
    code, lines, _ = machine(
        "debug", "--machine", "--core", "rtu0.0",
        stdin="poll\nhalt\npoll\npoll\nquit\n",
    )
    assert code == 0
    envs = envelopes(lines)
    first = envs[0]
    assert first["ok"] is True
    assert first["cmd"] == "poll"
    r52_rows = {row["core"]: row for row in first["result"]["r52"]}
    assert set(r52_rows) == {"rtu0.0", "rtu0.1"}
    assert r52_rows["rtu0.0"]["halted"] is False
    assert [row["name"] for row in first["result"]["m33"]] == ["smu", "ce0", "ce1"]
    assert first["result"]["focus"] == "rtu0.0"
    assert first["result"]["sync"] is False

    second = envs[2]
    rows = {row["core"]: row for row in second["result"]["r52"]}
    assert rows["rtu0.0"]["halted"] is True
    assert "location" in rows["rtu0.0"]
    # A poll is status-only: polling a halted core must report it halted
    # again, not resume it.
    third = envs[3]
    rows = {row["core"]: row for row in third["result"]["r52"]}
    assert rows["rtu0.0"]["halted"] is True


# ---------------------------------------------------------------------------
# Long operations stream progress
# ---------------------------------------------------------------------------


def test_load_emits_progress_events_before_its_envelope(machine, opened, tmp_path) -> None:
    p = _write_load_elf(tmp_path)
    code, lines, _ = machine(
        "debug", "--machine", stdin=f'load "{p}"\nquit\n'
    )
    assert code == 0
    progress = [l for l in lines if l.get("event") == "progress"]
    assert progress, "the load must stream progress"
    assert all(ev["seq"] == 1 for ev in progress)
    load_env = next(e for e in envelopes(lines) if e["seq"] == 1)
    assert load_env["ok"] is True
    assert load_env["result"]["entry"] == "0x32100021"
    # Stream order: every progress line precedes the result envelope.
    assert lines.index(progress[-1]) < lines.index(load_env)


def test_json_file_option_is_not_written_in_machine_mode(machine, opened, tmp_path) -> None:
    out = tmp_path / "out.json"
    code, lines, _ = machine(
        "--json", str(out), "debug", "--machine", "--core", "rtu0.0",
        stdin="halt\nquit\n",
    )
    assert code == 0
    assert not out.exists(), "machine mode captures payloads; it never writes files"
    assert envelopes(lines)[0]["result"]["halt"][0]["core"] == "rtu0.0"

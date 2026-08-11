"""`var sample` streaming: the kHz-class path behind the IDE plot.

Pins: the programmed-access cache that makes repeat same-address reads ~2
transactions, the samples-event stream shape (per-read dt offsets, raw
values), pacing, stop semantics, and that streaming lives only in machine
mode's event channel.
"""

from __future__ import annotations

import io
import json
import sys
import time

import pytest
from conftest import SyntheticTarget
from rich.console import Console

from s32edbg import cli
from s32edbg.dap.adiv6 import Dap
from s32edbg.data.expected_apmap import AP_AXI

ADDR = 0x3178_0000


def test_programmed_access_cache_collapses_repeat_reads() -> None:
    device = SyntheticTarget()
    device.mem[ADDR] = 0x1234
    dap = Dap(device)
    dap.mem_read(AP_AXI, ADDR)
    after_first = dap.ops
    dap.mem_read(AP_AXI, ADDR)
    second_cost = dap.ops - after_first
    # CSW and TAR writes skipped: only the data read remains (plus RDBUFF on
    # a pipelined transport).
    assert second_cost <= 2
    before_third = dap.ops
    dap.mem_read(AP_AXI, ADDR + 8)
    assert dap.ops - before_third > second_cost


def test_block_read_invalidates_the_access_cache() -> None:
    device = SyntheticTarget()
    for i in range(4):
        device.mem[ADDR + 4 * i] = i
    dap = Dap(device)
    dap.mem_read(AP_AXI, ADDR)
    dap.mem_read_block(AP_AXI, ADDR, 4)  # auto-increment moved TAR
    before = dap.ops
    dap.mem_read(AP_AXI, ADDR)
    # The single read after a block must re-program the AP, not trust the memo.
    assert dap.ops - before > 2


@pytest.fixture()
def opened(monkeypatch):
    device = SyntheticTarget()
    monkeypatch.setattr(cli, "discover", lambda *a, **k: object())
    monkeypatch.setattr(cli, "make_transport", lambda kind, **kw: device)
    return device


def test_var_sample_streams_events_and_stops(opened, monkeypatch) -> None:
    device = opened
    human = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=human, width=200, no_color=True))
    monkeypatch.setattr(cli, "err_console", Console(file=human, width=200, no_color=True))
    proto = io.StringIO()

    class SlowStdin:
        def isatty(self) -> bool:
            return False

        def __iter__(self):
            yield (
                f'var sample start --signal "cnt@0x{ADDR:08X}:32" --rate 200\n'
            )
            # Drive the "application": bump the value while the streamer runs.
            deadline = time.monotonic() + 4.0
            tick = 0
            while (
                time.monotonic() < deadline
                and proto.getvalue().count('"samples"') < 5
            ):
                device.mem[ADDR] = tick
                tick += 1
                time.sleep(0.01)
            yield "var sample status\n"
            yield "var sample stop\n"
            time.sleep(0.2)
            yield "quit\n"

    monkeypatch.setattr(
        sys, "argv", ["s32edbg", "debug", "--machine", "--core", "rtu0.0"]
    )
    monkeypatch.setattr(sys, "stdin", SlowStdin())
    monkeypatch.setattr(sys, "stdout", proto)
    assert cli.entry() == 0

    lines = [json.loads(l) for l in proto.getvalue().splitlines() if l.strip()]
    envs = [l for l in lines if "event" not in l]
    events = [l for l in lines if l.get("event") == "samples"]

    start_env = envs[0]
    assert start_env["ok"] is True, start_env
    assert start_env["result"]["streaming"] is True
    assert start_env["result"]["rate"] == 200

    assert len(events) >= 3
    for ev in events:
        assert ev["t0_ms"] > 0
        sig = ev["signals"][0]
        assert sig["name"] == "cnt"
        assert len(sig["dt"]) == len(sig["v"]) > 0
        assert all(b >= a for a, b in zip(sig["dt"], sig["dt"][1:]))
    # The stream saw the "application" moving the value.
    seen = {v for ev in events for v in ev["signals"][0]["v"]}
    assert len(seen) > 3

    status_env = envs[1]
    assert status_env["result"]["streaming"] is True
    assert status_env["result"]["samples_emitted"] > 0

    stop_env = envs[2]
    assert stop_env["result"]["streaming"] is False

    # Nothing streams after stop (at most one already-in-flight batch).
    stop_index = lines.index(stop_env)
    assert sum(
        1 for l in lines[stop_index + 1 :] if l.get("event") == "samples"
    ) <= 1


def test_var_sample_rejects_bad_specs(opened, monkeypatch) -> None:
    human = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=human, width=200, no_color=True))
    monkeypatch.setattr(cli, "err_console", Console(file=human, width=200, no_color=True))
    proto = io.StringIO()
    monkeypatch.setattr(
        sys, "argv", ["s32edbg", "debug", "--machine", "--core", "rtu0.0"]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        'var sample start --signal "nonsense"\n'
        "var sample start --signal \"a@0x31780000:32\" --signal \"b@0x31780004:32\" "
        "--signal \"c@0x31780008:32\" --signal \"d@0x3178000C:32\" "
        "--signal \"e@0x31780010:32\" --signal \"f@0x31780014:32\" "
        "--signal \"g@0x31780018:32\"\n"
        "quit\n"
    ))
    monkeypatch.setattr(sys, "stdout", proto)
    assert cli.entry() == 0
    lines = [json.loads(l) for l in proto.getvalue().splitlines() if l.strip()]
    envs = [l for l in lines if "event" not in l]
    assert envs[0]["ok"] is False
    assert "name@0xADDR:bits" in envs[0]["error"]["message"]
    assert envs[1]["ok"] is False
    assert "at most 6" in envs[1]["error"]["message"]
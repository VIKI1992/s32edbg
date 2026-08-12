"""Foreign comparators under `debug --machine --keep-breakpoints`.

The IDE always starts its session with --keep-breakpoints so a comparator
armed by a previous session or another tool survives to be adopted or
cleared by the user, instead of dying silently at the first write-touch
(guide §5.7 sanitation). These tests pin the three contracts the adoption
flow rests on: the foreign slot is listed, `bp set` never stomps it, and
`bp clear <index>` removes exactly it.
"""

from __future__ import annotations

import io
import json
import sys

import pytest
from conftest import CORE0_DEBUG, SyntheticTarget
from rich.console import Console

from s32edbg import cli
from s32edbg.data import r52debug as r52

FOREIGN_BP_ADDR = 0x32100040
FOREIGN_WP_ADDR = 0x317BA000
BP_SLOT = 3
WP_SLOT = 1


@pytest.fixture()
def machine(monkeypatch):
    human = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=human, width=200, no_color=True))
    monkeypatch.setattr(cli, "err_console", Console(file=human, width=200, no_color=True))
    device = SyntheticTarget()
    monkeypatch.setattr(cli, "discover", lambda *a, **k: object())
    monkeypatch.setattr(cli, "make_transport", lambda kind, **kw: device)

    def invoke(stdin: str, *, keep: bool = True):
        proto = io.StringIO()
        argv = ["s32edbg", "debug", "--machine", "--core", "rtu0.0"]
        if keep:
            argv.append("--keep-breakpoints")
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        monkeypatch.setattr(sys, "stdout", proto)
        code = cli.entry()
        lines = [json.loads(l) for l in proto.getvalue().splitlines() if l.strip()]
        return code, [l for l in lines if "event" not in l]

    return device, invoke


def arm_foreign(device: SyntheticTarget) -> None:
    """A previous session's leftovers, staged at register level."""
    value, control = r52.encode_breakpoint(FOREIGN_BP_ADDR)
    device.mem[CORE0_DEBUG + r52.dbgbvr(BP_SLOT)] = value
    device.mem[CORE0_DEBUG + r52.dbgbcr(BP_SLOT)] = control
    wvalue, wcontrol = r52.encode_watchpoint(FOREIGN_WP_ADDR, size=4, on="store")
    device.mem[CORE0_DEBUG + r52.dbgwvr(WP_SLOT)] = wvalue
    device.mem[CORE0_DEBUG + r52.dbgwcr(WP_SLOT)] = wcontrol


def test_foreign_slots_survive_and_are_listed(machine) -> None:
    device, invoke = machine
    arm_foreign(device)
    code, envs = invoke(
        "halt\n"          # write-touch: would sanitize without the flag
        "bp list\n"
        "wp list\n"
        "quit\n"
    )
    assert code == 0
    bps = envs[1]["result"]["breakpoints"]
    foreign = bps[BP_SLOT]
    assert foreign["enabled"] is True
    assert foreign["address"] == f"0x{FOREIGN_BP_ADDR:08X}"

    wps = envs[2]["result"]["watchpoints"]
    assert wps[WP_SLOT]["enabled"] is True
    assert wps[WP_SLOT]["address"] == f"0x{FOREIGN_WP_ADDR:08X}"
    assert wps[WP_SLOT]["bytes"] == 4
    assert wps[WP_SLOT]["on"] == "store"


def test_without_the_flag_first_touch_wipes_them(machine) -> None:
    device, invoke = machine
    arm_foreign(device)
    code, envs = invoke("halt\nbp list\nquit\n", keep=False)
    assert code == 0
    bps = envs[1]["result"]["breakpoints"]
    assert not any(b.get("enabled") for b in bps), "sanitation is the default"


def test_bp_set_never_stomps_a_foreign_slot(machine) -> None:
    device, invoke = machine
    arm_foreign(device)
    code, envs = invoke(
        "halt\n"
        "bp set 0x32100100\n"
        "wp set 0x317BB000\n"
        "bp list\n"
        "quit\n"
    )
    assert code == 0
    new_bp = envs[1]["result"]["breakpoint"]["index"]
    assert new_bp != BP_SLOT, "allocation must treat foreign slots as occupied"
    new_wp = envs[2]["result"]["watchpoint"]["index"]
    assert new_wp != WP_SLOT
    bps = envs[3]["result"]["breakpoints"]
    assert bps[BP_SLOT]["address"] == f"0x{FOREIGN_BP_ADDR:08X}", "still armed"
    assert bps[new_bp]["enabled"] is True


def test_bp_clear_removes_exactly_the_foreign_slot(machine) -> None:
    device, invoke = machine
    arm_foreign(device)
    code, envs = invoke(
        f"bp clear {BP_SLOT}\n"
        f"wp clear {WP_SLOT}\n"
        "bp list\n"
        "wp list\n"
        "quit\n"
    )
    assert code == 0
    assert envs[0]["result"]["cleared"] == [BP_SLOT]
    assert envs[1]["result"]["cleared"] == [WP_SLOT]
    assert not any(b.get("enabled") for b in envs[2]["result"]["breakpoints"])
    assert not any(w.get("enabled") for w in envs[3]["result"]["watchpoints"])
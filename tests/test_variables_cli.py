"""The `var` command family over the machine protocol, against the fake chip.

Handles are deterministic within a session -- generation 0 at open, ids
allocated in emission order -- which lets one scripted stdin fetch a row,
expand it and write through it. If handle allocation order ever changes,
these tests say so loudly; that is a contract worth pinning.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from conftest import CORE0_DEBUG, SyntheticTarget
from rich.console import Console

pytest.importorskip("elftools", reason="pip install -e .[ide]")
pytest.importorskip("capstone", reason="pip install -e .[ide]")

from s32edbg import cli
from s32edbg.symbols import variables as V
from s32edbg.symbols.dwarf import SymbolIndex

FIXTURE_ELF = Path(__file__).parent / "fixtures" / "fixture.elf"
SYM = f'sym load "{FIXTURE_ELF}"'


@pytest.fixture(scope="module")
def index() -> SymbolIndex:
    return SymbolIndex.load(str(FIXTURE_ELF))


@pytest.fixture(scope="module")
def vindex(index) -> V.VariableIndex:
    return index.variables()


def global_named(vindex, name):
    entries = vindex.globals_by_name.get(name)
    assert entries, f"no global {name!r}"
    return entries[0]


@pytest.fixture()
def machine(monkeypatch):
    human = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=human, width=200, no_color=True))
    monkeypatch.setattr(cli, "err_console", Console(file=human, width=200, no_color=True))
    device = SyntheticTarget()
    monkeypatch.setattr(cli, "discover", lambda *a, **k: object())
    monkeypatch.setattr(cli, "make_transport", lambda kind, **kw: device)

    def invoke(stdin: str):
        proto = io.StringIO()
        monkeypatch.setattr(
            sys, "argv", ["s32edbg", "debug", "--machine", "--core", "rtu0.0"]
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        monkeypatch.setattr(sys, "stdout", proto)
        code = cli.entry()
        lines = [json.loads(l) for l in proto.getvalue().splitlines() if l.strip()]
        return code, [l for l in lines if "event" not in l]

    return device, invoke


def stage_cfg(device, vindex) -> int:
    """Poke cfg's initializer image into synthetic memory; return its address."""
    cfg = global_named(vindex, "cfg").decl
    addr = cfg.static_addr
    tinfo = vindex.type_of(cfg.type_die)
    members = {m.name: m for m in tinfo.members}
    device.mem[addr] = 0x00005A01  # mode=MODE_RUN(1), flags=0x5A
    base = addr + members["limits"].offset
    for i, (lo, hi) in enumerate([(0, 10), (1, 20), (2, 30)]):
        device.mem[base + i * 8] = lo & 0xFFFFFFFF
        device.mem[base + i * 8 + 4] = hi & 0xFFFFFFFF
    greeting_addr = global_named(vindex, "greeting").decl.static_addr
    device.mem[greeting_addr] = 0x6C6C6568       # "hell"
    device.mem[greeting_addr + 4] = 0x0000006F   # "o\0"
    device.mem[addr + members["name"].offset] = greeting_addr
    device.mem[addr + members["next"].offset] = 0
    return addr


def test_var_globals_watch_and_member_paths(machine, vindex) -> None:
    device, invoke = machine
    counter_addr = global_named(vindex, "counter").decl.static_addr
    device.mem[counter_addr] = 5
    cfg_addr = stage_cfg(device, vindex)
    code, envs = invoke(
        f"{SYM}\n"
        "var globals --filter counter\n"
        "var watch counter\n"
        "var watch cfg.limits[1].max\n"
        "var watch cfg.mode\n"
        "var watch nosuch\n"
        "quit\n"
    )
    assert code == 0
    globals_env = envs[1]
    assert globals_env["ok"] is True
    assert globals_env["result"]["total"] == 1
    row = globals_env["result"]["variables"][0]
    assert row["name"] == "counter"
    assert row["value"]["display"] == "5"
    assert row["address"] == f"0x{counter_addr:08X}"
    assert row["handle"] == "0:1"
    assert row["static"] is False

    assert envs[2]["result"]["row"]["value"]["display"] == "5"
    max_row = envs[3]["result"]["row"]
    tinfo = vindex.type_of(global_named(vindex, "cfg").decl.type_die)
    members = {m.name: m for m in tinfo.members}
    lim_elem = vindex.type_of(vindex.type_of(members["limits"].type_die).elem)
    expected = cfg_addr + members["limits"].offset + 1 * lim_elem.size + 4
    assert max_row["address"] == f"0x{expected:08X}"
    assert max_row["value"]["display"] == "20"

    mode_row = envs[4]["result"]["row"]
    assert mode_row["value"]["display"] == "MODE_RUN (1)"

    assert envs[5]["ok"] is False
    assert envs[5]["error"]["kind"] == "usage"
    assert "no variable named" in envs[5]["error"]["message"]


def test_var_expand_struct_and_array_paging(machine, vindex) -> None:
    device, invoke = machine
    stage_cfg(device, vindex)
    code, envs = invoke(
        f"{SYM}\n"
        "var globals --filter cfg --limit 1\n"  # cfg -> handle 0:1
        "var expand 0:1\n"                      # members -> 0:2..0:6
        "var expand 0:4 --limit 2\n"            # limits array page
        "quit\n"
    )
    assert code == 0
    cfg_row = envs[1]["result"]["variables"][0]
    assert cfg_row["handle"] == "0:1"
    assert cfg_row["expandable"] is True
    assert cfg_row["static"] is True

    children = envs[2]["result"]["children"]
    assert [c["name"] for c in children] == ["mode", "flags", "limits", "name", "next"]
    by_name = {c["name"]: c for c in children}
    assert by_name["mode"]["value"]["display"] == "MODE_RUN (1)"
    assert by_name["flags"]["value"]["display"] == "'Z' (0x5A)"  # unsigned char
    assert by_name["flags"]["value"]["hex"] == "0x5A"
    assert by_name["limits"]["handle"] == "0:4"
    assert by_name["limits"]["expandable"] is True
    assert by_name["name"]["kind"] == "pointer"
    assert '"hello"' in by_name["name"]["value"]["display"]
    assert by_name["next"]["expandable"] is True  # NULL discovered on expand

    page = envs[3]["result"]
    assert [c["path"] for c in page["children"]] == ["cfg.limits[0]", "cfg.limits[1]"]
    assert page["more_from"] == 2
    assert page["children"][0]["value"]["display"] == "{…}"


def test_var_write_readback_and_stale_handles(machine, vindex) -> None:
    device, invoke = machine
    counter_addr = global_named(vindex, "counter").decl.static_addr
    device.mem[counter_addr] = 5
    code, envs = invoke(
        f"{SYM}\n"
        "var globals --filter counter\n"  # handle 0:1
        "var write 0:1 0x2A\n"
        "var refresh 0:1\n"
        "halt\n"                           # bumps the generation
        "var refresh 0:1\n"                # now stale
        "quit\n"
    )
    assert code == 0
    write_env = envs[2]
    assert write_env["ok"] is True
    assert write_env["result"]["wrote"] == "0x0000002A"
    assert write_env["result"]["match"] is True
    assert write_env["result"]["row"]["value"]["display"] == "42"
    assert device.mem[counter_addr] == 0x2A

    refresh_env = envs[3]
    assert refresh_env["result"]["rows"][0]["value"]["display"] == "42"

    stale_env = envs[5]
    assert stale_env["ok"] is False
    assert stale_env["error"]["kind"] == "usage"
    assert stale_env["error"]["message"].startswith("stale-handle:")


def test_var_refresh_carries_per_row_errors(machine, vindex) -> None:
    device, invoke = machine
    counter_addr = global_named(vindex, "counter").decl.static_addr
    ticks_addr = global_named(vindex, "ticks").decl.static_addr
    device.mem[counter_addr] = 1
    device.mem[ticks_addr] = 2
    device.faulting.add(ticks_addr)
    code, envs = invoke(
        f"{SYM}\n"
        "var globals --filter counter\n"  # 0:1
        "var globals --filter ticks\n"    # 0:2
        "var refresh 0:1 0:2\n"
        "quit\n"
    )
    assert code == 0
    ticks_row = envs[2]["result"]["variables"][0]
    assert ticks_row["error"] is not None  # faulted at the first read too
    rows = envs[3]["result"]["rows"]
    assert rows[0]["value"]["display"] == "1"
    assert rows[1]["error"] is not None
    assert envs[3]["ok"] is True, "a faulting row must not fail the batch"


def test_var_locals_evaluates_against_the_frame(machine, index, vindex) -> None:
    device, invoke = machine
    middle_f = next(f for f in index.funcs if f.name == "middle")
    # A PC past the prologue whose CFA rule is r7-based, found from the CFI
    # itself so a fixture rebuild cannot silently invalidate the staging.
    pc = None
    for candidate in range(middle_f.low, middle_f.high, 2):
        row = index.cfi_row_at(candidate)
        if row is not None and getattr(row.get("cfa"), "reg", None) == 7:
            pc = candidate
            break
    assert pc is not None, "no r7-based CFA row in middle -- fixture changed?"
    fp = 0x31780F00
    cfa = fp + index.cfi_row_at(pc)["cfa"].offset

    middle_vars = next(fv for fv in vindex.by_func_low.values() if fv.name == "middle")
    x_ops = V.parse_exprloc(middle_vars.cu, middle_vars.params[0].loc_attr.value)
    x_addr = (cfa + x_ops[0].args[0]) & 0xFFFFFFFF
    device.mem[x_addr & ~3] = 0x1234
    device.dlr[CORE0_DEBUG] = pc | 1
    device.regfile.setdefault(CORE0_DEBUG, {})
    device.regfile[CORE0_DEBUG][7] = fp
    device.regfile[CORE0_DEBUG][13] = 0x31780EF0

    code, envs = invoke(
        f"{SYM}\n"
        "halt\n"
        "var locals --core rtu0.0\n"
        "quit\n"
    )
    assert code == 0
    locals_env = envs[2]
    assert locals_env["ok"] is True
    result = locals_env["result"]
    assert result["func"] == "middle"
    assert result["cfa_source"] == "cfi"
    assert result["cfa"] == f"0x{cfa:08X}"
    rows = {r["name"]: r for r in result["variables"]}
    assert set(rows) == {"x", "y"}
    assert rows["x"]["address"] == f"0x{x_addr:08X}"
    assert rows["x"]["value"]["display"] == str(0x1234)


def test_var_locals_without_symbols_is_usage(machine) -> None:
    _device, invoke = machine
    code, envs = invoke("halt\nvar locals --core rtu0.0\nquit\n")
    assert code == 0
    assert envs[1]["ok"] is False
    assert envs[1]["error"]["kind"] == "usage"
    assert "sym load" in envs[1]["error"]["message"]

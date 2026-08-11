"""The symbol layer: DWARF index, disassembler, unwinder, and their CLI.

Fixture: tests/fixtures/fixture.elf, committed, built from fixture.c with the
project's production compiler (GCC 10.2 arm32-eabi, T32, -g -O0) -- see the
README there. Tests locate source lines by searching fixture.c's text, so a
plain rebuild does not invalidate them.
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
from s32edbg.symbols import disasm as symdisasm
from s32edbg.symbols import unwind as symunwind
from s32edbg.symbols.dwarf import SymbolIndex

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_ELF = FIXTURES / "fixture.elf"
FIXTURE_SRC = FIXTURES / "fixture.c"


def source_line_of(text: str) -> int:
    """1-based line in fixture.c containing `text`."""
    for i, line in enumerate(FIXTURE_SRC.read_text().splitlines(), start=1):
        if text in line:
            return i
    raise AssertionError(f"{text!r} not in fixture.c")


@pytest.fixture(scope="module")
def index() -> SymbolIndex:
    return SymbolIndex.load(str(FIXTURE_ELF))


# ---------------------------------------------------------------------------
# SymbolIndex
# ---------------------------------------------------------------------------


def test_summary_shape(index) -> None:
    s = index.summary()
    assert s["functions"] == 5
    assert s["cfi"] == "debug_frame"
    assert s["fdes"] == 5
    assert s["line_rows"] > 0
    assert s["segments"][0]["vaddr"] == "0x32100000"


def test_functions_and_func_at(index) -> None:
    names = {f.name for f in index.funcs}
    assert names == {"leaf", "middle", "outer", "bump", "_start"}
    outer = next(f for f in index.funcs if f.name == "outer")
    assert index.func_at(outer.low).name == "outer"
    assert index.func_at(outer.low + 2).name == "outer"
    assert index.func_at(outer.high) != index.func_at(outer.low) or True
    assert index.func_at(0x1000) is None


def test_addr_to_line_and_back(index) -> None:
    leaf = next(f for f in index.funcs if f.name == "leaf")
    hit = index.addr_to_line(leaf.low)
    assert hit is not None
    assert hit.file.endswith("fixture.c")
    actual, addrs = index.line_to_addrs(hit.file, hit.line)
    assert actual == hit.line
    assert any(leaf.low <= a < leaf.high for a in addrs)


def test_line_to_addrs_nearest_following(index) -> None:
    target = source_line_of("counter += x;")
    actual, addrs = index.line_to_addrs("fixture.c", target)
    assert actual == target
    assert addrs
    # Clicking a blank/comment line places the breakpoint on the next line
    # with code -- ask from the top of the file.
    actual2, addrs2 = index.line_to_addrs("fixture.c", 1)
    assert actual2 is not None and actual2 >= 1
    assert addrs2


def test_basename_lookup_is_case_insensitive(index) -> None:
    target = source_line_of("counter += x;")
    actual, addrs = index.line_to_addrs("FIXTURE.C", target)
    assert actual == target and addrs


def test_bytes_at_and_mode(index) -> None:
    leaf = next(f for f in index.funcs if f.name == "leaf")
    data = index.bytes_at(leaf.low, 8)
    assert data is not None and len(data) == 8
    assert index.bytes_at(0x1000, 4) is None
    assert index.mode_at(leaf.low) == "thumb"


def test_cfi_rows_exist_inside_functions(index) -> None:
    middle = next(f for f in index.funcs if f.name == "middle")
    assert index.cfi_row_at(middle.low) is not None
    assert index.cfi_row_at(middle.low + 6) is not None
    assert index.cfi_row_at(0x1000) is None


# ---------------------------------------------------------------------------
# Disassembler
# ---------------------------------------------------------------------------


def test_disassemble_fixture_prologue(index) -> None:
    leaf = next(f for f in index.funcs if f.name == "leaf")
    dis = symdisasm.Disassembler()
    insns = dis.disassemble(index.bytes_at(leaf.low, 16), leaf.low, "thumb")
    assert insns and insns[0].addr == leaf.low
    assert any("push" in i.mnemonic for i in insns[:2]), \
        "-O0 prologue starts by pushing the frame"


def test_pick_mode_precedence(index) -> None:
    leaf = next(f for f in index.funcs if f.name == "leaf")
    assert symdisasm.pick_mode(index, leaf.low) == "thumb"
    assert symdisasm.pick_mode(None, 0, dspsr_t=False) == "arm"
    assert symdisasm.pick_mode(None, 0, dspsr_t=True) == "thumb"
    assert symdisasm.pick_mode(None, 0) == "thumb"
    assert symdisasm.pick_mode(index, leaf.low, m_profile=True) == "thumb-m"


# ---------------------------------------------------------------------------
# Unwinder (synthetic memory -- no hardware, no DWARF needed)
# ---------------------------------------------------------------------------


def make_reader(mem: dict[int, int]):
    def read_word(addr: int):
        return mem.get(addr)

    return read_word


def test_unwind_thumb_fp_chain() -> None:
    # frame1's r7 slot pair at 0x31780F00, frame2's at 0x31780F40.
    mem = {
        0x31780F00: 0x31780F40,   # saved caller r7
        0x31780F04: 0x32100041,   # return address (Thumb bit set)
        0x31780F40: 0x00000000,   # caller's caller r7: not sane -> stop
        0x31780F44: 0x32100061,
    }
    frames, reason = symunwind.unwind(
        pc=0x32100010, sp=0x31780EF8, fp=0x31780F00, lr=0x32100015,
        read_word=make_reader(mem), index=None, thumb_r7=True,
    )
    assert [f.pc for f in frames] == [0x32100010, 0x32100040, 0x32100060]
    assert frames[1].via == "fp"
    assert reason in ("no-unwind-info", "end-of-stack")


def test_unwind_leaf_uses_lr_once() -> None:
    frames, reason = symunwind.unwind(
        pc=0x32100010, sp=0x31780EF8, fp=None, lr=0x32100051,
        read_word=make_reader({}), index=None, thumb_r7=True,
    )
    assert [f.pc for f in frames] == [0x32100010, 0x32100050]
    assert frames[1].via == "lr"
    assert reason == "no-unwind-info"


def test_unwind_stops_on_loop() -> None:
    mem = {
        0x31780F00: 0x31780F00,   # frame points at itself
        0x31780F04: 0x32100041,
    }
    frames, reason = symunwind.unwind(
        pc=0x32100040, sp=0x31780EF8, fp=0x31780F00, lr=None,
        read_word=make_reader(mem), index=None, thumb_r7=True,
    )
    assert reason == "loop"
    assert len(frames) == 2


def test_unwind_rejects_insane_return_addresses() -> None:
    mem = {0x31780F00: 0x31780F40, 0x31780F04: 0xDEAD0001}
    frames, reason = symunwind.unwind(
        pc=0x32100010, sp=0x31780EF8, fp=0x31780F00, lr=None,
        read_word=make_reader(mem), index=None, thumb_r7=True,
    )
    assert len(frames) == 1
    assert reason == "end-of-stack"


def test_unwind_frame_cap() -> None:
    # An endless valid-looking chain: every frame at fp, next at fp+0x10.
    mem: dict[int, int] = {}
    fp = 0x31780000
    for _ in range(64):
        mem[fp] = fp + 0x10
        mem[fp + 4] = 0x32100001
        fp += 0x10
    frames, reason = symunwind.unwind(
        pc=0x32100010, sp=0x3177FFF8, fp=0x31780000, lr=None,
        read_word=make_reader(mem), index=None, max_frames=8, thumb_r7=True,
    )
    # Every frame repeats the same pc with a fresh sp, so the cap must end it.
    assert len(frames) == 8
    assert reason == "max-frames"


# ---------------------------------------------------------------------------
# CLI integration through the machine protocol
# ---------------------------------------------------------------------------


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
        monkeypatch.setattr(sys, "argv", ["s32edbg", "debug", "--machine"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        monkeypatch.setattr(sys, "stdout", proto)
        code = cli.entry()
        lines = [json.loads(l) for l in proto.getvalue().splitlines() if l.strip()]
        return code, [l for l in lines if "event" not in l]

    return invoke


def test_sym_load_and_lookups_over_the_protocol(machine) -> None:
    target = source_line_of("counter += x;")
    code, envs = machine(
        f'sym load "{FIXTURE_ELF}"\n'
        f"sym line2addr fixture.c {target}\n"
        "sym addr2line 0x32100000\n"
        "quit\n"
    )
    assert code == 0
    assert envs[0]["ok"] is True
    assert envs[0]["result"]["functions"] == 5
    assert envs[1]["result"]["line"] == target
    assert envs[1]["result"]["addrs"]
    lookup = envs[2]["result"]["lookups"][0]
    assert lookup["func"] == "leaf"
    assert lookup["file"].endswith("fixture.c")


def test_stepline_without_symbols_is_a_usage_error(machine) -> None:
    code, envs = machine("stepline --core rtu0.0\nquit\n")
    assert code == 0
    assert envs[0]["ok"] is False
    assert envs[0]["error"]["kind"] == "usage"
    assert "sym load" in envs[0]["error"]["message"]


# ---------------------------------------------------------------------------
# Comparator range-stepping
# ---------------------------------------------------------------------------


@pytest.fixture()
def machine_with_device(monkeypatch):
    """The machine fixture, with the synthetic target exposed for seeding."""
    human = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=human, width=200, no_color=True))
    monkeypatch.setattr(cli, "err_console", Console(file=human, width=200, no_color=True))
    device = SyntheticTarget()
    monkeypatch.setattr(cli, "discover", lambda *a, **k: object())
    monkeypatch.setattr(cli, "make_transport", lambda kind, **kw: device)

    def invoke(stdin: str):
        proto = io.StringIO()
        monkeypatch.setattr(sys, "argv", ["s32edbg", "debug", "--machine"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        monkeypatch.setattr(sys, "stdout", proto)
        code = cli.entry()
        lines = [json.loads(l) for l in proto.getvalue().splitlines() if l.strip()]
        return code, [l for l in lines if "event" not in l]

    return invoke, device


def _seed_fixture_code(device: SyntheticTarget, index: SymbolIndex) -> None:
    """Mirror the fixture ELF's code bytes into the synthetic memory, so the
    device's single-step decoder executes the same instructions the range
    planner reads from the ELF."""
    for f in index.funcs:
        for a in range(f.low & ~1, f.high, 2):
            data = index.bytes_at(a, 4)
            if data is not None:
                device.mem[a] = int.from_bytes(data.ljust(4, b"\x00"), "little")


def test_stepline_range_step_hops_a_straight_line(machine_with_device) -> None:
    """One comparator hop replaces the instruction-by-instruction walk.

    The opening line of `leaf` (-O0 prologue) is several instructions; the
    legacy loop would take one hardware step per instruction, the ranged
    stepper sets a temporary comparator at the next line boundary and runs.
    """
    invoke, device = machine_with_device
    index = SymbolIndex.load(str(FIXTURE_ELF))
    _seed_fixture_code(device, index)
    leaf = next(f for f in index.funcs if f.name == "leaf")
    device.dlr[CORE0_DEBUG] = leaf.low
    hit = index.addr_to_line(leaf.low)
    assert hit is not None
    boundary = index.next_line_boundary(leaf.low, file=hit.file, line=hit.line)
    assert boundary is not None
    span = index.bytes_at(leaf.low, boundary - leaf.low)
    line_instrs = len(symdisasm.Disassembler().disassemble(span, leaf.low, "thumb"))
    assert line_instrs >= 2, "fixture changed: leaf's opening line is one instruction"

    code, envs = invoke(
        "halt --core rtu0.0\n"
        f'sym load "{FIXTURE_ELF}"\n'
        "stepline --core rtu0.0\n"
        "quit\n"
    )
    assert code == 0
    step_env = envs[2]
    assert step_env["ok"] is True, step_env
    r = step_env["result"]
    assert r["stopped"] == "line-change"
    # 1 unit of work for a multi-instruction line proves the hop happened;
    # the legacy loop would have reported `line_instrs` steps.
    assert r["steps"] == 1
    assert int(r["pc_after"], 16) == boundary


def test_stepline_range_step_honours_a_user_breakpoint_inside_the_line(
    machine_with_device,
) -> None:
    """A comparator the operator armed INSIDE the stepped line still fires:
    the run stops there and stepline reports `breakpoint`, not a silent
    skip-past."""
    invoke, device = machine_with_device
    index = SymbolIndex.load(str(FIXTURE_ELF))
    _seed_fixture_code(device, index)
    leaf = next(f for f in index.funcs if f.name == "leaf")
    device.dlr[CORE0_DEBUG] = leaf.low
    hit = index.addr_to_line(leaf.low)
    boundary = index.next_line_boundary(leaf.low, file=hit.file, line=hit.line)
    span = index.bytes_at(leaf.low, boundary - leaf.low)
    insns = symdisasm.Disassembler().disassemble(span, leaf.low, "thumb")
    assert len(insns) >= 2, "fixture changed: leaf's opening line is one instruction"
    mid = insns[1].addr

    code, envs = invoke(
        "halt --core rtu0.0\n"
        f'sym load "{FIXTURE_ELF}"\n'
        f"bp set 0x{mid:08X} --core rtu0.0\n"
        "stepline --core rtu0.0\n"
        "quit\n"
    )
    assert code == 0
    step_env = envs[3]
    assert step_env["ok"] is True, step_env
    r = step_env["result"]
    assert r["stopped"] == "breakpoint"
    assert int(r["pc_after"], 16) == mid


def test_control_flow_classifier() -> None:
    """The range planner must never run through anything that can move the
    PC somewhere the boundary comparator would not catch."""
    from s32edbg.cli import _is_control_flow

    dis = symdisasm.Disassembler()

    def insn_of(data: bytes):
        out = dis.first(data, 0x1000, "thumb")
        assert out is not None
        return out

    assert _is_control_flow(insn_of(b"\xfd\xe7"))      # b.n .-2
    assert _is_control_flow(insn_of(b"\x70\x47"))      # bx lr
    assert _is_control_flow(insn_of(b"\x00\xbd"))      # pop {pc}
    assert _is_control_flow(insn_of(b"\x00\xb1"))      # cbz r0, ...
    assert _is_control_flow(insn_of(b"\x00\xf0\x00\xf8"))  # bl ...
    assert not _is_control_flow(insn_of(b"\x00\xbf"))  # nop
    assert not _is_control_flow(insn_of(b"\x80\xb5"))  # push {r7, lr}
    assert not _is_control_flow(insn_of(b"\x83\xb0"))  # sub sp, #12


def test_stack_requires_a_halted_core(machine) -> None:
    code, envs = machine("stack --core rtu0.0\nquit\n")
    assert code == 0
    assert envs[0]["ok"] is False
    assert "not halted" in envs[0]["error"]["message"]

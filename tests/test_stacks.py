"""The stack monitor: ARXML config parsing, discovery, painted-word scans.

Fixture: stacks_os_ecuc.arxml names three stacks. TestA and TestB join to
``OsCfg_Stack_<name>_Dyn`` arrays in fixture.elf; Ghost deliberately has no
symbol (the stale-ELF path). TestB's ARXML size (96) disagrees with its ELF
array (128) on purpose -- the scanner must say so and scan the ARXML size.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
from pathlib import Path

import pytest
from conftest import SyntheticTarget
from rich.console import Console

pytest.importorskip("elftools", reason="pip install -e .[ide]")
pytest.importorskip("capstone", reason="pip install -e .[ide]")

from s32edbg import cli, stackcfg

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_ELF = FIXTURES / "fixture.elf"
ARXML = FIXTURES / "stacks_os_ecuc.arxml"
SYM = f'sym load "{FIXTURE_ELF}"'
FILL = 0xAAAAAAAA
TESTA_ADDR = 0x32100118  # OsCfg_Stack_TestA_Dyn, 64 words
TESTB_ADDR = 0x32100218  # OsCfg_Stack_TestB_Dyn, 32 words (ARXML says 24)


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


def paint(device, addr: int, words: int) -> None:
    for i in range(words):
        device.mem[addr + 4 * i] = FILL


# -- stackcfg: parsing and discovery ----------------------------------------


def test_parse_os_ecuc_reads_every_stack_container() -> None:
    entries = stackcfg.parse_os_ecuc(ARXML)
    by_name = {e.name: e for e in entries}
    assert set(by_name) == {"TestA", "TestB", "Ghost"}

    a = by_name["TestA"]
    assert a.size == 256
    assert a.stack_type == "CORE_KERNEL_STACK"
    assert a.core == "OsCore0"
    assert a.users == ["InitTask", "TimerIsr"]

    b = by_name["TestB"]
    assert b.size == 96
    assert b.core == "OsCore1"
    assert b.users == ["WorkerTask"]

    assert by_name["Ghost"].size == 512
    assert by_name["Ghost"].users == []


def test_discover_arxml_gates_on_actual_stacks(tmp_path) -> None:
    (tmp_path / "deep" / "config").mkdir(parents=True)
    real = tmp_path / "deep" / "config" / "app_Os_ecuc.arxml"
    shutil.copy(ARXML, real)
    # Decoy 1: valid XML, no probe string at all.
    (tmp_path / "other.arxml").write_text("<AUTOSAR><X>nothing</X></AUTOSAR>")
    # Decoy 2: the probe string appears but no stack containers exist --
    # a pre-merge ECUC snapshot in miniature. Must be parse-gated out.
    (tmp_path / "snapshot.arxml").write_text(
        "<AUTOSAR><NOTE>OsStackSummary appears only as text</NOTE></AUTOSAR>"
    )
    # Decoy 3: probe hit but not XML -- parse errors are skipped, not fatal.
    (tmp_path / "broken.arxml").write_text("OsStackSummary <<< not xml")
    # A copy inside .git is never considered.
    (tmp_path / ".git").mkdir()
    shutil.copy(ARXML, tmp_path / ".git" / "stale.arxml")

    assert stackcfg.discover_arxml(tmp_path) == [str(real)]


def test_discover_arxml_falls_back_past_naming(tmp_path) -> None:
    """A stack-bearing file with an unconventional name is still found --
    the name-plausibility pass is an optimization, not a filter."""
    real = tmp_path / "weird.arxml"
    shutil.copy(ARXML, real)
    assert stackcfg.discover_arxml(tmp_path) == [str(real)]


# -- the watermark scanner, driven directly ---------------------------------


def _reader(words: list[int]):
    def read_at(idx: int, count: int) -> list[int]:
        return words[idx : idx + count]

    return read_at


def test_stack_watermark_finds_the_first_dirty_word() -> None:
    words = [FILL] * 64
    for i in range(54, 64):
        words[i] = 0x11111111
    w, canary_ok = cli._stack_watermark(_reader(words), 64, FILL)
    assert (w, canary_ok) == (54, True)


def test_stack_watermark_survives_fill_valued_live_data() -> None:
    # Painted prefix [0, 20); used region contains a stray fill-valued word
    # at 30 that a bare binary search could mistake for free space.
    words = [0x22222222] * 64
    for i in range(20):
        words[i] = FILL
    words[30] = FILL
    w, _canary = cli._stack_watermark(_reader(words), 64, FILL)
    assert w == 20


def test_stack_watermark_edges() -> None:
    untouched = [FILL] * 32
    w, canary_ok = cli._stack_watermark(_reader(untouched), 32, FILL)
    assert (w, canary_ok) == (32, True)

    unpainted = [0x00000123] * 32
    w, canary_ok = cli._stack_watermark(_reader(unpainted), 32, FILL)
    assert (w, canary_ok) == (0, False)

    canary_hit = [FILL] * 32
    canary_hit[0] = 0xDEADBEEF
    w, canary_ok = cli._stack_watermark(_reader(canary_hit), 32, FILL)
    assert w == 0
    assert canary_ok is False


# -- machine-mode end to end -------------------------------------------------


def test_stacks_config_and_scan(machine) -> None:
    device, invoke = machine
    # TestA: fully painted, top 8 of 64 words dirtied -> 32/256 B = 12.5 %.
    paint(device, TESTA_ADDR, 64)
    for i in range(56, 64):
        device.mem[TESTA_ADDR + 4 * i] = 0xCAFE0000 + i
    # TestB: ARXML scans 24 words; paint them, dirty the top 4 -> 16/96 B.
    paint(device, TESTB_ADDR, 24)
    for i in range(20, 24):
        device.mem[TESTB_ADDR + 4 * i] = 0xBEEF0000 + i

    code, envs = invoke(
        f"{SYM}\n"
        f'stacks config --project "{FIXTURES}"\n'
        "stacks scan\n"
        "quit\n"
    )
    assert code == 0

    config_env = envs[1]
    assert config_env["ok"] is True
    assert config_env["cmd"] == "stacks config"
    cfg = config_env["result"]
    assert cfg["arxml"].endswith("stacks_os_ecuc.arxml")
    assert cfg["total"] == 3
    assert {s["name"] for s in cfg["stacks"]} == {"TestA", "TestB", "Ghost"}

    scan_env = envs[2]
    assert scan_env["ok"] is True
    assert scan_env["cmd"] == "stacks scan"
    result = scan_env["result"]
    assert result["pattern"] == "0xAAAAAAAA"
    assert result["scanned"] == 2
    rows = {r["name"]: r for r in result["rows"]}

    a = rows["TestA"]
    assert a["address"] == f"0x{TESTA_ADDR:08X}"
    assert a["size"] == 256
    assert a["size_elf"] == 256
    assert a["used"] == 32
    assert a["free"] == 224
    assert a["pct"] == 12.5
    assert a["overflow"] is False
    assert a["watermark"] == f"0x{TESTA_ADDR + 4 * 56:08X}"
    assert a["note"] is None
    assert a["users"] == ["InitTask", "TimerIsr"]

    b = rows["TestB"]
    assert b["size"] == 96
    assert b["size_elf"] == 128
    assert "ELF array is 128" in b["note"]
    assert b["used"] == 16
    assert b["pct"] == round(16 * 100.0 / 96, 1)
    assert b["overflow"] is False

    g = rows["Ghost"]
    assert g["error"] is not None
    assert "OsCfg_Stack_Ghost_Dyn" in g["error"]
    assert g["used"] is None


def test_scan_overflow_and_unpainted_notes(machine) -> None:
    device, invoke = machine
    # TestA painted except the canary word -> overflow, watermark at word 1.
    paint(device, TESTA_ADDR, 64)
    device.mem[TESTA_ADDR] = 0x00000001
    # TestB left entirely unpainted (zeros) -> the StartOS note.
    code, envs = invoke(
        f"{SYM}\n"
        f'stacks config --arxml "{ARXML}"\n'
        "stacks scan\n"
        "quit\n"
    )
    assert code == 0
    rows = {r["name"]: r for r in envs[2]["result"]["rows"]}
    assert rows["TestA"]["overflow"] is True
    assert rows["TestA"]["watermark"] == f"0x{TESTA_ADDR:08X}"
    assert rows["TestB"]["used"] == 96
    assert "unpainted" in rows["TestB"]["note"]


def test_scan_fault_hits_one_row_only(machine) -> None:
    device, invoke = machine
    paint(device, TESTA_ADDR, 64)
    paint(device, TESTB_ADDR, 24)
    # The binary search's very first probe of TestA is word 32 -- fault it.
    device.faulting.add(TESTA_ADDR + 4 * 32)
    code, envs = invoke(
        f"{SYM}\n"
        f'stacks config --arxml "{ARXML}"\n'
        "stacks scan\n"
        "quit\n"
    )
    assert code == 0
    result = envs[2]["result"]
    rows = {r["name"]: r for r in result["rows"]}
    assert rows["TestA"]["error"] is not None
    assert rows["TestB"]["error"] is None, "one faulting stack must not kill the scan"
    assert rows["TestB"]["used"] == 0
    assert result["scanned"] == 1


def test_scan_without_config_is_usage(machine) -> None:
    _device, invoke = machine
    code, envs = invoke(f"{SYM}\nstacks scan\nquit\n")
    assert code == 0
    assert envs[1]["ok"] is False
    assert envs[1]["error"]["kind"] == "usage"
    assert "stacks config" in envs[1]["error"]["message"]


def test_config_multiple_candidates_are_reported(machine, tmp_path) -> None:
    _device, invoke = machine
    shutil.copy(ARXML, tmp_path / "one_Os_ecuc.arxml")
    shutil.copy(ARXML, tmp_path / "two_Os_ecuc.arxml")
    code, envs = invoke(
        f'stacks config --project "{tmp_path}"\n'
        "stacks scan\n"
        "quit\n"
    )
    assert code == 0
    result = envs[0]["result"]
    assert len(result["candidates"]) == 2
    assert result["total"] == 0
    assert envs[1]["ok"] is False, "ambiguous discovery must not store a config"

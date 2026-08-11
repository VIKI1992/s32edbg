"""The variables layer: index, types, locations, values, paths -- and its CLI.

Fixture: tests/fixtures/fixture.elf (GCC 10.2 arm32-eabi, -g -O0). Note this
compiler defaults to -fshort-enums (AAPCS): `enum mode` is one byte, so
`struct config`'s offsets are mode@0, flags@1, limits@4 -- the tests read
offsets from the DWARF rather than assuming the packing.
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

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_ELF = FIXTURES / "fixture.elf"


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


# ---------------------------------------------------------------------------
# 1. The index
# ---------------------------------------------------------------------------


def test_globals_table(vindex) -> None:
    names = {g.name for g in vindex.globals}
    assert {"counter", "ticks", "ratio", "greeting", "cfg", "calls"} <= names
    assert global_named(vindex, "counter").external is True
    assert global_named(vindex, "cfg").external is False
    assert global_named(vindex, "counter").decl.static_addr is not None


def test_function_local_static_is_owned(vindex) -> None:
    calls = global_named(vindex, "calls")
    assert calls.owner_func == "bump"
    assert calls.external is False
    bump = next(fv for fv in vindex.by_func_low.values() if fv.name == "bump")
    assert [s.name for s in bump.statics] == ["calls"]
    assert [p.name for p in bump.params] == ["delta"]
    assert "lp" in [l.name for l in bump.locals]


def test_params_and_locals(vindex) -> None:
    middle = next(fv for fv in vindex.by_func_low.values() if fv.name == "middle")
    assert [p.name for p in middle.params] == ["x"]
    assert [l.name for l in middle.locals] == ["y"]
    assert middle.frame_base_attr is not None


def test_type_rendering(vindex) -> None:
    cfg = global_named(vindex, "cfg").decl
    tinfo = vindex.type_of(cfg.type_die)
    assert tinfo.kind == "struct"
    assert tinfo.name == "struct config"
    assert tinfo.expandable

    ratio = global_named(vindex, "ratio").decl
    assert vindex.type_of(ratio.type_die).kind == "float"

    ticks = global_named(vindex, "ticks").decl
    ticks_info = vindex.type_of(ticks.type_die)
    assert ticks_info.kind == "uint"
    assert "volatile" in ticks_info.name

    greeting = global_named(vindex, "greeting").decl
    ginfo = vindex.type_of(greeting.type_die)
    assert ginfo.kind == "array"
    assert ginfo.count == 6  # "hello" + NUL


def test_struct_members_and_offsets(vindex) -> None:
    cfg = global_named(vindex, "cfg").decl
    tinfo = vindex.type_of(cfg.type_die)
    members = {m.name: m for m in tinfo.members}
    assert set(members) == {"mode", "flags", "limits", "name", "next"}
    # Offsets must be consistent with the member sizes the DWARF declares
    # (-fshort-enums makes `mode` one byte on this toolchain).
    mode_info = vindex.type_of(members["mode"].type_die)
    assert mode_info.kind == "enum"
    assert members["mode"].offset == 0
    assert members["flags"].offset == mode_info.size
    limits_info = vindex.type_of(members["limits"].type_die)
    assert limits_info.kind == "array"
    assert limits_info.count == 3
    elem_info = vindex.type_of(limits_info.elem)
    assert elem_info.kind == "struct"
    assert members["name"].offset == members["limits"].offset + 3 * elem_info.size
    name_info = vindex.type_of(members["name"].type_die)
    assert name_info.kind == "pointer"
    assert "char" in name_info.name and "*" in name_info.name


def test_enum_names(vindex) -> None:
    cfg = global_named(vindex, "cfg").decl
    tinfo = vindex.type_of(cfg.type_die)
    mode = next(m for m in tinfo.members if m.name == "mode")
    mode_info = vindex.type_of(mode.type_die)
    assert mode_info.enum_names == {0: "MODE_IDLE", 1: "MODE_RUN", 2: "MODE_FAULT"}


# ---------------------------------------------------------------------------
# 2. The evaluator
# ---------------------------------------------------------------------------


def ops_of(vindex, decl):
    return V.parse_exprloc(decl.cu, decl.loc_attr.value)


def test_fbreg_evaluates_against_the_frame_base(vindex) -> None:
    middle = next(fv for fv in vindex.by_func_low.values() if fv.name == "middle")
    x = middle.params[0]
    ops = ops_of(vindex, x)
    assert ops[0].op_name == "DW_OP_fbreg"
    offset = ops[0].args[0]
    loc = V.evaluate(
        ops, frame_base=lambda: (0x31780F20, "cfi"), reg_value=lambda n: None
    )
    assert loc.kind == "memory"
    assert loc.address == (0x31780F20 + offset) & 0xFFFFFFFF

    lost = V.evaluate(
        ops, frame_base=lambda: (None, "no CFI row"), reg_value=lambda n: None
    )
    assert lost.kind == "unavailable"
    assert "frame base" in lost.reason


def test_addr_reg_breg_and_unsupported(vindex) -> None:
    cu = next(iter(vindex.by_func_low.values())).cu
    def parse(*raw):
        return V.parse_exprloc(cu, bytes(raw))

    addr = V.evaluate(
        parse(0x03, 0x94, 0x00, 0x10, 0x32),
        frame_base=lambda: (None, "-"), reg_value=lambda n: None,
    )
    assert (addr.kind, addr.address) == ("memory", 0x32100094)

    reg = V.evaluate(
        parse(0x50),  # DW_OP_reg0
        frame_base=lambda: (None, "-"), reg_value=lambda n: None,
    )
    assert (reg.kind, reg.reg) == ("register", 0)

    breg = V.evaluate(
        parse(0x7D, 0x08),  # DW_OP_breg13 +8
        frame_base=lambda: (None, "-"),
        reg_value=lambda n: 0x31780F00 if n == 13 else None,
    )
    assert (breg.kind, breg.address) == ("memory", 0x31780F08)

    unrec = V.evaluate(
        parse(0x7D, 0x08),
        frame_base=lambda: (None, "-"), reg_value=lambda n: None,
    )
    assert unrec.kind == "unavailable" and "r13" in unrec.reason

    bad = V.evaluate(
        parse(0x30, 0x9F),  # DW_OP_lit0, DW_OP_stack_value
        frame_base=lambda: (None, "-"), reg_value=lambda n: None,
    )
    assert bad.kind == "unavailable"
    assert "DW_OP_stack_value" in bad.reason


def test_frame_cfa_sources(index, vindex) -> None:
    middle = next(f for f in index.funcs if f.name == "middle")
    cfa, source = V.frame_cfa(index, middle.low + 4, sp=0x31780EF8, fp=0x31780F00, thumb=True)
    assert source == "cfi" and cfa is not None

    cfa_fp, source_fp = V.frame_cfa(index, 0x1000, sp=None, fp=0x31780F00, thumb=True)
    assert (cfa_fp, source_fp) == (0x31780F08, "fp")
    cfa_a32, _ = V.frame_cfa(index, 0x1000, sp=None, fp=0x31780F00, thumb=False)
    assert cfa_a32 == 0x31780F04

    none_cfa, why = V.frame_cfa(index, 0x1000, sp=None, fp=None, thumb=True)
    assert none_cfa is None and "frame pointer" in why


def test_make_frame_base_uses_call_frame_cfa(vindex) -> None:
    middle = next(fv for fv in vindex.by_func_low.values() if fv.name == "middle")
    fb = V.make_frame_base(middle, 0x31780F20, "cfi", lambda n: None)
    assert fb() == (0x31780F20, "cfi")
    fb_none = V.make_frame_base(middle, None, "no CFI row", lambda n: None)
    assert fb_none()[0] is None


# ---------------------------------------------------------------------------
# 3. Value formatting
# ---------------------------------------------------------------------------


def reader_over(words: dict[int, int]):
    return lambda addr: words.get(addr)


def leaf_type(vindex, kind: str, name: str, size: int, **kw) -> V.TypeInfo:
    return V.TypeInfo(kind, name, size, **kw)


def test_scalar_rendering(vindex) -> None:
    mem = {0x31780000: 0xFFFFFFFF}
    rd = reader_over(mem)
    loc = V.Location("memory", address=0x31780000)

    value, err = V.read_value(loc, leaf_type(vindex, "int", "int", 4), rd, None)
    assert err is None and value == {"display": "-1", "hex": "0xFFFFFFFF"}

    value, _ = V.read_value(loc, leaf_type(vindex, "uint", "unsigned int", 4), rd, None)
    assert value["display"] == str(0xFFFFFFFF)

    mem[0x31780000] = 0x41
    value, _ = V.read_value(loc, leaf_type(vindex, "char", "char", 1), rd, None)
    assert value["display"] == "'A' (0x41)"

    value, _ = V.read_value(
        loc,
        leaf_type(vindex, "enum", "enum mode", 1,
                  enum_names={0: "MODE_IDLE", 1: "MODE_RUN"}),
        rd, None,
    )
    assert value["display"] == str(0x41)  # unnamed value falls back to number

    mem[0x31780000] = 0x3FC00000  # 1.5f
    value, _ = V.read_value(loc, leaf_type(vindex, "float", "float", 4), rd, None)
    assert value["display"] == "1.5"


def test_char_pointer_preview_and_range_guard(vindex) -> None:
    greeting = global_named(vindex, "greeting").decl
    addr = greeting.static_addr
    # "hell" then "o\0..." packed little-endian at the static's address.
    mem = {addr & ~3: 0x6C6C6568, (addr & ~3) + 4: 0x0000006F}
    cfg = global_named(vindex, "cfg").decl
    name_member = next(
        m for m in vindex.type_of(cfg.type_die).members if m.name == "name"
    )
    ptr_info = vindex.type_of(name_member.type_die)
    ptr_cell = 0x31780010
    mem[ptr_cell] = addr
    value, err = V.read_value(
        V.Location("memory", address=ptr_cell), ptr_info, reader_over(mem), None
    )
    assert err is None
    assert '"hello"' in value["display"]

    outside, err2 = V.read_value(
        V.Location("memory", address=0x40000000),
        leaf_type(vindex, "int", "int", 4), reader_over(mem), None,
    )
    assert outside is None and "outside the known" in err2


def test_register_located_values(vindex) -> None:
    tinfo = leaf_type(vindex, "int", "int", 4)
    value, err = V.read_value(
        V.Location("register", reg=3), tinfo, reader_over({}), {3: 0x2A}
    )
    assert err is None and value["display"] == "42"
    missing, err2 = V.read_value(
        V.Location("register", reg=3), tinfo, reader_over({}), {13: 0}
    )
    assert missing is None and "r3" in err2


# ---------------------------------------------------------------------------
# 4. The path language
# ---------------------------------------------------------------------------


def test_parse_path_shapes() -> None:
    p = V.parse_path("cfg.limits[2].max")
    assert p.base == "cfg"
    assert p.selectors == [
        ("member", "limits"), ("index", 2), ("member", "max"),
    ]
    arrow = V.parse_path("p->x")
    assert arrow.selectors == [("deref", None), ("member", "x")]
    stars = V.parse_path("**pp")
    assert stars.selectors == [("deref", None), ("deref", None)]
    mixed = V.parse_path("*cfg.next")
    assert mixed.selectors == [("member", "next"), ("deref", None)]


@pytest.mark.parametrize("bad", ["cfg.", "cfg[", "[0]", "cfg..x", "cfg->"])
def test_parse_path_errors_carry_columns(bad) -> None:
    with pytest.raises(V.PathError, match="column"):
        V.parse_path(bad)


def test_resolve_global_path_walks_members(index, vindex) -> None:
    cfg_addr = global_named(vindex, "cfg").decl.static_addr
    tinfo = vindex.type_of(global_named(vindex, "cfg").decl.type_die)
    limits = next(m for m in tinfo.members if m.name == "limits")
    lim_elem = vindex.type_of(vindex.type_of(limits.type_die).elem)
    max_member = next(m for m in lim_elem.members if m.name == "max")

    loc, type_die, decl = V.resolve_path(
        vindex, index._dwarf, V.parse_path("cfg.limits[1].max"),
        scope=None, read_word=lambda a: 0,
    )
    expected = cfg_addr + limits.offset + 1 * lim_elem.size + max_member.offset
    assert loc.kind == "memory" and loc.address == expected
    assert vindex.type_of(type_die).kind == "int"
    assert decl.name == "cfg"


def test_resolution_order_prefers_frame_locals(index, vindex) -> None:
    # Give `middle` a local named like the global to prove locals win.
    middle = next(fv for fv in vindex.by_func_low.values() if fv.name == "middle")
    shadow = V.VarDecl(
        name="counter", type_die=middle.params[0].type_die,
        loc_attr=middle.params[0].loc_attr, cu=middle.cu,
        die=middle.params[0].die,
    )
    middle.locals.append(shadow)
    try:
        scope = V.FrameScope(
            func=middle, pc=0,  # scope=None decls match any pc
            frame_base=lambda: (0x31780F20, "cfi"),
            reg_value=lambda n: None,
            cu_file="fixture.c",
        )
        loc, _t, decl = V.resolve_path(
            vindex, index._dwarf, V.parse_path("counter"),
            scope=scope, read_word=lambda a: 0,
        )
        assert decl is shadow
        assert loc.kind == "memory" and (loc.address & 0xFFFF0000) == 0x31780000

        loc_global, _t2, decl2 = V.resolve_path(
            vindex, index._dwarf, V.parse_path("counter"),
            scope=None, read_word=lambda a: 0,
        )
        assert decl2 is not shadow
        assert loc_global.address == global_named(vindex, "counter").decl.static_addr
    finally:
        middle.locals.remove(shadow)


def test_unknown_name_and_bad_selectors(index, vindex) -> None:
    with pytest.raises(V.PathError, match="no variable named"):
        V.resolve_path(vindex, index._dwarf, V.parse_path("nosuch"),
                       scope=None, read_word=lambda a: 0)
    with pytest.raises(V.PathError, match="not a struct"):
        V.resolve_path(vindex, index._dwarf, V.parse_path("counter.x"),
                       scope=None, read_word=lambda a: 0)
    with pytest.raises(V.PathError, match="not a pointer"):
        V.resolve_path(vindex, index._dwarf, V.parse_path("*counter"),
                       scope=None, read_word=lambda a: 0)
    with pytest.raises(V.PathError, match="out of range"):
        V.resolve_path(vindex, index._dwarf, V.parse_path("cfg.limits[9].max"),
                       scope=None, read_word=lambda a: 0)

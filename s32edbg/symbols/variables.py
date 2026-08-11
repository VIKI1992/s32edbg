"""DWARF variables: the index, the location evaluator, the path language.

What the Variables view needs out of an ELF, built lazily on first use (a
production image holds thousands of variable DIEs nobody may ever look at):

- **VariableIndex**: every global (external and static, including
  function-local statics), and per-function parameter/local lists with their
  lexical-block scopes -- joined to the run-time world by function low_pc.
- **Types**: just enough of the DWARF type graph to name, size, format and
  expand a value -- base types by encoding, enums by name, pointers, arrays,
  structs/unions with member offsets. Bitfields are reported, not decoded.
- **The location evaluator**: a deliberately closed opcode table.
  GCC -O0 emits exactly DW_OP_addr / DW_OP_fbreg (frame base
  DW_OP_call_frame_cfa) -- measured on this project's compiler -- and
  optimized builds add DW_OP_regN/bregN and .debug_loc lists. Anything
  outside that set becomes "unsupported location: <op>" -- the honesty rule:
  report, never guess a value.
- **The path language**: `cfg.limits[2].max`, `*ptr`, `p->x` -- the watch
  grammar, resolved locals-first under a frame scope.

Values are read through the caller's cached word reader and only inside
`unwind.SANE_RANGES` -- an address outside the known RAM/flash windows is
reported instead of read, which is the volatile/MMIO safety valve for the
live-refresh mode.
"""

from __future__ import annotations

import struct as _struct
from dataclasses import dataclass, field
from typing import Any, Callable

from . import require_pyelftools
from .unwind import SANE_RANGES

WORD_MASK = 0xFFFFFFFF

# DW_ATE encodings (DWARF5 table 7.11) -> value kind.
_ENCODING_KIND = {
    0x01: "uint",   # address
    0x02: "bool",
    0x04: "float",
    0x05: "int",
    0x06: "char",   # signed_char
    0x07: "uint",
    0x08: "char",   # unsigned_char
}


@dataclass
class Member:
    name: str
    offset: int | None
    type_die: Any
    error: str | None = None


@dataclass
class TypeInfo:
    kind: str  # int|uint|bool|char|enum|float|pointer|array|struct|union|void|other
    name: str
    size: int | None
    die: Any = None
    members: list[Member] | None = None
    elem: Any = None          # element/target type DIE for array/pointer
    count: int | None = None  # array element count when known
    enum_names: dict[int, str] | None = None

    @property
    def expandable(self) -> bool:
        if self.kind in ("struct", "union"):
            return bool(self.members)
        if self.kind == "array":
            return self.elem is not None
        if self.kind == "pointer":
            # char* renders as a string preview instead of expanding.
            return self.elem is not None
        return False


@dataclass
class VarDecl:
    name: str
    type_die: Any
    loc_attr: Any            # raw DW_AT_location AttributeValue, or None
    cu: Any
    die: Any
    scope: tuple[int, int] | None = None  # lexical-block PC range
    static_addr: int | None = None        # pre-parsed [DW_OP_addr x]

    def in_scope(self, pc: int) -> bool:
        return self.scope is None or self.scope[0] <= pc < self.scope[1]


@dataclass
class FuncVars:
    name: str
    params: list[VarDecl] = field(default_factory=list)
    locals: list[VarDecl] = field(default_factory=list)
    statics: list[VarDecl] = field(default_factory=list)
    frame_base_attr: Any = None
    cu: Any = None
    #: The CU's primary-source name -- the statics_by_cu key for file-scope
    #: resolution under this function's frame.
    cu_name: str = ""


@dataclass
class GlobalEntry:
    name: str
    decl: VarDecl
    external: bool
    cu_file: str
    owner_func: str | None = None


@dataclass
class Location:
    kind: str  # "memory" | "register" | "unavailable"
    address: int | None = None
    reg: int | None = None
    reason: str | None = None


class PathError(ValueError):
    """A watch-path parse or resolution error, message ready to show."""


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


def _die_name(die: Any) -> str | None:
    attr = die.attributes.get("DW_AT_name")
    if attr is not None:
        v = attr.value
        return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
    for link in ("DW_AT_specification", "DW_AT_abstract_origin"):
        if link in die.attributes:
            try:
                return _die_name(die.get_DIE_from_attribute(link))
            except Exception:
                return None
    return None


def _die_type(die: Any) -> Any:
    if "DW_AT_type" in die.attributes:
        try:
            return die.get_DIE_from_attribute("DW_AT_type")
        except Exception:
            return None
    for link in ("DW_AT_specification", "DW_AT_abstract_origin"):
        if link in die.attributes:
            try:
                return _die_type(die.get_DIE_from_attribute(link))
            except Exception:
                return None
    return None


def _high_pc(die: Any, low: int) -> int | None:
    attr = die.attributes.get("DW_AT_high_pc")
    if attr is None:
        return None
    return attr.value if attr.form == "DW_FORM_addr" else low + attr.value


class VariableIndex:
    def __init__(self, dwarf: Any) -> None:
        self._dwarf = dwarf
        self.by_func_low: dict[int, FuncVars] = {}
        self.globals: list[GlobalEntry] = []
        self.globals_by_name: dict[str, list[GlobalEntry]] = {}
        self.statics_by_cu: dict[str, dict[str, GlobalEntry]] = {}
        self._types: dict[int, TypeInfo] = {}

    @classmethod
    def build(cls, dwarf: Any, _symbol_index: Any) -> "VariableIndex":
        require_pyelftools()
        vindex = cls(dwarf)
        for cu in dwarf.iter_CUs():
            top = cu.get_top_DIE()
            cu_name = _die_name(top) or "<unknown>"
            vindex._walk(cu, top, None, None, cu_name)
        self_sort = sorted(vindex.globals, key=lambda g: g.name.lower())
        vindex.globals = self_sort
        return vindex

    # -- construction --------------------------------------------------------

    def _walk(
        self,
        cu: Any,
        die: Any,
        func: FuncVars | None,
        scope: tuple[int, int] | None,
        cu_name: str,
    ) -> None:
        for child in die.iter_children():
            tag = child.tag
            if tag == "DW_TAG_subprogram":
                low_attr = child.attributes.get("DW_AT_low_pc")
                if low_attr is None:
                    continue  # declaration / abstract instance: no code
                low = low_attr.value & ~1
                fv = FuncVars(
                    name=_die_name(child) or f"sub_{low:08X}",
                    frame_base_attr=child.attributes.get("DW_AT_frame_base"),
                    cu=cu,
                    cu_name=cu_name,
                )
                self.by_func_low[low] = fv
                self._walk(cu, child, fv, None, cu_name)
                for s in fv.statics:
                    self._add_global(s, external=False, cu_file=cu_name,
                                     owner=fv.name)
            elif tag == "DW_TAG_lexical_block":
                low_attr = child.attributes.get("DW_AT_low_pc")
                rng = scope
                if low_attr is not None:
                    high = _high_pc(child, low_attr.value)
                    if high is not None:
                        rng = (low_attr.value & ~1, high)
                self._walk(cu, child, func, rng, cu_name)
            elif tag in ("DW_TAG_variable", "DW_TAG_formal_parameter"):
                decl = self._decl(cu, child, scope)
                if decl is None:
                    continue
                if func is None:
                    if decl.loc_attr is None:
                        continue  # a declaration; the definition is elsewhere
                    ext = child.attributes.get("DW_AT_external")
                    self._add_global(
                        decl,
                        external=bool(ext.value) if ext is not None else False,
                        cu_file=cu_name,
                    )
                elif tag == "DW_TAG_formal_parameter":
                    func.params.append(decl)
                elif decl.static_addr is not None:
                    func.statics.append(decl)
                else:
                    func.locals.append(decl)
            elif tag == "DW_TAG_namespace":
                self._walk(cu, child, func, scope, cu_name)

    def _decl(self, cu: Any, die: Any, scope: tuple[int, int] | None) -> VarDecl | None:
        name = _die_name(die)
        type_die = _die_type(die)
        if name is None or type_die is None:
            return None
        loc = die.attributes.get("DW_AT_location")
        static_addr: int | None = None
        if loc is not None and isinstance(loc.value, list) and len(loc.value) == 5:
            if loc.value[0] == 0x03:  # DW_OP_addr, 4-byte address
                static_addr = int.from_bytes(bytes(loc.value[1:5]), "little")
        return VarDecl(
            name=name, type_die=type_die, loc_attr=loc, cu=cu, die=die,
            scope=scope, static_addr=static_addr,
        )

    def _add_global(
        self, decl: VarDecl, *, external: bool, cu_file: str,
        owner: str | None = None,
    ) -> None:
        entry = GlobalEntry(decl.name, decl, external, cu_file, owner)
        self.globals.append(entry)
        self.globals_by_name.setdefault(decl.name, []).append(entry)
        if not external:
            self.statics_by_cu.setdefault(cu_file, {})[decl.name] = entry

    # -- types ---------------------------------------------------------------

    def type_of(self, die: Any) -> TypeInfo:
        if die is None:
            return TypeInfo("void", "void", None)
        cached = self._types.get(die.offset)
        if cached is not None:
            return cached
        info = self._resolve_type(die)
        self._types[die.offset] = info
        return info

    def _resolve_type(self, die: Any) -> TypeInfo:
        quals: list[str] = []
        typedef_name: str | None = None
        current = die
        for _ in range(32):  # typedef chains are short; guard anyway
            if current is None:
                return TypeInfo("void", " ".join([*quals, "void"]).strip(), None)
            tag = current.tag
            if tag == "DW_TAG_typedef":
                if typedef_name is None:
                    typedef_name = _die_name(current)
                current = _die_type(current)
            elif tag == "DW_TAG_const_type":
                if "const" not in quals:
                    quals.append("const")
                current = _die_type(current)
            elif tag == "DW_TAG_volatile_type":
                if "volatile" not in quals:
                    quals.append("volatile")
                current = _die_type(current)
            else:
                break
        else:
            return TypeInfo("other", "<type chain too deep>", None)

        def named(base_name: str) -> str:
            if typedef_name is not None:
                return typedef_name
            out = base_name
            for q in reversed(quals):
                if not out.startswith(q + " "):  # "const const char" dedup
                    out = q + " " + out
            return out

        tag = current.tag
        size_attr = current.attributes.get("DW_AT_byte_size")
        size = size_attr.value if size_attr is not None else None

        if tag == "DW_TAG_base_type":
            enc = current.attributes.get("DW_AT_encoding")
            kind = _ENCODING_KIND.get(enc.value if enc else -1, "other")
            return TypeInfo(kind, named(_die_name(current) or "?"), size, current)
        if tag == "DW_TAG_enumeration_type":
            names = {}
            for child in current.iter_children():
                if child.tag == "DW_TAG_enumerator":
                    cv = child.attributes.get("DW_AT_const_value")
                    n = _die_name(child)
                    if cv is not None and n is not None:
                        names[cv.value] = n
            return TypeInfo(
                "enum", named(f"enum {_die_name(current) or '<anon>'}"),
                size or 4, current, enum_names=names,
            )
        if tag == "DW_TAG_pointer_type":
            elem = _die_type(current)
            elem_info = self.type_of(elem)
            return TypeInfo(
                "pointer", named(elem_info.name + " *"), size or 4,
                current, elem=elem,
            )
        if tag == "DW_TAG_array_type":
            elem = _die_type(current)
            elem_info = self.type_of(elem)
            count: int | None = None
            for child in current.iter_children():
                if child.tag == "DW_TAG_subrange_type":
                    ub = child.attributes.get("DW_AT_upper_bound")
                    cnt = child.attributes.get("DW_AT_count")
                    if cnt is not None:
                        count = cnt.value
                    elif ub is not None:
                        count = ub.value + 1
                    break  # outer dimension only in v1
            total = (
                elem_info.size * count
                if (elem_info.size is not None and count is not None)
                else size
            )
            label = f"{elem_info.name} [{count if count is not None else ''}]"
            return TypeInfo(
                "array", named(label), total, current, elem=elem, count=count,
            )
        if tag in ("DW_TAG_structure_type", "DW_TAG_union_type"):
            kind = "struct" if tag == "DW_TAG_structure_type" else "union"
            members: list[Member] = []
            for child in current.iter_children():
                if child.tag != "DW_TAG_member":
                    continue
                mname = _die_name(child) or "<anon>"
                mtype = _die_type(child)
                if "DW_AT_bit_size" in child.attributes:
                    members.append(Member(mname, None, mtype,
                                          "bitfield (not supported in v1)"))
                    continue
                dml = child.attributes.get("DW_AT_data_member_location")
                if dml is None:
                    offset: int | None = 0  # unions
                elif isinstance(dml.value, int):
                    offset = dml.value
                elif (
                    isinstance(dml.value, list)
                    and len(dml.value) >= 2
                    and dml.value[0] == 0x23  # DW_OP_plus_uconst
                ):
                    offset = _read_uleb(dml.value[1:])
                else:
                    members.append(Member(mname, None, mtype,
                                          "unsupported member location"))
                    continue
                members.append(Member(mname, offset, mtype))
            return TypeInfo(
                kind, named(f"{kind} {_die_name(current) or '<anon>'}"),
                size, current, members=members,
            )
        return TypeInfo("other", named(_die_name(current) or f"<{tag}>"), size, current)


def _read_uleb(data: list[int]) -> int:
    result = 0
    shift = 0
    for byte in data:
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return result


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def parse_exprloc(cu: Any, raw: list[int] | bytes) -> list[Any]:
    from elftools.dwarf.dwarf_expr import DWARFExprParser

    data = bytes(raw) if not isinstance(raw, bytes) else raw
    return DWARFExprParser(cu.structs).parse_expr(data)


def location_ops(dwarf: Any, decl: VarDecl, pc: int | None) -> list[Any] | None:
    """The DWARF expression governing `decl` at `pc`, or None (not live).

    Exprloc forms parse directly. Loclist forms (DW_FORM_sec_offset, and the
    data4/data8 forms of DWARF < 4) need the entry covering `pc`, with CU
    base-address tracking.
    """
    attr = decl.loc_attr
    if attr is None:
        return None
    if attr.form in ("DW_FORM_exprloc", "DW_FORM_block1", "DW_FORM_block"):
        return parse_exprloc(decl.cu, attr.value)

    from elftools.dwarf.locationlists import (
        BaseAddressEntry,
        LocationEntry,
        LocationParser,
    )

    loclists = dwarf.location_lists()
    if loclists is None:
        return None
    parsed = LocationParser(loclists).parse_from_attribute(
        attr, decl.cu["version"], die=decl.die
    )
    if not isinstance(parsed, list):
        return parse_exprloc(decl.cu, parsed.loc_expr)
    if pc is None:
        return None
    top = decl.cu.get_top_DIE()
    base_attr = top.attributes.get("DW_AT_low_pc")
    base = base_attr.value if base_attr is not None else 0
    for entry in parsed:
        if isinstance(entry, BaseAddressEntry):
            base = entry.base_address
        elif isinstance(entry, LocationEntry):
            begin, end = entry.begin_offset, entry.end_offset
            if not entry.is_absolute:
                begin, end = begin + base, end + base
            if begin <= pc < end:
                return parse_exprloc(decl.cu, entry.loc_expr)
    return None


def evaluate(
    ops: list[Any] | None,
    *,
    frame_base: Callable[[], tuple[int | None, str]],
    reg_value: Callable[[int], int | None],
) -> Location:
    """The closed-table evaluator: one recognized pattern, or an honest no."""
    if ops is None:
        return Location("unavailable", reason="value not live at this PC")
    if not ops:
        return Location("unavailable", reason="empty location expression")
    if len(ops) == 1:
        op = ops[0]
        name = op.op_name
        if name == "DW_OP_addr":
            return Location("memory", address=op.args[0] & WORD_MASK)
        if name == "DW_OP_fbreg":
            fb, why = frame_base()
            if fb is None:
                return Location("unavailable", reason=f"no frame base ({why})")
            return Location("memory", address=(fb + op.args[0]) & WORD_MASK)
        if name == "DW_OP_regx":
            return Location("register", reg=op.args[0])
        if name.startswith("DW_OP_reg"):
            return Location("register", reg=int(name[len("DW_OP_reg"):]))
        if name == "DW_OP_bregx":
            regnum, off = op.args[0], op.args[1]
        elif name.startswith("DW_OP_breg"):
            regnum, off = int(name[len("DW_OP_breg"):]), op.args[0]
        else:
            regnum = None
        if regnum is not None:
            base = reg_value(regnum)
            if base is None:
                return Location(
                    "unavailable",
                    reason=f"r{regnum} not recoverable in this frame",
                )
            return Location("memory", address=(base + off) & WORD_MASK)
    names = " ".join(op.op_name for op in ops)
    return Location("unavailable", reason=f"unsupported location expression: {names}")


def frame_cfa(
    index: Any, pc: int, sp: int | None, fp: int | None, thumb: bool
) -> tuple[int | None, str]:
    """The CFA for a frame: the CFI row when there is one, else the GCC
    prologue-derived value the FP unwinder itself relies on (fp+8 for T32/r7
    frames, fp+4 for A32/r11 -- exactly `_fp_step`'s caller_sp)."""
    row = index.cfi_row_at(pc) if index is not None else None
    if row is not None:
        cfa_rule = row.get("cfa")
        base = {13: sp, 11: fp, 7: fp}.get(getattr(cfa_rule, "reg", None))
        if base is not None:
            return (base + (cfa_rule.offset or 0)) & WORD_MASK, "cfi"
    if (
        fp is not None
        and fp % 4 == 0
        and any(lo <= fp < hi for lo, hi in SANE_RANGES)
    ):
        return (fp + (8 if thumb else 4)) & WORD_MASK, "fp"
    return None, "no CFI row and no usable frame pointer"


def make_frame_base(
    func: FuncVars,
    cfa: int | None,
    cfa_source: str,
    reg_value: Callable[[int], int | None],
) -> Callable[[], tuple[int | None, str]]:
    """Turn a function's DW_AT_frame_base into the evaluator's closure."""
    attr = func.frame_base_attr
    if attr is None:
        return lambda: (None, "the function declares no frame base")
    try:
        ops = parse_exprloc(func.cu, attr.value)
    except Exception:
        return lambda: (None, "unparseable frame base")
    if len(ops) == 1 and ops[0].op_name == "DW_OP_call_frame_cfa":
        if cfa is None:
            return lambda: (None, cfa_source)
        return lambda: (cfa, cfa_source)
    if len(ops) == 1 and ops[0].op_name.startswith("DW_OP_reg"):
        n = (
            ops[0].args[0]
            if ops[0].op_name == "DW_OP_regx"
            else int(ops[0].op_name[len("DW_OP_reg"):])
        )
        return lambda: (reg_value(n), f"r{n}")
    names = " ".join(op.op_name for op in ops)
    return lambda: (None, f"unsupported frame base: {names}")


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def _in_sane_ranges(addr: int, length: int) -> bool:
    return any(lo <= addr and addr + length <= hi for lo, hi in SANE_RANGES)


def read_bytes(
    addr: int, length: int, read_word: Callable[[int], int | None]
) -> bytes | None:
    out = bytearray()
    a0 = addr & ~3
    end = addr + length
    if length > 8:
        # An aggregate is one contiguous span: let a block-capable reader
        # fetch it in one auto-increment burst instead of a checked
        # transaction per word. Optional -- plain readers just loop.
        prefetch = getattr(read_word, "prefetch", None)
        if prefetch is not None:
            prefetch(a0, (end - a0 + 3) // 4)
    for wa in range(a0, end, 4):
        w = read_word(wa)
        if w is None:
            return None
        out += (w & WORD_MASK).to_bytes(4, "little")
    off = addr - a0
    return bytes(out[off : off + length])


def string_preview(
    addr: int, read_word: Callable[[int], int | None], limit: int = 32
) -> str | None:
    """A short printable prefix at addr, or None. Reads word by word and keeps
    what it got: a preview must not vanish because a later word faulted."""
    if not _in_sane_ranges(addr, 1):
        return None
    out: list[str] = []
    truncated = True
    a = addr
    while len(out) < limit:
        w = read_word(a & ~3)
        if w is None:
            if not out:
                return None
            break
        data = (w & WORD_MASK).to_bytes(4, "little")
        stop = False
        for i in range(a - (a & ~3), 4):
            b = data[i]
            if b == 0:
                truncated = False
                stop = True
                break
            if not (0x20 <= b < 0x7F):
                stop = True
                break
            out.append(chr(b))
            if len(out) >= limit:
                break
        if stop:
            break
        a = (a & ~3) + 4
    return '"' + "".join(out) + ('…"' if truncated and out else '"')


def read_value(
    loc: Location,
    tinfo: TypeInfo,
    read_word: Callable[[int], int | None],
    reg_file: dict[int, int] | None,
) -> tuple[dict[str, str] | None, str | None]:
    """(value {display, hex}, error). Aggregates get a summary display."""
    if loc.kind == "unavailable":
        return None, loc.reason
    size = tinfo.size or 4

    if loc.kind == "register":
        if reg_file is None or loc.reg not in reg_file:
            return None, f"r{loc.reg} not recoverable in this frame"
        raw = reg_file[loc.reg] & WORD_MASK
        return _render(raw.to_bytes(4, "little")[:size or 4], tinfo, read_word), None

    assert loc.address is not None
    if tinfo.kind in ("struct", "union"):
        return {"display": "{…}", "hex": f"@0x{loc.address:08X}"}, None
    if tinfo.kind == "array":
        if tinfo.elem is not None:
            # char arrays preview in place
            pass
        if is_char_elem(tinfo):
            preview = string_preview(loc.address, read_word)
            if preview is not None:
                return {"display": preview, "hex": f"@0x{loc.address:08X}"}, None
        return {"display": tinfo.name, "hex": f"@0x{loc.address:08X}"}, None
    if not _in_sane_ranges(loc.address, size):
        return None, (
            f"address 0x{loc.address:08X} is outside the known RAM/flash "
            "windows -- not read automatically"
        )
    data = read_bytes(loc.address, size, read_word)
    if data is None:
        return None, f"memory at 0x{loc.address:08X} did not read"
    value = _render(data, tinfo, read_word)
    return value, None


def is_char_elem(tinfo: TypeInfo) -> bool:
    if tinfo.elem is None:
        return False
    # Cheap check without an index: elem resolved lazily by callers who have
    # one; here we only need "is it a 1-byte char-ish base type".
    die = tinfo.elem
    for _ in range(8):
        if die is None:
            return False
        if die.tag in ("DW_TAG_const_type", "DW_TAG_volatile_type", "DW_TAG_typedef"):
            die = _die_type(die)
            continue
        break
    if die is None or die.tag != "DW_TAG_base_type":
        return False
    enc = die.attributes.get("DW_AT_encoding")
    return enc is not None and enc.value in (0x06, 0x08)


def _render(
    data: bytes, tinfo: TypeInfo, read_word: Callable[[int], int | None]
) -> dict[str, str]:
    size = len(data)
    raw = int.from_bytes(data, "little")
    hexstr = f"0x{raw:0{size * 2}X}"
    kind = tinfo.kind
    if kind == "int":
        signed = int.from_bytes(data, "little", signed=True)
        return {"display": str(signed), "hex": hexstr}
    if kind == "uint":
        return {"display": str(raw), "hex": hexstr}
    if kind == "bool":
        return {"display": "true" if raw else "false", "hex": hexstr}
    if kind == "char":
        ch = raw & 0xFF
        disp = f"'{chr(ch)}' (0x{ch:02X})" if 0x20 <= ch < 0x7F else f"'\\x{ch:02X}'"
        return {"display": disp, "hex": hexstr}
    if kind == "enum":
        name = (tinfo.enum_names or {}).get(raw)
        return {
            "display": f"{name} ({raw})" if name else str(raw),
            "hex": hexstr,
        }
    if kind == "float":
        try:
            if size == 8:
                value = _struct.unpack("<d", data)[0]
            else:
                value = _struct.unpack("<f", data[:4])[0]
            return {"display": repr(value), "hex": hexstr}
        except _struct.error:
            return {"display": hexstr, "hex": hexstr}
    if kind == "pointer":
        out = {"display": f"0x{raw:08X}", "hex": f"0x{raw:08X}"}
        if raw and is_char_elem(tinfo):
            preview = string_preview(raw, read_word)
            if preview is not None:
                out["display"] = f"0x{raw:08X} {preview}"
        return out
    return {"display": hexstr, "hex": hexstr}


# ---------------------------------------------------------------------------
# The path language
# ---------------------------------------------------------------------------


@dataclass
class PathExpr:
    base: str
    #: ("member", name) | ("index", i) | ("deref", None), applied in order.
    selectors: list[tuple[str, Any]]
    text: str


_IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$")
_IDENT_CONT = _IDENT_START | set("0123456789")


def parse_path(text: str) -> PathExpr:
    s = text.strip()
    i = 0

    def err(msg: str, col: int) -> PathError:
        return PathError(f"{msg} at column {col + 1} in {s!r}")

    derefs = 0
    while i < len(s) and s[i] == "*":
        derefs += 1
        i += 1
    start = i
    if i >= len(s) or s[i] not in _IDENT_START:
        raise err("expected an identifier", i)
    while i < len(s) and s[i] in _IDENT_CONT:
        i += 1
    base = s[start:i]
    selectors: list[tuple[str, Any]] = []

    def read_ident(after: str) -> str:
        nonlocal i
        if i >= len(s) or s[i] not in _IDENT_START:
            raise err(f"expected identifier after {after!r}", i)
        st = i
        while i < len(s) and s[i] in _IDENT_CONT:
            i += 1
        return s[st:i]

    while i < len(s):
        c = s[i]
        if c == ".":
            i += 1
            selectors.append(("member", read_ident(".")))
        elif c == "-" and s[i : i + 2] == "->":
            i += 2
            selectors.append(("deref", None))
            selectors.append(("member", read_ident("->")))
        elif c == "[":
            i += 1
            st = i
            while i < len(s) and s[i] != "]":
                i += 1
            if i >= len(s):
                raise err("unclosed '['", st)
            idx_text = s[st:i].strip()
            i += 1
            try:
                idx = int(idx_text, 0)
            except ValueError:
                raise err(f"bad array index {idx_text!r}", st) from None
            selectors.append(("index", idx))
        else:
            raise err(f"unexpected {c!r}", i)
    # Leading derefs bind loosest: *cfg.next means *(cfg.next).
    selectors.extend([("deref", None)] * derefs)
    return PathExpr(base, selectors, s)


@dataclass
class FrameScope:
    func: FuncVars
    pc: int
    frame_base: Callable[[], tuple[int | None, str]]
    reg_value: Callable[[int], int | None]
    cu_file: str


def resolve_path(
    vindex: VariableIndex,
    dwarf: Any,
    path: PathExpr,
    *,
    scope: FrameScope | None,
    read_word: Callable[[int], int | None],
) -> tuple[Location, Any, VarDecl]:
    """Resolve to (Location, type DIE, base decl). Raises PathError."""
    decl, frame_base, reg_value, pc = _resolve_base(vindex, path.base, scope)
    ops = location_ops(dwarf, decl, pc)
    loc = evaluate(ops, frame_base=frame_base, reg_value=reg_value)
    type_die = decl.type_die

    if loc.kind == "register" and path.selectors:
        raise PathError(
            f"{path.base} lives in a register; member/index/deref paths need "
            "a memory-located base"
        )
    for kind, arg in path.selectors:
        if loc.kind != "memory" or loc.address is None:
            raise PathError(f"cannot apply selectors: {loc.reason or 'no address'}")
        tinfo = vindex.type_of(type_die)
        if kind == "member":
            if tinfo.kind not in ("struct", "union") or not tinfo.members:
                raise PathError(f"'.{arg}': {tinfo.name} is not a struct/union")
            member = next((m for m in tinfo.members if m.name == arg), None)
            if member is None:
                raise PathError(f"{tinfo.name} has no member {arg!r}")
            if member.error is not None or member.offset is None:
                raise PathError(f"{tinfo.name}.{arg}: {member.error or 'no offset'}")
            loc = Location("memory", address=(loc.address + member.offset) & WORD_MASK)
            type_die = member.type_die
        elif kind == "index":
            if tinfo.kind == "array":
                elem_info = vindex.type_of(tinfo.elem)
                if tinfo.count is not None and not 0 <= arg < tinfo.count:
                    raise PathError(
                        f"index [{arg}] out of range for {tinfo.name}"
                    )
                base_addr = loc.address
            elif tinfo.kind == "pointer":
                elem_info = vindex.type_of(tinfo.elem)
                base_addr = _deref(loc.address, read_word, path.text)
            else:
                raise PathError(f"'[{arg}]': {tinfo.name} is not an array or pointer")
            if elem_info.size is None:
                raise PathError(f"element size of {tinfo.name} is unknown")
            loc = Location(
                "memory", address=(base_addr + arg * elem_info.size) & WORD_MASK
            )
            type_die = tinfo.elem
        else:  # deref
            if tinfo.kind != "pointer":
                raise PathError(f"'*': {tinfo.name} is not a pointer")
            loc = Location("memory", address=_deref(loc.address, read_word, path.text))
            type_die = tinfo.elem
    return loc, type_die, decl


def _deref(addr: int, read_word: Callable[[int], int | None], text: str) -> int:
    if not _in_sane_ranges(addr, 4):
        raise PathError(
            f"pointer at 0x{addr:08X} is outside the known windows ({text})"
        )
    data = read_bytes(addr, 4, read_word)
    if data is None:
        raise PathError(f"pointer at 0x{addr:08X} did not read ({text})")
    target = int.from_bytes(data, "little") & ~1
    if target == 0:
        raise PathError(f"NULL pointer dereference in {text}")
    return target


def _resolve_base(
    vindex: VariableIndex, name: str, scope: FrameScope | None
) -> tuple[VarDecl, Callable[[], tuple[int | None, str]], Callable[[int], int | None], int | None]:
    no_base = lambda: (None, "no frame scope")  # noqa: E731
    no_reg = lambda _n: None  # noqa: E731
    if scope is not None:
        for decl in [*scope.func.params, *scope.func.locals]:
            if decl.name == name and decl.in_scope(scope.pc):
                return decl, scope.frame_base, scope.reg_value, scope.pc
        for decl in scope.func.statics:
            if decl.name == name:
                return decl, scope.frame_base, scope.reg_value, scope.pc
        cu_statics = vindex.statics_by_cu.get(scope.cu_file, {})
        if name in cu_statics:
            return cu_statics[name].decl, no_base, scope.reg_value, scope.pc
    candidates = vindex.globals_by_name.get(name, [])
    externals = [g for g in candidates if g.external]
    if externals:
        return externals[0].decl, no_base, no_reg, scope.pc if scope else None
    statics = [g for g in candidates if not g.external and g.owner_func is None]
    if len(statics) == 1:
        return statics[0].decl, no_base, no_reg, scope.pc if scope else None
    if len(statics) > 1:
        cus = ", ".join(sorted({g.cu_file for g in statics}))
        raise PathError(f"{name!r} is a static in several files ({cus}) -- ambiguous")
    owned = [g for g in candidates if g.owner_func is not None]
    if len(owned) == 1:
        return owned[0].decl, no_base, no_reg, scope.pc if scope else None
    raise PathError(f"no variable named {name!r} in scope")

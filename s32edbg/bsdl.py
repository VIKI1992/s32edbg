"""BSDL parsing and the boundary-cell-to-pin model.

The boundary-scan feature's source of truth. RM 136.3.4 withholds two facts --
"The size of the boundary scan register and bit ordering is device-dependent
and can be found in the device BSDL file" -- and this module supplies both from
the BSDL that RM Table 984 names: the register length (494 for the bundled
S32E277 27x27 SIP) and the cell-number-to-pin mapping that turns a raw capture
into named pin states.

This is a parser for the NXP BSDL_GEN v5.0 dialect the bundled file uses, not
for general VHDL. The dialect is regular enough for a two-stage approach: a
quote-aware scanner first strips ``--`` comments (they appear *between* the
string fragments of multi-line attributes, e.g. the commented-out
JTAGC_EXTEST_* opcodes) and splits on ``;``; anchored patterns then pick each
construct apart, joining ``"..." & "..."`` fragment chains beforehand. Field
values arrive padded with tabs and stray trailing spaces (``ETH_0_MII_RXER ``)
and are stripped everywhere.

Model notes, from the bundled file and IEEE 1149.1-2001:

  * Cell 0 is nearest TDO, so bit ``n`` of an LSB-first capture is cell ``n``.
    ``decode_sample`` takes ``reverse=True`` as the bench-falsifiable escape
    hatch should hardware disagree.
  * Every observable pin here is a BC_7 ``bidir`` data cell plus a BC_1/BC_2
    ``control`` cell (disval 0, result Z), except ``DDR0_RESET_N`` -- a lone
    ``output2`` with no control cell (always driven, no Z state).
  * ``linkage`` ports (power, AUR_*, PBU..PBZ, analogs) and the plain inputs
    JCOMP/POR_B have no cells at all: not observable, never drivable.
  * The DESIGN_WARNING says DDR0_D_nn pins cannot SAMPLE their inputs in
    mission mode (PRELOAD unaffected); modeled as ``sample_input_blocked``.
  * PRIVATE opcodes may contain ``X`` don't-cares, so they stay pattern
    strings; named opcodes are pure binary and become ints.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from .errors import BsdlError

#: Fuse-sourced IDCODE bits, invalid immediately out of reset (RM L412549) --
#: the same rule tap.probe applies when it reads the ID twice.
IDCODE_FUSE_MASK = 0x001E_0000  # bits [20:17]

#: DESIGN_WARNING: "DDR0_D_nn pins cannot SAMPLE the input pins during mission
#: mode as the path to the BSR is blocked. PRELOAD is not affected."
_SAMPLE_BLOCKED_PREFIX = "DDR0_D"


@dataclass(frozen=True)
class Port:
    """One design port: direction, width, and its BGA ball(s)."""

    name: str
    direction: str            # "in" | "out" | "buffer" | "inout" | "linkage"
    width: int                # 1, or N for bit_vector(N-1 downto 0)
    balls: tuple[str, ...]    # empty when the pin map lacks the port

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "direction": self.direction,
            "width": self.width,
            "balls": list(self.balls),
        }


@dataclass(frozen=True)
class BoundaryCell:
    """One BOUNDARY_REGISTER entry, fields as the standard names them."""

    num: int
    cell: str                 # "BC_1" | "BC_2" | "BC_7"
    port: str | None          # None for "*"
    function: str             # "internal" | "control" | "bidir" | "output2" | ...
    safe: str                 # "0" | "1" | "X"
    ccell: int | None = None
    disval: int | None = None
    rslt: str | None = None   # "Z" ...

    def as_dict(self) -> dict[str, Any]:
        return {
            "num": self.num,
            "cell": self.cell,
            "port": self.port,
            "function": self.function,
            "safe": self.safe,
            "ccell": self.ccell,
            "disval": self.disval,
            "rslt": self.rslt,
        }


@dataclass(frozen=True)
class PinDef:
    """A pin the BSR can observe (and possibly drive): data + control cell."""

    name: str
    ball: str
    direction: str
    cell: int                       # data cell number
    function: str                   # data cell function (bidir / output2 / ...)
    control_cell: int | None
    disval: int | None
    disable_result: str | None
    safe: str
    sample_input_blocked: bool
    #: SIUL2 pad label from the IOMUX table (joined by ball): "PAD_086" on the
    #: main die, "AE MSCR 36" on the AE die (there the pad name IS the port
    #: name), None for dedicated pins (RESET_B, JTAG...).
    pad: str | None = None
    #: The register-level identity, e.g. "SIUL2_1 MSCR 86".
    pad_detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ball": self.ball,
            "direction": self.direction,
            "cell": self.cell,
            "function": self.function,
            "control_cell": self.control_cell,
            "disval": self.disval,
            "disable_result": self.disable_result,
            "safe": self.safe,
            "sample_input_blocked": self.sample_input_blocked,
            "pad": self.pad,
            "pad_detail": self.pad_detail,
        }


@dataclass(frozen=True)
class PinState:
    """One pin's decoded SAMPLE capture."""

    pin: PinDef
    level: int
    driver_enabled: bool | None     # None when the pin has no control cell

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.pin.name,
            "ball": self.pin.ball,
            "cell": self.pin.cell,
            "direction": self.pin.direction,
            "function": self.pin.function,
            "level": self.level,
            "driver_enabled": self.driver_enabled,
            "sample_input_blocked": self.pin.sample_input_blocked,
            "pad": self.pin.pad,
            "pad_detail": self.pin.pad_detail,
        }


@dataclass
class BsdlFile:
    """Everything this tool uses out of one parsed BSDL."""

    path: str
    entity: str
    physical_pin_map: str | None
    ports: dict[str, Port]
    tap_scan_hz: float | None
    compliance_patterns: str | None
    instruction_length: int
    opcodes: dict[str, int]                 # pure-binary named opcodes
    private_opcodes: tuple[str, ...]        # PRIVATE patterns, may contain X
    instruction_capture: int | None
    idcode: int
    register_access: dict[str, tuple[int | None, tuple[str, ...]]]
    boundary_length: int
    cells: list[BoundaryCell]               # index == num
    design_warnings: str
    notes: list[str] = field(default_factory=list)  # non-fatal parse findings

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.path,
            "entity": self.entity,
            "idcode": f"0x{self.idcode:08X}",
            "instruction_length": self.instruction_length,
            "boundary_length": self.boundary_length,
            "tap_scan_hz": self.tap_scan_hz,
            "compliance_patterns": self.compliance_patterns,
            "opcodes": {k: f"0x{v:04X}" for k, v in sorted(self.opcodes.items())},
            "register_access": {
                name: {"length": length, "instructions": list(instrs)}
                for name, (length, instrs) in self.register_access.items()
            },
            "notes": self.notes,
        }


# --------------------------------------------------------------------------
# Scanner: comments off, statements out, string fragments joined
# --------------------------------------------------------------------------


def _strip_comments(text: str) -> str:
    """Remove ``--`` comments, respecting double-quoted strings.

    A ``--`` inside a string literal stays; one outside eats to end of line.
    This is what makes the commented-out INSTRUCTION_OPCODE / PRIVATE entries
    (lines starting ``--      "..."``) vanish instead of parsing.
    """
    out: list[str] = []
    in_string = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == '"':
                in_string = False
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif ch == "-" and i + 1 < n and text[i + 1] == "-":
            while i < n and text[i] != "\n":
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _statements(text: str) -> list[str]:
    """Split on ``;`` outside strings; whitespace collapsed at the edges."""
    stmts: list[str] = []
    buf: list[str] = []
    in_string = False
    for ch in text:
        if ch == '"':
            in_string = not in_string
            buf.append(ch)
        elif ch == ";" and not in_string:
            stmts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def _join_fragments(value: str) -> str:
    """Concatenate every double-quoted fragment of a ``"..." & "..."`` chain."""
    return "".join(re.findall(r'"([^"]*)"', value))


def _split_top_commas(text: str) -> list[str]:
    """Split on commas not nested in parentheses (ball lists, cell tuples)."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _read_text(path: Path) -> str:
    """UTF-8 first, cp1252 fallback -- the DESIGN_WARNING carries curly quotes."""
    blob = path.read_bytes()
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return blob.decode("cp1252")


# --------------------------------------------------------------------------
# Construct parsers
# --------------------------------------------------------------------------

_PORT_ENTRY = re.compile(
    r"^(\w+)\s*:\s*(inout|in|out|buffer|linkage)\s+"
    r"(?:bit_vector\s*\(\s*(\d+)\s+downto\s+0\s*\)|bit)\s*$",
    re.IGNORECASE,
)

#: The entity name is glued to its colon ("...BGA27: entity is"), so the
#: subject must be \w+, not \S+ -- \S+ would eat the colon.
_ATTRIBUTE = re.compile(
    r"^attribute\s+(\w+)\s+of\s+\w+\s*:\s*(?:entity|signal)\s+is\s+(.*)$",
    re.DOTALL | re.IGNORECASE,
)

_CELL_ENTRY = re.compile(r"(\d+)\s*\(([^()]*)\)")

_REG_ACCESS_ENTRY = re.compile(r"^(\w+?)(?:\[(\d+)\])?\s*\((.*)\)$", re.DOTALL)


def _parse_ports(text: str, fail) -> dict[str, Port]:
    m = re.search(r"\bport\s*\(", text, re.IGNORECASE)
    if not m:
        fail("no port declaration found")
    depth = 1
    i = m.end()
    start = i
    while i < len(text) and depth:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    if depth:
        fail("port declaration never closes")
    ports: dict[str, Port] = {}
    for entry in text[start : i - 1].split(";"):
        entry = " ".join(entry.split())
        if not entry:
            continue
        pm = _PORT_ENTRY.match(entry)
        if not pm:
            fail(f"unparseable port entry: {entry!r}")
        name, direction, vec = pm.group(1), pm.group(2).lower(), pm.group(3)
        ports[name] = Port(
            name=name,
            direction=direction,
            width=int(vec) + 1 if vec else 1,
            balls=(),
        )
    return ports


def _parse_pin_map(value: str, ports: dict[str, Port], fail) -> dict[str, Port]:
    joined = _join_fragments(value)
    out = dict(ports)
    for entry in _split_top_commas(joined):
        if ":" not in entry:
            fail(f"unparseable pin-map entry: {entry!r}")
        name, _, ballpart = entry.partition(":")
        name = name.strip()
        ballpart = ballpart.strip()
        if ballpart.startswith("("):
            balls = tuple(b.strip() for b in ballpart.strip("()").split(",") if b.strip())
        else:
            balls = (ballpart,)
        if name in out:
            p = out[name]
            out[name] = Port(p.name, p.direction, p.width, balls)
        # A mapped name without a port declaration is possible in principle;
        # ignore rather than fail -- the pin map is only used for labeling.
    return out


def _parse_opcodes(
    value: str, irlen: int, fail
) -> tuple[dict[str, int], tuple[str, ...]]:
    joined = _join_fragments(value)
    opcodes: dict[str, int] = {}
    private: list[str] = []
    for m in re.finditer(r"(\w+)\s*\(([^()]*)\)", joined):
        name = m.group(1)
        patterns = [p.strip() for p in m.group(2).split(",") if p.strip()]
        if not patterns:
            fail(f"instruction {name} has no opcode pattern")
        for p in patterns:
            if len(p) != irlen or not re.fullmatch(r"[01X]+", p):
                fail(
                    f"instruction {name}: pattern {p!r} is not {irlen} bits of 0/1/X"
                )
        if len(patterns) == 1 and "X" not in patterns[0]:
            opcodes[name] = int(patterns[0], 2)
        else:
            private.extend(patterns)
    return opcodes, tuple(private)


def _parse_cells(value: str, fail) -> list[BoundaryCell]:
    joined = _join_fragments(value)
    cells: dict[int, BoundaryCell] = {}
    for m in _CELL_ENTRY.finditer(joined):
        num = int(m.group(1))
        fields = [f.strip() for f in m.group(2).split(",")]
        if len(fields) not in (4, 7):
            fail(f"cell {num}: expected 4 or 7 fields, got {len(fields)}: {fields}")
        cell, port, function, safe = fields[:4]
        entry = BoundaryCell(
            num=num,
            cell=cell,
            port=None if port == "*" else port,
            function=function.lower(),
            safe=safe.upper(),
            ccell=int(fields[4]) if len(fields) == 7 else None,
            disval=int(fields[5]) if len(fields) == 7 else None,
            rslt=fields[6] if len(fields) == 7 else None,
        )
        if num in cells:
            fail(f"cell {num} defined twice")
        cells[num] = entry
    return [cells[n] for n in sorted(cells)]


def _parse_register_access(
    value: str, fail
) -> dict[str, tuple[int | None, tuple[str, ...]]]:
    joined = _join_fragments(value)
    out: dict[str, tuple[int | None, tuple[str, ...]]] = {}
    for entry in _split_top_commas(joined):
        if not entry:
            continue
        m = _REG_ACCESS_ENTRY.match(entry)
        if not m:
            fail(f"unparseable REGISTER_ACCESS entry: {entry!r}")
        name, length, instrs = m.group(1), m.group(2), m.group(3)
        out[name] = (
            int(length) if length else None,
            tuple(i.strip() for i in instrs.split(",") if i.strip()),
        )
    return out


# --------------------------------------------------------------------------
# Whole-file parse
# --------------------------------------------------------------------------


def parse_bsdl_text(text: str, source: str) -> BsdlFile:
    """Parse BSDL text. ``source`` labels errors and the result's ``path``."""

    def fail(reason: str) -> None:
        raise BsdlError(f"{source}: {reason}")

    clean = _strip_comments(text)

    em = re.search(r"\bentity\s+(\w+)\s+is\b", clean, re.IGNORECASE)
    if not em:
        fail("no entity declaration -- not a BSDL file?")
    entity = em.group(1)

    gm = re.search(
        r'PHYSICAL_PIN_MAP\s*:\s*string\s*:=\s*"(\w+)"', clean, re.IGNORECASE
    )
    physical_pin_map = gm.group(1) if gm else None

    ports = _parse_ports(clean, fail)

    pin_map_value: str | None = None
    tap_scan_hz: float | None = None
    compliance: str | None = None
    irlen: int | None = None
    opcode_value: str | None = None
    capture: int | None = None
    idcode: int | None = None
    reg_access: dict[str, tuple[int | None, tuple[str, ...]]] = {}
    boundary_length: int | None = None
    cells_value: str | None = None
    warnings_text = ""
    notes: list[str] = []

    for stmt in _statements(clean):
        cm = re.match(
            r"^constant\s+(\w+)\s*:\s*PIN_MAP_STRING\s*:=\s*(.*)$",
            stmt,
            re.DOTALL | re.IGNORECASE,
        )
        if cm:
            # First constant wins as the fallback; the one the PHYSICAL_PIN_MAP
            # generic names overrides it.
            if cm.group(1) == physical_pin_map or pin_map_value is None:
                pin_map_value = cm.group(2)
            continue
        am = _ATTRIBUTE.match(stmt)
        if not am:
            continue
        name, value = am.group(1).upper(), am.group(2).strip()
        if name == "TAP_SCAN_CLOCK":
            fm = re.search(r"\(\s*([0-9.eE+]+)", value)
            tap_scan_hz = float(fm.group(1)) if fm else None
        elif name == "COMPLIANCE_PATTERNS":
            compliance = " ".join(_join_fragments(value).split())
        elif name == "INSTRUCTION_LENGTH":
            irlen = int(value)
        elif name == "INSTRUCTION_OPCODE":
            opcode_value = value
        elif name == "INSTRUCTION_CAPTURE":
            capture = int(_join_fragments(value), 2)
        elif name == "IDCODE_REGISTER":
            bits = _join_fragments(value)
            if len(bits) != 32 or not re.fullmatch(r"[01]+", bits):
                fail(f"IDCODE_REGISTER is not 32 binary bits: {bits!r}")
            idcode = int(bits, 2)
        elif name == "REGISTER_ACCESS":
            reg_access = _parse_register_access(value, fail)
        elif name == "BOUNDARY_LENGTH":
            boundary_length = int(value)
        elif name == "BOUNDARY_REGISTER":
            cells_value = value
        elif name == "DESIGN_WARNING":
            warnings_text = _join_fragments(value)

    if irlen is None:
        fail("no INSTRUCTION_LENGTH attribute")
    if opcode_value is None:
        fail("no INSTRUCTION_OPCODE attribute")
    if idcode is None:
        fail("no IDCODE_REGISTER attribute")
    if idcode & 1 != 1:
        fail(f"IDCODE 0x{idcode:08X} violates IEEE 1149.1 (LSB must be 1)")
    if boundary_length is None:
        fail("no BOUNDARY_LENGTH attribute")
    if cells_value is None:
        fail("no BOUNDARY_REGISTER attribute")

    if pin_map_value is not None:
        ports = _parse_pin_map(pin_map_value, ports, fail)

    opcodes, private_opcodes = _parse_opcodes(opcode_value, irlen, fail)
    cells = _parse_cells(cells_value, fail)

    if len(cells) != boundary_length:
        fail(
            f"BOUNDARY_LENGTH says {boundary_length} cells but "
            f"{len(cells)} were defined"
        )
    if cells and (cells[0].num != 0 or cells[-1].num != boundary_length - 1):
        fail("boundary cell numbering has gaps or does not start at 0")
    for c in cells:
        if c.ccell is not None:
            if not 0 <= c.ccell < boundary_length:
                fail(f"cell {c.num}: control cell {c.ccell} does not exist")
            target = cells[c.ccell]
            if target.function not in ("control", "controlr", "internal"):
                fail(
                    f"cell {c.num}: control cell {c.ccell} has function "
                    f"{target.function!r}, not control"
                )
        if c.port is not None and c.port not in ports:
            notes.append(f"cell {c.num} names undeclared port {c.port!r}")

    return BsdlFile(
        path=source,
        entity=entity,
        physical_pin_map=physical_pin_map,
        ports=ports,
        tap_scan_hz=tap_scan_hz,
        compliance_patterns=compliance,
        instruction_length=irlen,
        opcodes=opcodes,
        private_opcodes=private_opcodes,
        instruction_capture=capture,
        idcode=idcode,
        register_access=reg_access,
        boundary_length=boundary_length,
        cells=cells,
        design_warnings=warnings_text,
        notes=notes,
    )


def parse_bsdl(path: str | Path) -> BsdlFile:
    """Parse a BSDL file from disk. Raises BsdlError; missing file is OSError."""
    p = Path(path)
    return parse_bsdl_text(_read_text(p), str(p))


# --------------------------------------------------------------------------
# Bundled files
# --------------------------------------------------------------------------


def bundled_names() -> list[str]:
    """Names of the ``*.bsdl`` files shipped in s32edbg.data."""
    root = resources.files("s32edbg.data")
    return sorted(
        entry.name for entry in root.iterdir() if entry.name.endswith(".bsdl")
    )


@lru_cache(maxsize=None)
def _bundled(name: str) -> BsdlFile:
    blob = resources.files("s32edbg.data").joinpath(name).read_bytes()
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        text = blob.decode("cp1252")
    return parse_bsdl_text(text, f"bundled:{name}")


def drop_cells(b: BsdlFile, start: int, count: int, *, why: str = "") -> BsdlFile:
    """A derived model with ``count`` cells removed at ``start``, renumbered.

    For silicon whose chain omits a segment the BSDL describes (the measured
    S32E277 case: 30 internal cells at 262). Refuses to drop anything that
    would lose information: cells with a port, or cells some kept pin uses as
    its control cell. Everything above the cut shifts down, control-cell
    references included, so the derived model decodes and drives directly.
    """
    end = start + count
    removed = [c for c in b.cells if start <= c.num < end]
    if len(removed) != count:
        raise BsdlError(
            f"{b.path}: cannot drop cells {start}..{end - 1}: only "
            f"{len(removed)} exist there"
        )
    for c in removed:
        if c.port is not None:
            raise BsdlError(
                f"{b.path}: refusing to drop cell {c.num}: it belongs to pin {c.port}"
            )
    kept_targets = {
        c.ccell for c in b.cells if c.ccell is not None and not start <= c.num < end
    }
    hit = sorted(t for t in kept_targets if start <= t < end)
    if hit:
        raise BsdlError(
            f"{b.path}: refusing to drop cells {start}..{end - 1}: kept cells "
            f"use {hit} as control cells"
        )

    def shift(n: int) -> int:
        return n - count if n >= end else n

    new_cells = [
        dataclasses.replace(
            c,
            num=shift(c.num),
            ccell=None if c.ccell is None else shift(c.ccell),
        )
        for c in b.cells
        if not start <= c.num < end
    ]
    note = f"cell quirk: dropped cells {start}..{end - 1} ({count})" + (
        f" -- {why}" if why else ""
    )
    return dataclasses.replace(
        b,
        cells=new_cells,
        boundary_length=b.boundary_length - count,
        notes=[*b.notes, note],
    )


def _apply_quirks(b: BsdlFile) -> BsdlFile:
    from .data import bscan as data_bscan

    quirk = data_bscan.BSDL_CELL_QUIRKS.get(b.idcode)
    if quirk is None:
        return b
    start, count, why = quirk
    return drop_cells(b, start, count, why=why)


def load_bundled(idcode: int | None, *, apply_quirks: bool = True) -> BsdlFile | None:
    """The bundled BSDL matching an IDCODE, exact first, fuse-bits-masked second.

    Bits [20:17] come from the secure fuse box and are not valid immediately
    out of reset (RM L412549), so a masked retry mirrors what ``tap.probe``
    does. The known S32E2 IDs differ at bit 14, so masking cannot conflate
    them. None when nothing matches or no idcode was given. Measured silicon
    deviations (``data.bscan.BSDL_CELL_QUIRKS``) are applied unless
    ``apply_quirks=False`` asks for the file verbatim.
    """
    if idcode is None:
        return None
    files = [_bundled(n) for n in bundled_names()]
    match = next((b for b in files if b.idcode == idcode), None)
    if match is None:
        match = next(
            (
                b
                for b in files
                if (b.idcode & ~IDCODE_FUSE_MASK) == (idcode & ~IDCODE_FUSE_MASK)
            ),
            None,
        )
    if match is None:
        return None
    return _apply_quirks(match) if apply_quirks else match


def resolve(bsdl_path: str | None, idcode: int | None) -> BsdlFile | None:
    """The BSDL to use: explicit path wins (verbatim, no quirks -- an explicit
    file is an explicit choice), else bundled-by-IDCODE, else the single
    bundled file; quirked in the bundled cases. None when nothing applies."""
    if bsdl_path:
        return parse_bsdl(bsdl_path)
    b = load_bundled(idcode)
    if b is not None:
        return b
    names = bundled_names()
    if len(names) == 1:
        return _apply_quirks(_bundled(names[0]))
    return None


# --------------------------------------------------------------------------
# Pin view
# --------------------------------------------------------------------------

#: Data-cell functions: the cell samples/drives the pin itself. ``control``
#: and ``internal`` cells are wiring, not pins.
_DATA_FUNCTIONS = ("input", "output2", "output3", "bidir", "observe_only")


def _pad_identity(ball: str) -> tuple[str | None, str | None]:
    """(pad label, register detail) for a ball, from the generated IOMUX table.

    Joined by ball — package-level truth for the BGA 27x27, whatever BSDL is
    loaded (a different package's BSDL would join garbage; this tool targets
    one package). Main-die pads label as their PAD_xxx name; AE-die pads keep
    the port name as the row name already, so their label is the MSCR index
    that the name alone does not reveal.
    """
    from .data.pads_bga27 import BALL_TO_PAD

    entry = BALL_TO_PAD.get(ball)
    if entry is None:
        return None, None
    pad, instance, mscr = entry
    detail = f"{instance} MSCR {mscr}"
    return (f"AE MSCR {mscr}" if instance == "SIUL2_AE" else pad), detail


def pin_rows(b: BsdlFile) -> list[PinDef]:
    """Every observable pin, one row per data cell, sorted by cell number."""
    rows: list[PinDef] = []
    for c in b.cells:
        if c.port is None or c.function not in _DATA_FUNCTIONS:
            continue
        port = b.ports.get(c.port)
        ball = ",".join(port.balls) if port else "?"
        pad, pad_detail = _pad_identity(ball)
        rows.append(
            PinDef(
                name=c.port,
                ball=ball,
                direction=port.direction if port else "?",
                cell=c.num,
                function=c.function,
                control_cell=c.ccell,
                disval=c.disval,
                disable_result=c.rslt,
                safe=c.safe,
                sample_input_blocked=c.port.startswith(_SAMPLE_BLOCKED_PREFIX),
                pad=pad,
                pad_detail=pad_detail,
            )
        )
    return rows


def decode_sample(
    b: BsdlFile, bits: list[int], *, reverse: bool = False
) -> list[PinState]:
    """Turn an LSB-first capture (bit n = cell n) into named pin states.

    ``reverse`` flips the orientation (bit n = cell length-1-n) -- the escape
    hatch if the bench falsifies the IEEE cell-0-nearest-TDO default. Extra
    pad bits beyond boundary_length (the SVF path shifts whole bytes) are
    ignored.
    """
    n = b.boundary_length
    if len(bits) < n:
        raise BsdlError(
            f"capture has {len(bits)} bits but the boundary register needs {n}"
        )

    def bit(cell: int) -> int:
        return bits[n - 1 - cell] if reverse else bits[cell]

    states: list[PinState] = []
    for pin in pin_rows(b):
        enabled: bool | None = None
        if pin.control_cell is not None and pin.disval is not None:
            enabled = bit(pin.control_cell) != pin.disval
        states.append(PinState(pin=pin, level=bit(pin.cell), driver_enabled=enabled))
    return states


def name_cells(b: BsdlFile, indices: list[int]) -> list[str]:
    """Human names for raw cell indices, for diffs and diagnostics."""
    by_data = {p.cell: p for p in pin_rows(b)}
    by_control: dict[int, PinDef] = {}
    for p in by_data.values():
        if p.control_cell is not None:
            by_control.setdefault(p.control_cell, p)
    out: list[str] = []
    for i in indices:
        if i >= b.boundary_length:
            out.append(f"cell {i} (pad)")
        elif i in by_data:
            out.append(by_data[i].name)
        elif i in by_control:
            out.append(f"cell {i} (control for {by_control[i].name})")
        else:
            out.append(f"cell {i} ({b.cells[i].function})")
    return out


# --------------------------------------------------------------------------
# Drive vector (EXTEST / CLAMP preload)
# --------------------------------------------------------------------------


def safe_vector(b: BsdlFile) -> int:
    """Every cell at its BSDL safe value (X counts as 0): all drivers off."""
    v = 0
    for c in b.cells:
        if c.safe == "1":
            v |= 1 << c.num
    return v


def build_drive_vector(
    b: BsdlFile, drives: dict[str, str]
) -> tuple[int, list[dict[str, Any]]]:
    """The PRELOAD vector for a set of ``PIN -> "0"|"1"|"Z"`` requests.

    Undriven cells hold their safe values, control cells their disval (driver
    off), so only the requested pins ever drive. Refuses -- with the reason --
    pins that do not exist, exist but have no boundary cell (AUR_*, JCOMP,
    POR_B, power), sit on ``in``/``linkage`` ports, or are asked for ``Z``
    without a control cell (DDR0_RESET_N is always driven).
    """
    pins = {p.name.upper(): p for p in pin_rows(b)}
    vector = safe_vector(b)
    plan: list[dict[str, Any]] = []
    for name, raw in drives.items():
        value = raw.strip().upper()
        if value not in ("0", "1", "Z"):
            raise BsdlError(f"{name}: drive value must be 0, 1 or Z, not {raw!r}")
        pin = pins.get(name.upper())
        if pin is None:
            port = next(
                (p for p in b.ports.values() if p.name.upper() == name.upper()), None
            )
            if port is not None:
                raise BsdlError(
                    f"{port.name} has no boundary-scan cell "
                    f"({port.direction} port) -- not observable or drivable"
                )
            raise BsdlError(f"unknown pin {name!r} -- see `bscan pins` for the list")
        if pin.direction in ("in", "linkage"):
            raise BsdlError(
                f"{pin.name} is a {pin.direction} port; driving it is meaningless"
            )
        if value == "Z":
            if pin.control_cell is None:
                raise BsdlError(
                    f"{pin.name} has no control cell ({pin.function}); it cannot "
                    "be put in Z"
                )
            vector &= ~(1 << pin.cell)
            vector &= ~(1 << pin.control_cell)
            if pin.disval == 1:
                vector |= 1 << pin.control_cell
            plan.append(_plan_row(pin, None, False))
            continue
        level = int(value)
        if level:
            vector |= 1 << pin.cell
        else:
            vector &= ~(1 << pin.cell)
        if pin.control_cell is not None and pin.disval is not None:
            enable = 1 - pin.disval
            if enable:
                vector |= 1 << pin.control_cell
            else:
                vector &= ~(1 << pin.control_cell)
        plan.append(_plan_row(pin, level, True))
    plan.sort(key=lambda r: r["cell"])
    return vector, plan


def _plan_row(pin: PinDef, level: int | None, driving: bool) -> dict[str, Any]:
    return {
        "name": pin.name,
        "ball": pin.ball,
        "cell": pin.cell,
        "control_cell": pin.control_cell,
        "level": level,
        "driving": driving,
    }


# --------------------------------------------------------------------------
# Cross-checks against the RM opcode table
# --------------------------------------------------------------------------


def opcode_disagreements(b: BsdlFile) -> list[str]:
    """Where the BSDL's 16-bit opcodes differ from RM Table 959 as coded.

    The BSDL wins at shift time (``jtag.bscan.ir_for``); this exists so a
    divergence is loud rather than silent. Today the bundled file yields
    exactly one line: ENABLE_CENSOR_CTRL 0x0100 vs the RM's 0x0107 -- an
    instruction boundary scan never issues.
    """
    from .data import bscan as data_bscan

    if b.instruction_length != 16:
        return []
    name_map = {
        "BYPASS": data_bscan.JTAGC_IR_BYPASS,
        "IDCODE": data_bscan.JTAGC_IR_IDCODE,
        "SAMPLE": data_bscan.JTAGC_IR_SAMPLE_PRELOAD,
        "PRELOAD": data_bscan.JTAGC_IR_SAMPLE_PRELOAD,
        "EXTEST": data_bscan.JTAGC_IR_EXTEST,
        "HIGHZ": data_bscan.JTAGC_IR_HIZ,
        "CLAMP": data_bscan.JTAGC_IR_CLAMP,
        "EXT_TEST_CTRL": data_bscan.JTAGC_IR_EXT_TEST_CTRL,
        "ENABLE_CENSOR_CTRL": data_bscan.JTAGC_IR_CENSOR_CTRL,
    }
    out: list[str] = []
    for name, opcode8 in name_map.items():
        if name not in b.opcodes:
            continue
        rm = data_bscan.JTAGC_IR16.get(opcode8)
        if rm is not None and rm != b.opcodes[name]:
            out.append(
                f"{name}: BSDL 0x{b.opcodes[name]:04X} vs RM Table 959 0x{rm:04X}"
            )
    return out

"""CoreSight ROM table walker for ADIv6 / SoC-600.

Handles both table classes, which differ in ways that are easy to get wrong:

  Class 0x1 (legacy, ADIv5-style) -- ADIv6 section D3.4.4, p306
      PRESENT is bit[0]; FORMAT is bit[1]. Entry region 0x000..0xEFC (960
      entries). End of table is signalled *only* by a literal 0x00000000.
      ADIv6 p298: "an entry that is marked as not present must be skipped ...
      you must not assume that an entry that is marked not present represents
      the end of the ROM Table."
      MEMTYPE sits at 0xFCC, where a class 0x9 table has DEVTYPE, so ADIv6 p291
      says: "When identifying Class 0x1 ROM Tables, DEVARCH and DEVTYPE are
      treated as having a value of 0." We therefore do not read them.

  Class 0x9 (CoreSight) -- ADIv6 section D4.5.17, p343
      PRESENT is bits[1:0]:
          0b00 = not present AND this is the final entry
          0b10 = not present, not final
          0b11 = present
          0b01 = reserved
      Entry region 0x000..0x7FC. DEVID.FORMAT selects the entry stride:
      0x0 -> 32-bit entries (512 max), 0x1 -> 64-bit entries (256 max).
      Identified by DEVARCH.ARCHID == 0x0AF7.

Common to both -- ADIv6 p289:
      "Component_n_Address = ROM_Base_Address + (ROMENTRY<n>.OFFSET << 12)"
      OFFSET is signed, two's complement. A present entry may not have
      OFFSET == 0, because that would point back at the containing table.

Notes on things deliberately *not* done:
  * PIDR4.SIZE is not used to infer component size. ADIv6 p289: "The use of the
    PIDR4.SIZE field is deprecated."
  * No LAR unlock is attempted. The LAR/LSR software lock gates writes only, is
    not implemented at all for an AP ("For an AP, the lock mechanism is not
    implemented", ADIv6 p163), and SoC-600 ROM/AP LAR/LSR read RO 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..data.known_ids import (
    CLASS_CORESIGHT,
    CLASS_NAMES,
    CLASS_ROM_TABLE,
    DevArch,
    decode_devarch,
    decode_devtype,
    jep106_name,
)
from ..errors import DapError

#: A 32-bit reader over some address space. Must raise DapError on a fault.
Read32 = Callable[[int], int]


class SupportsRead32(Protocol):
    def __call__(self, addr: int, /) -> int: ...


# --------------------------------------------------------------------------
# Component ID block -- ADIv6 Table D1-1, p276
# --------------------------------------------------------------------------

OFF_PIDR4 = 0xFD0
OFF_PIDR0 = 0xFE0
OFF_PIDR1 = 0xFE4
OFF_PIDR2 = 0xFE8
OFF_PIDR3 = 0xFEC
OFF_CIDR0 = 0xFF0
OFF_CIDR1 = 0xFF4
OFF_CIDR2 = 0xFF8
OFF_CIDR3 = 0xFFC
OFF_DEVARCH = 0xFBC
OFF_DEVTYPE = 0xFCC
OFF_DEVID = 0xFC8

#: Fixed CIDR preamble values -- ADIv6 Table D1-1.
CIDR0_PREAMBLE = 0x0D
CIDR1_PREAMBLE = 0x0
CIDR2_PREAMBLE = 0x05
CIDR3_PREAMBLE = 0xB1

#: ARCHID that marks a class 0x9 table as a ROM table -- ADIv6 p333.
ARCHID_ROM_TABLE = 0x0AF7

#: DEVID.FORMAT -- ADIv6 p334.
DEVID_FORMAT_32BIT = 0x0
DEVID_FORMAT_64BIT = 0x1


@dataclass(frozen=True)
class ComponentId:
    """The decoded 0xFD0..0xFFC identification block of a CoreSight component."""

    base: int
    cidr: tuple[int, int, int, int]
    pidr: tuple[int, int, int, int, int]  # PIDR0..3, PIDR4
    devarch: DevArch | None
    devtype_raw: int | None
    devid_raw: int | None
    preamble_ok: bool

    @property
    def klass(self) -> int:
        return (self.cidr[1] >> 4) & 0xF

    @property
    def class_name(self) -> str:
        return CLASS_NAMES.get(self.klass, f"reserved(0x{self.klass:X})")

    @property
    def part_number(self) -> int:
        """12-bit part number: PIDR1[3:0] << 8 | PIDR0[7:0] (ADIv6 p280)."""
        return ((self.pidr[1] & 0x0F) << 8) | (self.pidr[0] & 0xFF)

    @property
    def jep106_identity(self) -> int:
        """7-bit JEP106 identity: PIDR2[2:0] << 4 | PIDR1[7:4] (ADIv6 p283)."""
        return ((self.pidr[2] & 0x07) << 4) | ((self.pidr[1] >> 4) & 0x0F)

    @property
    def jep106_continuation(self) -> int:
        """JEP106 continuation count: PIDR4[3:0] (ADIv6 p283)."""
        return self.pidr[4] & 0x0F

    @property
    def designer_name(self) -> str:
        return jep106_name(self.jep106_continuation, self.jep106_identity)

    @property
    def jedec_valid(self) -> bool:
        """PIDR2[3] must be 1 to indicate a JEDEC-assigned code (ADIv6 p283)."""
        return bool((self.pidr[2] >> 3) & 1)

    @property
    def revision(self) -> int:
        return (self.pidr[2] >> 4) & 0xF

    @property
    def revand(self) -> int:
        return (self.pidr[3] >> 4) & 0xF

    @property
    def customer_modified(self) -> int:
        return self.pidr[3] & 0xF

    @property
    def is_rom_table(self) -> bool:
        if self.klass == CLASS_ROM_TABLE:
            return True
        if self.klass == CLASS_CORESIGHT and self.devarch is not None:
            return self.devarch.present and self.devarch.archid == ARCHID_ROM_TABLE
        return False

    @property
    def devid_format(self) -> int:
        """Class 0x9 ROM table entry width selector (ADIv6 p334)."""
        if self.devid_raw is None:
            return DEVID_FORMAT_32BIT
        return self.devid_raw & 0xF

    @property
    def devtype(self) -> dict[str, object] | None:
        if self.devtype_raw is None:
            return None
        return decode_devtype(self.devtype_raw)

    def describe(self) -> str:
        bits = [f"class 0x{self.klass:X} ({self.class_name})"]
        bits.append(f"part 0x{self.part_number:03X}")
        bits.append(self.designer_name)
        if self.devarch is not None and self.devarch.present:
            name = self.devarch.archid_name or f"ARCHID 0x{self.devarch.archid:04X}"
            bits.append(name)
        if self.devtype_raw:
            dt = decode_devtype(self.devtype_raw)
            bits.append(str(dt.get("name") or dt["major_name"]))
        return ", ".join(bits)


def read_component_id(read32: Read32, base: int) -> ComponentId:
    """Read and decode the identification block at ``base``.

    Does not raise on a bad preamble -- ADIv6 p195 says a debugger meeting an
    invalid ID block should warn and keep going. Check ``preamble_ok``.
    """
    cidr = (
        read32(base + OFF_CIDR0) & 0xFF,
        read32(base + OFF_CIDR1) & 0xFF,
        read32(base + OFF_CIDR2) & 0xFF,
        read32(base + OFF_CIDR3) & 0xFF,
    )
    pidr = (
        read32(base + OFF_PIDR0) & 0xFF,
        read32(base + OFF_PIDR1) & 0xFF,
        read32(base + OFF_PIDR2) & 0xFF,
        read32(base + OFF_PIDR3) & 0xFF,
        read32(base + OFF_PIDR4) & 0xFF,
    )
    preamble_ok = (
        cidr[0] == CIDR0_PREAMBLE
        and (cidr[1] & 0x0F) == CIDR1_PREAMBLE
        and cidr[2] == CIDR2_PREAMBLE
        and cidr[3] == CIDR3_PREAMBLE
    )

    klass = (cidr[1] >> 4) & 0xF
    devarch = devtype_raw = devid_raw = None
    # ADIv6 p291: for a class 0x1 table, DEVARCH and DEVTYPE are treated as 0 --
    # MEMTYPE occupies the DEVTYPE offset, so reading it would misidentify.
    if klass != CLASS_ROM_TABLE:
        devarch = decode_devarch(read32(base + OFF_DEVARCH))
        devtype_raw = read32(base + OFF_DEVTYPE)
        devid_raw = read32(base + OFF_DEVID)

    return ComponentId(
        base=base,
        cidr=cidr,
        pidr=pidr,
        devarch=devarch,
        devtype_raw=devtype_raw,
        devid_raw=devid_raw,
        preamble_ok=preamble_ok,
    )


# --------------------------------------------------------------------------
# ROM table entries
# --------------------------------------------------------------------------


def sign_extend(value: int, bits: int) -> int:
    """Interpret the low ``bits`` of ``value`` as two's complement."""
    sign = 1 << (bits - 1)
    return (value & (sign - 1)) - (value & sign)


@dataclass(frozen=True)
class RomEntry:
    index: int
    raw: int
    present: bool
    final: bool  #: this entry terminates the table
    offset_pages: int  #: signed, in 4 KB units
    component_addr: int | None
    powerid: int | None

    @property
    def reserved_encoding(self) -> bool:
        return False


def decode_entry_class1(index: int, raw: int, rom_base: int) -> RomEntry:
    """ADIv6 section D3.4.4, p306. 32-bit entries only."""
    if raw == 0x00000000:
        return RomEntry(index, raw, False, True, 0, None, None)
    present = bool(raw & 0x1)
    poweridvalid = bool((raw >> 2) & 1)
    offset = sign_extend((raw >> 12) & 0xFFFFF, 20)
    return RomEntry(
        index=index,
        raw=raw,
        present=present,
        final=False,
        offset_pages=offset,
        component_addr=(rom_base + (offset << 12)) & 0xFFFFFFFF if present else None,
        powerid=((raw >> 4) & 0x1F) if poweridvalid else None,
    )


def decode_entry_class9(index: int, raw: int, rom_base: int, *, wide: bool) -> RomEntry:
    """ADIv6 section D4.5.17, p343. PRESENT is two bits."""
    present_field = raw & 0x3
    present = present_field == 0b11
    final = present_field == 0b00
    poweridvalid = bool((raw >> 2) & 1)
    if wide:
        offset = sign_extend((raw >> 12) & ((1 << 52) - 1), 52)
    else:
        offset = sign_extend((raw >> 12) & 0xFFFFF, 20)
    return RomEntry(
        index=index,
        raw=raw,
        present=present,
        final=final,
        offset_pages=offset,
        component_addr=(rom_base + (offset << 12)) if present else None,
        powerid=((raw >> 4) & 0x1F) if poweridvalid else None,
    )


#: Entry-region limits. ADIv6 p297 (class 0x1) and p311 (class 0x9): the entry at
#: the last offset is always the final entry, even if it does not look like one.
CLASS1_LAST_ENTRY_OFFSET = 0xEFC
CLASS9_LAST_ENTRY_OFFSET = 0x7FC


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------


@dataclass
class Node:
    """A discovered component; ``entries``/``children`` are populated for tables."""

    base: int
    ident: ComponentId | None
    error: str | None = None
    is_table: bool = False
    entries: list[RomEntry] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)
    depth: int = 0
    revisit_of: int | None = None  #: set when a duplicate reference is detected

    def flatten(self) -> list["Node"]:
        out = [self]
        for child in self.children:
            out.extend(child.flatten())
        return out


DEFAULT_MAX_DEPTH = 16


def walk(
    read32: Read32,
    base: int,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    _visited: set[int] | None = None,
    _depth: int = 0,
) -> Node:
    """Recursively walk the ROM table at ``base``.

    ADIv6 prohibits circular and duplicate references (section D2.6.3), but a
    walker must not hang if silicon violates that, so a visited set and a depth
    cap are enforced regardless. Faults are recorded on the node and the walk
    continues, per ADIv6 p195: "Arm recommends that it continues operating."
    """
    visited = _visited if _visited is not None else set()

    if base in visited:
        return Node(base=base, ident=None, error="already visited", depth=_depth, revisit_of=base)
    visited.add(base)

    if _depth > max_depth:
        return Node(base=base, ident=None, error=f"max depth {max_depth} reached", depth=_depth)

    try:
        ident = read_component_id(read32, base)
    except DapError as exc:
        return Node(base=base, ident=None, error=f"ID block unreadable: {exc}", depth=_depth)

    node = Node(base=base, ident=ident, depth=_depth)

    if not ident.preamble_ok:
        node.error = (
            "invalid CoreSight component ID preamble "
            f"(CIDR {ident.cidr[3]:#04x} {ident.cidr[2]:#04x} "
            f"{ident.cidr[1]:#04x} {ident.cidr[0]:#04x})"
        )
        return node

    if not ident.is_rom_table:
        return node

    node.is_table = True
    klass = ident.klass
    if klass == CLASS_ROM_TABLE:
        stride, last_off, wide = 4, CLASS1_LAST_ENTRY_OFFSET, False
    else:
        wide = ident.devid_format == DEVID_FORMAT_64BIT
        stride = 8 if wide else 4
        last_off = CLASS9_LAST_ENTRY_OFFSET

    index = 0
    off = 0x000
    while off <= last_off:
        try:
            if wide:
                lo = read32(base + off)
                hi = read32(base + off + 4)
                raw = (hi << 32) | lo
            else:
                raw = read32(base + off)
        except DapError as exc:
            node.entries.append(RomEntry(index, 0, False, False, 0, None, None))
            node.children.append(
                Node(base=base + off, ident=None, error=f"entry read failed: {exc}", depth=_depth + 1)
            )
            off += stride
            index += 1
            continue

        if klass == CLASS_ROM_TABLE:
            entry = decode_entry_class1(index, raw, base)
        else:
            entry = decode_entry_class9(index, raw, base, wide=wide)
        node.entries.append(entry)

        if entry.present and entry.component_addr is not None:
            if entry.offset_pages == 0:
                node.children.append(
                    Node(
                        base=entry.component_addr,
                        ident=None,
                        error="entry offset is 0, which self-references the table (prohibited)",
                        depth=_depth + 1,
                    )
                )
            else:
                node.children.append(
                    walk(
                        read32,
                        entry.component_addr,
                        max_depth=max_depth,
                        _visited=visited,
                        _depth=_depth + 1,
                    )
                )

        if entry.final:
            break

        off += stride
        index += 1

    return node


def summarise(root: Node) -> dict[str, int]:
    nodes = root.flatten()
    return {
        "nodes": len(nodes),
        "tables": sum(1 for n in nodes if n.is_table),
        "components": sum(1 for n in nodes if n.ident is not None and not n.is_table),
        "errors": sum(1 for n in nodes if n.error is not None),
    }

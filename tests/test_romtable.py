"""Unit tests for the ROM-table decoder. No hardware required."""

from __future__ import annotations

import pytest

from s32edbg.dap import romtable as rt
from s32edbg.errors import DapError

# --------------------------------------------------------------------------
# Synthetic device builder
# --------------------------------------------------------------------------

CIDR_CLASS9 = {rt.OFF_CIDR0: 0x0D, rt.OFF_CIDR1: 0x90, rt.OFF_CIDR2: 0x05, rt.OFF_CIDR3: 0xB1}
CIDR_CLASS1 = {rt.OFF_CIDR0: 0x0D, rt.OFF_CIDR1: 0x10, rt.OFF_CIDR2: 0x05, rt.OFF_CIDR3: 0xB1}
CIDR_CLASS9_COMPONENT = dict(CIDR_CLASS9)

DEVARCH_ROM = 0x47700AF7  # Arm, present, ARCHID 0x0AF7
DEVARCH_MEMAP = 0x47700A17


class FakeDevice:
    """Sparse 32-bit address space; unmapped reads raise DapError."""

    def __init__(self) -> None:
        self.mem: dict[int, int] = {}
        self.reads: list[int] = []

    def read32(self, addr: int) -> int:
        self.reads.append(addr)
        try:
            return self.mem[addr]
        except KeyError:
            raise DapError(f"no response at 0x{addr:08X}") from None

    def put(self, base: int, values: dict[int, int]) -> None:
        for off, val in values.items():
            self.mem[base + off] = val

    def add_class9_rom(self, base: int, entries: list[int], *, devid: int = 0x0) -> None:
        self.put(base, CIDR_CLASS9)
        self.put(base, {rt.OFF_DEVARCH: DEVARCH_ROM, rt.OFF_DEVTYPE: 0x00, rt.OFF_DEVID: devid})
        self.put(base, {o: 0 for o in (rt.OFF_PIDR0, rt.OFF_PIDR1, rt.OFF_PIDR2, rt.OFF_PIDR3, rt.OFF_PIDR4)})
        for i, raw in enumerate(entries):
            self.mem[base + i * 4] = raw

    def add_class1_rom(self, base: int, entries: list[int]) -> None:
        self.put(base, CIDR_CLASS1)
        self.put(base, {o: 0 for o in (rt.OFF_PIDR0, rt.OFF_PIDR1, rt.OFF_PIDR2, rt.OFF_PIDR3, rt.OFF_PIDR4)})
        # MEMTYPE lives at 0xFCC on a class 0x1 table; poison it so that a
        # walker which wrongly reads DEVTYPE there would produce garbage.
        self.mem[base + rt.OFF_DEVTYPE] = 0xDEADBEEF
        self.mem[base + rt.OFF_DEVARCH] = 0xDEADBEEF
        for i, raw in enumerate(entries):
            self.mem[base + i * 4] = raw

    def add_component(self, base: int, *, devtype: int = 0x15, part: int = 0x9A5) -> None:
        self.put(base, CIDR_CLASS9_COMPONENT)
        self.put(
            base,
            {
                rt.OFF_DEVARCH: DEVARCH_MEMAP,
                rt.OFF_DEVTYPE: devtype,
                rt.OFF_DEVID: 0,
                rt.OFF_PIDR0: part & 0xFF,
                rt.OFF_PIDR1: 0xB0 | ((part >> 8) & 0xF),  # DES_0=0xB, PART1
                rt.OFF_PIDR2: 0x4B,  # REVISION=4, JEDEC=1, DES_1=0b011
                rt.OFF_PIDR3: 0x00,
                rt.OFF_PIDR4: 0x04,  # continuation 4 -> Arm
            },
        )


def entry9(offset_pages: int, *, present: bool = True, powerid: int | None = None) -> int:
    raw = (offset_pages & 0xFFFFF) << 12
    raw |= 0b11 if present else 0b10
    if powerid is not None:
        raw |= (1 << 2) | ((powerid & 0x1F) << 4)
    return raw


def entry1(offset_pages: int, *, present: bool = True) -> int:
    raw = (offset_pages & 0xFFFFF) << 12
    raw |= 0b10  # FORMAT = 1, 32-bit
    if present:
        raw |= 0b01
    return raw


# --------------------------------------------------------------------------
# Sign extension and address arithmetic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,bits,expected",
    [(0x00001, 20, 1), (0xFFFFF, 20, -1), (0x80000, 20, -524288), (0x7FFFF, 20, 524287)],
)
def test_sign_extend(value: int, bits: int, expected: int) -> None:
    assert rt.sign_extend(value, bits) == expected


def test_negative_offset_resolves_below_table_base() -> None:
    """ADIv6 p344: negative OFFSET values are permitted, two's complement."""
    raw = entry9(-1 & 0xFFFFF)
    entry = rt.decode_entry_class9(0, raw, 0x0040_0000, wide=False)
    assert entry.present
    assert entry.offset_pages == -1
    assert entry.component_addr == 0x003F_F000


def test_real_s32e_apb_rom_first_entry() -> None:
    """Regression against a proven-on-silicon value.

    The first entry of this target's APB debug ROM at 0x01BFE000 reads
    0xFE402003 once the debug-enable gate is open. Its OFFSET field is 0xFE402,
    which is *negative* as a signed 20-bit value (-0x1BFE pages), so the
    component sits far below the table:
        0x01BFE000 + (-0x1BFE << 12) = 0x00000000
    and 0x00000000 is exactly where the RTU0 subsystem ROM table lives per RM
    Table 965. Getting the sign wrong here would send the walker to 0x1F7FC000
    and the whole tree would be missed.
    """
    entry = rt.decode_entry_class9(0, 0xFE402003, 0x01BF_E000, wide=False)
    assert entry.present and not entry.final
    assert entry.offset_pages == -0x1BFE
    assert entry.component_addr == 0x0000_0000


# --------------------------------------------------------------------------
# Class 0x9 two-bit PRESENT semantics -- the field most often mis-decoded
# --------------------------------------------------------------------------


def test_class9_present_encodings() -> None:
    base = 0x1000
    # 0b11 present
    e = rt.decode_entry_class9(0, (2 << 12) | 0b11, base, wide=False)
    assert (e.present, e.final) == (True, False)
    # 0b10 not present, NOT final -- the walk must continue past it
    e = rt.decode_entry_class9(1, (2 << 12) | 0b10, base, wide=False)
    assert (e.present, e.final) == (False, False)
    # 0b00 not present AND final
    e = rt.decode_entry_class9(2, 0x00000000, base, wide=False)
    assert (e.present, e.final) == (False, True)


def test_class9_skips_absent_entry_then_continues() -> None:
    dev = FakeDevice()
    dev.add_class9_rom(0x0000_0000, [entry9(1, present=False), entry9(2), 0x00000000])
    dev.add_component(0x0000_2000, devtype=0x14)
    root = rt.walk(dev.read32, 0x0000_0000)
    assert root.is_table
    # One absent entry, one present, one terminator.
    assert [e.present for e in root.entries] == [False, True, False]
    assert len(root.children) == 1
    assert root.children[0].base == 0x0000_2000


def test_class1_only_zero_terminates() -> None:
    """A class 0x1 entry with PRESENT=0 must NOT be read as end-of-table."""
    dev = FakeDevice()
    dev.add_class1_rom(0x0000_0000, [entry1(1, present=False), entry1(2), 0x00000000])
    dev.add_component(0x0000_2000)
    root = rt.walk(dev.read32, 0x0000_0000)
    assert root.is_table
    assert root.ident is not None and root.ident.klass == rt.CLASS_ROM_TABLE
    assert len(root.children) == 1, "walk stopped early on a not-present entry"
    assert root.children[0].base == 0x0000_2000


def test_class1_devarch_and_devtype_are_not_read() -> None:
    """ADIv6 p291: for class 0x1, DEVARCH/DEVTYPE are treated as 0.

    The fake poisons those offsets with 0xDEADBEEF; a correct walker never reads
    them, so the decoded identity must not carry those values.
    """
    dev = FakeDevice()
    dev.add_class1_rom(0x0000_0000, [0x00000000])
    root = rt.walk(dev.read32, 0x0000_0000)
    assert root.ident is not None
    assert root.ident.devarch is None
    assert root.ident.devtype_raw is None
    assert (0x0000_0000 + rt.OFF_DEVTYPE) not in dev.reads


# --------------------------------------------------------------------------
# PIDR / JEP106 assembly
# --------------------------------------------------------------------------


def test_pidr_decode_arm_designer_and_part() -> None:
    dev = FakeDevice()
    dev.add_component(0x0000_1000, part=0x9E2)
    ident = rt.read_component_id(dev.read32, 0x0000_1000)
    assert ident.preamble_ok
    assert ident.part_number == 0x9E2
    # DES_0 = 0xB, DES_1 = 0b011 -> identity 0x3B; continuation 4 -> Arm
    assert ident.jep106_identity == 0x3B
    assert ident.jep106_continuation == 4
    assert ident.designer_name == "Arm"
    assert ident.jedec_valid
    assert ident.revision == 4


def test_bad_preamble_is_reported_not_raised() -> None:
    dev = FakeDevice()
    dev.put(0x0000_0000, {rt.OFF_CIDR0: 0x00, rt.OFF_CIDR1: 0x90, rt.OFF_CIDR2: 0x05, rt.OFF_CIDR3: 0xB1})
    dev.put(
        0x0000_0000,
        {o: 0 for o in (rt.OFF_PIDR0, rt.OFF_PIDR1, rt.OFF_PIDR2, rt.OFF_PIDR3, rt.OFF_PIDR4)},
    )
    dev.put(0x0000_0000, {rt.OFF_DEVARCH: 0, rt.OFF_DEVTYPE: 0, rt.OFF_DEVID: 0})
    root = rt.walk(dev.read32, 0x0000_0000)
    assert root.error is not None and "preamble" in root.error
    assert not root.is_table


# --------------------------------------------------------------------------
# Robustness: loops, depth, faults
# --------------------------------------------------------------------------


def test_self_referencing_entry_is_flagged_not_followed() -> None:
    dev = FakeDevice()
    dev.add_class9_rom(0x0000_0000, [entry9(0), 0x00000000])
    root = rt.walk(dev.read32, 0x0000_0000)
    assert len(root.children) == 1
    assert root.children[0].error is not None
    assert "self-reference" in root.children[0].error


def test_circular_reference_terminates() -> None:
    dev = FakeDevice()
    # table A at 0x0 -> table B at 0x1000 -> back to A
    dev.add_class9_rom(0x0000_0000, [entry9(1), 0x00000000])
    dev.add_class9_rom(0x0000_1000, [entry9(-1 & 0xFFFFF), 0x00000000])
    root = rt.walk(dev.read32, 0x0000_0000)
    nodes = root.flatten()
    revisits = [n for n in nodes if n.revisit_of is not None]
    assert revisits, "cycle was not detected"
    assert len(nodes) < 10


def test_unreadable_component_records_error_and_continues() -> None:
    dev = FakeDevice()
    dev.add_class9_rom(0x0000_0000, [entry9(1), entry9(2), 0x00000000])
    # 0x1000 is absent from the fake memory -> DapError; 0x2000 is fine.
    dev.add_component(0x0000_2000)
    root = rt.walk(dev.read32, 0x0000_0000)
    assert len(root.children) == 2
    assert root.children[0].error is not None
    assert root.children[1].ident is not None
    summary = rt.summarise(root)
    assert summary["errors"] == 1


def test_powerid_is_decoded_when_valid() -> None:
    e = rt.decode_entry_class9(0, entry9(3, powerid=5), 0x0, wide=False)
    assert e.powerid == 5
    e = rt.decode_entry_class9(0, entry9(3), 0x0, wide=False)
    assert e.powerid is None


def test_wide_entries_use_64bit_stride() -> None:
    dev = FakeDevice()
    dev.add_class9_rom(0x0000_0000, [], devid=rt.DEVID_FORMAT_64BIT)
    # 64-bit entry: low word carries PRESENT + low offset bits, high word is 0.
    dev.mem[0x0000_0000] = entry9(1)
    dev.mem[0x0000_0004] = 0
    dev.mem[0x0000_0008] = 0
    dev.mem[0x0000_000C] = 0
    dev.add_component(0x0000_1000)
    root = rt.walk(dev.read32, 0x0000_0000)
    assert root.ident is not None and root.ident.devid_format == rt.DEVID_FORMAT_64BIT
    assert len(root.children) == 1
    assert root.children[0].base == 0x0000_1000

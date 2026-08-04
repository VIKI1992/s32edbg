"""The ELF32 reader behind `load`.

Synthetic images are built here byte-by-byte with struct, because the parser's
job is precisely to reject the shapes a toolchain never produces -- ELF64,
big-endian, truncated tables, overlapping LMAs -- and no toolchain can be asked
to produce them. One golden test runs against the real application ELF when it
exists on this machine, pinning the numbers the bench run must reproduce.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from s32edbg import elf
from s32edbg.errors import ElfError

BR1_REAR_ELF = Path(r"C:\br1_rear_ecu\build\bin\br1_rear.elf")


def make_elf(
    *,
    segments: list[tuple[int, int, bytes]] | None = None,  # (paddr, memsz_extra, data)
    ei_class: int = 1,
    ei_data: int = 1,
    machine: int = elf.EM_ARM,
    entry: int = 0x32100021,
    phentsize: int = elf.PHDR_SIZE,
    types: list[int] | None = None,
    truncate_phdrs: bool = False,
    truncate_data: bool = False,
) -> bytes:
    """Assemble a minimal ELF32: header, program headers, then segment bytes."""
    if segments is None:
        segments = [(0x32100000, 0, b"\x21\x00\x00\xea")]
    phnum = len(segments)
    phoff = 52
    data_off = phoff + phnum * elf.PHDR_SIZE
    ident = elf.ELF_MAGIC + bytes([ei_class, ei_data, 1, 0]) + b"\x00" * 8
    ehdr = struct.pack(
        "<HHIIIIIHHHHHH",
        2,  # ET_EXEC
        machine,
        1,
        entry,
        phoff,
        0,
        0,
        52,
        phentsize,
        phnum,
        0,
        0,
        0,
    )
    phdrs = b""
    blobs = b""
    off = data_off
    for i, (paddr, memsz_extra, data) in enumerate(segments):
        p_type = types[i] if types else elf.PT_LOAD
        filesz = len(data)
        phdrs += struct.pack(
            "<IIIIIIII", p_type, off, paddr, paddr, filesz, filesz + memsz_extra, 5, 4
        )
        blobs += data
        off += filesz
    out = ident + ehdr + phdrs + blobs
    if truncate_phdrs:
        out = out[: phoff + phnum * elf.PHDR_SIZE - 4]
    if truncate_data:
        out = out[:-1]
    return out


def test_a_minimal_image_parses_with_gdb_load_semantics() -> None:
    img = elf.parse(make_elf())
    assert img.entry == 0x32100021
    assert img.entry_pc == 0x32100020
    assert img.entry_thumb is True
    assert len(img.segments) == 1
    seg = img.segments[0]
    assert (seg.paddr, seg.filesz) == (0x32100000, 4)
    assert img.total_filesz == 4


def test_segments_are_sorted_by_lma_and_nobits_are_skipped() -> None:
    blob = make_elf(
        segments=[
            (0x32200000, 0, b"BBBB"),
            (0x31780000, 16, b""),  # filesz 0: NOBITS-style, never written
            (0x32100000, 0, b"AAAA"),
        ]
    )
    img = elf.parse(blob)
    assert [s.paddr for s in img.segments] == [0x32100000, 0x32200000]


def test_a_non_load_segment_is_ignored() -> None:
    blob = make_elf(
        segments=[(0x32100000, 0, b"AAAA"), (0x32200000, 0, b"EXID")],
        types=[elf.PT_LOAD, 0x70000001],  # EXIDX
    )
    img = elf.parse(blob)
    assert len(img.segments) == 1


def test_an_arm_entry_point_reports_not_thumb() -> None:
    img = elf.parse(make_elf(entry=0x32100020))
    assert img.entry_pc == 0x32100020
    assert img.entry_thumb is False


@pytest.mark.parametrize(
    "kwargs, needle",
    [
        (dict(ei_class=2), "ELF64"),
        (dict(ei_data=2), "big-endian"),
        (dict(machine=62), "machine 62"),
        (dict(entry=0), "no entry point"),
        (dict(phentsize=56), "56-byte program headers"),
        (dict(segments=[]), "no program headers"),
        (dict(truncate_phdrs=True), "program-header table out of bounds"),
        (dict(truncate_data=True), "past end of file"),
        (
            dict(segments=[(0x32100000, 0, b"AAAAAAAA"), (0x32100004, 0, b"BBBB")]),
            "overlap",
        ),
        (
            dict(segments=[(0x32100000, 16, b"")]),
            "no PT_LOAD segment with file content",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_unsupported_shapes_are_rejected_with_a_reason(kwargs, needle) -> None:
    with pytest.raises(ElfError, match=needle):
        elf.parse(make_elf(**kwargs))


def test_not_an_elf_at_all() -> None:
    with pytest.raises(ElfError, match="not an ELF"):
        elf.parse(b"MZ\x90\x00 definitely a PE")


def test_load_reports_a_missing_file_as_an_elf_error(tmp_path) -> None:
    with pytest.raises(ElfError, match="cannot read"):
        elf.load(tmp_path / "nope.elf")


def test_load_returns_the_blob_the_segments_index_into(tmp_path) -> None:
    p = tmp_path / "app.elf"
    p.write_bytes(make_elf(segments=[(0x32100000, 0, b"\xde\xad\xbe\xef")]))
    img, blob = elf.load(p)
    seg = img.segments[0]
    assert blob[seg.offset : seg.offset + seg.filesz] == b"\xde\xad\xbe\xef"
    assert img.name == "app.elf"


@pytest.mark.skipif(not BR1_REAR_ELF.exists(), reason="br1_rear.elf not on this machine")
def test_golden_numbers_for_the_real_application_elf() -> None:
    """The numbers the bench acceptance asserts. Measured 2026-08-04 from
    br1_rear.elf; if the application build changes they move together."""
    img, _blob = elf.load(BR1_REAR_ELF)
    assert img.entry == 0x32100021  # brsStartupEntryHyp | Thumb
    assert img.entry_pc == 0x32100020
    assert img.entry_thumb is True
    assert len(img.segments) == 11
    assert img.total_filesz == 3_677_786
    assert all(s.paddr % 4 == 0 for s in img.segments)
    assert min(s.paddr for s in img.segments) == 0x32100000
    assert max(s.end for s in img.segments) <= 0x327FFFFF
    ragged = [s for s in img.segments if s.filesz % 4]
    assert [(s.paddr, s.filesz) for s in ragged] == [(0x32101300, 50)]

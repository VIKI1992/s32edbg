"""Minimal ELF32 reader for the `load` command.

Deliberately not a general ELF library: it reads exactly what a RAM loader
needs -- the entry point and the PT_LOAD program headers -- and rejects
everything outside the one supported shape (little-endian ELF32 for ARM).
The load semantics mirror GDB's `load`, which is what the reference plugin
uses for the same job: the bytes of every PT_LOAD segment with ``p_filesz > 0``
belong at ``p_paddr`` (the LMA), and BSS/NOBITS space is not written -- the
application's own startup zeroes it. pyelftools would add a dependency to a
tool that otherwise needs none; the two structures parsed here are 52 and 32
bytes with fixed layouts.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .errors import ElfError

ELF_MAGIC = b"\x7fELF"
EI_CLASS_32 = 1
EI_DATA_LE = 1
EM_ARM = 40
PT_LOAD = 1

_EHDR = struct.Struct("<HHIIIIIHHHHHH")  # from e_type, after the 16-byte ident
_PHDR = struct.Struct("<IIIIIIII")
PHDR_SIZE = _PHDR.size


@dataclass(frozen=True)
class Segment:
    """One PT_LOAD program header with file content (``filesz > 0``)."""

    index: int  # program-header index in the ELF, for error messages
    paddr: int  # LMA -- where the loader writes the bytes
    vaddr: int  # VMA -- where the code expects them at run time
    offset: int  # file offset of the bytes
    filesz: int
    memsz: int

    @property
    def end(self) -> int:
        return self.paddr + self.filesz


@dataclass(frozen=True)
class Image:
    """Everything the loader needs to know about one ELF."""

    name: str
    entry: int  # raw e_entry; bit 0 = Thumb entry
    segments: tuple[Segment, ...]  # PT_LOAD with filesz > 0, sorted by paddr
    total_filesz: int

    @property
    def entry_pc(self) -> int:
        """The value to put in the PC: e_entry with the Thumb bit stripped."""
        return self.entry & ~1

    @property
    def entry_thumb(self) -> bool:
        """True when e_entry marks a Thumb (T32) entry point."""
        return bool(self.entry & 1)


def parse(blob: bytes, *, name: str = "<elf>") -> Image:
    """Parse an in-memory ELF. Raises ElfError for anything unsupported."""
    if len(blob) < 16 or blob[:4] != ELF_MAGIC:
        raise ElfError(f"{name} is not an ELF file (no \\x7fELF magic)")
    if blob[4] != EI_CLASS_32:
        raise ElfError(
            f"{name} is ELF64; this loader handles the ELF32 images the "
            "S32E27's 32-bit cores run"
        )
    if blob[5] != EI_DATA_LE:
        raise ElfError(f"{name} is big-endian; this target is little-endian")
    if len(blob) < 16 + _EHDR.size:
        raise ElfError(f"{name} is truncated: no complete ELF header")

    (
        _e_type,
        e_machine,
        _e_version,
        e_entry,
        e_phoff,
        _e_shoff,
        _e_flags,
        _e_ehsize,
        e_phentsize,
        e_phnum,
        _e_shentsize,
        _e_shnum,
        _e_shstrndx,
    ) = _EHDR.unpack_from(blob, 16)

    if e_machine != EM_ARM:
        raise ElfError(
            f"{name} is built for machine {e_machine}, not ARM ({EM_ARM})"
        )
    if e_entry == 0:
        raise ElfError(
            f"{name} has no entry point (e_entry = 0); nothing to set the PC to"
        )
    if e_phentsize != PHDR_SIZE:
        raise ElfError(
            f"{name} has {e_phentsize}-byte program headers; ELF32 defines {PHDR_SIZE}"
        )
    if e_phnum == 0:
        raise ElfError(f"{name} has no program headers, so nothing is loadable")
    if e_phoff + e_phnum * PHDR_SIZE > len(blob):
        raise ElfError(f"{name} is truncated: program-header table out of bounds")

    segments: list[Segment] = []
    for i in range(e_phnum):
        (
            p_type,
            p_offset,
            p_vaddr,
            p_paddr,
            p_filesz,
            p_memsz,
            _p_flags,
            _p_align,
        ) = _PHDR.unpack_from(blob, e_phoff + i * PHDR_SIZE)
        if p_type != PT_LOAD or p_filesz == 0:
            continue  # GDB-`load` semantics: only PT_LOAD file content is written
        if p_offset + p_filesz > len(blob):
            raise ElfError(
                f"{name} is truncated: segment {i} claims bytes "
                f"[0x{p_offset:X}, 0x{p_offset + p_filesz:X}) past end of file"
            )
        segments.append(
            Segment(
                index=i,
                paddr=p_paddr,
                vaddr=p_vaddr,
                offset=p_offset,
                filesz=p_filesz,
                memsz=p_memsz,
            )
        )

    if not segments:
        raise ElfError(f"{name} has no PT_LOAD segment with file content")

    segments.sort(key=lambda s: s.paddr)
    for a, b in zip(segments, segments[1:]):
        if a.end > b.paddr:
            raise ElfError(
                f"{name}: segments {a.index} and {b.index} overlap at LMA "
                f"0x{b.paddr:08X} -- a double write is never intended, refusing"
            )

    return Image(
        name=name,
        entry=e_entry,
        segments=tuple(segments),
        total_filesz=sum(s.filesz for s in segments),
    )


def load(path: str | Path) -> tuple[Image, bytes]:
    """Read a file and parse it. The blob is returned for the segment bytes."""
    p = Path(path)
    try:
        blob = p.read_bytes()
    except OSError as exc:
        raise ElfError(f"cannot read {p}: {exc}") from None
    return parse(blob, name=p.name), blob

"""The symbol index: everything the IDE needs out of one ELF.

Built once per `sym load` and cached on the debug session. Four sub-indexes:

- **line table** (DWARF line programs, both directions): address -> file:line
  for the views, file:line -> addresses for gutter breakpoints. line_to_addrs
  uses "nearest following line with code" semantics, the way every debugger
  places a breakpoint clicked on a comment.
- **functions** (DW_TAG_subprogram ranges, .symtab STT_FUNC fallback): names
  for stack frames and a boundary to snap disassembly windows to.
- **mapping symbols** ($a/$t/$d, ELF for the Arm Architecture §5.5.5): what
  encoding a given address holds -- the reliable Thumb/ARM discriminator.
- **CFI** (.debug_frame / .eh_frame): unwind rules. FDEs are indexed by range
  eagerly but decoded lazily per lookup -- decoding every FDE of a production
  image up front costs seconds for rows that are never asked for.

The raw PT_LOAD bytes are kept in memory so disassembly reads code from the
file instead of over the wire (~4 KB/s on the pure ADIv6 path).
"""

from __future__ import annotations

import bisect
import hashlib
import io
import os
from dataclasses import dataclass, field
from typing import Any

from . import require_pyelftools


@dataclass
class LineHit:
    file: str
    line: int


@dataclass
class FuncSpan:
    low: int
    high: int  # exclusive
    name: str


@dataclass
class Segment:
    vaddr: int
    data: bytes


@dataclass
class SymbolIndex:
    path: str
    size: int
    mtime: float
    sha1: str
    entry: int
    #: sorted by address: (addr, file, line) rows; end_sequence rows carry
    #: line None and terminate a run.
    line_rows: list[tuple[int, str | None, int | None]] = field(default_factory=list)
    #: file path (as the DWARF spells it, and lowercased basename) -> sorted
    #: {line -> [addrs]} maps.
    file_lines: dict[str, dict[int, list[int]]] = field(default_factory=dict)
    funcs: list[FuncSpan] = field(default_factory=list)
    #: sorted (addr, kind) with kind in "arm"|"thumb"|"data".
    mapping: list[tuple[int, str]] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    #: sorted (initial_location, end, fde) for lazy CFI decoding.
    _fdes: list[tuple[int, int, Any]] = field(default_factory=list)
    _decoded_fde: dict[int, Any] = field(default_factory=dict)
    cfi_source: str = "none"

    # -- construction --------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "SymbolIndex":
        require_pyelftools()
        from elftools.elf.elffile import ELFFile

        blob = open(path, "rb").read()
        st = os.stat(path)
        elffile = ELFFile(io.BytesIO(blob))
        index = cls(
            path=path,
            size=st.st_size,
            mtime=st.st_mtime,
            sha1=hashlib.sha1(blob).hexdigest(),
            entry=elffile.header["e_entry"] & ~1,
        )
        index._load_segments(elffile)
        index._load_symtab(elffile)
        if elffile.has_dwarf_info():
            dwarf = elffile.get_dwarf_info()
            index._load_lines(dwarf)
            index._load_functions(dwarf)
            index._load_cfi(dwarf)
        index.funcs.sort(key=lambda f: f.low)
        index.mapping.sort()
        index.line_rows.sort(key=lambda r: r[0])
        return index

    def _load_segments(self, elffile) -> None:
        for seg in elffile.iter_segments():
            if seg["p_type"] != "PT_LOAD" or seg["p_filesz"] == 0:
                continue
            self.segments.append(Segment(seg["p_vaddr"], seg.data()))
        self.segments.sort(key=lambda s: s.vaddr)

    def _load_symtab(self, elffile) -> None:
        symtab = elffile.get_section_by_name(".symtab")
        if symtab is None:
            return
        func_spans: list[FuncSpan] = []
        for sym in symtab.iter_symbols():
            name = sym.name
            value = sym["st_value"]
            if name in ("$a", "$t", "$d") or name[:2] in ("$a", "$t", "$d") and (
                len(name) == 2 or name[2] == "."
            ):
                kind = {"a": "arm", "t": "thumb", "d": "data"}[name[1]]
                self.mapping.append((value, kind))
            elif sym["st_info"]["type"] == "STT_FUNC" and sym["st_size"] > 0:
                func_spans.append(FuncSpan(value & ~1, (value & ~1) + sym["st_size"], name))
        # DWARF subprogram entries win; symtab fills the gaps (assembly).
        self._symtab_funcs = func_spans

    def _load_lines(self, dwarf) -> None:
        for cu in dwarf.iter_CUs():
            lp = dwarf.line_program_for_CU(cu)
            if lp is None:
                continue
            comp_dir = ""
            try:
                top = cu.get_top_DIE()
                raw = top.attributes.get("DW_AT_comp_dir")
                if raw is not None:
                    comp_dir = _to_str(raw.value)
            except Exception:
                pass
            files = _file_table(lp, comp_dir)
            for entry in lp.get_entries():
                state = entry.state
                if state is None:
                    continue
                if state.end_sequence:
                    self.line_rows.append((state.address, None, None))
                    continue
                path = files.get(state.file)
                if path is None:
                    continue
                self.line_rows.append((state.address, path, state.line))
                for key in (path, os.path.basename(path).lower()):
                    self.file_lines.setdefault(key, {}).setdefault(
                        state.line, []
                    ).append(state.address)

    def _load_functions(self, dwarf) -> None:
        seen: set[tuple[int, int]] = set()
        for cu in dwarf.iter_CUs():
            for die in cu.iter_DIEs():
                if die.tag != "DW_TAG_subprogram":
                    continue
                low_attr = die.attributes.get("DW_AT_low_pc")
                high_attr = die.attributes.get("DW_AT_high_pc")
                name_attr = die.attributes.get("DW_AT_name")
                if low_attr is None or high_attr is None:
                    continue
                low = low_attr.value & ~1
                high = high_attr.value
                if high_attr.form != "DW_FORM_addr":
                    high = low + high  # DWARF4+: size, not address
                name = _to_str(name_attr.value) if name_attr is not None else f"sub_{low:08X}"
                if (low, high) in seen:
                    continue
                seen.add((low, high))
                self.funcs.append(FuncSpan(low, high, name))
        covered = [(f.low, f.high) for f in self.funcs]
        for f in getattr(self, "_symtab_funcs", []):
            if not any(lo <= f.low < hi for lo, hi in covered):
                self.funcs.append(f)

    def _load_cfi(self, dwarf) -> None:
        entries = []
        if dwarf.has_CFI():
            entries = dwarf.CFI_entries()
            self.cfi_source = "debug_frame"
        elif dwarf.has_EH_CFI():
            entries = dwarf.EH_CFI_entries()
            self.cfi_source = "eh_frame"
        for i, entry in enumerate(entries):
            header = getattr(entry, "header", None)
            if header is None or "initial_location" not in header:
                continue  # a CIE
            low = header["initial_location"] & ~1
            self._fdes.append((low, low + header["address_range"], entry))
        self._fdes.sort(key=lambda t: t[0])

    # -- queries -------------------------------------------------------------

    def summary(self) -> dict[str, object]:
        return {
            "image": self.path,
            "size": self.size,
            "sha1": self.sha1,
            "mtime": self.mtime,
            "entry": f"0x{self.entry:08X}",
            "line_rows": len(self.line_rows),
            "functions": len(self.funcs),
            "mapping_symbols": len(self.mapping),
            "cfi": self.cfi_source,
            "fdes": len(self._fdes),
            "segments": [
                {"vaddr": f"0x{s.vaddr:08X}", "bytes": len(s.data)}
                for s in self.segments
            ],
        }

    def addr_to_line(self, addr: int) -> LineHit | None:
        addr &= ~1
        rows = self.line_rows
        if not rows:
            return None
        i = bisect.bisect_right(rows, addr, key=lambda r: r[0]) - 1
        if i < 0:
            return None
        row_addr, path, line = rows[i]
        if path is None or line is None:
            return None  # inside an end_sequence gap
        return LineHit(path, line)

    def line_to_addrs(self, file: str, line: int) -> tuple[int | None, list[int]]:
        """Nearest following line with code: (actual line, its addresses)."""
        table = self.file_lines.get(file)
        if table is None:
            table = self.file_lines.get(os.path.basename(file).lower())
        if not table:
            return None, []
        lines = sorted(table)
        i = bisect.bisect_left(lines, line)
        if i >= len(lines):
            return None, []
        actual = lines[i]
        # A line's statement can be entered at several addresses (loop
        # rotation, cold/hot splits); report the lowest first, deduplicated.
        return actual, sorted(set(table[actual]))

    def func_at(self, addr: int) -> FuncSpan | None:
        addr &= ~1
        funcs = self.funcs
        i = bisect.bisect_right(funcs, addr, key=lambda f: f.low) - 1
        if i < 0:
            return None
        f = funcs[i]
        return f if f.low <= addr < f.high else None

    def mode_at(self, addr: int) -> str | None:
        """"arm" | "thumb" | "data" from the mapping symbols, else None."""
        addr &= ~1
        i = bisect.bisect_right(self.mapping, (addr, "￿")) - 1
        if i < 0:
            return None
        return self.mapping[i][1]

    def bytes_at(self, addr: int, length: int) -> bytes | None:
        """File-backed code bytes at a virtual address, or None outside."""
        for seg in self.segments:
            if seg.vaddr <= addr and addr + length <= seg.vaddr + len(seg.data):
                off = addr - seg.vaddr
                return seg.data[off : off + length]
        return None

    def cfi_row_at(self, addr: int) -> dict | None:
        """The decoded CFI row governing addr: {'cfa': CFARule, regnum: rule}."""
        addr &= ~1
        fdes = self._fdes
        i = bisect.bisect_right(fdes, addr, key=lambda t: t[0]) - 1
        if i < 0:
            return None
        low, high, fde = fdes[i]
        if not (low <= addr < high):
            return None
        table = self._decoded_fde.get(low)
        if table is None:
            try:
                table = fde.get_decoded().table
            except Exception:
                table = []
            self._decoded_fde[low] = table
        row = None
        for candidate in table:
            if candidate["pc"] <= addr:
                row = candidate
            else:
                break
        return row


def _to_str(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _file_table(lp, comp_dir: str) -> dict[int, str]:
    """Resolve a line program's file indices to full path strings.

    DWARF <=4: both tables are 1-based and directory index 0 means the
    compilation directory. DWARF 5: both are 0-based and entry 0 *is* the
    primary source file / compilation directory.
    """
    header = lp.header
    version = header["version"]
    dirs = [_to_str(d) for d in header["include_directory"]]
    table: dict[int, str] = {}
    for i, fe in enumerate(header["file_entry"]):
        name = _to_str(fe.name)
        dir_index = fe["dir_index"]
        if version >= 5:
            directory = dirs[dir_index] if 0 <= dir_index < len(dirs) else ""
        else:
            directory = dirs[dir_index - 1] if 1 <= dir_index <= len(dirs) else ""
        if os.path.isabs(name):
            path = name
        else:
            base = directory or comp_dir
            if base and not os.path.isabs(base) and comp_dir:
                base = os.path.join(comp_dir, base)
            path = os.path.join(base, name) if base else name
        path = os.path.normpath(path)
        table[i if version >= 5 else i + 1] = path
    return table

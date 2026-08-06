"""Disassembly via capstone, with the mode chosen the reliable way.

Mode selection order for an address: the ELF's mapping symbols ($t/$a --
authoritative, they are what the linker knew), then the halted core's
DSPSR.T (what the core will actually execute next), then Thumb -- every
line of this application measured so far is T32, and the M33s architecturally
cannot be anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import require_capstone
from .dwarf import SymbolIndex


@dataclass
class Insn:
    addr: int
    size: int
    data: bytes
    mnemonic: str
    op_str: str

    def as_dict(self) -> dict[str, object]:
        return {
            "addr": f"0x{self.addr:08X}",
            "size": self.size,
            "bytes": self.data.hex(),
            "mnemonic": self.mnemonic,
            "op_str": self.op_str,
        }


class Disassembler:
    def __init__(self) -> None:
        require_capstone()
        import capstone as cs

        self._thumb = cs.Cs(cs.CS_ARCH_ARM, cs.CS_MODE_THUMB)
        self._arm = cs.Cs(cs.CS_ARCH_ARM, cs.CS_MODE_ARM)
        self._thumb_m = cs.Cs(cs.CS_ARCH_ARM, cs.CS_MODE_THUMB | cs.CS_MODE_MCLASS)
        for md in (self._thumb, self._arm, self._thumb_m):
            md.skipdata = True  # keep decoding across literal pools

    def _engine(self, mode: str) -> Any:
        return {"arm": self._arm, "thumb": self._thumb, "thumb-m": self._thumb_m}[mode]

    def disassemble(self, data: bytes, addr: int, mode: str) -> list[Insn]:
        out: list[Insn] = []
        for insn in self._engine(mode).disasm(data, addr):
            out.append(
                Insn(insn.address, insn.size, bytes(insn.bytes), insn.mnemonic, insn.op_str)
            )
        return out

    def first(self, data: bytes, addr: int, mode: str) -> Insn | None:
        for insn in self.disassemble(data, addr, mode):
            return insn
        return None


def pick_mode(
    index: SymbolIndex | None,
    addr: int,
    *,
    dspsr_t: bool | None = None,
    m_profile: bool = False,
) -> str:
    if m_profile:
        return "thumb-m"
    if index is not None:
        mapped = index.mode_at(addr)
        if mapped == "arm":
            return "arm"
        if mapped == "thumb":
            return "thumb"
    if dspsr_t is not None:
        return "thumb" if dspsr_t else "arm"
    return "thumb"


#: T32 BL/BLX immediate first halfword signature (Armv8-A ARM F3.1 32-bit
#: space) -- used for the M33 step-over call detector.
def thumb_is_call(insn: Insn) -> bool:
    return insn.mnemonic in ("bl", "blx")

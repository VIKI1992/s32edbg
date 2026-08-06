"""Best-effort stack unwinding for the IDE's call-stack view.

Three mechanisms, tried in this order per frame:

1. **DWARF CFI** (.debug_frame/.eh_frame rows from the symbol index): the
   compiler's own description -- CFA = base register + offset, the return
   address (r14/lr's rule) restored from the CFA-relative slot.
2. **Frame-pointer chain**: GCC AAPCS32 prologues. A32 `push {fp, lr}; add
   fp, sp, #4` leaves fp pointing at the saved-lr slot ([fp] = return
   address, [fp-4] = caller fp); T32 `push {r7, lr}; add r7, sp, #0` leaves
   r7 at the saved-r7 slot ([r7] = caller r7, [r7+4] = return address).
3. **LR**, once, for the innermost frame only: a leaf that never pushed.

Every candidate frame is sanity-checked (return address strips to something
inside the image or the RAM windows, stack pointers move down-up, no repeat
frames) and the walk stops with a stated reason instead of inventing frames.
The caller supplies the memory reader; this module never touches a DAP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .dwarf import SymbolIndex

#: Where code or stacks can legitimately live on this part (loader
#: rtuboot.LOADABLE_WINDOWS plus the SMU SRAM the M33 applet uses).
SANE_RANGES = (
    (0x2400_0000, 0x2600_0000),  # SMU SRAM (M33)
    (0x3178_0000, 0x3200_0000),  # RTU0 DataRAM + extension
    (0x3210_0000, 0x3280_0000),  # RTU0 CodeRAM window
    (0x0000_0000, 0x0100_0000),  # flash-mapped/boot images (M33 vectors)
)


def _sane(addr: int | None) -> bool:
    if addr is None:
        return False
    return any(lo <= addr < hi for lo, hi in SANE_RANGES)


@dataclass
class Frame:
    index: int
    pc: int
    sp: int | None
    fp: int | None
    via: str  # "regs" | "cfi" | "fp" | "lr"
    func: str | None = None
    file: str | None = None
    line: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "pc": f"0x{self.pc:08X}",
            "sp": f"0x{self.sp:08X}" if self.sp is not None else None,
            "fp": f"0x{self.fp:08X}" if self.fp is not None else None,
            "via": self.via,
            "func": self.func,
            "file": self.file,
            "line": self.line,
        }


def _annotate(frame: Frame, index: SymbolIndex | None) -> Frame:
    if index is not None:
        f = index.func_at(frame.pc)
        if f is not None:
            frame.func = f.name
        hit = index.addr_to_line(frame.pc)
        if hit is not None:
            frame.file, frame.line = hit.file, hit.line
    return frame


def _cfi_step(
    row: dict,
    pc: int,
    sp: int | None,
    fp: int | None,
    lr: int | None,
    read_word: Callable[[int], int | None],
) -> tuple[int, int, int | None] | None:
    """One CFI unwind: returns (return address, caller sp, caller fp)."""
    from elftools.dwarf.callframe import RegisterRule

    cfa_rule = row.get("cfa")
    if cfa_rule is None or getattr(cfa_rule, "reg", None) is None:
        return None
    base = {13: sp, 11: fp, 7: fp}.get(cfa_rule.reg)
    if base is None:
        return None
    cfa = (base + (cfa_rule.offset or 0)) & 0xFFFFFFFF

    def restore(regnum: int, current: int | None) -> int | None:
        rule = row.get(regnum)
        if rule is None:
            return current  # same-value by default
        if rule.type == RegisterRule.OFFSET:
            return read_word((cfa + rule.arg) & 0xFFFFFFFF)
        if rule.type == RegisterRule.SAME_VALUE:
            return current
        if rule.type == RegisterRule.REGISTER:
            return {13: sp, 11: fp, 14: lr, 15: pc}.get(rule.arg)
        return None

    ra = restore(14, lr)
    if ra is None:
        return None
    new_fp = restore(11, fp)
    return ra & 0xFFFFFFFF, cfa, new_fp


def _fp_step(
    fp: int | None, read_word: Callable[[int], int | None], *, thumb_r7: bool
) -> tuple[int, int, int] | None:
    """One frame-pointer step: (return address, caller sp~, caller fp)."""
    if fp is None or fp % 4 or not _sane(fp):
        return None
    if thumb_r7:
        caller_fp = read_word(fp)
        ra = read_word(fp + 4)
        caller_sp = fp + 8
    else:
        ra = read_word(fp)
        caller_fp = read_word(fp - 4)
        caller_sp = fp + 4
    if ra is None or caller_fp is None:
        return None
    return ra & 0xFFFFFFFF, caller_sp, caller_fp


def unwind(
    *,
    pc: int,
    sp: int | None,
    fp: int | None,
    lr: int | None,
    read_word: Callable[[int], int | None],
    index: SymbolIndex | None,
    max_frames: int = 32,
    thumb_r7: bool = False,
) -> tuple[list[Frame], str]:
    """Walk the stack. Returns (frames, stop reason)."""
    frames: list[Frame] = [_annotate(Frame(0, pc & ~1, sp, fp, "regs"), index)]
    seen = {(pc & ~1, sp)}
    reason = "max-frames"
    cur_pc, cur_sp, cur_fp, cur_lr = pc & ~1, sp, fp, lr

    while len(frames) < max_frames:
        step: tuple[int, int | None, int | None] | None = None
        via = ""
        if index is not None:
            row = index.cfi_row_at(cur_pc)
            if row is not None:
                got = _cfi_step(row, cur_pc, cur_sp, cur_fp, cur_lr, read_word)
                if got is not None:
                    step, via = got, "cfi"
        if step is None:
            got = _fp_step(cur_fp, read_word, thumb_r7=thumb_r7)
            if got is not None:
                step, via = got, "fp"
        if step is None and len(frames) == 1 and cur_lr not in (None, 0):
            # A leaf frame that never pushed: its caller is simply LR.
            step, via = (cur_lr, cur_sp, cur_fp), "lr"
        if step is None:
            reason = "no-unwind-info"
            break

        ra, new_sp, new_fp = step
        ra &= ~1
        if not _sane(ra):
            reason = "end-of-stack"
            break
        key = (ra, new_sp)
        if key in seen:
            reason = "loop"
            break
        seen.add(key)
        frames.append(
            _annotate(Frame(len(frames), ra, new_sp, new_fp, via), index)
        )
        cur_pc, cur_sp, cur_fp, cur_lr = ra, new_sp, new_fp, None

    return frames, reason

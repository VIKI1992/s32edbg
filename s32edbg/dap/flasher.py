"""The `flash` (external QSPI NOR) flow: controller bring-up, M33 applet ops.

A port of s32multitool's supported raw-QSPI route (its ``RawQspiMx25uw25a45g``
driver plus the NXP flash-profile init script -- see ``data/qspiflash.py`` for
the verbatim tables and citations) onto this tool's transport stack. The
mechanism is unchanged:

* the host initialises the QuadSPI_0 controller by direct register writes and
  owns the four physical LUT slots (slot 0 = active command, 1 = READ/AHB,
  2 = WREN, 3 = RDSR);
* every program/erase/buffered-read runs inside the vendored 2 KiB Cortex-M33
  RAM applet at 0x25000000, called with (op, offset, buffer, size) in r0-r3
  and ending in ``bkpt #0`` with rc in r0;
* chip erase alone is host-driven (WREN, slot-0 CHIP_ERASE, RDSR poll) --
  the applet has no op for it;
* verify is a byte-exact read-back compare through the memory-mapped AHB
  window; there is no CRC anywhere.

What IS different is the transport discipline, because the reference rides a
gdb/gta stack that reports faults and this tool rides hand-bound DLL calls
that do not: every register access goes through the checked ``memaccess``
layer, bulk staging reuses the loader's ``SegmentWriter`` (WriteMem fast path
with first-block read-back proof and tiered fallback), and bulk read-back gets
the mirror-image ``BulkReader`` over the new ReadMem binding. One divergence
from the reference's *ordering* is deliberate: the raw touch-up table runs
before the first JEDEC read rather than after it, because there is no S32DS
``flash()`` prepare underneath us and a cold controller needs MTO disarmed
before its first IP command can be trusted.

Ownership note: the caller (the CLI's flash bring-up) delivers this module a
quiesced SoC -- reset done, LADDER-1 applied, every reachable M33 pinned by
DHCSR, the M33 EDBGREQ latches cleared (gates.clear_m33_edbgreq; a latch left
set re-halts the SMU the moment it is resumed into the applet), and the SMU
core object preflighted. This module never resets anything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ..data import qspiflash as qf
from ..errors import (
    DapError,
    FlashError,
    S32EError,
    TransportError,
    UnsupportedOperation,
)
from . import m33 as m33_mod
from . import memaccess
from .adiv6 import Dap
from .loader import WRITE_SLICE_WORDS, SegmentWriter, run_init_actions

#: Applet-call deadlines by operation, host-side. The applet itself guards on
#: cycle counts (.c:76-78); these only bound how long the host polls DHCSR.
#: Sized from the datasheet worst cases over one call's maximum span: a
#: 0x100000 erase span is 256 4K sectors (max ~200 ms each) or 16 64K blocks
#: (max ~1 s each); a 0x30000 program call is 768 pages.
APPLET_TIMEOUT_S = {
    qf.OP_PROGRAM_BUFFER: 60.0,
    qf.OP_READ_BUFFER: 30.0,
    qf.OP_ERASE_4K_RANGE: 120.0,
    qf.OP_ERASE_64K_RANGE: 120.0,
}

#: Registers saved around every applet call and restored in ``finally``, in
#: restore order. ``control`` is DCRSR selector 20 -- the packed
#: CONTROL/FAULTMASK/BASEPRI/PRIMASK word, so zeroing it before the call
#: covers all four specials the reference zeroes individually through GDB.
APPLET_SAVED_REGS = (
    "r0", "r1", "r2", "r3", "lr", "sp", "msp", "psp", "xpsr", "pc", "control",
)

#: Stay under the DLL's measured ~4 MB per-connection WriteMem budget
#: (test-plan Finding 9: past it every call returns -1 in microseconds and
#: only a fresh connection revives it). Whether ReadMem draws on the same
#: budget is unmeasured (bench item u-f3), so both directions count against
#: this conservatively; crossing it demotes the fast paths mid-flow instead
#: of letting them wedge mid-chunk.
FAST_BUDGET_BYTES = 3 * 1024 * 1024

#: First fast-read block is fully cross-checked against ADIv6; keep it small
#: (the proof costs the pure path's ~4 KB/s once).
READ_PROOF_BYTES = 0x1000
#: Sampled cross-check stride for later fast-read blocks, in words -- the
#: read twin of the writer's FAST_SAMPLE_STRIDE.
READ_SAMPLE_STRIDE = 256


@dataclass
class FlashReport:
    """One flash operation's outcome, for the console table and --json."""

    op: str
    offset: int = 0
    length: int = 0
    mode: str | None = None
    jedec: int | None = None
    ap_mem: int = 0
    ahb_base: int = 0
    erase_spans_4k: int = 0
    erase_spans_64k: int = 0
    erase_bytes: int = 0
    erase_seconds: float = 0.0
    program_runs: int = 0
    skipped_pages: int = 0
    programmed_bytes: int = 0
    program_seconds: float = 0.0
    verify: str = "none"
    verify_seconds: float = 0.0
    fast_write: str = "unused"  # "used" | "demoted" | "unused"
    fast_read: str = "unused"
    applet_calls: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "op": self.op,
            "offset": f"0x{self.offset:08X}",
            "length": self.length,
            "mode": self.mode,
            "jedec": f"0x{self.jedec:08X}" if self.jedec is not None else None,
            "ap_mem": f"0x{self.ap_mem:08X}",
            "ahb_base": f"0x{self.ahb_base:08X}",
            "erase": {
                "spans_4k": self.erase_spans_4k,
                "spans_64k": self.erase_spans_64k,
                "bytes": self.erase_bytes,
                "seconds": round(self.erase_seconds, 3),
            },
            "program": {
                "runs": self.program_runs,
                "skipped_pages": self.skipped_pages,
                "bytes": self.programmed_bytes,
                "seconds": round(self.program_seconds, 3),
            },
            "verify": self.verify,
            "verify_seconds": round(self.verify_seconds, 3),
            "fast_write": self.fast_write,
            "fast_read": self.fast_read,
            "applet_calls": self.applet_calls,
            "seconds": round(self.seconds, 3),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Pure planning helpers -- verbatim ports, unit-testable offline
# ---------------------------------------------------------------------------


def align_down(value: int, alignment: int) -> int:
    return value - (value % alignment)


def align_up(value: int, alignment: int) -> int:
    remainder = value % alignment
    return value if remainder == 0 else value + (alignment - remainder)


def build_erase_plan(
    start_offset: int, total_length: int, *, chunk: int = qf.ERASE_CHUNK
) -> list[tuple[int, int, int, str, int]]:
    """(offset, length, unit, lut_name, applet_op) spans covering the range.

    Verbatim port of the reference planner (raw driver :748-788): 64K-aligned
    middle stretches use the 64K block erase, ragged head/tail use 4K
    sectors, and every span is capped at the applet erase chunk (aligned down
    to its own unit, as erase_applet_chunk_size does for the 64K case).
    """
    if start_offset % qf.SECTOR_SIZE:
        raise FlashError("erase plan offset must be 4 KiB aligned")
    if total_length % qf.SECTOR_SIZE:
        raise FlashError("erase plan length must be a multiple of 4 KiB")
    cursor = start_offset
    end_offset = start_offset + total_length
    block_chunk = max((chunk // qf.BLOCK_ERASE_SIZE) * qf.BLOCK_ERASE_SIZE, qf.BLOCK_ERASE_SIZE)
    plan: list[tuple[int, int, int, str, int]] = []
    while cursor < end_offset:
        remaining = end_offset - cursor
        if cursor % qf.BLOCK_ERASE_SIZE == 0 and remaining >= qf.BLOCK_ERASE_SIZE:
            span = (remaining // qf.BLOCK_ERASE_SIZE) * qf.BLOCK_ERASE_SIZE
            span = min(span, block_chunk)
            plan.append(
                (cursor, span, qf.BLOCK_ERASE_SIZE, qf.LUT_BLOCK_ERASE_64K, qf.OP_ERASE_64K_RANGE)
            )
            cursor += span
            continue
        next_block = align_up(cursor, qf.BLOCK_ERASE_SIZE)
        if next_block == cursor:
            next_block = end_offset
        span = min(end_offset, next_block) - cursor
        span = min(span, chunk)
        plan.append((cursor, span, qf.SECTOR_SIZE, qf.LUT_SECTOR_ERASE, qf.OP_ERASE_4K_RANGE))
        cursor += span
    return plan


def build_non_blank_program_runs(
    data: bytes, program_length: int, max_run_length: int
) -> tuple[list[tuple[int, int]], int, int]:
    """(runs, blank_pages, non_blank_pages) -- the smart blank-page skip.

    Verbatim port (raw driver :277-314): 256-byte pages that are entirely
    0xFF are omitted; contiguous non-blank pages coalesce into runs capped at
    ``max_run_length``. Correct only because erase-before-write guarantees a
    skipped page already reads 0xFF.
    """
    if max_run_length % qf.PAGE_SIZE:
        raise FlashError("smart program run size must be 256-byte aligned")
    blank_page = b"\xff" * qf.PAGE_SIZE
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    run_length = 0
    blank_pages = 0
    non_blank_pages = 0
    page_offset = 0
    while page_offset < program_length:
        page = data[page_offset : page_offset + qf.PAGE_SIZE]
        if len(page) < qf.PAGE_SIZE:
            page = page + b"\xff" * (qf.PAGE_SIZE - len(page))
        if page == blank_page:
            blank_pages += 1
            if run_start is not None:
                runs.append((run_start, run_length))
                run_start = None
                run_length = 0
        else:
            non_blank_pages += 1
            if run_start is None:
                run_start = page_offset
                run_length = qf.PAGE_SIZE
            elif run_length + qf.PAGE_SIZE > max_run_length:
                runs.append((run_start, run_length))
                run_start = page_offset
                run_length = qf.PAGE_SIZE
            else:
                run_length += qf.PAGE_SIZE
        page_offset += qf.PAGE_SIZE
    if run_start is not None:
        runs.append((run_start, run_length))
    return runs, blank_pages, non_blank_pages


def _words_of(data: bytes) -> list[int]:
    if len(data) % 4:
        data = data + b"\xff" * (4 - len(data) % 4)
    return [int.from_bytes(data[i : i + 4], "little") for i in range(0, len(data), 4)]


# ---------------------------------------------------------------------------
# BulkReader: ReadMem fast path with cross-check proof and demotion
# ---------------------------------------------------------------------------


class BulkReader:
    """Reads byte blocks, preferring the DLL's ReadMem while it earns trust.

    The mirror image of the loader's SegmentWriter, with the trust flowing
    the other way: a fast read's *content* is the thing in question, so the
    first block is fully compared against an independent ADIv6 read and later
    blocks are spot-compared (first, last, every {stride}th word). A content
    mismatch or missing export demotes permanently; an ambiguous rc falls
    back for that block only (the writer's measured bus-contention policy).
    """

    def __init__(
        self,
        dap: Dap,
        transport: object,
        *,
        ap: int,
        use_fast: bool,
        note: Callable[[str], None],
    ) -> None:
        self.dap = dap
        self.transport = transport
        self.ap = ap
        self.note = note
        self.fast_alive = bool(use_fast and getattr(transport, "can_block_read", False))
        self.fast_ever_used = False
        self.fast_demoted = False
        self.fast_bytes = 0
        self._first_fast_block = True

    def read_bytes(self, addr: int, nbytes: int) -> bytes:
        if addr % 4:
            raise FlashError(f"bulk reads are word-aligned; got 0x{addr:08X}")
        out = bytearray()
        cursor = 0
        while cursor < nbytes:
            slice_bytes = qf.AHB_READ_CHUNK
            if self.fast_alive and self._first_fast_block:
                slice_bytes = READ_PROOF_BYTES  # keep the full-proof block cheap
            chunk = min(slice_bytes, nbytes - cursor)
            out += self._read_block(addr + cursor, chunk)
            cursor += chunk
        return bytes(out)

    def _read_block(self, addr: int, nbytes: int) -> bytes:
        if self.fast_alive:
            try:
                data = self.transport.read_mem_block(addr, nbytes)  # type: ignore[attr-defined]
                # The DLL moved SELECT/CSW/TAR beneath the Dap object.
                self.dap.invalidate_select_cache()
                self.dap.invalidate_csw_cache(self.ap)
                self._cross_check(addr, data)
                self.fast_ever_used = True
                self.fast_bytes += nbytes
                return data
            except UnsupportedOperation as exc:
                self._demote(addr, exc)
            except TransportError as exc:
                self.dap.invalidate_select_cache()
                self.dap.invalidate_csw_cache(self.ap)
                self.note(
                    f"ReadMem failed at 0x{addr:08X} ({exc}) -- reading this "
                    "block via ADIv6; the fast path stays on"
                )
            except S32EError as exc:  # content mismatch, DapError
                self._demote(addr, exc)
        return self._read_adiv6(addr, nbytes)

    def _demote(self, addr: int, exc: Exception) -> None:
        self.fast_alive = False
        self.fast_demoted = True
        self.dap.invalidate_select_cache()
        self.dap.invalidate_csw_cache(self.ap)
        self.note(
            f"fast read path demoted at 0x{addr:08X} "
            f"({type(exc).__name__}: {exc}) -- staying on ADIv6 reads"
        )

    def _cross_check(self, addr: int, data: bytes) -> None:
        nwords = len(data) // 4
        if nwords == 0:
            return
        words = [int.from_bytes(data[i * 4 : i * 4 + 4], "little") for i in range(nwords)]
        if self._first_fast_block:
            got = memaccess.read_words(self.dap, self.ap, addr, nwords)
            if got != words:
                bad = next(i for i, (a, b) in enumerate(zip(got, words)) if a != b)
                raise FlashError(
                    f"ReadMem proof block disagreed with ADIv6 at "
                    f"0x{addr + bad * 4:08X}: DLL 0x{words[bad]:08X}, "
                    f"ADIv6 0x{got[bad]:08X}"
                )
            self._first_fast_block = False
            self.note(
                f"ReadMem fast path proven against a full independent ADIv6 "
                f"read of the first block ({nwords} words)"
            )
            return
        idxs = sorted({0, nwords - 1} | set(range(0, nwords, READ_SAMPLE_STRIDE)))
        for i in idxs:
            got = memaccess.read_words(self.dap, self.ap, addr + i * 4, 1)[0]
            if got != words[i]:
                raise FlashError(
                    f"ReadMem sampled cross-check disagreed with ADIv6 at "
                    f"0x{addr + i * 4:08X}: DLL 0x{words[i]:08X}, ADIv6 0x{got:08X}"
                )

    def _read_adiv6(self, addr: int, nbytes: int) -> bytes:
        nwords = (nbytes + 3) // 4
        words = memaccess.read_words(self.dap, self.ap, addr, nwords)
        raw = b"".join(w.to_bytes(4, "little") for w in words)
        return raw[:nbytes]


# ---------------------------------------------------------------------------
# The flasher
# ---------------------------------------------------------------------------


class QspiFlasher:
    """Drives the QuadSPI controller and the M33 applet over one memory AP.

    ``ap_mem`` is the AP whose view carries all three address classes the
    reference uses -- the controller registers at 0x42320000, the applet RAM
    at 0x2500xxxx, and the memory-mapped flash window at ``ahb_base`` -- the
    SMU M33's own AHB-AP by default, since every one of those addresses is an
    M33-view address (bench item u-f1 records the per-class reachability
    question; `preflight` probes before anything destructive can run).
    """

    def __init__(
        self,
        dap: Dap,
        transport: object,
        smu: m33_mod.M33Core,
        *,
        note: Callable[[str], None],
        ap_mem: int | None = None,
        ahb_base: int = qf.AHB_WINDOW_BASES[0],
        use_fast: bool = True,
    ) -> None:
        self.dap = dap
        self.transport = transport
        self.smu = smu
        self.note = note
        self.ap_mem = m33_mod.M33_APS["smu"] if ap_mem is None else ap_mem
        self.ahb_base = ahb_base
        self.use_fast = use_fast
        self.mode: str | None = None
        self.jedec: int | None = None
        self.applet_loaded = False
        self.applet_calls = 0
        self._luts_ready = False
        self._writer_fast_bytes = 0
        self.writer = SegmentWriter(
            dap,
            transport,
            use_fast=use_fast,
            note=note,
            ap=self.ap_mem,
            ap_type="AHB-AP" if self.ap_mem != 0x01C2_0000 else "AXI-AP",
        )
        self.reader = BulkReader(dap, transport, ap=self.ap_mem, use_fast=use_fast, note=note)

    # ------------------------------------------------------------ plumbing --

    def _read32(self, offset: int) -> int:
        return memaccess.read_words(self.dap, self.ap_mem, qf.QSPI_BASE + offset, 1)[0]

    def _write32(self, offset: int, value: int) -> None:
        memaccess.write_word(self.dap, self.ap_mem, qf.QSPI_BASE + offset, value)

    def _budget_spent(self) -> int:
        return self.reader.fast_bytes + getattr(self, "_writer_fast_bytes", 0)

    def _budget_charge(self, nbytes: int) -> None:
        """Demote both fast paths before the DLL's WriteMem budget wedges them.

        Finding 9: past ~4 MB per connection every WriteMem fast-fails with
        no wire traffic and nothing but a fresh connection revives it. The
        CLI opens this flow on a fresh connection; this counter keeps the
        flow from spending its way into the wedge mid-chunk.
        """
        if self.writer.fast_alive:
            self._writer_fast_bytes += nbytes
        if (
            self._budget_spent() > FAST_BUDGET_BYTES
            and (self.writer.fast_alive or self.reader.fast_alive)
        ):
            self.writer.fast_alive = False
            self.reader.fast_alive = False
            self.note(
                f"fast paths retired after {self._budget_spent():,} bytes -- "
                "staying under the DLL's ~4 MB per-connection WriteMem budget "
                "(Finding 9); continuing on pure ADIv6"
            )

    def _stage(self, addr: int, data: bytes) -> None:
        """Write bytes into M33 RAM through the writer's slice/trust machinery."""
        words = _words_of(data)
        self._budget_charge(len(words) * 4)
        for off in range(0, len(words), WRITE_SLICE_WORDS):
            block = words[off : off + WRITE_SLICE_WORDS]
            self.writer.write_block(addr + off * 4, block)
        self._repin_after_fast()

    def _repin_after_fast(self) -> None:
        """Re-assert the SMU halt after any DLL fast-path burst.

        Finding 6, flash edition (measured 2026-08-05): the very first
        WriteMem staging burst through the registered AHB-AP left the SMU
        running again -- the DLL's stop-mode wrapper resumes behind our back
        despite the arm-time model sync -- and the next DCRSR transfer died
        with S_REGRDY clear. A running SMU mid-flow is worse than a broken
        register read: it executes the boot chain while this flow owns the
        QSPI controller. One C_HALT re-assert (never `halt()` -- its first
        write would *resume* an already-halted core) after every burst, and
        a loud note when it actually caught the core running.
        """
        if not (self.writer.fast_ever_used or self.reader.fast_ever_used):
            return
        _dhcsr, was_running = m33_mod.ensure_halted(self.dap, self.smu)
        if was_running:
            self.note(
                "the DLL resumed the SMU behind our back (Finding 6); "
                "re-pinned it by DHCSR before continuing"
            )

    # ------------------------------------------------------------ bring-up --

    def preflight(self) -> list[str]:
        """Prove the memory AP serves this flow before anything destructive.

        Three probes, one per address class: the controller must answer a
        register read (MCR), the applet RAM must hold a written word, and the
        policy layer must accept the AP at all. The AHB window is deliberately
        not probed here -- it only means anything after the READ LUT is
        installed, and its base is settled lazily by `_window_read`.
        """
        _report, notes = memaccess.preflight(self.dap, self.ap_mem, write=True)
        try:
            mcr = self._read32(qf.QSPI_MCR)
        except DapError as exc:
            raise FlashError(
                f"the QuadSPI controller at 0x{qf.QSPI_BASE:08X} did not answer "
                f"through AP 0x{self.ap_mem:08X} ({exc}). Bench item u-f1: try "
                "the other candidate AP (--ap axi / --ap smu)."
            ) from exc
        probe_addr = qf.APPLET_STATUS  # mailbox space: applet-owned scratch
        probe_value = 0xA5C3_5A3C
        memaccess.write_word(self.dap, self.ap_mem, probe_addr, probe_value)
        got = memaccess.read_words(self.dap, self.ap_mem, probe_addr, 1)[0]
        if got != probe_value:
            raise FlashError(
                f"applet RAM at 0x{probe_addr:08X} did not hold a written word "
                f"through AP 0x{self.ap_mem:08X} (wrote 0x{probe_value:08X}, "
                f"read 0x{got:08X}) -- wrong AP or unpowered CE RAM"
            )
        self.note(
            f"flash preflight ok on AP 0x{self.ap_mem:08X}: MCR reads "
            f"0x{mcr:08X}, applet RAM holds data"
        )
        return notes

    def controller_init(self) -> None:
        """The vendored NXP bring-up -- run only when the controller is dead.

        BootROM boots from this very flash, so on any board that came through
        the quiesce reset with a programmed bootloader the PLL, pads and
        controller are already configured before this flow starts (measured
        2026-08-05: MCR reads 0x010F004C post-reset and RDID answers). The
        probe-first order is not just thrift: the PLL block at 0x40220000
        faults for the debugger on both candidate APs (XRDC denial, reads and
        writes alike -- Finding 11), so on this silicon the cold tables
        *cannot* run and a BootROM-dead controller currently has no
        debugger-side recovery. The touch-up must precede the probe: a cold
        controller needs MTO disarmed before its first IP command can be
        trusted, and SFACR=0 before any SFAR-addressed command.
        """
        run_init_actions(self.dap, qf.ACTIONS_RAW_TOUCHUP, ap_base=self.ap_mem, note=self.note)
        try:
            mode, jedec = self.detect_mode()
        except (FlashError, DapError):
            mode, jedec = None, None
        if mode is not None:
            self.note(
                f"QSPI controller already alive (BootROM left it configured: "
                f"JEDEC 0x{jedec:08X} answered in {mode.upper()}) -- skipping "
                "the PLL/pad/controller init tables"
            )
            return
        self.note(
            "QSPI controller not answering -- running the full NXP bring-up "
            "(on this silicon the PLL writes fault for the debugger under "
            "XRDC, Finding 11; expect this path to need a bench answer)"
        )
        self.note("QSPI bring-up: PLL + MC_CGM muxes")
        run_init_actions(self.dap, qf.ACTIONS_PLL, ap_base=self.ap_mem, note=self.note)
        run_init_actions(self.dap, qf.ACTIONS_M33_CLOCK, ap_base=self.ap_mem, note=self.note)
        self.note("QSPI bring-up: SIUL2 pads + QuadSPI controller")
        run_init_actions(self.dap, qf.ACTIONS_QSPI_PADS, ap_base=self.ap_mem, note=self.note)
        run_init_actions(self.dap, qf.ACTIONS_QSPI_CONTROLLER, ap_base=self.ap_mem, note=self.note)

    def prepare(self) -> tuple[str, int]:
        """Raw-ops touch-up, mode detect, READ LUT install. Returns (mode, jedec).

        Reference order note: its install_luts detects the mode first because
        an S32DS prepare has already tamed the controller underneath it; here
        the touch-up (MTO off, SFACR=0) runs first so the very first IP
        command on a cold controller is already trustworthy.
        """
        run_init_actions(self.dap, qf.ACTIONS_RAW_TOUCHUP, ap_base=self.ap_mem, note=self.note)
        mode, jedec = self.detect_mode()
        self._load_virtual_lut(qf.PHYS_COMMAND_LUT, ())  # clear slot 0
        self._load_virtual_lut(qf.PHYS_AHB_LUT, qf.luts_for_mode(mode)[qf.LUT_READ])
        self._luts_ready = True
        self.note(f"LUT slot 1 armed with the {mode.upper()} READ sequence (AHB window live)")
        return mode, jedec

    def detect_mode(self) -> tuple[str, int]:
        """SPI first, OPI on an invalid answer -- the reference's per-operation
        detection (:512-532), self-contained via the slot-0 RDID LUT."""
        if self.mode is not None and self.jedec is not None:
            return self.mode, self.jedec
        spi = self._read_jedec_for_mode("spi")
        self.note(f"SPI JEDEC probe read 0x{spi:08X}")
        if not qf.is_invalid_jedec(spi):
            self.mode, self.jedec = "spi", spi
            return self.mode, self.jedec
        opi = self._read_jedec_for_mode("opi")
        self.note(f"OPI JEDEC probe read 0x{opi:08X}")
        if qf.is_invalid_jedec(opi):
            raise FlashError(
                f"no valid JEDEC ID in either mode (SPI 0x{spi:08X}, OPI "
                f"0x{opi:08X}) -- flash unpowered, pads unrouted, or the "
                "controller bring-up did not run"
            )
        self.mode, self.jedec = "opi", opi
        return self.mode, self.jedec

    def check_jedec(self, *, force: bool = False) -> None:
        mode, jedec = self.detect_mode()
        if jedec in qf.EXPECTED_JEDEC_IDS:
            self.note(f"JEDEC 0x{jedec:08X} ({mode}) matches the expected MX25UW25A45G")
            return
        message = (
            f"JEDEC 0x{jedec:08X} is not the expected MX25UW25A45G "
            f"({', '.join(f'0x{x:08X}' for x in qf.EXPECTED_JEDEC_IDS)})"
        )
        if force:
            self.note(f"{message} -- proceeding anyway (--force-jedec)")
            return
        raise FlashError(message + ". Refusing destructive work against an unknown part.")

    # ------------------------------------------------------- IP primitives --

    def _clear_buffers(self) -> None:
        self._write32(qf.QSPI_MCR, self._read32(qf.QSPI_MCR) | qf.QSPI_MCR_CLR_TXRX)
        deadline = time.monotonic() + qf.BUFFER_CLEAR_TIMEOUT_S
        while time.monotonic() < deadline:
            if (self._read32(qf.QSPI_MCR) & qf.QSPI_MCR_CLR_TXRX) == 0:
                return
            time.sleep(0.001)
        raise FlashError(
            f"QSPI buffer clear timed out, MCR=0x{self._read32(qf.QSPI_MCR):08X}"
        )

    def _clear_flags(self) -> None:
        for offset, value, _name in qf.CLEAR_FLAGS_WRITES:
            self._write32(offset, value)

    def _wait_ip_done(self, label: str, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self._read32(qf.QSPI_SR) & qf.QSPI_SR_IP_BUSY:
                return
            time.sleep(0.005)
        raise FlashError(
            f"QSPI IP command timed out during {label}: "
            f"SR=0x{self._read32(qf.QSPI_SR):08X} FR=0x{self._read32(qf.QSPI_FR):08X}"
        )

    def _ip_command(self, seqid: int, base: int, count: int, label: str) -> None:
        self._clear_buffers()
        self._clear_flags()
        self._write32(qf.QSPI_SFAR, base)
        self._write32(qf.QSPI_IPCR, (seqid << 24) | count)
        self._wait_ip_done(label, qf.IP_TIMEOUT_S)

    def _write_lut_entry(self, seqid: int, registers: list[int]) -> None:
        base = qf.QSPI_LUT_BASE + seqid * qf.QSPI_LUT_STRIDE
        for i in range(qf.LUT_SEQUENCE_REGS):
            self._write32(base + i * 4, registers[i] if i < len(registers) else 0)

    def _unlock_luts(self) -> None:
        self._write32(qf.QSPI_LUTKEY, qf.QSPI_LUTKEY_VALUE)
        self._write32(qf.QSPI_LCKCR, qf.QSPI_LCKCR_UNLOCK)

    def _lock_luts(self) -> None:
        self._write32(qf.QSPI_LUTKEY, qf.QSPI_LUTKEY_VALUE)
        self._write32(qf.QSPI_LCKCR, qf.QSPI_LCKCR_LOCK)

    def _load_virtual_lut(self, physical_seqid: int, operations: tuple[int, ...]) -> None:
        """Pack a flat halfword list into the 5 LUT registers of one slot.

        Verbatim port of load_virtual_lut (:951-968), including its stopping
        rule: a pair whose second halfword is 0 ends the sequence.
        """
        registers: list[int] = []
        index = 0
        while len(registers) < qf.LUT_SEQUENCE_REGS:
            op0 = operations[index] if index < len(operations) else 0
            index += 1
            op1 = 0
            if op0 != 0 and index < len(operations):
                op1 = operations[index]
                index += 1
            registers.append(qf.lut_pair(op0, op1))
            if op1 == 0:
                break
        while len(registers) < qf.LUT_SEQUENCE_REGS:
            registers.append(0)
        self._unlock_luts()
        self._write_lut_entry(physical_seqid, registers)
        self._lock_luts()

    def _load_command_lut(self, name: str) -> None:
        assert self.mode is not None
        self._load_virtual_lut(qf.PHYS_COMMAND_LUT, qf.luts_for_mode(self.mode)[name])

    def _load_applet_luts(self, slot0_name: str) -> None:
        """The applet's fixed-slot contract: 0 = active command, 2 = WREN,
        3 = RDSR (raw driver :918-930 -- erase rides slot 0 too)."""
        assert self.mode is not None
        seqs = qf.luts_for_mode(self.mode)
        self._load_virtual_lut(qf.PHYS_COMMAND_LUT, seqs[slot0_name])
        self._load_virtual_lut(qf.PHYS_WREN_LUT, seqs[qf.LUT_WREN])
        self._load_virtual_lut(qf.PHYS_RDSR_LUT, seqs[qf.LUT_RDSR])

    def _read_jedec_for_mode(self, mode: str) -> int:
        seqs = qf.luts_for_mode(mode)
        self._clear_buffers()
        self._clear_flags()
        self._load_virtual_lut(qf.PHYS_COMMAND_LUT, seqs[qf.LUT_RDID])
        self._write32(qf.QSPI_SFAR, qf.IP_FLASH_BASE)
        ip_count = 4 if mode == "opi" else 0
        self._write32(qf.QSPI_IPCR, (qf.PHYS_COMMAND_LUT << 24) | ip_count)
        self._wait_ip_done(f"rdid_{mode}", qf.IP_TIMEOUT_S)
        time.sleep(0.005)
        jedec = self._read32(qf.QSPI_RBDR)
        self._clear_buffers()
        return jedec

    def read_status(self) -> int:
        self._clear_buffers()
        self._clear_flags()
        self._load_command_lut(qf.LUT_RDSR)
        self._write32(qf.QSPI_SFAR, 0)
        self._write32(qf.QSPI_IPCR, (qf.PHYS_COMMAND_LUT << 24) | 1)
        self._wait_ip_done("rdsr", qf.IP_TIMEOUT_S)
        time.sleep(0.005)
        status = self._read32(qf.QSPI_RBDR) & 0xFF
        self._clear_buffers()
        return status

    def _write_enable(self) -> int:
        status = 0
        deadline = time.monotonic() + qf.WREN_TIMEOUT_S
        while (status & qf.FLASH_WEL) != qf.FLASH_WEL:
            if time.monotonic() > deadline:
                raise FlashError(f"QSPI write-enable timed out, status=0x{status:02X}")
            self._clear_buffers()
            self._clear_flags()
            self._load_command_lut(qf.LUT_WREN)
            self._write32(qf.QSPI_SFAR, 0)
            self._write32(qf.QSPI_IPCR, qf.PHYS_COMMAND_LUT << 24)
            self._wait_ip_done("wren", qf.IP_TIMEOUT_S)
            status = self.read_status()
        return status

    def _wait_ready(self, label: str, timeout_s: float) -> int:
        deadline = time.monotonic() + timeout_s
        status = 0xFF
        while time.monotonic() < deadline:
            status = self.read_status()
            if not status & qf.FLASH_WIP:
                return status
            time.sleep(0.05)
        raise FlashError(
            f"flash stayed busy (WIP) for {timeout_s:.0f}s during {label}, "
            f"status=0x{status:02X}"
        )

    # ----------------------------------------------------------- the applet --

    def _load_applet(self) -> None:
        if self.applet_loaded:
            return
        blob = qf.applet_bytes()  # size+SHA re-verified on every load
        self._stage(qf.APPLET_BASE, blob)
        self.applet_loaded = True
        self.note(f"applet staged at 0x{qf.APPLET_BASE:08X} ({len(blob)} bytes, SHA-pinned)")

    def run_applet(
        self, op: int, offset: int, size: int, payload: bytes | None = None
    ) -> None:
        """One applet call: stage, aim the core, run to bkpt, judge rc.

        The register file is saved first and restored in ``finally`` whatever
        happens, so a failed flash op leaves the SMU exactly as the session
        found it (the reference does the same through GDB).
        """
        self._load_applet()
        if payload is not None:
            if len(payload) > qf.APPLET_DATA_SIZE:
                raise FlashError(
                    f"applet payload {len(payload)} bytes exceeds the "
                    f"0x{qf.APPLET_DATA_SIZE:X} buffer"
                )
            self._stage(qf.APPLET_DATA, payload)
        saved = {
            name: m33_mod.read_register(self.dap, self.smu, name)
            for name in APPLET_SAVED_REGS
        }
        try:
            m33_mod.write_register(self.dap, self.smu, "control", 0)
            for name in ("sp", "msp", "psp"):
                m33_mod.write_register(self.dap, self.smu, name, qf.APPLET_STACK)
            m33_mod.write_register(self.dap, self.smu, "r0", op)
            m33_mod.write_register(self.dap, self.smu, "r1", qf.IP_FLASH_BASE + offset)
            m33_mod.write_register(self.dap, self.smu, "r2", qf.APPLET_DATA)
            m33_mod.write_register(self.dap, self.smu, "r3", size)
            # DCRSR owns the DebugReturnAddress without bit 0; EPSR.T comes
            # from the xpsr write (the reference sets $pc = base|1 through
            # GDB, which owns that convention -- same end state).
            m33_mod.write_register(self.dap, self.smu, "pc", qf.APPLET_ENTRY)
            m33_mod.write_register(
                self.dap, self.smu, "xpsr", saved["xpsr"] | 0x0100_0000
            )
            self.applet_calls += 1
            timeout_s = APPLET_TIMEOUT_S.get(op, 60.0)
            _dhcsr, rehalted = m33_mod.resume(self.dap, self.smu, timeout_s=2.0)
            if not rehalted:
                m33_mod.wait_halt(self.dap, self.smu, timeout_s=timeout_s)
            cause = m33_mod.halt_cause(self.dap, self.smu)
            causes = cause.get("causes") or []
            if "breakpoint" not in causes:
                hint = (
                    " An 'external debug request' here means an M33 EDBGREQ "
                    "latch was still set (gates.clear_m33_edbgreq must run "
                    "after the reset-quiesce)."
                    if "external debug request" in causes
                    else ""
                )
                raise FlashError(
                    f"the applet stopped for {', '.join(causes) or 'no recorded cause'} "
                    f"instead of its terminating bkpt (DFSR {cause.get('raw')})." + hint
                )
            rc = m33_mod.read_register(self.dap, self.smu, "r0")
            if rc != 0:
                raise FlashError(self._describe_applet_failure(op, offset, size, rc))
        finally:
            for name in APPLET_SAVED_REGS:
                try:
                    m33_mod.write_register(self.dap, self.smu, name, saved[name])
                except S32EError:
                    self.note(f"could not restore SMU register {name} after the applet call")

    def _describe_applet_failure(self, op: int, offset: int, size: int, rc: int) -> str:
        rc_name = qf.RC_NAMES.get(rc, "unknown rc")
        lines = [
            f"the QSPI applet failed: op={op} offset=0x{offset:08X} "
            f"size=0x{size:X} rc=0x{rc:08X} ({rc_name}). Status mailbox:"
        ]
        try:
            words = memaccess.read_words(
                self.dap, self.ap_mem, qf.APPLET_STATUS, len(qf.MAILBOX_LABELS)
            )
            if words[0] != qf.APPLET_STATUS_MAGIC:
                lines.append(
                    f"  (magic reads 0x{words[0]:08X}, expected "
                    f"0x{qf.APPLET_STATUS_MAGIC:08X} -- mailbox suspect)"
                )
            lines += [
                f"  {label} = 0x{value:08X}"
                for label, value in zip(qf.MAILBOX_LABELS, words)
            ]
        except S32EError as exc:
            lines.append(f"  (mailbox unreadable: {exc})")
        return "\n".join(lines)

    # ----------------------------------------------------------- operations --

    def _require_prepared(self) -> None:
        if not self._luts_ready:
            raise FlashError("flasher used before prepare() -- controller state unknown")

    def erase(self, offset: int, length: int, report: FlashReport) -> None:
        """Applet-driven range erase; 4 KiB alignment enforced like the reference."""
        self._require_prepared()
        if offset % qf.SECTOR_SIZE:
            raise FlashError("erase offset must be 4 KiB aligned")
        if length % qf.SECTOR_SIZE or length == 0:
            raise FlashError("erase length must be a non-zero multiple of 4 KiB")
        t0 = time.monotonic()
        plan = build_erase_plan(offset, length)
        report.erase_spans_4k = sum(1 for p in plan if p[2] == qf.SECTOR_SIZE)
        report.erase_spans_64k = sum(1 for p in plan if p[2] == qf.BLOCK_ERASE_SIZE)
        self.note(
            f"erase plan: {len(plan)} span(s), "
            f"{report.erase_spans_64k}×64K + {report.erase_spans_4k}×4K units"
        )
        for span_offset, span_length, unit, lut_name, op in plan:
            self._clear_buffers()
            self._clear_flags()
            self._load_applet_luts(lut_name)
            s0 = time.monotonic()
            self.run_applet(op, span_offset, span_length)
            self.note(
                f"erased 0x{span_offset:08X}..0x{span_offset + span_length:08X} "
                f"(unit 0x{unit:X}) in {time.monotonic() - s0:.2f}s"
            )
            report.erase_bytes += span_length
        report.erase_seconds = time.monotonic() - t0

    def chip_erase(self, report: FlashReport) -> None:
        """Host-driven whole-chip erase -- WREN, slot-0 CHIP_ERASE, WIP poll.

        The one operation with no applet op (reference :345-363); the RDSR
        poll may legitimately run minutes (datasheet-scale chip erase).
        """
        self._require_prepared()
        t0 = time.monotonic()
        self._clear_buffers()
        self._clear_flags()
        self._write_enable()
        self._clear_buffers()
        self._clear_flags()
        self._load_command_lut(qf.LUT_CHIP_ERASE)
        self._write32(qf.QSPI_SFAR, qf.IP_FLASH_BASE)
        self._write32(qf.QSPI_IPCR, qf.PHYS_COMMAND_LUT << 24)
        self._wait_ip_done("chip_erase_command", qf.IP_TIMEOUT_S)
        self.note("chip erase command issued; polling WIP (this can take minutes)")
        self._wait_ready("chip_erase", qf.CHIP_ERASE_TIMEOUT_S)
        report.erase_bytes = qf.DEVICE_SIZE
        report.erase_seconds = time.monotonic() - t0

    def program(
        self,
        data: bytes,
        offset: int,
        report: FlashReport,
        *,
        skip_blank: bool = True,
    ) -> None:
        """Erase-then-program ``data`` at ``offset`` (verify is the caller's step)."""
        self._require_prepared()
        if offset % qf.PAGE_SIZE:
            raise FlashError("program offset must be 256-byte aligned")
        if not data:
            raise FlashError("nothing to program: the input is empty")
        program_length = align_up(len(data), qf.PAGE_SIZE)
        padded = data + b"\xff" * (program_length - len(data))

        erase_start = align_down(offset, qf.SECTOR_SIZE)
        erase_length = align_up(offset + program_length, qf.SECTOR_SIZE) - erase_start
        self.erase(erase_start, erase_length, report)

        t0 = time.monotonic()
        if skip_blank:
            runs, blank_pages, non_blank = build_non_blank_program_runs(
                padded, program_length, qf.PROGRAM_CHUNK
            )
            report.skipped_pages = blank_pages
            self.note(
                f"blank-page skip: {blank_pages} of {blank_pages + non_blank} "
                f"pages are 0xFF and stay unprogrammed ({len(runs)} run(s))"
            )
        else:
            runs = [
                (o, min(qf.PROGRAM_CHUNK, program_length - o))
                for o in range(0, program_length, qf.PROGRAM_CHUNK)
            ]
        if runs:
            self._clear_buffers()
            self._clear_flags()
            self._load_applet_luts(qf.LUT_PAGE_PROGRAM)
        for run_offset, run_length in runs:
            payload = padded[run_offset : run_offset + run_length]
            s0 = time.monotonic()
            self.run_applet(qf.OP_PROGRAM_BUFFER, offset + run_offset, run_length, payload)
            seconds = time.monotonic() - s0
            rate = run_length / 1024 / seconds if seconds > 0 else 0.0
            self.note(
                f"programmed 0x{offset + run_offset:08X}  {run_length:>9,} bytes  "
                f"{seconds:6.2f}s  {rate:8.1f} KB/s"
            )
            report.programmed_bytes += run_length
        report.program_runs = len(runs)
        report.program_seconds = time.monotonic() - t0

    # ---------------------------------------------------------- read/verify --

    def _window_read(self, offset: int, nbytes: int) -> bytes:
        """Read flash content through the memory-mapped window.

        The window base is a bench uncertainty (u-f1: the reference profile
        carries both 0x0 and 0x08000000); a faulting base fails over to the
        alternate once, loudly. A base that reads *wrong data* cannot be
        detected here -- that is what the byte-exact program verify is for.
        """
        self._require_prepared()
        try:
            data = self.reader.read_bytes(self.ahb_base + offset, nbytes)
        except DapError as exc:
            alternates = [b for b in qf.AHB_WINDOW_BASES if b != self.ahb_base]
            if not alternates:
                raise
            fallback = alternates[0]
            self.note(
                f"AHB window read at base 0x{self.ahb_base:08X} faulted ({exc}); "
                f"trying base 0x{fallback:08X}"
            )
            self.ahb_base = fallback
            data = self.reader.read_bytes(self.ahb_base + offset, nbytes)
        # A fast ReadMem burst rides the same DLL machinery as WriteMem; a
        # resumed SMU would boot the application while this flow owns the
        # controller (and the app reconfigures QSPI/XRDC underneath us).
        self._repin_after_fast()
        return data

    def read(self, offset: int, nbytes: int) -> bytes:
        return self._window_read(offset, nbytes)

    def read_via_applet(self, offset: int, nbytes: int) -> bytes:
        """Window-independent read: applet op 4 into the payload buffer.

        The bench fallback if no AHB window base answers on this AP; ~one
        applet call per 192 KiB plus the RAM read-back.
        """
        self._require_prepared()
        out = bytearray()
        cursor = 0
        while cursor < nbytes:
            chunk = min(qf.PROGRAM_CHUNK, nbytes - cursor)
            self._clear_buffers()
            self._clear_flags()
            self._load_virtual_lut(
                qf.PHYS_AHB_LUT, qf.luts_for_mode(self.mode or "spi")[qf.LUT_READ]
            )
            self.run_applet(qf.OP_READ_BUFFER, offset + cursor, chunk)
            out += self.reader.read_bytes(qf.APPLET_DATA, chunk)
            self._repin_after_fast()
            cursor += chunk
        return bytes(out)

    def verify(
        self, data: bytes, offset: int, report: FlashReport, *, mode: str = "full"
    ) -> None:
        """Compare flash content against ``data`` -- byte-exact, first diff named.

        ``full`` reads the whole range back (reference parity); ``sampled``
        checks the first and last word plus one word per sector, which
        catches a wrong offset or a missed erase but not a single flipped
        page. ``none`` records that nothing was checked.
        """
        report.verify = mode
        if mode == "none":
            return
        t0 = time.monotonic()
        length = align_up(len(data), qf.PAGE_SIZE)
        padded = data + b"\xff" * (length - len(data))
        if mode == "sampled":
            word_offsets = sorted(
                {0, length - 4} | set(range(0, length, qf.SECTOR_SIZE))
            )
            for w in word_offsets:
                got = self._window_read(offset + w, 4)
                want = padded[w : w + 4]
                if got != want:
                    raise FlashError(
                        f"sampled verify mismatched at flash offset 0x{offset + w:08X}: "
                        f"expected {want.hex()}, read {got.hex()}"
                    )
            report.verify_seconds = time.monotonic() - t0
            self.note(f"sampled verify passed ({len(word_offsets)} words)")
            return
        got = self._window_read(offset, length)
        if got != padded:
            bad = next(i for i, (a, b) in enumerate(zip(got, padded)) if a != b)
            raise FlashError(
                f"verify mismatched at flash offset 0x{offset + bad:08X}: "
                f"expected 0x{padded[bad]:02X}, read 0x{got[bad]:02X} "
                f"(first difference; {sum(1 for a, b in zip(got, padded) if a != b)} "
                f"differing bytes in 0x{length:X})"
            )
        report.verify_seconds = time.monotonic() - t0
        self.note(f"full verify passed: 0x{length:X} bytes byte-exact")

    def blank_check(
        self, offset: int, length: int, report: FlashReport, *, mode: str = "full"
    ) -> None:
        """Erase verify: the range must read all-0xFF (reference pattern-compare)."""
        report.verify = f"blank-{mode}"
        t0 = time.monotonic()
        if mode == "sampled":
            word_offsets = sorted(
                {0, length - 4} | set(range(0, length, qf.SECTOR_SIZE))
            )
            for w in word_offsets:
                got = self._window_read(offset + w, 4)
                if got != b"\xff\xff\xff\xff":
                    raise FlashError(
                        f"blank check failed at flash offset 0x{offset + w:08X}: "
                        f"read {got.hex()}, expected ffffffff"
                    )
            report.verify_seconds = time.monotonic() - t0
            self.note(f"sampled blank check passed ({len(word_offsets)} words)")
            return
        got = self._window_read(offset, length)
        bad = next((i for i, b in enumerate(got) if b != 0xFF), None)
        if bad is not None:
            raise FlashError(
                f"blank check failed at flash offset 0x{offset + bad:08X}: "
                f"read 0x{got[bad]:02X}, expected 0xFF"
            )
        report.verify_seconds = time.monotonic() - t0
        self.note(f"blank check passed: 0x{length:X} bytes read 0xFF")

    def finish_report(self, report: FlashReport) -> None:
        report.mode = self.mode
        report.jedec = self.jedec
        report.ap_mem = self.ap_mem
        report.ahb_base = self.ahb_base
        report.applet_calls = self.applet_calls
        report.fast_write = (
            "demoted"
            if self.writer.fast_demoted
            else ("used" if self.writer.fast_ever_used else "unused")
        )
        report.fast_read = (
            "demoted"
            if self.reader.fast_demoted
            else ("used" if self.reader.fast_ever_used else "unused")
        )

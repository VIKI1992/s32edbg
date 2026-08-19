"""Boundary scan: SAMPLE/PRELOAD freely, EXTEST/HIGHZ/CLAMP behind an opt-in.

Sampling is non-invasive. RM 136.4.4.2 on PRELOAD: "System operation is not
affected." By contrast RM 136.4.4.3/.4/.5 say EXTEST, HIGHZ and CLAMP each
"assert the internal system reset for the MCU", and HIGHZ additionally puts
"all output drivers ... in an inactive drive state". On a live ECU that is a
reset and a bus-float, so the drive instructions sit behind an explicit
consent flag (``DRIVE_OPT_IN``); without it they raise UnsafeOperation with
the RM citation. A latched drive also *persists* after its JTAGLoad run exits
-- the pins stay driven and the MCU stays in reset until ``release()`` (a TAP
reset) or a power-cycle.

RM 136.3.4 withholds the BSR length and bit ordering ("can be found in the
device BSDL file"); ``s32edbg.bsdl`` supplies both from the bundled BSDL, so
a capture decodes to named pin states and the length is verified in one
marker-delay SVF run instead of measured by probing. Without a BSDL the raw
indexed bit vector remains available unchanged.

Before any of it works, the EXT_TEST_CTRL preamble from RM Table 984 must be
shifted in; it selects the BSR segments for this package. Every SVF this
module emits replays that preamble, which is idempotent at either chain
width.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import bsdl as bsdl_mod
from ..data.bscan import (
    BSDL_FILES,
    DRIVE_PIN_INSTRUCTIONS,
    JTAGC_IR_CLAMP,
    JTAGC_IR_EXTEST,
    JTAGC_IR_HIZ,
    JTAGC_IR_SAMPLE_PRELOAD,
    jtagc_ir,
    preamble_steps,
)
from ..errors import TransportError, UnsafeOperation
from ..transport.base import Transport
from . import tap, tapseq

#: The consent flag for the drive instructions, spelled once. The CLI declares
#: an option by this name; the refusal message cites it so the operator knows
#: the door exists and what walking through it does.
DRIVE_OPT_IN = "--i-understand-boundary-scan-drives-pins"


@dataclass
class PreambleResult:
    steps: list[dict[str, object]] = field(default_factory=list)
    ok: bool = True

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "steps": self.steps}


@dataclass
class SampleResult:
    bsr_length: int
    bits: list[int]
    raw: int
    #: Real cell count when a BSDL decoded the capture (bsr_length keeps the
    #: byte-rounded shift width the SVF path uses); None without one.
    cells: int | None = None
    #: Named pin rows (bsdl.PinState.as_dict() shapes); None without a BSDL.
    pins: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "bsr_length": self.bsr_length,
            "raw_hex": f"0x{self.raw:0{(self.bsr_length + 3) // 4}X}",
            "bits": self.bits,
        }
        if self.pins is None:
            # Byte-for-byte the pre-BSDL shape: JSON consumers keep working.
            out["note"] = (
                "Bit indices are shift positions, not pin names. RM 136.3.4 puts the "
                "BSR length and bit ordering in the BSDL file, which is not available."
            )
        else:
            out["cells"] = self.cells
            out["pins"] = self.pins
        return out

    def diff(self, other: "SampleResult") -> list[int]:
        """Indices whose value changed between two captures."""
        n = min(self.bsr_length, other.bsr_length)
        return [i for i in range(n) if self.bits[i] != other.bits[i]]

    def diff_named(
        self, other: "SampleResult", b: bsdl_mod.BsdlFile
    ) -> list[dict[str, object]]:
        """The name-aware diff: which cells changed, and whose pins they are."""
        changed = self.diff(other)
        names = bsdl_mod.name_cells(b, changed)
        return [
            {"cell": i, "name": name, "from": self.bits[i], "to": other.bits[i]}
            for i, name in zip(changed, names)
        ]

    def decode(
        self, b: bsdl_mod.BsdlFile, *, reverse_cells: bool = False
    ) -> "SampleResult":
        """Fill ``cells``/``pins`` from a BSDL. Returns self for chaining."""
        states = bsdl_mod.decode_sample(b, self.bits, reverse=reverse_cells)
        self.cells = b.boundary_length
        self.pins = [s.as_dict() for s in states]
        return self


def ir_for(
    opcode8: int, bsdl_name: str, ir_length: int, b: bsdl_mod.BsdlFile | None
) -> int:
    """The instruction value to shift: the BSDL's opcode when one is loaded
    and matches the IR width, the RM Table 959 record otherwise.

    Today the two agree for every instruction boundary scan issues (including
    the non-duplicated HIGHZ 0x0509); the BSDL winning here means a future
    file self-corrects instead of silently colliding with the table.
    """
    if b is not None and b.instruction_length == ir_length and bsdl_name in b.opcodes:
        return b.opcodes[bsdl_name]
    return jtagc_ir(opcode8, ir_length)


def reject_drive_instruction(name: str) -> None:
    """Always raises; exists so the refusal carries the RM reason and the door."""
    key = name.upper()
    if key in DRIVE_PIN_INSTRUCTIONS:
        _, reason = DRIVE_PIN_INSTRUCTIONS[key]
        raise UnsafeOperation(
            f"{key} is not enabled: it {reason}, and a latched drive persists "
            f"until TAP reset or power-cycle. Re-run with {DRIVE_OPT_IN} to "
            "drive pins anyway."
        )
    raise UnsafeOperation(f"{key} is not a boundary-scan instruction this tool implements")


def run_preamble(t: Transport, ir_length: int) -> PreambleResult:
    """Shift the RM Table 984 preamble: EXT_TEST_CTRL, TLM_TDR, TLM_TAP_SELECT_TDR.

    All three IR/DR pairs go through one jtag_sequence() call. They must stay in
    a single run: each DR payload is only meaningful while its own instruction is
    loaded, and on a batch backend separate shifts would land in separate
    JLink.exe runs with a reconnect in between.
    """
    result = PreambleResult()
    plan = preamble_steps(ir_length)
    steps = [(ir_value, ir_length, dr_value, dr_bits) for _, _, ir_value, dr_value, dr_bits in plan]

    try:
        captures = t.jtag_sequence(steps)
    except (TransportError, ValueError, NotImplementedError) as exc:
        result.ok = False
        result.steps.append({"step": plan[0][0], "ok": False, "error": str(exc)})
        return result

    for (label, _, ir_value, _, dr_bits), tdo in zip(plan, captures):
        step: dict[str, object] = {
            "step": label,
            "ir": f"0x{ir_value:0{ir_length // 4}X}",
            "ir_bits": ir_length,
            "dr_bits": dr_bits,
            "ok": True,
        }
        # None means the backend drove the pins but cannot report TDO, which is
        # normal for the Commander path and not a failure -- the preamble is a
        # write-only sequence.
        step["tdo_hex"] = f"0x{tdo:0{(dr_bits + 3) // 4}X}" if tdo is not None else None
        result.steps.append(step)
    return result


def measure_bsr_length(t: Transport, ir_length: int, *, max_bits: int = 4096) -> int:
    """Measure the boundary-scan register length.

    Assumes run_preamble() has already selected the BSR segments for this
    package; without it the measured length is meaningless.
    """
    length = tap.measure_dr_length(
        t, jtagc_ir(JTAGC_IR_SAMPLE_PRELOAD, ir_length), ir_length, max_bits=max_bits
    )
    if length <= 1:
        raise TransportError(
            f"measured a boundary-scan register length of {length}, which is the "
            "bypass register. The EXT_TEST_CTRL preamble probably did not take."
        )
    return length


def sample(
    t: Transport,
    ir_length: int,
    bsr_length: int,
    *,
    bsdl: bsdl_mod.BsdlFile | None = None,
    reverse_cells: bool = False,
) -> SampleResult:
    """Capture pin states with SAMPLE/PRELOAD.

    Non-invasive: PRELOAD writes only the boundary-scan update latches, which
    do not drive anything unless EXTEST or CLAMP is subsequently selected, and
    sampling never selects either. With a BSDL the result carries named pin
    rows; without one it is the raw indexed vector, unchanged.
    """
    captures = t.jtag_sequence(
        [(ir_for(JTAGC_IR_SAMPLE_PRELOAD, "SAMPLE", ir_length, bsdl), ir_length, 0, bsr_length)]
    )
    raw = captures[0] if captures else None
    if raw is None:
        raise TransportError(
            f"{t.name} transport shifted the boundary-scan register but cannot report "
            "TDO, so there is nothing to sample. Use a backend that returns a TDO "
            "capture."
        )
    bits = [(raw >> i) & 1 for i in range(bsr_length)]
    result = SampleResult(bsr_length=bsr_length, bits=bits, raw=raw)
    if bsdl is not None:
        result.decode(bsdl, reverse_cells=reverse_cells)
    return result


def svf_sample(
    ir_length: int,
    bsr_length: int,
    *,
    install=None,
    serial: str | None = None,
    log_file=None,
    frequency_hz: int | None = None,
    on_byte=None,
    bsdl: bsdl_mod.BsdlFile | None = None,
    reverse_cells: bool = False,
    previous: SampleResult | None = None,
) -> SampleResult:
    """Capture pin states through the SVF path, one byte per JTAGLoad run.

    This is the route that works on hardware. The raw-shift path needs a
    transport that can report TDO, and the only one that can requires J-Link's
    full connect first -- which resets the target and so destroys the very pin
    state being sampled.

    Costs one JTAGLoad invocation per byte, because JTAGLoad stops at the first
    TDO mismatch and the mismatch report is the only way it reveals received
    data. ``on_byte(index, total, value)`` is called after each, for progress.

    ``previous`` is the adaptive fast path: one full-vector compare against
    the last capture first, and a clean pass means nothing changed -- the
    whole register confirmed in a single run instead of one per byte. Any
    mismatch falls back to the per-byte read (the mismatch report does not say
    *which* byte differed, so there is no partial reuse).

    Still non-invasive: PRELOAD only loads the boundary-scan update latches,
    which drive nothing unless EXTEST or CLAMP is selected, and sampling never
    selects either (RM 136.4.4.2).
    """
    from ..transport import svf as svf_mod

    cells = bsdl.boundary_length if bsdl is not None else bsr_length
    if bsr_length % 8:
        bsr_length = ((bsr_length + 7) // 8) * 8  # round up; tail bits are discarded below

    ir_value = ir_for(JTAGC_IR_SAMPLE_PRELOAD, "SAMPLE", ir_length, bsdl)
    nbytes = bsr_length // 8

    with tempfile.TemporaryDirectory(prefix="s32edbg-bscan-") as tmp:
        path = Path(tmp) / "sample.svf"

        if previous is not None and previous.bsr_length == bsr_length:
            path.write_text(
                svf_mod.emit_dr_verify(
                    ir_length,
                    ir_value,
                    bsr_length,
                    expect=previous.raw & ((1 << min(cells, bsr_length)) - 1),
                    mask_bits=min(cells, bsr_length),
                    frequency_hz=frequency_hz,
                ),
                encoding="ascii",
            )
            check = svf_mod.run_svf(path, install=install, serial=serial, log_file=log_file)
            if check.ok:
                confirmed = SampleResult(
                    bsr_length=bsr_length, bits=list(previous.bits), raw=previous.raw
                )
                if bsdl is not None:
                    confirmed.decode(bsdl, reverse_cells=reverse_cells)
                return confirmed
            # Something changed (or the compare could not run): full read.

        values: list[int] = []
        for i in range(nbytes):
            path.write_text(
                svf_mod.emit_dr_read(
                    ir_length, ir_value, bsr_length, i, frequency_hz=frequency_hz
                ),
                encoding="ascii",
            )
            result = svf_mod.run_svf(path, install=install, serial=serial, log_file=log_file)
            got = svf_mod.parse_found_byte(result.stdout)
            if got is None and svf_mod.TDO_MISMATCH not in result.stdout and not result.ok:
                raise TransportError(
                    f"JTAGLoad failed reading byte {i} of the boundary-scan register: "
                    + (result.failures()[0] if result.failures() else "no diagnostic")
                )
            values.append(got or 0)
            if on_byte is not None:
                on_byte(i, nbytes, values[-1])

    raw = int.from_bytes(bytes(values), "little")
    bits = [(raw >> i) & 1 for i in range(bsr_length)]
    result = SampleResult(bsr_length=bsr_length, bits=bits, raw=raw)
    if bsdl is not None:
        result.decode(bsdl, reverse_cells=reverse_cells)
    return result


def svf_read_idcode(
    *,
    install=None,
    serial: str | None = None,
    log_file=None,
    frequency_hz: int | None = None,
) -> int:
    """Read the IDCODE through the SVF path: four byte-masked SDR-32 compares.

    No SIR is needed -- entering Test-Logic-Reset loads the IDCODE instruction
    (RM 136.3.1) -- so the read is valid whatever the chain's IR width, which
    is exactly why sampling can use it to pick a bundled BSDL before anything
    knows the chain state. Costs four JTAGLoad runs. On an already-preambled
    chain the 32-bit window sees only the TAP nearest TDO; a value the caller
    cannot match means "power-cycle first or pass --bsdl".
    """
    from ..transport import svf as svf_mod

    values: list[int] = []
    with tempfile.TemporaryDirectory(prefix="s32edbg-bscan-") as tmp:
        path = Path(tmp) / "idcode.svf"
        for i in range(4):
            path.write_text(
                svf_mod.emit_idcode_read(i, frequency_hz=frequency_hz),
                encoding="ascii",
            )
            result = svf_mod.run_svf(path, install=install, serial=serial, log_file=log_file)
            got = svf_mod.parse_found_byte(result.stdout)
            if got is None and svf_mod.TDO_MISMATCH not in result.stdout and not result.ok:
                raise TransportError(
                    f"JTAGLoad failed reading IDCODE byte {i}: "
                    + (result.failures()[0] if result.failures() else "no diagnostic")
                )
            values.append(got or 0)
    return int.from_bytes(bytes(values), "little")


def svf_verify_length(
    ir_length: int,
    expected_len: int,
    *,
    bsdl: bsdl_mod.BsdlFile | None = None,
    install=None,
    serial: str | None = None,
    log_file=None,
    frequency_hz: int | None = None,
) -> bool:
    """One-run proof that the boundary register is ``expected_len`` bits.

    The marker-delay design from test-plan §7.3, collapsed to O(1) by knowing
    the expected length from the BSDL: "Download successful." iff the marker
    re-emerged exactly ``expected_len`` clocks after it went in.
    """
    from ..transport import svf as svf_mod

    ir_value = ir_for(JTAGC_IR_SAMPLE_PRELOAD, "SAMPLE", ir_length, bsdl)
    with tempfile.TemporaryDirectory(prefix="s32edbg-bscan-") as tmp:
        path = Path(tmp) / "length.svf"
        path.write_text(
            svf_mod.emit_marker_verify(
                ir_length,
                ir_value,
                expected_len,
                tapseq.MEASURE_MARKER,
                frequency_hz=frequency_hz,
            ),
            encoding="ascii",
        )
        result = svf_mod.run_svf(path, install=install, serial=serial, log_file=log_file)
    return result.ok


def _run_drive_svf(
    text: str,
    *,
    install,
    serial: str | None,
    log_file,
) -> dict[str, object]:
    from ..transport import svf as svf_mod

    with tempfile.TemporaryDirectory(prefix="s32edbg-bscan-") as tmp:
        path = Path(tmp) / "drive.svf"
        path.write_text(text, encoding="ascii")
        result = svf_mod.run_svf(path, install=install, serial=serial, log_file=log_file)
    return {"ok": result.ok, "failures": result.failures()}


def drive_pins(
    b: bsdl_mod.BsdlFile,
    drives: dict[str, str],
    *,
    ir_length: int,
    install=None,
    serial: str | None = None,
    log_file=None,
    frequency_hz: int | None = None,
) -> dict[str, object]:
    """EXTEST: drive the requested pins, everything else at safe values.

    One write-only JTAGLoad run: preamble, PRELOAD the vector, latch EXTEST.
    The drive persists after the run exits -- pins driven, MCU in internal
    system reset (RM 136.4.4.3) -- until ``release()`` or a power-cycle.
    Consent (``DRIVE_OPT_IN``) is the CLI's job; this function assumes it.
    """
    if not drives:
        raise UnsafeOperation("EXTEST with no pins to drive is HIGHZ-without-Z; refusing")
    vector, plan = bsdl_mod.build_drive_vector(b, drives)
    return _drive(b, "EXTEST", JTAGC_IR_EXTEST, vector, plan, ir_length=ir_length,
                  install=install, serial=serial, log_file=log_file, frequency_hz=frequency_hz)


def highz(
    b: bsdl_mod.BsdlFile,
    *,
    ir_length: int,
    install=None,
    serial: str | None = None,
    log_file=None,
    frequency_hz: int | None = None,
) -> dict[str, object]:
    """HIGHZ: float every output driver (and assert internal MCU reset)."""
    vector, plan = bsdl_mod.build_drive_vector(b, {})
    return _drive(b, "HIGHZ", JTAGC_IR_HIZ, vector, plan, ir_length=ir_length,
                  install=install, serial=serial, log_file=log_file, frequency_hz=frequency_hz)


def clamp(
    b: bsdl_mod.BsdlFile,
    drives: dict[str, str],
    *,
    ir_length: int,
    install=None,
    serial: str | None = None,
    log_file=None,
    frequency_hz: int | None = None,
) -> dict[str, object]:
    """CLAMP: freeze pins at the preloaded pattern (requested drives on top of
    safe values, i.e. everything undriven), bypass register selected."""
    vector, plan = bsdl_mod.build_drive_vector(b, drives)
    return _drive(b, "CLAMP", JTAGC_IR_CLAMP, vector, plan, ir_length=ir_length,
                  install=install, serial=serial, log_file=log_file, frequency_hz=frequency_hz)


def _drive(
    b: bsdl_mod.BsdlFile,
    name: str,
    opcode8: int,
    vector: int,
    plan: list[dict[str, object]],
    *,
    ir_length: int,
    install,
    serial: str | None,
    log_file,
    frequency_hz: int | None,
) -> dict[str, object]:
    from ..transport import svf as svf_mod

    text = svf_mod.emit_drive(
        ir_length,
        ir_for(JTAGC_IR_SAMPLE_PRELOAD, "PRELOAD", ir_length, b),
        vector,
        b.boundary_length,
        ir_for(opcode8, name, ir_length, b),
        name,
        frequency_hz=frequency_hz,
    )
    out = _run_drive_svf(text, install=install, serial=serial, log_file=log_file)
    out.update({"instruction": name, "pins": plan, "cells": b.boundary_length})
    return out


def release(
    *,
    install=None,
    serial: str | None = None,
    log_file=None,
    frequency_hz: int | None = None,
) -> dict[str, object]:
    """Undo a latched drive: TAP reset reloads IDCODE, pins return to the
    system -- and the MCU, freed from the internal reset, boots. The BSDL
    DESIGN_WARNING wants a functional POR before further BSR work, so the
    guidance after this is: power-cycle before scanning again."""
    from ..transport import svf as svf_mod

    out = _run_drive_svf(
        svf_mod.emit_release(frequency_hz=frequency_hz),
        install=install, serial=serial, log_file=log_file,
    )
    out["instruction"] = "TAP-RESET"
    return out


def bsdl_hint(idcode: int | None) -> str | None:
    """One line about BSDL availability for this IDCODE, if known."""
    if idcode is None:
        return None
    name = BSDL_FILES.get(idcode)
    if name is None:
        return None
    b = bsdl_mod.load_bundled(idcode)
    if b is not None:
        return (
            f"RM Table 984 names {name} for IDCODE 0x{idcode:08X}; the tool bundles a "
            f"matching BSDL (entity {b.entity}, {b.boundary_length} cells) and uses it "
            "automatically -- see `bscan pins`."
        )
    return (
        f"RM Table 984 names {name} for IDCODE 0x{idcode:08X}. No bundled BSDL "
        "matches; supply one with --bsdl to map the raw bit vector to pin names."
    )

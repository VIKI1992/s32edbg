"""Boundary scan, SAMPLE/PRELOAD only.

Scope is deliberately non-invasive. RM 136.4.4.2 on PRELOAD: "System operation is
not affected." By contrast RM 136.4.4.3/.4/.5 say EXTEST, HIGHZ and CLAMP each
"assert the internal system reset for the MCU", and HIGHZ additionally puts "all
output drivers ... in an inactive drive state". On a live ECU that is a reset and
a bus-float, so those instructions are not implemented -- attempting one raises
UnsafeOperation with the RM citation.

Two things the RM does not give us, and how they are handled:
  * The BSR length ("The size of the boundary scan register and bit ordering is
    device-dependent and can be found in the device BSDL file", RM 136.3.4) --
    measured with a walking-ones marker.
  * Bit-to-pin mapping -- needs the BSDL, which is not available, so output is a
    raw indexed bit vector.

Before any of that works, the EXT_TEST_CTRL preamble from RM Table 984 must be
shifted in; it selects the BSR segments for this package.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..data.bscan import (
    BSDL_FILES,
    DRIVE_PIN_INSTRUCTIONS,
    JTAGC_IR_SAMPLE_PRELOAD,
    jtagc_ir,
    preamble_steps,
)
from ..errors import TransportError, UnsafeOperation
from ..transport.base import Transport
from . import tap


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

    def as_dict(self) -> dict[str, object]:
        return {
            "bsr_length": self.bsr_length,
            "raw_hex": f"0x{self.raw:0{(self.bsr_length + 3) // 4}X}",
            "bits": self.bits,
            "note": (
                "Bit indices are shift positions, not pin names. RM 136.3.4 puts the "
                "BSR length and bit ordering in the BSDL file, which is not available."
            ),
        }

    def diff(self, other: "SampleResult") -> list[int]:
        """Indices whose value changed between two captures."""
        n = min(self.bsr_length, other.bsr_length)
        return [i for i in range(n) if self.bits[i] != other.bits[i]]


def reject_drive_instruction(name: str) -> None:
    """Always raises; exists so the refusal carries the RM reason."""
    key = name.upper()
    if key in DRIVE_PIN_INSTRUCTIONS:
        _, reason = DRIVE_PIN_INSTRUCTIONS[key]
        raise UnsafeOperation(
            f"{key} is not implemented: it {reason}. This build is SAMPLE/PRELOAD only."
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


def sample(t: Transport, ir_length: int, bsr_length: int) -> SampleResult:
    """Capture pin states with SAMPLE/PRELOAD.

    Non-invasive: PRELOAD writes only the boundary-scan update latches, which do
    not drive anything unless EXTEST or CLAMP is subsequently selected, and this
    tool never selects either.
    """
    captures = t.jtag_sequence(
        [(jtagc_ir(JTAGC_IR_SAMPLE_PRELOAD, ir_length), ir_length, 0, bsr_length)]
    )
    raw = captures[0] if captures else None
    if raw is None:
        raise TransportError(
            f"{t.name} transport shifted the boundary-scan register but cannot report "
            "TDO, so there is nothing to sample. Use a backend that returns a TDO "
            "capture."
        )
    bits = [(raw >> i) & 1 for i in range(bsr_length)]
    return SampleResult(bsr_length=bsr_length, bits=bits, raw=raw)


def svf_sample(
    ir_length: int,
    bsr_length: int,
    *,
    install=None,
    serial: str | None = None,
    log_file=None,
    frequency_hz: int | None = None,
    on_byte=None,
) -> SampleResult:
    """Capture pin states through the SVF path, one byte per JTAGLoad run.

    This is the route that works on hardware. The raw-shift path needs a
    transport that can report TDO, and the only one that can requires J-Link's
    full connect first -- which resets the target and so destroys the very pin
    state being sampled.

    Costs one JTAGLoad invocation per byte, because JTAGLoad stops at the first
    TDO mismatch and the mismatch report is the only way it reveals received
    data. ``on_byte(index, total, value)`` is called after each, for progress.

    Still non-invasive: PRELOAD only loads the boundary-scan update latches,
    which drive nothing unless EXTEST or CLAMP is selected, and this tool never
    selects either (RM 136.4.4.2).
    """
    from ..transport import svf as svf_mod

    if bsr_length % 8:
        bsr_length = ((bsr_length + 7) // 8) * 8  # round up; tail bits are discarded below

    ir_value = jtagc_ir(JTAGC_IR_SAMPLE_PRELOAD, ir_length)
    nbytes = bsr_length // 8
    values: list[int] = []
    with tempfile.TemporaryDirectory(prefix="s32edbg-bscan-") as tmp:
        path = Path(tmp) / "sample.svf"
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
    return SampleResult(bsr_length=bsr_length, bits=bits, raw=raw)


def bsdl_hint(idcode: int | None) -> str | None:
    """The BSDL filename RM Table 984 names for this IDCODE, if known."""
    if idcode is None:
        return None
    name = BSDL_FILES.get(idcode)
    if name is None:
        return None
    return (
        f"RM Table 984 names {name} for IDCODE 0x{idcode:08X}. The RM does not say "
        "where to obtain it; with it, the raw bit vector could be mapped to pin names."
    )

"""Transport abstraction: the raw primitives every backend must provide.

Deliberately narrow. Everything ADIv6-specific (SELECT composition, the AP-read
pipeline, the power-up handshake) lives one layer up in dap/adiv6.py so it is
written once and unit-testable against a fake transport.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import IntEnum

from ..errors import UnsupportedOperation


class Tif(IntEnum):
    """Target interface. Values match the J-Link JLINK_TIF_* constants."""

    JTAG = 0
    SWD = 1


#: Raw DP register indices, i.e. address/4. These match both the ADIv6 DP
#: register addresses and the J-Link JLINK_CORESIGHT_DP_REG_* constants.
DP_REG_DPIDR = 0  # also ABORT on write
DP_REG_CTRL_STAT = 1
DP_REG_SELECT = 2  # write-only
DP_REG_RDBUFF = 3


@dataclass
class ProbeInfo:
    serial: str | None = None
    hardware_version: str | None = None
    firmware: str | None = None
    dll_version: str | None = None
    vtref_mv: int | None = None
    pins: dict[str, int] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "serial": self.serial,
            "hardware_version": self.hardware_version,
            "firmware": self.firmware,
            "dll_version": self.dll_version,
            "vtref_mv": self.vtref_mv,
            "pins": self.pins,
            "capabilities": self.capabilities,
            "notes": self.notes,
        }


class Transport(abc.ABC):
    """Raw probe access.

    ``ap_read_pipelined`` describes the JTAG-DP behaviour this backend exposes.
    ADIv6 JTAG-DP returns the *previous* result from an APACC read, so the real
    value must be collected from RDBUFF. J-Link's own Commander help states this
    for ReadAP/ReadDP. Whether the DLL wrapper hides it is not documented
    anywhere on this machine, so the value is calibrated at connect time by
    dap.adiv6.Dap.calibrate_ap_read() rather than assumed.
    """

    name: str = "base"
    ap_read_pipelined: bool = True

    #: Which protocol was last selected, or None if nothing selected one.
    #:
    #: The DAP layer needs this because DP address 0x0 is not the same register
    #: on both protocols. Over SWD it is DPIDR (and DPIDR1/BASEPTR by bank);
    #: over JTAG, ADIv6 Figure A1-2 marks DPACC address 0b00 **Reserved**, so
    #: reading it returns a value that is neither an error nor the register
    #: asked for. Without this, `read_dpidr()` over JTAG returned 0x4BA07477 on
    #: this target -- close enough to the real 0x4C013477 to pass a glance.
    selected_tif: "Tif | None" = None

    # -- lifecycle ---------------------------------------------------------
    @abc.abstractmethod
    def open(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- probe / link ------------------------------------------------------
    @abc.abstractmethod
    def probe_info(self) -> ProbeInfo: ...

    @abc.abstractmethod
    def select_interface(self, tif: Tif) -> None: ...

    @abc.abstractmethod
    def set_speed_khz(self, khz: int) -> None: ...

    # -- DP / AP -----------------------------------------------------------
    @abc.abstractmethod
    def dp_reg_read(self, index: int) -> int: ...

    @abc.abstractmethod
    def dp_reg_write(self, index: int, value: int) -> None: ...

    @abc.abstractmethod
    def ap_reg_read(self, index: int) -> int: ...

    @abc.abstractmethod
    def ap_reg_write(self, index: int, value: int) -> None: ...

    # -- bulk memory / reset pin -------------------------------------------
    #: Whether write_mem_block() can push a byte block through the probe's own
    #: memory machinery in one call. Off everywhere except the DLL backend
    #: when its (hand-bound, unverifiable-on-this-machine) WriteMem export
    #: resolves -- callers must verify by read-back and fall back to the pure
    #: ADIv6 path on any doubt.
    can_block_write: bool = False

    def write_mem_block(self, addr: int, data: bytes) -> None:
        raise UnsupportedOperation(
            f"{self.name} transport cannot block-write target memory"
        )

    #: Whether set_reset_pin() can drive the probe's nRESET line.
    can_pin_reset: bool = False

    def set_reset_pin(self, asserted: bool) -> None:
        """Drive nRESET: True pulls the line low (reset asserted)."""
        raise UnsupportedOperation(
            f"{self.name} transport cannot drive the reset pin"
        )

    # -- raw JTAG ----------------------------------------------------------
    #: Whether raw_shift() can drive arbitrary TMS/TDI vectors at all.
    can_raw_shift: bool = False
    #: Whether raw_shift() additionally reports the TDO capture. These are two
    #: separate capabilities because J-Link Commander's
    #: ``WJTAGRaw <NumBits> <TMS> <TDI>`` drives arbitrary-width vectors but
    #: prints no response, so it can run a write-only sequence (the RM Table 984
    #: boundary-scan preamble, an SWJ protocol switch) yet cannot read a
    #: register back.
    raw_shift_returns_tdo: bool = False

    def raw_shift(self, nbits: int, tms: int, tdi: int) -> int | None:
        """Clock ``nbits`` of TMS/TDI, LSB-first, and return the TDO capture.

        Returns None when the backend drove the pins but cannot report TDO.
        Callers must treat None as "no answer available", never as zero -- a
        silently-zero capture would look like a dead TDO line.
        """
        raise UnsupportedOperation(f"{self.name} transport cannot shift raw JTAG vectors")

    def ir_shift(self, value: int, nbits: int) -> int:
        raise UnsupportedOperation(f"{self.name} transport cannot shift IR directly")

    def dr_shift(self, value: int, nbits: int) -> int:
        raise UnsupportedOperation(f"{self.name} transport cannot shift DR directly")

    def reset_tap(self) -> None:
        raise UnsupportedOperation(f"{self.name} transport cannot reset the TAP directly")

    def jtag_sequence(self, steps: list[tuple[int, int, int, int]]) -> list[int | None]:
        """Run ``(ir_value, ir_bits, dr_value, dr_bits)`` steps back to back.

        Returns each step's DR capture, or None where the backend cannot report
        one.

        This exists because a batch backend gets a fresh JLink.exe run per call,
        so calling ir_shift() and then dr_shift() would drop the loaded
        instruction in between and silently shift against whatever the reconnect
        left selected. Anything that needs an instruction and its data register
        to stay paired -- the RM Table 984 preamble, SAMPLE/PRELOAD -- must go
        through here rather than calling the two separately.

        The default runs them one at a time, which is correct for any backend
        that holds TAP state in-process.
        """
        out: list[int | None] = []
        for ir_value, ir_bits, dr_value, dr_bits in steps:
            self.ir_shift(ir_value, ir_bits)
            out.append(self.dr_shift(dr_value, dr_bits))
        return out

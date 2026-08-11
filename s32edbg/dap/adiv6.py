"""ADIv6 DP/AP access model.

The key departure from ADIv5: an AP is not selected by an APSEL index, it is
*memory-mapped* in a flat address space. ADIv6 p75 (B2.2.11):

    "ADDR ... Address output bits[63:4], formed by concatenating bits[31:0] of
     SELECT1 with bits[31:4] of SELECT. The ADDR field selects a four-word bank
     of system locations to access. Bits[3:2] of the address, which are used to
     select a specific register in a bank, are provided with APACC transactions.
     Bits[1:0] are always 0b00."

So reaching AP register at absolute AP-space address ``A``:
    SELECT.ADDR = A >> 4          (written as SELECT = A & 0xFFFF_FFF0 | DPBANKSEL)
    APACC A[3:2] = (A >> 2) & 3   (the "RegIndex" a J-Link ReadAP/WriteAP takes)

DPBANKSEL (SELECT[3:0]) additionally chooses which DP register appears at DP
addresses 0x0 and 0x4 -- ADIv6 Table B2-6:
    0x0 -> DPIDR   / CTRL-STAT
    0x1 -> DPIDR1  / DLCR
    0x2 -> BASEPTR0/ TARGETID
    0x3 -> BASEPTR1/ DLPIDR
    0x5 -> (RES0)  / SELECT1
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass

from ..data.expected_apmap import APBIC1_WINDOW, TWIN_AP_L1_OFFSET
from ..data.known_ids import DpIdr, decode_dpidr, decode_dpidr1
from ..errors import DapError, PowerUpError
from ..transport.base import DP_REG_RDBUFF, DP_REG_SELECT, Tif, Transport

# CTRL/STAT bits -- ADIv6 p58 (B2.2.3)
CDBGPWRUPREQ = 1 << 28
CDBGPWRUPACK = 1 << 29
CSYSPWRUPREQ = 1 << 30
CSYSPWRUPACK = 1 << 31
CDBGRSTREQ = 1 << 26
CDBGRSTACK = 1 << 27
STICKYERR = 1 << 5
STICKYCMP = 1 << 4
STICKYORUN = 1 << 1
WDATAERR = 1 << 7
ERRMODE = 1 << 24

# DPBANKSEL values
BANK_DPIDR = 0x0
BANK_DPIDR1 = 0x1
BANK_BASEPTR0 = 0x2
BANK_BASEPTR1 = 0x3
BANK_SELECT1 = 0x5

# MEM-AP register offsets within an APv2 -- ADIv6 Table C2-7, p192
AP_CSW = 0xD00
AP_TAR_LO = 0xD04
AP_TAR_HI = 0xD08
AP_DRW = 0xD0C
AP_BASE_HI = 0xDF0
AP_CFG = 0xDF4
AP_BASE_LO = 0xDF8
AP_IDR = 0xDFC

# CSW fields -- size in bits[2:0], AddrInc in bits[5:4] (ADIv6 C2.6.4)
CSW_SIZE_MASK = 0b111
CSW_SIZE_8BIT = 0b000
CSW_SIZE_16BIT = 0b001
CSW_SIZE_32BIT = 0b010
CSW_ADDRINC_MASK = 0b11 << 4
CSW_ADDRINC_OFF = 0b00 << 4
CSW_ADDRINC_SINGLE = 0b01 << 4

#: CSW.Size encoding for each access width in bits.
CSW_SIZE_FOR_BITS = {8: CSW_SIZE_8BIT, 16: CSW_SIZE_16BIT, 32: CSW_SIZE_32BIT}

#: ADIv6 C2.6.4: TAR auto-increment is only guaranteed within a 1 KB window, so
#: TAR must be rewritten at every boundary. Ignoring this returns data from the
#: wrong addresses with no error anywhere -- silently wrong, which is worse than
#: a fault.
TAR_AUTOINC_BOUNDARY = 0x400

# AP CFG fields -- ADIv6 C2.6.5
CFG_BE = 1 << 0
CFG_LA = 1 << 1
CFG_LD = 1 << 2

#: BASE register -- ADIv6 p195 (C2.6.2). Bit[1] Format is RAO for the ADIv6
#: format; bit[0] P indicates a debug entry is present.
BASE_FORMAT = 1 << 1
BASE_PRESENT = 1 << 0


@dataclass(frozen=True)
class MemApConfig:
    """Decoded MEM-AP CFG register (ADIv6 C2.6.5)."""

    raw: int
    big_endian: bool
    large_address: bool
    large_data: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "cfg": f"0x{self.raw:08X}",
            "big_endian": self.big_endian,
            "large_address": self.large_address,
            "large_data": self.large_data,
        }


def _lane_extract(word: int, addr: int, size: int) -> int:
    """Pull a sub-word value out of the byte lane DRW presented it in.

    ADIv6 C2.6.6: for transfers narrower than a word the data appears in the
    lane selected by the low address bits, not right-aligned.
    """
    if size == 32:
        return word & 0xFFFF_FFFF
    shift = (addr & (4 - size // 8)) * 8
    return (word >> shift) & ((1 << size) - 1)


def _lane_place(value: int, addr: int, size: int) -> int:
    """Replicate a sub-word value into the byte lane DRW expects it in."""
    if size == 32:
        return value & 0xFFFF_FFFF
    shift = (addr & (4 - size // 8)) * 8
    return (value & ((1 << size) - 1)) << shift


@dataclass
class DapInfo:
    #: None when the protocol in use cannot reach it -- see
    #: Dap.dp_address0_readable. `unavailable` then says why, in words meant for
    #: the operator. Reporting None is the point: the alternative is printing the
    #: reserved register's value, which looks like an answer.
    dpidr: DpIdr | None
    dpidr1: dict[str, object] | None
    baseptr: int | None
    ctrl_stat: int
    ap_read_pipelined: bool
    unavailable: str | None = None


class Dap:
    """ADIv6 debug-port access built on a raw Transport."""

    def __init__(self, transport: Transport) -> None:
        self.t = transport
        #: -1 = unknown: never trust the DP's DPBANKSEL at construction. The
        #: J-Link DLL's own Connect() runs before this object exists and newer
        #: DLL versions leave DPBANKSEL on a non-zero bank (measured with a
        #: V13 probe, 2026-08-04: every CTRL/STAT access silently hit the
        #: wrong bank -- reads 0x00000000, writes discarded -- so the DAP
        #: "never powered up"). The first banked DP access re-selects.
        self._bank = -1
        self._select_addr = 0
        #: Whether _select_addr/_bank mirror the DP's real SELECT. False until
        #: the first write, and after invalidate_select_cache().
        self._select_valid = False
        self.ap_read_pipelined = transport.ap_read_pipelined
        self._csw_cache: dict[int, int] = {}
        self._cfg_cache: dict[int, MemApConfig] = {}
        #: DP/AP transactions issued through this object since construction.
        #: One increment is one ReadAPDPReg/WriteAPDPReg call, which on the
        #: DLL transport is one USB round trip (~1 ms) -- the unit every slow
        #: operation is made of. Machine mode reports the per-command delta in
        #: its envelopes, so a slow command names its own cost.
        self.ops = 0
        #: Last gates.GateReport read through this connection. Owned by
        #: dap.gates (status(cached=True) / invalidate_status_cache); lives
        #: here because the report describes this connection's target.
        self.gates_report_cache = None

    # ---------------------------------------------------------------- DP ---

    def _write_select(self, addr: int, bank: int) -> None:
        # Skip the DP write when SELECT already holds this value. CSW, TAR and
        # DRW share one 16-byte SELECT granule by design (ADIv6 B2.2.11), so a
        # block transfer would otherwise pay one redundant SELECT write -- a
        # whole USB round trip on the DLL transport -- per data word. Only
        # valid while nothing else moves SELECT beneath this object; anything
        # that lets the DLL run its own transactions (a fast-path block write,
        # a command string, a reconnect) must call invalidate_select_cache().
        if (
            self._select_valid
            and self._select_addr == (addr & 0xFFFF_FFF0)
            and self._bank == (bank & 0xF)
        ):
            return
        value = (addr & 0xFFFF_FFF0) | (bank & 0xF)
        self.ops += 1
        self.t.dp_reg_write(DP_REG_SELECT, value)
        self._select_addr = addr & 0xFFFF_FFF0
        self._bank = bank & 0xF
        self._select_valid = True

    def invalidate_select_cache(self) -> None:
        """Forget the mirrored SELECT value.

        Call after any operation that may have moved SELECT outside this
        object's view -- the WriteMem fast path, exec_command, a reconnect.
        ``_bank`` is set to an impossible value so the banked-DP-read shortcut
        in dp_read/dp_write cannot trust it either."""
        self._select_valid = False
        self._bank = -1

    def dp_read(self, addr: int, bank: int = BANK_DPIDR) -> int:
        """Read a DP register at ``addr`` (0x0/0x4/0x8/0xC) in ``bank``."""
        if addr in (0x0, 0x4) and bank != self._bank:
            self._write_select(self._select_addr, bank)
        self.ops += 1
        return self.t.dp_reg_read(addr >> 2)

    def dp_write(self, addr: int, value: int, bank: int = BANK_DPIDR) -> None:
        if addr in (0x0, 0x4) and bank != self._bank:
            self._write_select(self._select_addr, bank)
        self.ops += 1
        self.t.dp_reg_write(addr >> 2, value)

    #: Why DP address 0x0 is unreadable over JTAG, in the operator's words.
    DP_ADDR0_RESERVED_ON_JTAG = (
        "DP address 0x0 cannot be read over JTAG: ADIv6 Figure A1-2 makes JTAG-DP "
        "DPACC address 0b00 Reserved. That covers DPIDR, DPIDR1 and BASEPTR0/1, "
        "which all live there and are selected by DPBANKSEL. Reading it anyway "
        "returns a plausible-looking value that is not the register asked for -- "
        "on this target 0x4BA07477 rather than 0x4C013477. Over JTAG, take DPIDR "
        "from J-Link's connect banner instead."
    )

    @property
    def dp_address0_readable(self) -> bool:
        """Whether DPIDR / DPIDR1 / BASEPTR can be read through DPACC at all.

        False over JTAG. True over SWD, and true when the transport never said
        which protocol it selected -- an unknown interface is not a reason to
        refuse a read that may be perfectly valid.
        """
        return getattr(self.t, "selected_tif", None) != Tif.JTAG

    def _require_dp_address0(self) -> None:
        if not self.dp_address0_readable:
            raise DapError(self.DP_ADDR0_RESERVED_ON_JTAG)

    def read_dpidr(self) -> DpIdr:
        self._require_dp_address0()
        return decode_dpidr(self.dp_read(0x0, BANK_DPIDR))

    def read_dpidr1(self) -> dict[str, object]:
        self._require_dp_address0()
        return decode_dpidr1(self.dp_read(0x0, BANK_DPIDR1))

    def read_baseptr(self) -> int | None:
        """Top-level AP-space pointer, BASEPTR0/1 (ADIv6 p55).

        Returns None when BASEPTR0.VALID is clear, which the J-Link DLL reports
        as "APSpace base (BASEPTR0): Invalid. Assuming 0" -- and also over JTAG,
        where the register is not reachable at all. None rather than an
        exception because every caller already has a fallback for an absent
        BASEPTR (APBIC1_ROM_BASE), whereas a *wrong* pointer sends the ROM walk
        to a garbage address, which is what it did before this check existed.
        """
        if not self.dp_address0_readable:
            return None
        lo = self.dp_read(0x0, BANK_BASEPTR0)
        if not (lo & 1):
            return None
        hi = self.dp_read(0x0, BANK_BASEPTR1)
        return ((hi << 32) | (lo & 0xFFFF_F000)) & ((1 << 64) - 1)

    def read_ctrl_stat(self) -> int:
        return self.dp_read(0x4, BANK_DPIDR)

    def write_ctrl_stat(self, value: int) -> None:
        self.dp_write(0x4, value, BANK_DPIDR)

    def clear_sticky(self) -> None:
        """Clear the sticky error flags by writing 1 to them (ADIv6 p58)."""
        self.write_ctrl_stat(self.read_ctrl_stat() | STICKYERR | STICKYCMP | STICKYORUN)

    @contextmanager
    def checked_span(self):
        """One sticky clear + one CTRL/STAT check around MANY plain accesses.

        The per-access checked wrappers cost two extra DP transactions per
        32-bit value -- affordable for a dozen identity reads, ruinous for a
        register-file sweep of ~50 accesses per stop. Inside a span, use the
        plain mem_read/mem_write; STICKYERR is latching, so a fault anywhere
        in the span is still set at the exit check and raises DapError.

        Deliberately coarse: the span cannot say WHICH access faulted, and
        every value read inside a dirty span is rubbish. Callers must treat
        the DapError as "throw the whole batch away" and re-run the
        per-access checked path to localise.
        """
        self.clear_sticky()
        try:
            yield
        except BaseException:
            # The span died mid-flight; a latched sticky error must not leak
            # into whatever the caller does next.
            self.clear_sticky()
            raise
        if self.read_ctrl_stat() & STICKYERR:
            self.clear_sticky()
            raise DapError(
                "an access inside this checked span faulted; every value read "
                "in the span is suspect"
            )

    # ------------------------------------------------------- power-up ------

    def power_up(self, *, system: bool = False, timeout_s: float = 1.0) -> int:
        """Run the ADIv6 p83 power-up handshake.

        ADIv6 is explicit that CSYSPWRUPREQ must never be asserted without
        CDBGPWRUPREQ ("The response to this combination of requests is
        UNPREDICTABLE"), and that AP transfers are only guaranteed while both
        REQ and ACK are high.
        """
        want = CDBGPWRUPREQ | (CSYSPWRUPREQ if system else 0)
        need = CDBGPWRUPACK | (CSYSPWRUPACK if system else 0)

        # Clear the sticky error flags in the same write. A latched STICKYERR
        # makes the DP discard subsequent AP traffic, so powering up without
        # clearing it produces a DAP that acknowledges power yet returns zeros
        # for every AP read. The validated vendor sequence for this target writes
        # CTRL/STAT = 0xF0000020, i.e. both power-up requests plus STICKYERR.
        self.write_ctrl_stat(self.read_ctrl_stat() | want | STICKYERR | STICKYCMP | STICKYORUN)
        deadline = time.monotonic() + timeout_s
        while True:
            stat = self.read_ctrl_stat()
            if (stat & need) == need:
                return stat
            if time.monotonic() > deadline:
                raise PowerUpError(
                    "debug power-up not acknowledged within "
                    f"{timeout_s:.1f}s (CTRL/STAT=0x{stat:08X}; "
                    f"CDBGPWRUPACK={'1' if stat & CDBGPWRUPACK else '0'}"
                    + (f", CSYSPWRUPACK={'1' if stat & CSYSPWRUPACK else '0'}" if system else "")
                    + ")"
                )
            time.sleep(0.005)

    def reinitialise_after_protocol_switch(
        self, *, system: bool = False, timeout_s: float = 1.0
    ) -> int:
        """Bring the DP back up after a JTAG/SWD switch.

        ADIv6 B5.2 is explicit: "For ADIv6 or later, the state of the DP
        registers after a switch is UNKNOWN, and they must be programmed by the
        debugger before initiating any AP transactions. Programming the DP
        registers might include clearing any sticky errors."

        power_up() cannot be used directly here because it starts from a
        read_ctrl_stat() and ORs into it -- and that read is exactly what is
        undefined after a switch. So this writes absolute values instead of
        read-modify-writing, and resets the cached SELECT/CSW state that the
        switch has invalidated.
        """
        self._bank = 0
        self._select_addr = 0
        self.invalidate_csw_cache()
        self.ops += 1
        self.t.dp_reg_write(DP_REG_SELECT, 0)

        want = CDBGPWRUPREQ | (CSYSPWRUPREQ if system else 0)
        need = CDBGPWRUPACK | (CSYSPWRUPACK if system else 0)
        self.write_ctrl_stat(want | STICKYERR | STICKYCMP | STICKYORUN)

        deadline = time.monotonic() + timeout_s
        while True:
            stat = self.read_ctrl_stat()
            if (stat & need) == need:
                return stat
            if time.monotonic() > deadline:
                raise PowerUpError(
                    "debug power-up not acknowledged after a protocol switch within "
                    f"{timeout_s:.1f}s (CTRL/STAT=0x{stat:08X})"
                )
            time.sleep(0.005)

    def power_down(self, timeout_s: float = 1.0) -> None:
        """Deassert the requests and wait for ACK to drop.

        ADIv6 p83 requires waiting for CxxxPWRUPACK to go low before making a
        new power-up request, "to ensure that the power control handshaking
        mechanism is not violated".
        """
        self.write_ctrl_stat(self.read_ctrl_stat() & ~(CDBGPWRUPREQ | CSYSPWRUPREQ))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not (self.read_ctrl_stat() & (CDBGPWRUPACK | CSYSPWRUPACK)):
                return
            time.sleep(0.005)

    # ---------------------------------------------------------------- AP ---

    def ap_read(self, ap_base: int, offset: int = 0) -> int:
        """Read the AP-space word at ``ap_base + offset``."""
        full = (ap_base + offset) & 0xFFFF_FFFF
        self._write_select(full, BANK_DPIDR)
        index = (full >> 2) & 0x3
        self.ops += 1
        value = self.t.ap_reg_read(index)
        if self.ap_read_pipelined:
            # ADIv6 JTAG-DP: the APACC read returned the *previous* result; the
            # value we asked for is now in RDBUFF.
            self.ops += 1
            value = self.t.dp_reg_read(DP_REG_RDBUFF)
        return value & 0xFFFF_FFFF

    def ap_write(self, ap_base: int, offset: int, value: int) -> None:
        full = (ap_base + offset) & 0xFFFF_FFFF
        self._write_select(full, BANK_DPIDR)
        self.ops += 1
        self.t.ap_reg_write((full >> 2) & 0x3, value & 0xFFFF_FFFF)

    def calibrate_ap_read(self, ap_base: int, expected_idr: int) -> bool:
        """Decide empirically whether this transport pipelines AP reads.

        Nothing on this machine documents whether JLINK_CORESIGHT_ReadAPDPReg
        de-pipelines JTAG-DP AP reads, so rather than assume, read a known IDR
        both ways and keep the mode that produces the documented value.
        Returns True if a working mode was found.
        """
        for pipelined in (self.ap_read_pipelined, not self.ap_read_pipelined):
            self.ap_read_pipelined = pipelined
            try:
                if self.ap_read(ap_base, AP_IDR) == expected_idr:
                    return True
            except DapError:
                continue
        return False

    # ------------------------------------------------- MEM-AP memory -------

    def read_ap_cfg(self, ap_base: int) -> "MemApConfig":
        """Decode a MEM-AP's CFG register (ADIv6 C2.6.5), caching the result."""
        cached = self._cfg_cache.get(ap_base)
        if cached is not None:
            return cached
        raw = self.ap_read(ap_base, AP_CFG)
        cfg = MemApConfig(
            raw=raw,
            big_endian=bool(raw & CFG_BE),
            large_address=bool(raw & CFG_LA),
            large_data=bool(raw & CFG_LD),
        )
        self._cfg_cache[ap_base] = cfg
        return cfg

    def _csw_base(self, ap_base: int) -> int:
        """CSW with Size and AddrInc cleared, preserving everything else.

        ADIv6 C2.6.4 leaves Prot, Type, Mode and DbgSwEnable IMPLEMENTATION
        DEFINED with an SoC-chosen reset value. Those bits carry the bus
        attributes an access is issued with -- privilege, cacheability, secure
        state. Overwriting CSW wholesale silently changes them for every
        subsequent transfer, which on the AXI-AP is the kind of mistake RM
        Table 957 says needs a destructive reset to undo. So read once per AP,
        keep the bits we do not own, and only ever set the two fields we do.
        """
        cached = self._csw_cache.get(ap_base)
        if cached is None:
            cached = self.ap_read(ap_base, AP_CSW) & ~(CSW_SIZE_MASK | CSW_ADDRINC_MASK)
            self._csw_cache[ap_base] = cached
        return cached

    def invalidate_csw_cache(self, ap_base: int | None = None) -> None:
        """Drop cached CSW/CFG state, e.g. after a reset or protocol switch."""
        if ap_base is None:
            self._csw_cache.clear()
            self._cfg_cache.clear()
        else:
            self._csw_cache.pop(ap_base, None)
            self._cfg_cache.pop(ap_base, None)

    def _program_access(self, ap_base: int, addr: int, *, size: int, autoinc: int) -> None:
        if size not in CSW_SIZE_FOR_BITS:
            raise ValueError(f"access size must be one of 8, 16, 32 bits, not {size}")
        self.ap_write(ap_base, AP_CSW, self._csw_base(ap_base) | CSW_SIZE_FOR_BITS[size] | autoinc)
        self._set_tar(ap_base, addr)

    def _set_tar(self, ap_base: int, addr: int) -> None:
        """Point the AP at ``addr``, writing TAR_HI first when it applies.

        Order matters: on a long-address AP, writing the low half first would
        briefly pair a new low half with a stale high half, aiming the AP at a
        real but unintended address.
        """
        if addr > 0xFFFF_FFFF:
            if not self.read_ap_cfg(ap_base).large_address:
                raise DapError(
                    f"0x{addr:X} needs a 64-bit address but the AP at 0x{ap_base:08X} "
                    "does not report CFG.LA"
                )
            self.ap_write(ap_base, AP_TAR_HI, (addr >> 32) & 0xFFFF_FFFF)
        self.ap_write(ap_base, AP_TAR_LO, addr & 0xFFFF_FFFF)

    def mem_read(self, ap_base: int, addr: int, *, size: int = 32) -> int:
        """Read one ``size``-bit value at ``addr`` through the MEM-AP."""
        self._program_access(ap_base, addr, size=size, autoinc=CSW_ADDRINC_OFF)
        raw = self.ap_read(ap_base, AP_DRW)
        return _lane_extract(raw, addr, size)

    def mem_write(self, ap_base: int, addr: int, value: int, *, size: int = 32) -> None:
        self._program_access(ap_base, addr, size=size, autoinc=CSW_ADDRINC_OFF)
        self.ap_write(ap_base, AP_DRW, _lane_place(value, addr, size))

    def mem_read_block(self, ap_base: int, addr: int, count: int, *, size: int = 32) -> list[int]:
        """Read ``count`` consecutive ``size``-bit values using auto-increment.

        Rewrites TAR at every 1 KB boundary, which ADIv6 C2.6.4 requires because
        auto-increment is only guaranteed inside one such window.

        If the AP turns out to implement AddrInc as RO 0b00, the read-back of TAR
        after the first transfer will not have advanced; the loop then falls back
        to writing TAR for every value rather than returning the same word N
        times.
        """
        if count < 0:
            raise ValueError("count must not be negative")
        stride = size // 8
        out: list[int] = []
        autoinc_works = True
        i = 0
        while i < count:
            here = addr + i * stride
            window_left = TAR_AUTOINC_BOUNDARY - (here % TAR_AUTOINC_BOUNDARY)
            chunk = min(count - i, max(1, window_left // stride))
            if not autoinc_works:
                chunk = 1
            self._program_access(ap_base, here, size=size, autoinc=CSW_ADDRINC_SINGLE)
            for j in range(chunk):
                raw = self.ap_read(ap_base, AP_DRW)
                out.append(_lane_extract(raw, here + j * stride, size))
            if autoinc_works and chunk > 1:
                advanced = self.ap_read(ap_base, AP_TAR_LO)
                if advanced == (here & 0xFFFF_FFFF):
                    # AddrInc is not implemented; everything after the first
                    # value in this chunk came from the same address.
                    autoinc_works = False
                    del out[len(out) - chunk + 1 :]
                    i += 1
                    continue
            i += chunk
        return out

    def mem_write_block(
        self, ap_base: int, addr: int, values: list[int], *, size: int = 32
    ) -> None:
        """Write consecutive ``size``-bit values using auto-increment.

        The write-side mirror of mem_read_block: TAR is rewritten at every 1 KB
        boundary (ADIv6 C2.6.4 only guarantees auto-increment inside one such
        window), and an AP whose AddrInc is RO 0b00 is detected by reading TAR
        back after the first multi-value chunk.

        **Ordinary memory only, never FIFOs or W1C registers.** When AddrInc
        turns out not to be implemented, every value of the affected chunk has
        already been stored at the chunk's first address; the fallback then
        re-issues the whole chunk one TAR write per value, so addresses can be
        written more than once. Idempotent on RAM, wrong for anything with
        side effects.
        """
        count = len(values)
        stride = size // 8
        autoinc_works = True
        i = 0
        while i < count:
            here = addr + i * stride
            window_left = TAR_AUTOINC_BOUNDARY - (here % TAR_AUTOINC_BOUNDARY)
            chunk = min(count - i, max(1, window_left // stride))
            if not autoinc_works:
                chunk = 1
            self._program_access(ap_base, here, size=size, autoinc=CSW_ADDRINC_SINGLE)
            for j in range(chunk):
                self.ap_write(
                    ap_base, AP_DRW, _lane_place(values[i + j], here + j * stride, size)
                )
            if autoinc_works and chunk > 1:
                advanced = self.ap_read(ap_base, AP_TAR_LO)
                if advanced == (here & 0xFFFF_FFFF):
                    # AddrInc is not implemented: everything in this chunk
                    # landed at its first address. Re-issue it value by value.
                    autoinc_works = False
                    continue
            i += chunk

    # Kept so existing callers (romtable's readers) keep working; they now
    # inherit CSW preservation for free.
    def mem_read_checked(self, ap_base: int, addr: int, *, size: int = 32) -> int | None:
        """Read, returning None if the access actually faulted.

        **A faulting bus access does not raise.** It returns 0x00000000, latches
        CTRL/STAT.STICKYERR, and from then on every subsequent AP transfer also
        returns zero until the flag is cleared -- still without raising. So a
        plain read cannot tell "this location is zero" from "this location
        faulted" from "something faulted earlier and I am now reading rubbish".

        Measured: reading RTU0.GPR at 0x76120000 through the AXI-AP faults,
        because XRDC denies the debugger that peripheral slice. Without this
        check the tool reported 0x00000000 as the register's value.

        Costs two extra DP accesses per read. Worth it anywhere a wrong answer
        would be taken at face value.
        """
        self.clear_sticky()
        value = self.mem_read(ap_base, addr, size=size)
        if self.read_ctrl_stat() & STICKYERR:
            self.clear_sticky()
            return None
        return value

    def mem_write_checked(self, ap_base: int, addr: int, value: int, *, size: int = 32) -> bool:
        """Write, returning whether the access landed. See mem_read_checked."""
        self.clear_sticky()
        self.mem_write(ap_base, addr, value, size=size)
        if self.read_ctrl_stat() & STICKYERR:
            self.clear_sticky()
            return False
        return True

    def mem_read32(self, ap_base: int, addr: int) -> int:
        return self.mem_read(ap_base, addr, size=32)

    def mem_write32(self, ap_base: int, addr: int, value: int) -> None:
        self.mem_write(ap_base, addr, value, size=32)

    def read_ap_base(self, ap_base: int) -> tuple[int | None, bool]:
        """Read a MEM-AP's BASE register.

        Returns (rom_table_address, present). ADIv6 p195: BASEADDR is bits
        [31:12] padded with 0x000; bit[1] Format is RAO for the ADIv6 format and
        bit[0] P says whether a debug entry exists.
        """
        raw = self.ap_read(ap_base, AP_BASE_LO)
        present = bool(raw & BASE_PRESENT)
        return ((raw & 0xFFFF_F000) if present else None), present

    def mem_reader(self, ap_base: int):
        """A Read32 closure suitable for romtable.walk() through a MEM-AP."""

        def read32(addr: int) -> int:
            return self.mem_read32(ap_base, addr)

        return read32

    def apspace_reader(self):
        """A Read32 closure that reads raw AP space (for the top-level ROM table)."""

        def read32(addr: int) -> int:
            if not (APBIC1_WINDOW[0] <= addr <= APBIC1_WINDOW[1]):
                raise DapError(
                    f"0x{addr:08X} is outside the APBIC1 window "
                    f"0x{APBIC1_WINDOW[0]:08X}-0x{APBIC1_WINDOW[1]:08X}; RM L412334 states "
                    "external tool direct access is limited to APBIC1's 4 MB"
                )
            return self.ap_read(addr, 0)

        return read32

    # ------------------------------------------------------------ info -----

    def info(self) -> DapInfo:
        """Identity and state, with the unreachable parts reported as absent.

        CTRL/STAT is at DP address 0x4 and stays readable on both protocols, so
        even when identity is unavailable there is still evidence about whether
        the DAP is alive.
        """
        if not self.dp_address0_readable:
            return DapInfo(
                dpidr=None,
                dpidr1=None,
                baseptr=None,
                ctrl_stat=self.read_ctrl_stat(),
                ap_read_pipelined=self.ap_read_pipelined,
                unavailable=self.DP_ADDR0_RESERVED_ON_JTAG,
            )
        dpidr = self.read_dpidr()
        dpidr1 = self.read_dpidr1() if dpidr.is_adiv6 else None
        baseptr = self.read_baseptr() if dpidr.is_adiv6 else None
        return DapInfo(
            dpidr=dpidr,
            dpidr1=dpidr1,
            baseptr=baseptr,
            ctrl_stat=self.read_ctrl_stat(),
            ap_read_pipelined=self.ap_read_pipelined,
        )


def twin_ap_l1(ap_base: int) -> int:
    """The self-hosted (AP-L1) view of a SoC-600 twin AP.

    SoC-600 TRM section 2.2: the two logical APs are contiguous, selected by
    paddr_s[12]; AP-L0 is the debugger view, AP-L1 is dedicated to self-hosted
    debug. This is what makes the RM's cores-side addresses sit 0x1000 above the
    debugger-side ones.
    """
    return ap_base + TWIN_AP_L1_OFFSET

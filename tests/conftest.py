"""Shared test doubles.

Three layers, each adding one behaviour the layer below does not model:

``FakeTransport``   DP/AP register traffic and the JTAG-DP read pipeline.
``MemApFake``       MEM-AP semantics: CSW/TAR/DRW and the 1 KB auto-increment wrap.
``SyntheticTarget`` a whole plausible S32E2 -- DP identity, the RM Table 957 AP
                    map, and two ROM trees -- so the CLI can be driven end to
                    end with no probe attached.
"""

from __future__ import annotations

from s32edbg.dap import adiv6
from s32edbg.dap import romtable as rt
from s32edbg.data import a32
from s32edbg.data import coresight as cs
from s32edbg.data import r52debug as r52
from s32edbg.data.expected_apmap import (
    AP_APB,
    APBIC1_ROM_BASE,
    APBIC2_ROM_BASE,
    EXPECTED_APS,
)
from s32edbg.data.ladder1 import MDM_DEVARCH_EXPECTED, MDM_DEVARCH
from s32edbg.transport.base import ProbeInfo, Tif, Transport


class FakeTransport(Transport):
    """Records DP/AP traffic and serves AP space from a dict.

    Models the two behaviours that matter: SELECT-based AP addressing, and the
    ADIv6 JTAG-DP read pipeline where an APACC read returns the previous result.
    """

    name = "fake"

    def __init__(self, *, pipelined: bool = True) -> None:
        self.ap_read_pipelined = pipelined
        self.select = 0
        self.ctrl_stat = 0
        self.ap_space: dict[int, int] = {}
        self.log: list[tuple[str, int, int]] = []
        self._pending: int | None = None
        self.auto_ack = True
        self.unreadable: set[int] = set()

    def open(self) -> None: ...
    def close(self) -> None: ...

    def probe_info(self) -> ProbeInfo:
        return ProbeInfo()

    def select_interface(self, tif: Tif) -> None: ...
    def set_speed_khz(self, khz: int) -> None: ...

    def dp_reg_read(self, index: int) -> int:
        self.log.append(("dp_read", index, 0))
        if index == 1:
            return self.ctrl_stat
        if index == 3:  # RDBUFF
            return self._pending if self._pending is not None else 0
        return 0

    def dp_reg_write(self, index: int, value: int) -> None:
        self.log.append(("dp_write", index, value))
        if index == 2:
            self.select = value
        elif index == 1:
            # The sticky flags are write-1-to-clear (ADIv6 p58), not writable
            # storage. Modelling them as storage made `clear_sticky()` *set*
            # them, so every checked read looked like a faulted read -- which is
            # exactly the failure mode `_read_checked` exists to detect.
            sticky = adiv6.STICKYERR | adiv6.STICKYCMP | adiv6.STICKYORUN
            kept = self.ctrl_stat & sticky & ~value
            if self.auto_ack:
                ack = 0
                if value & adiv6.CDBGPWRUPREQ:
                    ack |= adiv6.CDBGPWRUPACK
                if value & adiv6.CSYSPWRUPREQ:
                    ack |= adiv6.CSYSPWRUPACK
            else:
                ack = 0
            self.ctrl_stat = (
                value & ~(adiv6.CDBGPWRUPACK | adiv6.CSYSPWRUPACK | sticky)
            ) | ack | kept

    def _addr(self, index: int) -> int:
        return (self.select & 0xFFFF_FFF0) | (index << 2)

    def ap_reg_read(self, index: int) -> int:
        from s32edbg.errors import DapError

        addr = self._addr(index)
        self.log.append(("ap_read", index, addr))
        if addr in self.unreadable:
            raise DapError(f"fault at 0x{addr:08X}")
        value = self.ap_space.get(addr, 0)
        if self.ap_read_pipelined:
            previous, self._pending = self._pending, value
            return previous if previous is not None else 0
        return value

    def ap_reg_write(self, index: int, value: int) -> None:
        addr = self._addr(index)
        self.log.append(("ap_write", index, addr))
        self.ap_space[addr] = value


class MemApFake(FakeTransport):
    """A fake that actually behaves like a MEM-AP.

    Models the one behaviour that matters for block transfers: with
    CSW.AddrInc set, TAR advances after each DRW access but **wraps inside its
    1 KB window** rather than carrying into the next one. That is what ADIv6
    C2.6.4 permits, and it is why a debugger must rewrite TAR at every boundary.
    Without the wrap modelled, a block read that ignores the rule still looks
    correct in tests and only fails on silicon.
    """

    def __init__(self, *, autoinc_supported: bool = True) -> None:
        super().__init__(pipelined=False)
        self.mem: dict[int, int] = {}
        self.tar = 0
        self.csw = 0
        self.autoinc_supported = autoinc_supported
        #: Addresses whose access faults. Modelled the way silicon behaves and
        #: not by raising: the transfer returns zero, latches STICKYERR, and
        #: every later transfer returns zero too until the flag is cleared.
        #: Measured on this target -- an OS-locked core faults on EDSCR while
        #: MIDR and EDDFR still read correctly.
        self.faulting: set[int] = set()

    def _fault(self) -> None:
        self.ctrl_stat |= adiv6.STICKYERR

    def _stride(self) -> int:
        return {0b000: 1, 0b001: 2, 0b010: 4}.get(self.csw & 0b111, 4)

    def _advance(self) -> None:
        if not self.autoinc_supported or (self.csw & (0b11 << 4)) != adiv6.CSW_ADDRINC_SINGLE:
            return
        page = self.tar & ~(adiv6.TAR_AUTOINC_BOUNDARY - 1)
        offset = (self.tar + self._stride()) & (adiv6.TAR_AUTOINC_BOUNDARY - 1)
        self.tar = page | offset

    def ap_reg_read(self, index: int) -> int:
        off = self._addr(index) & 0xFFF
        if off == adiv6.AP_DRW:
            if self.tar in self.faulting or self.ctrl_stat & adiv6.STICKYERR:
                # Either this access faults, or an earlier one did and the DP is
                # still discarding transfers. Both read as zero, with no error.
                if self.tar in self.faulting:
                    self._fault()
                self._advance()
                return 0
            value = self.mem.get(self.tar, 0)
            self._advance()
            return value
        if off == adiv6.AP_TAR_LO:
            return self.tar & 0xFFFF_FFFF
        if off == adiv6.AP_CSW:
            return self.csw
        return super().ap_reg_read(index)

    def ap_reg_write(self, index: int, value: int) -> None:
        off = self._addr(index) & 0xFFF
        if off == adiv6.AP_TAR_LO:
            self.tar = value
        elif off == adiv6.AP_CSW:
            self.csw = value
        elif off == adiv6.AP_DRW:
            if self.tar in self.faulting:
                self._fault()
            elif not self.ctrl_stat & adiv6.STICKYERR:
                self.on_mem_write(self.tar, value)
                self.mem[self.tar] = value
            self._advance()
        super().ap_reg_write(index, value)

    def on_mem_write(self, addr: int, value: int) -> None:
        """Hook for subclasses that model side effects of a write."""


# ---------------------------------------------------------------------------
# A whole synthetic S32E2, for driving the CLI with no probe attached
# ---------------------------------------------------------------------------

#: A CoreSight class 0x9 identification block. CIDR1[7:4] carries the class.
CIDR_CLASS9 = {rt.OFF_CIDR0: 0x0D, rt.OFF_CIDR1: 0x90, rt.OFF_CIDR2: 0x05, rt.OFF_CIDR3: 0xB1}
DEVARCH_ROM_TABLE = 0x4770_0AF7  # Arm, PRESENT, ARCHID 0x0AF7
DEVARCH_MEM_AP = 0x4770_0A17

#: RM Table 961 for S32E277/8/9 in the 27x27 package, and the DPIDR read off
#: this silicon (DPv3 = ADIv6). Shared with the hardware suite so the two
#: cannot drift apart.
IDCODE_S32E277 = 0x0831_F01D
DPIDR_S32E2 = 0x4C01_3477


def class9_entry(rom_base: int, component: int) -> int:
    """A present class 0x9 ROM entry pointing at ``component`` (ADIv6 D4.5.17)."""
    pages = (component - rom_base) >> 12
    return ((pages & 0xFFFFF) << 12) | 0b11


def put_id_block(store: dict[int, int], base: int, *, devarch: int, devtype: int = 0x00) -> None:
    for off, val in CIDR_CLASS9.items():
        store[base + off] = val
    store[base + rt.OFF_DEVARCH] = devarch
    store[base + rt.OFF_DEVTYPE] = devtype
    store[base + rt.OFF_DEVID] = 0
    for off in (rt.OFF_PIDR0, rt.OFF_PIDR1, rt.OFF_PIDR2, rt.OFF_PIDR3, rt.OFF_PIDR4):
        store[base + off] = 0


def put_rom_table(store: dict[int, int], base: int, children: list[int]) -> None:
    put_id_block(store, base, devarch=DEVARCH_ROM_TABLE)
    for i, child in enumerate(children):
        store[base + i * 4] = class9_entry(base, child)
    store[base + len(children) * 4] = 0  # terminator


# ---------------------------------------------------------------------------
# Cortex-R52 core debug frames
# ---------------------------------------------------------------------------

#: Read off silicon: Arm (0x41), Cortex-R52 (part 0xD13).
MIDR_CORTEX_R52 = 0x411F_D131

#: DEVARCH for an Armv8-R processor debug component: Arm, PRESENT, archid 0x6A05.
DEVARCH_R52_DEBUG = 0x4770_6A05
DEVARCH_CTI = 0x4770_1A14

#: EDDFR encoding eight breakpoints and eight watchpoints (Armv8 stores the
#: count minus one, so the nibbles are 7).
EDDFR_8BP_8WP = (7 << 12) | (7 << 20)

#: DBGAUTHSTATUS with every signal implemented and enabled (0b11 per field).
AUTHSTATUS_ALL_GRANTED = 0xFF

#: The two core debug frames the ROM walk found on this silicon, and their CTIs.
CORE0_DEBUG, CORE0_CTI = 0x0041_0000, 0x0042_0000
CORE1_DEBUG, CORE1_CTI = 0x0051_0000, 0x0052_0000


def put_core_frame(
    store: dict[int, int],
    base: int,
    *,
    aff0: int = 0,
    aff1: int = 0,
    midr: int = MIDR_CORTEX_R52,
    eddfr: int = EDDFR_8BP_8WP,
    authstatus: int = AUTHSTATUS_ALL_GRANTED,
    edscr: int = r52.EDSCR_STATUS_RUNNING,
    edprsr: int = r52.EDPRSR_PU,
    lsr: int = 0,
) -> None:
    """Install a plausible Cortex-R52 external debug frame at ``base``.

    Defaults describe a healthy running core with invasive debug granted, which
    is the state the bench board is in. Individual tests override the pieces
    they are about.
    """
    put_id_block(store, base, devarch=DEVARCH_R52_DEBUG, devtype=0x15)
    store[base + rt.OFF_DEVARCH] = DEVARCH_R52_DEBUG
    store[base + r52.MIDR] = midr
    store[base + r52.EDDFR_LO] = eddfr
    store[base + r52.EDSCR] = edscr
    store[base + r52.EDPRSR] = edprsr
    store[base + cs.DEVAFF0] = (aff1 << 8) | aff0
    store[base + cs.DEVAFF1] = 0
    store[base + cs.AUTHSTATUS] = authstatus
    store[base + cs.LSR] = lsr


def put_cti(store: dict[int, int], base: int) -> None:
    put_id_block(store, base, devarch=DEVARCH_CTI, devtype=0x14)


class SyntheticTarget(MemApFake):
    """Answers like a healthy, gate-open S32E2.

    Enough of one to drive `connect`, `gates`, `apmap`, `romdump`, `mem` and
    `report` end to end. It is not a simulator -- it exists so the CLI wiring,
    the JSON contract and the safety policy can be tested on a machine with no
    J-Link installed at all.
    """

    name = "synthetic"

    def __init__(self) -> None:
        super().__init__()
        self.dpidr = DPIDR_S32E2
        self.closed = False

        for ap in EXPECTED_APS:
            self.ap_space[ap.base + adiv6.AP_IDR] = ap.idr or 0
            put_id_block(self.ap_space, ap.base, devarch=DEVARCH_MEM_AP)
        self.ap_space[0x01C1_0000 + MDM_DEVARCH] = MDM_DEVARCH_EXPECTED

        # The AP-space ROM table: RM Table 957's map, as a walkable tree.
        put_rom_table(self.ap_space, APBIC1_ROM_BASE, [ap.base for ap in EXPECTED_APS])

        # The APB-AP's BASE points at the APBIC2 ROM, which lives behind the
        # MEM-AP rather than in AP space -- the same split as on silicon.
        self.ap_space[AP_APB + adiv6.AP_BASE_LO] = APBIC2_ROM_BASE | adiv6.BASE_PRESENT

        # The APBIC2 tree, shaped like the real one: the ROM enumerates the two
        # core debug frames the silicon lists, and each core's CTI sits at
        # +0x10000 without being enumerated -- which is why runcontrol derives
        # the CTI address by stride rather than looking it up.
        put_rom_table(self.mem, APBIC2_ROM_BASE, [CORE0_DEBUG, CORE1_DEBUG])
        put_core_frame(self.mem, CORE0_DEBUG, aff0=0, aff1=0)
        put_core_frame(self.mem, CORE1_DEBUG, aff0=1, aff1=0)
        put_cti(self.mem, CORE0_CTI)
        put_cti(self.mem, CORE1_CTI)
        self.core_frames = {CORE0_DEBUG: CORE0_CTI, CORE1_DEBUG: CORE1_CTI}
        #: Per-core register files, reachable only by executing instructions
        #: through EDITR -- exactly as on silicon, where there is no register
        #: file to address directly.
        self.regfile: dict[int, dict[int, int]] = {
            CORE0_DEBUG: {n: 0x1000 + n for n in range(15)},
            CORE1_DEBUG: {n: 0x2000 + n for n in range(15)},
        }
        self.cpsr: dict[int, int] = {CORE0_DEBUG: 0x6000_0113, CORE1_DEBUG: 0x6000_0113}
        #: DLR is the PC in Halting Debug state (Cortex-R52 TRM 4.2.31), and
        #: DSPSR the saved PSR. Both are p15 coprocessor reads, not MRS.
        self.dlr: dict[int, int] = {CORE0_DEBUG: 0x321D_2EE6, CORE1_DEBUG: 0x321D_53DE}
        self.dspsr: dict[int, int] = {CORE0_DEBUG: 0x0000_003F, CORE1_DEBUG: 0x0000_003F}
        #: Which instruction encoding this fake's cores accept through EDITR.
        #: T32 by default because that is what the bench silicon does; flip it
        #: to prove the calibration finds A32 too.
        self.accepts_t32 = True
        #: Whether CTI pulses have any effect. See on_mem_write.
        self.cti_enabled = True
        #: Every write to a core's EDSCR, so tests can assert on transient values
        #: like the interrupt mask that are restored before the call returns.
        self.edscr_writes: list[tuple[int, int]] = []
        #: Every DBGBCR write, so the transient suspend-and-restore around a
        #: step is observable after the call returns.
        self.dbgbcr_writes: list[tuple[int, int]] = []

    # -- run-control side effects -------------------------------------------

    def on_mem_write(self, addr: int, value: int) -> None:
        """Model the writes that actually change core state.

        Without this a fake accepts a CTIAPPPULSE and nothing happens, so a halt
        that never took would still look like a pass. The mapping is the one the
        code under test relies on: channel 0 -> trigger out 0 -> debug request,
        channel 1 -> trigger out 1 -> restart.
        """
        for debug_base, cti_base in self.core_frames.items():
            # `cti_enabled` models a CTI whose pulses go nowhere -- a mis-wired
            # trigger, or a core that is not listening. It is deliberately
            # separate from the frame handling below: disabling the whole frame
            # would also break EDITR, so a test meaning "the halt does not take"
            # would silently also mean "registers cannot be read".
            if self.cti_enabled and addr == cti_base + r52.CTIAPPPULSE:
                if value & (1 << r52.CHANNEL_HALT):
                    self._set_halted(debug_base, True)
                if value & (1 << r52.CHANNEL_RESTART):
                    self._restart(debug_base)
            if addr == debug_base + r52.EDSCR:
                self.edscr_writes.append((debug_base, value))
            for _n in range(8):
                if addr == debug_base + r52.dbgbcr(_n):
                    self.dbgbcr_writes.append((addr, value))
            if addr == debug_base + r52.EDITR:
                self._execute(debug_base, value)
            if addr == debug_base + r52.EDRCR and value & r52.EDRCR_CSE:
                # EDSCR.ERR latches and stays latched. Without modelling the
                # clear, one failed instruction makes every later one look like
                # it failed too -- which stopped the encoding calibration from
                # ever reaching its second candidate.
                self.mem[debug_base + r52.EDSCR] = (
                    self.mem.get(debug_base + r52.EDSCR, 0) & ~r52.EDSCR_ERR
                )
            if addr == debug_base + r52.OSLAR and value == 0:
                self.mem[debug_base + r52.EDPRSR] = (
                    self.mem.get(debug_base + r52.EDPRSR, 0) & ~r52.EDPRSR_OSLK
                )
                # The lock is what was gating the core-domain registers.
                self.faulting.discard(debug_base + r52.EDSCR)

    def _armed_breakpoint(self, debug_base: int) -> int | None:
        """Address of the first enabled breakpoint comparator, if any."""
        for n in range(8):
            control = self.mem.get(debug_base + r52.dbgbcr(n), 0)
            if control & r52.DBGBCR_E:
                value = self.mem.get(debug_base + r52.dbgbvr(n), 0)
                decoded = r52.decode_breakpoint(value, control)
                return int(str(decoded["address"]), 16)
        return None

    def _restart(self, debug_base: int) -> None:
        """What happens when the restart channel is pulsed.

        Three outcomes, and the fake has to produce all three or the tests prove
        nothing:

        * ``EDECR.SS`` set -- execute one instruction and re-halt with a
          halting-step status. The PC advances by the instruction's real width,
          decoded from memory, so a step-over that gets the width wrong shows up.
        * an armed breakpoint -- run and halt on it, with the breakpoint status.
          This is what makes step-over's temporary breakpoint testable.
        * otherwise -- run.
        """
        if self.mem.get(debug_base + r52.EDECR, 0) & r52.EDECR_SS:
            pc = self.dlr.get(debug_base, 0)
            instruction = a32.decode(pc, self.mem.get(pc & ~1, 0), thumb=True)
            self.dlr[debug_base] = instruction.next_address
            self._set_status(debug_base, 0b011011)  # halting step, normal
            return

        armed = self._armed_breakpoint(debug_base)
        if armed is not None:
            self.dlr[debug_base] = armed
            self._set_status(debug_base, 0b000111)  # breakpoint
            return

        self._set_halted(debug_base, False)

    def _set_status(self, debug_base: int, status: int) -> None:
        edscr = self.mem.get(debug_base + r52.EDSCR, 0) & ~r52.EDSCR_STATUS_MASK
        self.mem[debug_base + r52.EDSCR] = edscr | status | r52.EDSCR_ITE
        self.mem[debug_base + r52.EDPRSR] = (
            self.mem.get(debug_base + r52.EDPRSR, 0) | r52.EDPRSR_HALTED
        )

    def _set_halted(self, debug_base: int, halted: bool) -> None:
        edscr = self.mem.get(debug_base + r52.EDSCR, 0) & ~r52.EDSCR_STATUS_MASK
        edprsr = self.mem.get(debug_base + r52.EDPRSR, 0)
        if halted:
            # ITE set: a halted core is ready to accept an instruction through
            # EDITR, which is the precondition every register transfer needs.
            self.mem[debug_base + r52.EDSCR] = edscr | 0b010011 | r52.EDSCR_ITE
            self.mem[debug_base + r52.EDPRSR] = edprsr | r52.EDPRSR_HALTED
        else:
            self.mem[debug_base + r52.EDSCR] = (
                edscr | r52.EDSCR_STATUS_RUNNING
            ) & ~r52.EDSCR_ITE
            self.mem[debug_base + r52.EDPRSR] = edprsr & ~r52.EDPRSR_HALTED

    def _execute(self, debug_base: int, instruction: int) -> None:
        """Model the core executing one instruction written to EDITR.

        Only the three transfers this tool emits are modelled; anything else
        sets EDSCR.ERR, which is what silicon does with an instruction it cannot
        execute. That matters: a fake that quietly accepted any encoding would
        let a wrong instruction encoding pass the round-trip oracle, which is
        the one thing the oracle exists to catch.
        """
        regs = self.regfile.setdefault(debug_base, {})

        # Match against the encodings rather than picking the register number
        # out of a fixed bit position: T32 and A32 put Rt in different places,
        # and this fake has to be able to tell a *wrong* encoding from a right
        # one without knowing which convention is in play.
        for rt in range(16):
            if instruction == r52.instr_reg_to_dtr(rt, t32=self.accepts_t32):
                self.mem[debug_base + r52.DBGDTRTX] = regs.get(rt, 0)
                return
            if instruction == r52.instr_dtr_to_reg(rt, t32=self.accepts_t32):
                regs[rt] = self.mem.get(debug_base + r52.DBGDTRRX, 0)
                return
            if instruction == r52.instr_cpsr_to_reg(rt, t32=self.accepts_t32):
                regs[rt] = self.cpsr.get(debug_base, 0)
                return
            if instruction == r52.instr_dlr_to_reg(rt, t32=self.accepts_t32):
                regs[rt] = self.dlr.get(debug_base, 0)
                return
            if instruction == r52.instr_dspsr_to_reg(rt, t32=self.accepts_t32):
                regs[rt] = self.dspsr.get(debug_base, 0)
                return

        # Anything else -- including the right instruction in the wrong
        # encoding -- sets EDSCR.ERR, which is what silicon does.
        self.mem[debug_base + r52.EDSCR] = (
            self.mem.get(debug_base + r52.EDSCR, 0) | r52.EDSCR_ERR
        )

    def lock_core(self, debug_base: int) -> None:
        """Put a core in the state three of the four bench cores come up in."""
        self.mem[debug_base + r52.EDPRSR] = (
            self.mem.get(debug_base + r52.EDPRSR, 0) | r52.EDPRSR_OSLK | r52.EDPRSR_EDAD
        )
        self.faulting.add(debug_base + r52.EDSCR)

    # -- transport surface the CLI reaches for with getattr -----------------

    def close(self) -> None:
        self.closed = True

    def read_idcode(self) -> int:
        return IDCODE_S32E277

    def probe_info(self) -> ProbeInfo:
        return ProbeInfo(
            serial="000000000",
            hardware_version="V11.00",
            firmware="J-Link V11 compiled Jan 01 2026",
            dll_version="8.10",
            vtref_mv=3300,
            pins={"TCK": 1, "TDI": 1, "TDO": 1, "TMS": 1, "TRST": 1, "RESET": 1},
        )

    def dp_reg_read(self, index: int) -> int:
        """DPIDR, DPIDR1 and BASEPTR0 all sit at DP address 0x0.

        Which one answers is chosen by DPBANKSEL in SELECT, so a fake that
        ignores the bank silently returns DPIDR for all three.
        """
        if index == 0:
            bank = self.select & 0xF
            if bank == adiv6.BANK_DPIDR:
                return self.dpidr
            if bank == adiv6.BANK_DPIDR1:
                return 0x0000_0020  # ASIZE 32, ERRMODE 0
            return 0  # BASEPTR0.VALID clear -- J-Link reports the same
        return super().dp_reg_read(index)

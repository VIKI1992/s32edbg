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
from s32edbg.data import ladder1 as lad
from s32edbg.data import m33debug as m33d
from s32edbg.data import r52debug as r52
from s32edbg.data import rtuboot
from s32edbg.data.expected_apmap import (
    AP_APB,
    AP_MDM,
    AP_SDA,
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
            value = self.on_mem_read(self.tar, self.mem.get(self.tar, 0))
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
                if not self.on_mem_write(self.tar, value):
                    self.mem[self.tar] = value
            self._advance()
        super().ap_reg_write(index, value)

    def on_mem_write(self, addr: int, value: int) -> bool | None:
        """Hook for subclasses that model side effects of a write.

        Return True to claim the write entirely -- the raw store is skipped,
        which is how registers with non-storage semantics (write-1-to-clear,
        key-guarded control words) are modelled.
        """

    def on_mem_read(self, addr: int, value: int) -> int:
        """Hook for subclasses that model read side effects (clear-on-read)."""
        return value


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
        self.ap_space[AP_SDA + lad.SDA_RSTCTRL] = lad.SDA_RSTCTRL_RESET_VALUE

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
        #: When set, CTIINTACK does not deassert the debug-request trigger, so
        #: the guide §5.5 acknowledge-poll times out -- the failure the poll
        #: exists to catch.
        self.cti_ack_broken = False
        #: Every write to a core's EDSCR, so tests can assert on transient values
        #: like the interrupt mask that are restored before the call returns.
        self.edscr_writes: list[tuple[int, int]] = []
        #: Every DBGBCR write, so the transient suspend-and-restore around a
        #: step is observable after the call returns.
        self.dbgbcr_writes: list[tuple[int, int]] = []

        # -- one Cortex-M33 (the SMU), behind its own AHB-AP -----------------
        #
        # The M33 debug registers live at fixed 0xE000_xxxx addresses in the
        # core's private bus, which do not collide with the APBIC2 map, so the
        # single shared memory dict can carry both. Only one M33 is modelled;
        # the tests drive it as `--core smu`.
        self.mem[m33d.CPUID] = 0x410F_D214  # Arm, Cortex-M33
        self.mem[m33d.FP_CTRL] = 0x1000_0080  # eight BPU comparators
        self.mem[m33d.DWT_CTRL] = 0x4 << 28  # four DWT comparators
        self.mem[m33d.DEMCR] = 0
        self.mem[m33d.DFSR] = 0
        #: The M33 behavioural state. DHCSR is *not* backed by the memory dict:
        #: its write side is key-guarded control and its read side is composed
        #: status, so the fake owns both (see on_mem_write / on_mem_read).
        self.m33 = {
            "control": 0,  # accepted C_DEBUGEN/C_HALT/C_STEP/C_MASKINTS bits
            "halted": False,
            "sticky": 0,  # S_RETIRE_ST / S_RESET_ST / S_RESTART_ST accumulate here
            "pc": 0x2500_1F70,
            "regs": {},
        }
        #: Every *accepted* DHCSR control word, in order -- what pins the
        #: guide §6.4 two-write step sequence.
        self.m33_dhcsr_writes: list[int] = []
        #: DHCSR writes that hit the §6.4 UNPREDICTABLE case: C_MASKINTS
        #: changed in the same write that clears C_HALT.
        self.m33_unpredictable: list[int] = []
        #: Every FP_COMP write, so the suspend-and-restore around an M33 step
        #: is observable after the call returns.
        self.fp_comp_writes: list[tuple[int, int]] = []

        # -- load-flow (MC_ME / RGM / SRAMC) model ---------------------------
        #
        # RGM PRST_0[1] comes up with the partition and all four cores in
        # reset; PSTAT mirrors it. Clearing a core's PRST bit while its MDM
        # EDBGREQ is latched halts the core straight out of reset -- the park
        # mechanism the load flow relies on.
        self.mem[rtuboot.RGM_PRST0_1] = 0x1F
        self.mem[rtuboot.RGM_PSTAT0_1] = 0x1F
        #: (pconf, pupd, stat) triples the MC_ME key handshake commits.
        self._mc_me_triples = (
            (0x4190_0300, 0x4190_0304, 0x4190_0308),
            (0x4190_0340, 0x4190_0344, 0x4190_0348),
            (0x4190_0360, 0x4190_0364, 0x4190_0368),
            (0x4190_0500, 0x4190_0504, 0x4190_0508),
            (0x4190_0540, 0x4190_0544, 0x4190_0548),
        )
        #: SRAMC controllers whose RAMCR.INIT edge is modelled, with the
        #: witness word each D-bank's scrub must zero. RAMCR.INIT is
        #: EDGE-triggered on silicon (root-caused 2026-07-03): writing INIT=1
        #: while it is already 1 does nothing, registers read back perfect.
        self._sramc_witness = {
            0x761D_0000: 0x3178_E1A0,
            0x761E_0000: 0x317C_0010,
            0x761F_0000: 0x3180_0010,
        }
        #: Every SRAMC base the tables touch: v2 D-banks plus the generic
        #: scrub's SMU/CE/RTU controllers (all offsets are <= 0x40, so masking
        #: the low byte recovers the base).
        self._sramc_bases = {a & ~0xFF for a, _v in rtuboot.SRAMC_V2_WRITES} | {
            a.addr & ~0xFF
            for a in rtuboot.ACTIONS_GENERIC_SRAM
            if a.op in ("write32", "poll")
        }

    def _loadflow_write(self, addr: int, value: int) -> None:
        """MC_ME key commits, SRAMC INIT edges, RGM mirror + EDBGREQ halts."""
        if addr == rtuboot.MC_ME_CTL_KEY and value == 0xA50F:
            # The inverted-key write commits pending process updates: STAT
            # mirrors PCONF, PUPD clears.
            for pconf, pupd, stat in self._mc_me_triples:
                self.mem[stat] = self.mem.get(pconf, 0)
                self.mem[pupd] = 0
            return
        if addr in self._sramc_bases:
            old = self.mem.get(addr, 0)
            if value & 1 and not old & 1:  # 0 -> 1 edge on INIT
                self.mem[addr + 0xC] = self.mem.get(addr + 0xC, 0) | 1  # IDONE
                witness = self._sramc_witness.get(addr)
                if witness is not None:
                    start = self.mem.get(addr + 0x4, 0)
                    end = self.mem.get(addr + 0x8, 0)
                    if start <= witness <= end:
                        self.mem[witness] = 0
            return
        if addr == rtuboot.RGM_PRST0_1:
            old = self.mem.get(rtuboot.RGM_PRST0_1, 0)
            self.mem[rtuboot.RGM_PSTAT0_1] = value
            control2 = self.ap_space.get(AP_MDM + lad.MDM_CONTROL2, 0)
            for debug_base, bit in ((CORE0_DEBUG, 0x2), (CORE1_DEBUG, 0x4)):
                if old & bit and not value & bit:
                    core_idx = 0 if debug_base == CORE0_DEBUG else 1
                    if control2 & lad.edbgreq_bit(core_idx):
                        # Out of cold reset with EDBGREQ pending: halted at the
                        # park loop, OS lock set exactly as silicon sets it.
                        self._set_halted(debug_base, True)
                        self.mem[debug_base + r52.EDPRSR] |= r52.EDPRSR_OSLK
                        self.faulting.add(debug_base + r52.EDSCR)

    # -- run-control side effects -------------------------------------------

    def on_mem_write(self, addr: int, value: int) -> bool | None:
        """Model the writes that actually change core state.

        Without this a fake accepts a CTIAPPPULSE and nothing happens, so a halt
        that never took would still look like a pass. The mapping is the one the
        code under test relies on: channel 0 -> trigger out 0 -> debug request,
        channel 1 -> trigger out 1 -> restart.
        """
        self._loadflow_write(addr, value)
        handled = self._m33_write(addr, value)
        if handled:
            return True
        for debug_base, cti_base in self.core_frames.items():
            # `cti_enabled` models a CTI whose pulses go nowhere -- a mis-wired
            # trigger, or a core that is not listening. It is deliberately
            # separate from the frame handling below: disabling the whole frame
            # would also break EDITR, so a test meaning "the halt does not take"
            # would silently also mean "registers cannot be read".
            if self.cti_enabled and addr == cti_base + r52.CTIAPPPULSE:
                if value & (1 << r52.CHANNEL_HALT):
                    self._set_halted(debug_base, True)
                    # The CTI holds the debug-request trigger asserted until it
                    # is acknowledged -- the state CTITRIGOUTSTATUS reports and
                    # CTIINTACK clears (guide §5.5).
                    self.mem[cti_base + r52.CTITRIGOUTSTATUS] = (
                        self.mem.get(cti_base + r52.CTITRIGOUTSTATUS, 0)
                        | (1 << r52.TRIGOUT_DEBUG_REQUEST)
                    )
                if value & (1 << r52.CHANNEL_RESTART):
                    self._restart(debug_base)
                    # CTM broadcast (guide §5.9): with the restart channel gated
                    # out to the matrix, one pulse reaches every other core
                    # whose gate is open too. A closed gate keeps it local,
                    # which is the tool's single-core default.
                    mask = 1 << r52.CHANNEL_RESTART
                    if self.mem.get(cti_base + r52.CTIGATE, 0) & mask:
                        for other_debug, other_cti in self.core_frames.items():
                            if other_debug != debug_base and (
                                self.mem.get(other_cti + r52.CTIGATE, 0) & mask
                            ):
                                self._restart(other_debug)
            if (
                addr == cti_base + r52.CTIINTACK
                and value & (1 << r52.TRIGOUT_DEBUG_REQUEST)
                and not self.cti_ack_broken
            ):
                self.mem[cti_base + r52.CTITRIGOUTSTATUS] = self.mem.get(
                    cti_base + r52.CTITRIGOUTSTATUS, 0
                ) & ~(1 << r52.TRIGOUT_DEBUG_REQUEST)
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
        return None

    def on_mem_read(self, addr: int, value: int) -> int:
        # EDPRSR.SDR is clear-on-read (guide §5.5) -- the read that observes
        # the restart consumes the evidence.
        for debug_base in self.core_frames:
            if addr == debug_base + r52.EDPRSR:
                self.mem[addr] = value & ~r52.EDPRSR_SDR
                return value
        if addr == m33d.DHCSR:
            state = self.m33
            status = state["sticky"] | m33d.DHCSR_S_REGRDY
            if state["halted"]:
                status |= m33d.DHCSR_S_HALT
            state["sticky"] = 0  # S_RETIRE/S_RESET/S_RESTART are clear-on-read
            return state["control"] | status
        for n in range(4):
            if addr == m33d.dwt_function(n):
                # The RO ID field [31:27] reads back on silicon (measured
                # 0b01011 on the SMU) -- a read-back check that forgets to
                # mask it fails against real hardware, so the fake fails it
                # the same way.
                return value | (0b01011 << 27)
        return value

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
        # Whatever the outcome, the PE left Debug state: EDPRSR.SDR (Sticky
        # Debug Restart) latches until read -- the guide §5.5 confirmation.
        self.mem[debug_base + r52.EDPRSR] = (
            self.mem.get(debug_base + r52.EDPRSR, 0) | r52.EDPRSR_SDR
        )
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

        Fidelity limit worth knowing: measured 2026-08-04, a core halted by an
        out-of-reset EDBGREQ with EDSCR.HDE clear refused the register round
        trip on silicon, while a reset-catch halt (also HDE-clear) allowed it
        on the 2026-08-02 bench -- so a naive HDE gate here would be wrong in
        one direction or the other. The load flow sets HDE explicitly
        (loader P5), like halt() always has.
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
            if instruction == r52.instr_reg_to_dlr(rt, t32=self.accepts_t32):
                self.dlr[debug_base] = regs.get(rt, 0)
                return
            if instruction == r52.instr_reg_to_dspsr(rt, t32=self.accepts_t32):
                self.dspsr[debug_base] = regs.get(rt, 0)
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

    # -- the M33 behavioural model -------------------------------------------

    def _m33_armed_fp(self) -> bool:
        """Whether an enabled FP_COMP matches the M33's current PC."""
        fp_ctrl = self.mem.get(m33d.FP_CTRL, 0)
        if not fp_ctrl & m33d.FP_CTRL_ENABLE:
            return False
        for n in range(m33d.fp_num_code(fp_ctrl)):
            raw = self.mem.get(m33d.fp_comp(n), 0)
            if raw & m33d.FP_COMP_BE and (raw & ~1) == self.m33["pc"]:
                return True
        return False

    def _m33_write(self, addr: int, value: int) -> bool:
        """The M33 registers whose write side is not plain storage."""
        state = self.m33

        if addr == m33d.FP_CTRL:
            # NUM_CODE is read-only; ENABLE takes effect only with KEY set, and
            # a KEY-less write is discarded entirely -- both like silicon.
            if value & m33d.FP_CTRL_KEY:
                current = self.mem.get(m33d.FP_CTRL, 0)
                self.mem[m33d.FP_CTRL] = (current & ~m33d.FP_CTRL_ENABLE) | (
                    value & m33d.FP_CTRL_ENABLE
                )
            return True

        if addr == m33d.DHCSR:
            if (value >> 16) & 0xFFFF != 0xA05F:
                return True  # no DBGKEY: silently discarded, like silicon
            bits = value & 0x3F
            self.m33_dhcsr_writes.append(bits)
            previous = state["control"]
            # Guide §6.4: changing C_MASKINTS in the same write that clears
            # C_HALT is UNPREDICTABLE. Recorded, never guessed at.
            if (
                state["halted"]
                and not bits & m33d.DHCSR_C_HALT
                and (bits ^ previous) & m33d.DHCSR_C_MASKINTS
            ):
                self.m33_unpredictable.append(bits)
            state["control"] = bits
            if not bits & m33d.DHCSR_C_DEBUGEN:
                return True
            if bits & m33d.DHCSR_C_HALT:
                if not state["halted"]:
                    state["halted"] = True
                    self.mem[m33d.DFSR] = self.mem.get(m33d.DFSR, 0) | m33d.DFSR_HALTED
            elif state["halted"]:
                state["sticky"] |= m33d.DHCSR_S_RESTART_ST
                if bits & m33d.DHCSR_C_STEP:
                    if self._m33_armed_fp():
                        # Re-trigger before the instruction executes: PC does
                        # not move -- the case comparator suspension prevents.
                        self.mem[m33d.DFSR] = self.mem.get(m33d.DFSR, 0) | m33d.DFSR_BKPT
                    else:
                        state["pc"] += 2
                        state["sticky"] |= m33d.DHCSR_S_RETIRE_ST
                        self.mem[m33d.DFSR] = self.mem.get(m33d.DFSR, 0) | m33d.DFSR_HALTED
                elif self._m33_armed_fp():
                    state["sticky"] |= m33d.DHCSR_S_RETIRE_ST
                    self.mem[m33d.DFSR] = self.mem.get(m33d.DFSR, 0) | m33d.DFSR_BKPT
                else:
                    state["halted"] = False
            return True

        if addr == m33d.DCRSR:
            selector = value & 0x7F
            if value & (1 << 16):  # REGWNR: write
                target = self.mem.get(m33d.DCRDR, 0)
                if selector == 15:
                    state["pc"] = target
                else:
                    state["regs"][selector] = target
            else:
                source = state["pc"] if selector == 15 else state["regs"].get(selector, 0)
                self.mem[m33d.DCRDR] = source
            return True

        if addr == m33d.DFSR:
            # Write-1-to-clear, not storage.
            self.mem[m33d.DFSR] = self.mem.get(m33d.DFSR, 0) & ~value
            return True

        if addr == m33d.AIRCR:
            if value == m33d.AIRCR_SYSRESETREQ:
                state["sticky"] |= m33d.DHCSR_S_RESET_ST
                demcr = self.mem.get(m33d.DEMCR, 0)
                if demcr & m33d.DEMCR_VC_CORERESET and state["control"] & m33d.DHCSR_C_DEBUGEN:
                    state["halted"] = True
                    state["pc"] = 0x2500_0000  # the reset handler entry
                    self.mem[m33d.DFSR] = self.mem.get(m33d.DFSR, 0) | m33d.DFSR_VCATCH
                else:
                    state["halted"] = False
            return True

        count = m33d.fp_num_code(self.mem.get(m33d.FP_CTRL, 0))
        for n in range(count):
            if addr == m33d.fp_comp(n):
                self.fp_comp_writes.append((addr, value))
                return False  # observable, but stored normally
        return False

    # -- SDA-AP reset machinery ----------------------------------------------
    #
    # Modelled to the *measured* behaviour, not the guide's §5.10.3 claim: a
    # functional reset wipes the R52 debug state whether or not SDARSTMASK is
    # set (bench, 2026-08-02), the cold reset re-sets each core's OS lock, and
    # a core whose SDARSTCTRL RSTREL bit is 0 stays held in reset until the
    # bit is set again -- at which point an armed EDECR.RCE catches it.

    #: rtu0.0 / rtu0.1 -> their SDARSTCTRL release-bit masks.
    RSTREL_BITS = {CORE0_DEBUG: 1 << 31, CORE1_DEBUG: 1 << 30}

    def ap_reg_write(self, index: int, value: int) -> None:
        addr = self._addr(index)
        if addr == AP_SDA + lad.SDA_SYSRSTC:
            sysrsts = self.ap_space.get(AP_SDA + lad.SDA_SYSRSTS, 0)
            if value & lad.SDA_SYSRSTC_SYSFUNCREQ:
                self.ap_space[AP_SDA + lad.SDA_SYSRSTS] = (
                    sysrsts | lad.SDA_SYSRSTS_SYSTEM_RESET
                )
            else:
                self.ap_space[AP_SDA + lad.SDA_SYSRSTS] = (
                    sysrsts & ~lad.SDA_SYSRSTS_SYSTEM_RESET
                )
                self._functional_reset_released()
        if addr == AP_SDA + lad.SDA_RSTCTRL:
            previous = self.ap_space.get(AP_SDA + lad.SDA_RSTCTRL, 0)
            super().ap_reg_write(index, value)
            in_reset = self.ap_space.get(AP_SDA + lad.SDA_SYSRSTS, 0) & (
                lad.SDA_SYSRSTS_SYSTEM_RESET
            )
            if not in_reset:
                for debug_base, bit in self.RSTREL_BITS.items():
                    if value & bit and not previous & bit:
                        self._core_released(debug_base)
            return
        super().ap_reg_write(index, value)

    def _functional_reset_released(self) -> None:
        rstctrl = self.ap_space.get(
            AP_SDA + lad.SDA_RSTCTRL, lad.SDA_RSTCTRL_RESET_VALUE
        )
        for debug_base in self.core_frames:
            # The reset wipes the RTU debug state and re-sets the OS lock;
            # while locked, EDSCR faults (measured on rtu1.*).
            for n in range(8):
                self.mem[debug_base + r52.dbgbcr(n)] = 0
                self.mem[debug_base + r52.dbgwcr(n)] = 0
            self.mem[debug_base + r52.EDECR] = 0
            self._set_halted(debug_base, False)
            edprsr = self.mem.get(debug_base + r52.EDPRSR, 0) | r52.EDPRSR_OSLK
            held = not rstctrl & self.RSTREL_BITS.get(debug_base, 1)
            if held:
                edprsr |= r52.EDPRSR_R
                edprsr &= ~r52.EDPRSR_HALTED
            self.mem[debug_base + r52.EDPRSR] = edprsr
            self.faulting.add(debug_base + r52.EDSCR)

    def _core_released(self, debug_base: int) -> None:
        """An RSTREL bit went 0 -> 1: the held core leaves reset."""
        edprsr = self.mem.get(debug_base + r52.EDPRSR, 0) & ~r52.EDPRSR_R
        self.mem[debug_base + r52.EDPRSR] = edprsr
        if self.mem.get(debug_base + r52.EDECR, 0) & r52.EDECR_RCE:
            self.dlr[debug_base] = 0x0040_0000  # the reset vector
            self._set_status(debug_base, r52.EDSCR_STATUS_RESET_CATCH)
        else:
            self._set_halted(debug_base, False)
        # The OS lock (set by the cold reset) still gates EDSCR until OSLAR=0.
        self.mem[debug_base + r52.EDPRSR] = (
            self.mem.get(debug_base + r52.EDPRSR, 0) | r52.EDPRSR_OSLK
        )
        self.faulting.add(debug_base + r52.EDSCR)

    # -- transport surface the CLI reaches for with getattr -----------------

    def open(self) -> None:
        self.open_calls = getattr(self, "open_calls", 0) + 1
        self.closed = False

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

"""RTU bring-up tables for the `load` (RAM-load) flow -- every number cited.

Provenance
----------
The user-designated reference for RAM loading is the S32 Debug VS Code plugin
(``C:/veh_ctrl_tools/s32_debug_VScode_plugin``). Its working gta-probe flow
executes NXP's own init-action lists; the tables below transcribe them
**verbatim** -- same registers, same values, same order -- from:

* ``resources/scripts/s32z2e2/s32z2e2_init_sequence.py`` (NXP, BSD-3-Clause):
  ``_rtu_partition_0`` (:360), ``_rtu_partition_1`` (:494), ``_sram_c_init``
  (:54), ``_rtu_sram_init`` (:48), ``_smu_sram_init`` (:147), ``_ce_sram_init``
  (:158), ``_r52_sram_init`` (:173), ``_rtu_partition_0_cores`` (:409).
* ``resources/local/sramcInit.py`` on the ``feat/jlink-migration`` branch:
  the v2 edge-trigger-fixed D-bank ladder and its post-init witness words.
* ``resources/scripts/s32z2e2/s32z2e2_memory_regions.py`` (:106-113): the
  loadable RAM windows.

Register naming: the NXP S32E2 headers used by the application call the RTU0
partition **PRTN1** (``S32E2_MC_ME.h``: PRTN1_PCONF at +0x300, PRTN1_CORE0_ADDR
at +0x34C, PRTN1_CORE1_ADDR at +0x36C), so comments below use PRTN1 even where
older notes said "PRTN2". The addresses are what matter and they are identical.

Timeout units: the gta opcodes carry small integers ("20", "40", "200") that
gta interprets as milliseconds. Poll timeouts here keep those numbers but are
floored at 1 s -- a longer timeout only slows down *failure*, never success,
and this tool polls over USB round trips that gta's firmware did not pay.
"""

from __future__ import annotations

from dataclasses import dataclass

#: T32 `b .` park instruction pair.
R52_PARK_INSTR = 0xE7FE_E7FE

#: Where the plugin parks each core: the per-core AXI-F code window
#: (0x7990_0000 aliases CodeRAM 0x3210_0000 -- the image's first word).
#: Kept for reference; NOT used, for a measured reason: the J-Link DLL's
#: WriteMem resumes the halted core behind the debugger's back (2026-08-04,
#: EDPRSR.SDR set mid-load on every fast-path run), and a core resumed onto
#: THIS park is standing exactly where the image's first block lands -- it
#: escaped into the half-written image. The gta probe has no such side
#: effect, which is why the plugin could park on the image.
RTU0_C0_PLUGIN_PARK_ADDR = 0x7990_0000
RTU0_C1_PLUGIN_PARK_ADDR = 0x79A0_0000

#: The parks actually used: top of the D1 DataRAM bank -- ECC-initialised by
#: the P4 ladder (0x317C0000-0x317FFFFF), inside the loadable window map but
#: outside every LMA this or any RAM image writes (all file content lands in
#: CodeRAM 0x3210+), clear of the SRAMC witness words, word-aligned as
#: MC_ME_PRTN1_COREx_ADDR requires. A core the DLL resumes spins here,
#: harmlessly, for the whole load.
RTU0_C0_PARK_ADDR = 0x317F_FFF0
RTU0_C1_PARK_ADDR = 0x317F_FFE0

#: RTU0 GPR CFG_CORE -- bit 0 selects split-lock (1) vs lockstep (0). The
#: plugin's R52_0_0 configuration programs split-lock before the cores leave
#: reset (mode latches at reset exit, guide §5.11).
RTU0_GPR_CFG_CORE = 0x7612_0000

#: MC_ME PRTN1 core registers (S32E2_MC_ME.h; base 0x41900000).
MC_ME_CTL_KEY = 0x4190_0000
MC_ME_PRTN1_CORE0_ADDR = 0x4190_034C
MC_ME_PRTN1_CORE1_ADDR = 0x4190_036C

#: MC_RGM PRST_0[1] -- partition bit 0, core0 bit 1, core1 bit 2.
RGM_PRST0_1 = 0x4185_0048
RGM_PSTAT0_1 = 0x4185_0148

#: MC_RGM FRENTC -- functional-reset entry timeout control (MC_RGM base
#: 0x41850000). The NXP RTD ``Mcu_Init`` of a running application arms it with
#: FRET_EN=1, FRET_TIMEOUT=0 (``Power_Ip_PBcfg.c``; read live as 0x00000001 on
#: both the br1_rear and br1_cabin boards -- test-plan Finding 18). A debugger
#: functional reset taken with debug requests latched then fails to complete
#: inside the zero timeout, MC_RGM escalates it to a destructive reset, and
#: the JTAG-DP disappears ~200 ms into the reboot. `load` clears it before its
#: own reset (loader P0b, Finding 19); writing 0 is the bench-proven value.
RGM_FRENTC = 0x4185_0028
RGM_FRENTC_FRET_EN = 0x1


@dataclass(frozen=True)
class InitAction:
    """One step of a bring-up table. Ops mirror the gta primitives exactly:

    ``write32``  -> gta ``write_reg32``   (one checked 32-bit AXI write)
    ``rmw``      -> gta ``set_reg``       (read, set (value & mask) in mask, write)
    ``poll``     -> gta ``check_reg``     (read until (v & mask) == value)
    ``sleep``    -> gta ``pause``         (fixed delay)

    ``optional`` marks an action whose XRDC denial is tolerated with a note
    (measured on this bench: the debugger reaches the SRAMC controllers in the
    0x76xxxxxx slice but not the RTU GPR). ``fallback_addr`` retries a faulted
    access at an alias of the same physical location.
    """

    op: str
    addr: int = 0
    value: int = 0
    mask: int = 0xFFFF_FFFF
    timeout_ms: int = 0
    label: str = ""
    optional: bool = False
    fallback_addr: int | None = None


def _w(addr: int, value: int, label: str = "") -> InitAction:
    return InitAction("write32", addr=addr, value=value, label=label)


def _rmw(addr: int, mask: int, value: int, label: str = "") -> InitAction:
    return InitAction("rmw", addr=addr, mask=mask, value=value, label=label)


def _poll(addr: int, mask: int, value: int, timeout_ms: int, label: str = "") -> InitAction:
    return InitAction("poll", addr=addr, mask=mask, value=value, timeout_ms=timeout_ms, label=label)


def _sleep(ms: int) -> InitAction:
    return InitAction("sleep", timeout_ms=ms)


def _me_key() -> tuple[InitAction, ...]:
    """The MC_ME hardware-process trigger: direct key then inverted key."""
    return (
        _w(MC_ME_CTL_KEY, 0x5AF0, "MC_ME key (direct)"),
        _w(MC_ME_CTL_KEY, 0xA50F, "MC_ME key (inverted)"),
    )


# ---------------------------------------------------------------------------
# RTU partition bring-up -- init_sequence.py `_rtu_partition_0` / `_rtu_partition_1`
# ---------------------------------------------------------------------------

ACTIONS_RTU0_PARTITION: tuple[InitAction, ...] = (
    _rmw(0x4190_0300, 0x1, 0x1, "PRTN1_PCONF.PCE"),
    _rmw(0x4190_0304, 0x1, 0x1, "PRTN1_PUPD.PCUD"),
    *_me_key(),
    _sleep(20),
    _poll(0x4190_0304, 0x1, 0x0, 40, "PRTN1_PUPD.PCUD clears"),
    _poll(0x4190_0308, 0x1, 0x1, 40, "PRTN1_STAT.PCS set"),
    _rmw(0x4189_0004, 0x8000_0000, 0x8000_0000, "RDC_0 RD1_CTRL unlock"),
    _rmw(0x4189_0004, 0x8, 0x0, "RDC_0 XBAR interface enable"),
    _poll(0x4189_0084, 0x10, 0x0, 40, "RDC_0 XBAR interface enabled"),
    _rmw(RGM_PRST0_1, 0x1, 0x0, "RGM PRST partition reset lift"),
    _rmw(0x4190_0300, 0x4, 0x0, "PRTN1_PCONF.OSSE clear"),
    _rmw(0x4190_0304, 0x4, 0x4, "PRTN1_PUPD.OSSUD"),
    *_me_key(),
    _sleep(20),
    _poll(0x4190_0304, 0x4, 0x0, 40, "PRTN1_PUPD.OSSUD clears"),
    _poll(0x4190_0308, 0x4, 0x0, 40, "PRTN1_STAT.OSSS clear"),
    _poll(RGM_PRST0_1, 0x1, 0x0, 40, "partition reset clear (plugin reads PRST)"),
    _rmw(0x4189_0004, 0x8000_0000, 0x0, "RDC_0 RD1_CTRL lock"),
    _w(0x4186_005C, 0x2, "RTU0 fence/drain assert"),
    _w(0x4186_005C, 0x0, "RTU0 fence/drain release"),
)

ACTIONS_RTU1_PARTITION: tuple[InitAction, ...] = (
    _rmw(0x4190_0500, 0x1, 0x1, "PRTN2_PCONF.PCE"),
    _rmw(0x4190_0504, 0x1, 0x1, "PRTN2_PUPD.PCUD"),
    *_me_key(),
    _sleep(20),
    _poll(0x4190_0504, 0x1, 0x0, 40, "PRTN2_PUPD.PCUD clears"),
    _poll(0x4190_0508, 0x1, 0x1, 40, "PRTN2_STAT.PCS set"),
    _rmw(0x418A_0004, 0x8000_0000, 0x8000_0000, "RDC_1 CTRL unlock"),
    _rmw(0x418A_0004, 0x8, 0x0, "RDC_1 XBAR interface enable"),
    _poll(0x418A_0084, 0x10, 0x0, 40, "RDC_1 XBAR interface enabled"),
    _rmw(0x4185_0050, 0x1, 0x0, "RGM PRST partition reset lift"),
    _rmw(0x4190_0500, 0x4, 0x0, "PRTN2_PCONF.OSSE clear"),
    _rmw(0x4190_0504, 0x4, 0x4, "PRTN2_PUPD.OSSUD"),
    *_me_key(),
    _sleep(20),
    _poll(0x4190_0504, 0x4, 0x0, 40, "PRTN2_PUPD.OSSUD clears"),
    _poll(0x4190_0508, 0x4, 0x0, 40, "PRTN2_STAT.OSSS clear"),
    _poll(0x4185_0050, 0x1, 0x0, 40, "partition reset clear"),
    _rmw(0x418A_0004, 0x8000_0000, 0x0, "RDC_1 CTRL lock"),
    _w(0x4186_0064, 0x2, "RTU1 fence/drain assert"),
    _w(0x4186_0064, 0x0, "RTU1 fence/drain release"),
    _w(0x7540_0000, 0x2, "RTU1 NIC"),
    _w(0x7550_0000, 0x2, "RTU1 NIC"),
    _w(0x7560_0000, 0x2, "RTU1 NIC"),
    _w(0x7570_0000, 0x2, "RTU1 NIC"),
)


# ---------------------------------------------------------------------------
# NXP generic SRAM scrubs -- init_sequence.py `_prologue` order:
# _smu_sram_init, _ce_sram_init, _r52_sram_init(0), _r52_sram_init(1).
# The `_sram_c_init` pattern is transcribed as-is, including its 0x0000-0xFFFF
# range -- the known-wrong-range NXP quirk the v2 D-bank ladder later corrects
# (its RAMCR=1 leftover is exactly why the v2 ladder writes RAMCR=0 first).
# ---------------------------------------------------------------------------


def _sram_c_init(base: int, name: str) -> tuple[InitAction, ...]:
    return (
        _w(base + 0xC, 0x1, f"{name} RAMSR: clear IDONE (W1C)"),
        _w(base + 0x4, 0x0000_0000, f"{name} RAMIAS"),
        _w(base + 0x8, 0x0000_FFFF, f"{name} RAMIAE"),
        _w(base + 0x0, 0x1, f"{name} RAMCR.INITREQ"),
        _poll(base + 0xC, 0x1, 0x1, 200, f"{name} RAMSR.IDONE"),
    )


def _rtu_sram_init(base: int, rammaxa: int, name: str) -> tuple[InitAction, ...]:
    return (_w(base + 0x40, rammaxa, f"{name} RAMMAXA"), *_sram_c_init(base, name))


_SMU_SRAMC = (
    (0x4526_0000, "SMU_SRAMCTL_0"),
    (0x4526_4000, "SMU_SRAMCTL_1"),
    (0x4526_8000, "SMU_SRAMCTL_2"),
    (0x4526_C000, "SMU_SRAMCTL_3"),
)
_CE_SRAMC = (
    (0x44A6_0000, "CE_SRAMCTL_0"),
    (0x44A6_4000, "CE_SRAMCTL_1"),
    (0x44A6_8000, "CE_SRAMCTL_2"),
)
_RTU0_SRAMC = (
    (0x760C_0000, 0x799F_FFFF, "RTU0_SRAMCTL_C0"),
    (0x760D_0000, 0x79AF_FFFF, "RTU0_SRAMCTL_C1"),
    (0x760E_0000, 0x79BF_FFFF, "RTU0_SRAMCTL_C2"),
    (0x760F_0000, 0x79CF_FFFF, "RTU0_SRAMCTL_C3"),
    (0x7624_0000, 0x79DF_FFFF, "RTU0_SRAMCTL_C4"),
    (0x7625_0000, 0x79EF_FFFF, "RTU0_SRAMCTL_C5"),
    (0x7626_0000, 0x79FF_FFFF, "RTU0_SRAMCTL_C6"),
    (0x761D_0000, 0x317B_FFFF, "RTU0_SRAMCTL_D0"),
    (0x761E_0000, 0x317F_FFFF, "RTU0_SRAMCTL_D1"),
    (0x761F_0000, 0x3187_FFFF, "RTU0_SRAMCTL_D2"),
)
_RTU1_SRAMC = (
    (0x768C_0000, 0x7D9F_FFFF, "RTU1_SRAMCTL_C0"),
    (0x768D_0000, 0x7DAF_FFFF, "RTU1_SRAMCTL_C1"),
    (0x768E_0000, 0x7DBF_FFFF, "RTU1_SRAMCTL_C2"),
    (0x768F_0000, 0x7DCF_FFFF, "RTU1_SRAMCTL_C3"),
    (0x76A4_0000, 0x7DDF_FFFF, "RTU1_SRAMCTL_C4"),
    (0x76A5_0000, 0x7DEF_FFFF, "RTU1_SRAMCTL_C5"),
    (0x76A6_0000, 0x7DFF_FFFF, "RTU1_SRAMCTL_C6"),
    (0x769D_0000, 0x357B_FFFF, "RTU1_SRAMCTL_D0"),
    (0x769E_0000, 0x357F_FFFF, "RTU1_SRAMCTL_D1"),
    (0x769F_0000, 0x3587_FFFF, "RTU1_SRAMCTL_D2"),
)

ACTIONS_GENERIC_SRAM: tuple[InitAction, ...] = (
    *(a for base, name in _SMU_SRAMC for a in _sram_c_init(base, name)),
    *(a for base, name in _CE_SRAMC for a in _sram_c_init(base, name)),
    *(a for base, maxa, name in _RTU0_SRAMC for a in _rtu_sram_init(base, maxa, name)),
    *(a for base, maxa, name in _RTU1_SRAMC for a in _rtu_sram_init(base, maxa, name)),
)


# ---------------------------------------------------------------------------
# Core bring-up -- init_sequence.py `_rtu_partition_0_cores(False, "0")` and the
# core-1 continuation the same function runs for "1".
# ---------------------------------------------------------------------------

ACTIONS_RTU0_CORE0: tuple[InitAction, ...] = (
    # The plugin selects split-lock here. Measured 2026-08-04 on this bench:
    # XRDC denies the debugger the RTU0 GPR at 0x76120000 (while allowing the
    # SRAMC controllers in the same slice), so the write cannot land from the
    # J-Link. Tolerated as optional: the register then keeps its reset default
    # -- LOCKSTEP (guide §5.11), which is exactly the mode this ECU boots with
    # naturally (the firmware never writes CFG_CORE; BrsHw.c verified). Cores
    # 0 and 1 exist in both modes; split-lock only adds cores 2/3 that
    # nothing here uses.
    InitAction(
        "rmw", addr=RTU0_GPR_CFG_CORE, mask=0x1, value=0x1,
        label="RTU0 GPR CFG_CORE: split-lock (optional; lockstep default on denial)",
        optional=True,
    ),
    # Park in DataRAM (see RTU0_C0_PARK_ADDR above), not in the plugin's
    # image-aliasing code window; the write path to the D banks is proven by
    # the P4 ladder that runs just before this.
    _w(RTU0_C0_PARK_ADDR, R52_PARK_INSTR, "park instruction at core0 boot"),
    _w(MC_ME_PRTN1_CORE0_ADDR, RTU0_C0_PARK_ADDR, "PRTN1_CORE0_ADDR = park"),
    _rmw(RGM_PRST0_1, 0x2, 0x0, "RGM: lift core0 reset"),
    _w(0x4190_0340, 0x1, "PRTN1_CORE0_PCONF.CCE"),
    _rmw(0x4190_0344, 0x1, 0x1, "PRTN1_CORE0_PUPD.CCUPD"),
    *_me_key(),
    _sleep(20),
    _poll(0x4190_0344, 0x1, 0x0, 40, "CORE0 CCUPD clears"),
    _poll(0x4190_0348, 0x1, 0x1, 40, "CORE0_STAT.CCS set"),
    _poll(RGM_PSTAT0_1, 0x2, 0x0, 40, "core0 reset lifted (PSTAT)"),
    _sleep(10),
)

#: Finding 19 retry: put core 0 back into reset so a re-armed EDBGREQ latch
#: catches it on the way out again, exactly as the first pass did. PRST_0[1]
#: bit 1 is core 0 -- the same per-core reset the application's own
#: BrsHwEarlyInitPowerOn asserts on core 1 through bit 2 -- and PSTAT mirrors
#: it. Run only when the park table above failed under a vanished debug hold.
ACTIONS_RTU0_CORE0_RESET: tuple[InitAction, ...] = (
    _rmw(RGM_PRST0_1, 0x2, 0x2, "RGM: put core0 back into reset"),
    _poll(RGM_PSTAT0_1, 0x2, 0x2, 40, "core0 in reset (PSTAT)"),
)

#: Only the continuation for core 1 -- the plugin's `"0" in core_nr` early
#: return means these run *after* the core-0 block. Note the plugin does no
#: CCE/PUPD for core 1 here, and writes the boot address before the park word.
ACTIONS_RTU0_CORE1: tuple[InitAction, ...] = (
    _w(MC_ME_PRTN1_CORE1_ADDR, RTU0_C1_PARK_ADDR, "PRTN1_CORE1_ADDR = park"),
    _w(RTU0_C1_PARK_ADDR, R52_PARK_INSTR, "park instruction at core1 boot"),
    _rmw(RGM_PRST0_1, 0x4, 0x0, "RGM: lift core1 reset"),
    _poll(RGM_PSTAT0_1, 0x4, 0x0, 40, "core1 reset lifted (PSTAT)"),
    _sleep(10),
)


# ---------------------------------------------------------------------------
# SRAMC v2 ladder -- feat/jlink-migration resources/local/sramcInit.py,
# verbatim, RAMCR=0 first per controller: RAMCR.INIT is EDGE-triggered
# (root-caused on target 2026-07-03) and the generic scrub above leaves INIT=1,
# so without the 0-write this whole table is a silent no-op with
# perfect-looking registers -- and DataRAM ECC stays uninitialized, which is
# the documented Os_Hal_CoreFreeze panic. Mirrors the firmware's own
# ARMStartup_CortexR52.c:242-296 values (RAMCR 0x101 = INIT | INIT_SYSA).
# ---------------------------------------------------------------------------

SRAMC_V2_WRITES: tuple[tuple[int, int], ...] = (
    (0x761D_0000, 0x0000_0000),
    (0x761D_0008, 0x317B_FFFF),
    (0x761D_0004, 0x3178_0000),
    (0x761D_0000, 0x0000_0101),
    (0x761E_0000, 0x0000_0000),
    (0x761E_0008, 0x317F_FFFF),
    (0x761E_0004, 0x317C_0000),
    (0x761E_0000, 0x0000_0101),
    (0x761F_0000, 0x0000_0000),
    (0x761F_0008, 0x3187_FFFF),
    (0x761F_0004, 0x3180_0000),
    (0x761F_0000, 0x0000_0101),
)
SRAMC_V2_STATUS_ADDRS: tuple[int, ...] = (0x761D_000C, 0x761E_000C, 0x761F_000C)
#: Words that MUST read 0 after a REAL init (the Os SuspendIntCounters trap
#: word per D-bank; never written by load or startup until much later).
SRAMC_V2_VERIFY_ADDRS: tuple[int, ...] = (0x3178_E1A0, 0x317C_0010, 0x3180_0010)


# ---------------------------------------------------------------------------
# Loadable RAM windows -- memory_regions.py:106-113 (r0_fast_data0/1,
# r0_extended, r0_code0_6). An ELF whose LMA falls outside is refused before a
# single write: it is linked for flash or another part, and writing it "works"
# right up until nothing runs.
# ---------------------------------------------------------------------------

LOADABLE_WINDOWS: tuple[tuple[int, int, str], ...] = (
    (0x3178_0000, 0x317F_FFFF, "RTU0 DataRAM (fast_data0+1)"),
    (0x3180_0000, 0x319F_FFFF, "RTU0 extended DataRAM"),
    (0x3210_0000, 0x327F_FFFF, "RTU0 CodeRAM (code0_6)"),
)

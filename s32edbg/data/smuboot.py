"""SMU M33 bring-up table for the flash flow -- every number cited.

Provenance
----------
The user-designated reference for external-flash work is s32multitool
(``C:/s32multitool``), which drives an NXP S32 Debug Probe through CCS/GTA/GDB
and can flash a board whose QSPI NOR is completely blank. It can because its
connect sequence boots the SMU itself, flash-independent: after the
EDBGREQ-latched reset and the SRAM scrubs it writes a two-word spin stub plus a
vector table into SMU SRAM and releases the core through MC_ME partition 0.
The table below transcribes that step -- ``_m33_smu()`` -- **verbatim**: same
registers, same values, same order, from the NXP runtime s32multitool ships in
``ext/nxp_s32ds_3_6_1_debugger_runtime.nxp`` (extracted:
``tools/S32Debugger/Debugger/scripts/s32z2e2/s32z2e2_init_sequence.py:210-250``,
NXP, BSD-3-Clause).

The surrounding steps of that connect sequence are NOT re-transcribed here
because this repo already carries them verbatim from the same NXP file:

* ``_enable_debug()`` / ``_reset_to_first_instruction()`` MDM writes ==
  ``ladder1.LADDER1`` + ``gates.arm_edbgreq`` (byte-identical values);
* ``_rtu_partition_0/1()`` == ``rtuboot.ACTIONS_RTU0/RTU1_PARTITION``;
* ``_smu_sram_init``/``_ce_sram_init``/``_r52_sram_init`` ==
  ``rtuboot.ACTIONS_GENERIC_SRAM`` (same SMU -> CE -> RTU0 -> RTU1 order).

The SMU comes out of the MC_ME release already halted at the stub because the
MDM EDBGREQ latch (CONTROL bit 8) is still set from the reset; the flash flow
then pins it by DHCSR and drops the latch (``gates.clear_smu_edbgreq``).

Address overlap with the QSPI applet -- deliberate, do not "fix"
----------------------------------------------------------------
``SMU_VECTOR_BASE`` (0x25002000) is byte-identical to
``qspiflash.APPLET_STATUS``, and the zeroed stack words at 0x25004000 fall
inside ``qspiflash.APPLET_DATA``. Benign by ordering: this table's writes land
first and the flasher clobbers them later -- s32multitool itself uses exactly
this memory map (its raw driver stages the applet mailbox over the same words).
Pinned by ``tests/test_flash.py`` so a future applet-map move is noticed.

Timeout units: the gta opcodes carry small integers ("20", "40") that gta
interprets as milliseconds. Poll timeouts here keep those numbers but are
floored at 1 s by the executor -- a longer timeout only slows down *failure*,
never success, and this tool polls over USB round trips that gta's firmware
did not pay (same convention as ``rtuboot``).
"""

from __future__ import annotations

from .rtuboot import MC_ME_CTL_KEY, InitAction

#: PRTN0_CORE0_ADDR target: the debugger-written vector table in SMU SRAM.
#: == qspiflash.APPLET_STATUS -- see the module docstring's overlap note.
SMU_VECTOR_BASE = 0x2500_2000

#: The two-word boot stub the vector table points at.
SMU_BOOT_STUB = 0x2500_2400

#: Initial SP the vector table carries; the four words below it are zeroed.
SMU_STACK_TOP = 0x2500_4000

#: NXP's comment for this write, verbatim: "Release Cortex-M33 core from WAIT
#: state". The containing block is not named in this repo's RM extracts; the
#: address and value are carried as-is.
SMU_CORE_WAIT_RELEASE = 0x4106_004C

#: MC_ME partition-0 core-0 registers (base 0x41900000, same block as
#: rtuboot.MC_ME_CTL_KEY and the PRTN1/PRTN2 tables).
MC_ME_PRTN0_CORE0_PCONF = 0x4190_0140
MC_ME_PRTN0_CORE0_PUPD = 0x4190_0144
MC_ME_PRTN0_CORE0_STAT = 0x4190_0148
MC_ME_PRTN0_CORE0_ADDR = 0x4190_014C


def _w(addr: int, value: int, label: str = "") -> InitAction:
    return InitAction("write32", addr=addr, value=value, label=label)


def _rmw(addr: int, mask: int, value: int, label: str = "") -> InitAction:
    return InitAction("rmw", addr=addr, mask=mask, value=value, label=label)


def _poll(addr: int, mask: int, value: int, timeout_ms: int, label: str = "") -> InitAction:
    return InitAction("poll", addr=addr, mask=mask, value=value, timeout_ms=timeout_ms, label=label)


def _sleep(ms: int) -> InitAction:
    return InitAction("sleep", timeout_ms=ms)


#: ``_m33_smu()`` verbatim. The two MC_ME key writes are spelled out rather
#: than shared with rtuboot's ``_me_key()`` so the table reads exactly like
#: the reference sequence.
ACTIONS_SMU_BOOT: tuple[InitAction, ...] = (
    _w(SMU_BOOT_STUB, 0xF800_F000, "SMU boot stub word 0"),
    _w(SMU_BOOT_STUB + 0x4, 0xE7FE_E7FE, "SMU boot stub word 1 (b .)"),
    _w(SMU_VECTOR_BASE, SMU_STACK_TOP, "vector[0]: initial SP"),
    _w(SMU_VECTOR_BASE + 0x04, 0x2500_2401, "vector[1]: reset PC -> stub (Thumb)"),
    _w(SMU_VECTOR_BASE + 0x08, 0x2500_2401, "vector[2] -> stub"),
    _w(SMU_VECTOR_BASE + 0x0C, 0x2500_2401, "vector[3] -> stub"),
    _w(SMU_VECTOR_BASE + 0x10, 0x2500_2401, "vector[4] -> stub"),
    _w(SMU_VECTOR_BASE + 0x14, 0x2500_2401, "vector[5] -> stub"),
    _w(SMU_VECTOR_BASE + 0x18, 0x2500_2401, "vector[6] -> stub"),
    _w(SMU_STACK_TOP, 0x0, "zero stack word 0"),
    _w(SMU_STACK_TOP + 0x4, 0x0, "zero stack word 1"),
    _w(SMU_STACK_TOP + 0x8, 0x0, "zero stack word 2"),
    _w(SMU_STACK_TOP + 0xC, 0x0, "zero stack word 3"),
    _w(SMU_CORE_WAIT_RELEASE, 0x0, "'Release Cortex-M33 core from WAIT state' (NXP)"),
    _w(MC_ME_PRTN0_CORE0_ADDR, SMU_VECTOR_BASE, "PRTN0_CORE0_ADDR = vector table"),
    _w(MC_ME_PRTN0_CORE0_PCONF, 0x1, "PRTN0_CORE0_PCONF.CCE"),
    _rmw(MC_ME_PRTN0_CORE0_PUPD, 0x1, 0x1, "PRTN0_CORE0_PUPD.CCUPD"),
    _w(MC_ME_CTL_KEY, 0x5AF0, "MC_ME key (direct)"),
    _w(MC_ME_CTL_KEY, 0xA50F, "MC_ME key (inverted)"),
    _sleep(20),
    _poll(MC_ME_PRTN0_CORE0_PUPD, 0x1, 0x0, 40, "PRTN0 CORE0 CCUPD clears"),
    _poll(MC_ME_PRTN0_CORE0_STAT, 0x1, 0x1, 40, "PRTN0 CORE0_STAT.CCS set"),
    _sleep(10),
)

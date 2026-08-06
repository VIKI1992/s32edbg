"""The SDA-AP write policy, which is deliberately narrow.

RM 135.13.4 makes the HSE challenge/response one-shot: "HSE allows one only
attempt for C/R comparison. If it fails, no further attempts are accepted." That
is why the authentication path is permanently forbidden. SDASYSRSTC is a
different register -- RM 135.11 calls it the debugger's own reset control -- and
it is the only software route onto a board that will not stay still.

No hardware required.
"""

from __future__ import annotations

import pytest
from conftest import FakeTransport

from s32edbg.dap import gates
from s32edbg.dap.adiv6 import Dap
from s32edbg.data.expected_apmap import AP_APB, AP_MDM, AP_SDA
from s32edbg.data.ladder1 import (
    SDA_AUTHCTL,
    SDA_SYSRSTC,
    SDA_SYSRSTC_SYSFUNCREQ,
)
from s32edbg.errors import UnsafeOperation


def test_the_authentication_register_is_still_refused() -> None:
    """The one-shot path RM 135.13.4 warns about stays permanently closed."""
    with pytest.raises(UnsafeOperation, match="SDA-AP"):
        gates.guard_write(AP_SDA, SDA_AUTHCTL)


def test_an_unspecified_sda_write_is_still_refused() -> None:
    """A caller that cannot say which register it means gets the old blanket
    refusal, so the narrowing cannot be used as a loophole."""
    with pytest.raises(UnsafeOperation):
        gates.guard_write(AP_SDA)


def test_the_reset_control_register_is_permitted() -> None:
    gates.guard_write(AP_SDA, SDA_SYSRSTC)  # must not raise


def test_the_refusal_names_which_registers_are_permitted() -> None:
    with pytest.raises(UnsafeOperation) as exc:
        gates.guard_write(AP_SDA, SDA_AUTHCTL)
    assert f"0x{SDA_SYSRSTC:03X}" in str(exc.value)


def test_other_aps_are_unaffected_by_the_narrowing() -> None:
    gates.guard_write(AP_MDM, 0x000)
    gates.guard_write(AP_APB, 0x000)


# ---------------------------------------------------------------------------
# Holding and releasing
# ---------------------------------------------------------------------------


def test_holding_asserts_only_the_functional_reset_bit() -> None:
    """SYSFUNCREQ holds the system; SYSRESTREQ would be a *destructive* reset
    that drops the debug connection entirely (RM 135.11). Only the former."""
    t = FakeTransport(pipelined=False)
    gates.hold_in_functional_reset(Dap(t))
    assert t.ap_space[AP_SDA + SDA_SYSRSTC] == SDA_SYSRSTC_SYSFUNCREQ


def test_releasing_clears_the_hold() -> None:
    t = FakeTransport(pipelined=False)
    dap = Dap(t)
    gates.hold_in_functional_reset(dap)
    gates.release_functional_reset(dap)
    assert t.ap_space[AP_SDA + SDA_SYSRSTC] == 0


def test_neither_hold_nor_release_ever_issues_a_destructive_reset() -> None:
    """A destructive reset would lose the connection, which defeats the point."""
    from s32edbg.data.ladder1 import SDA_SYSRSTC_SYSRESTREQ

    t = FakeTransport(pipelined=False)
    dap = Dap(t)
    gates.hold_in_functional_reset(dap)
    gates.release_functional_reset(dap)
    for _op, _index, addr in t.log:
        if addr == AP_SDA + SDA_SYSRSTC:
            written = t.ap_space.get(addr, 0)
            assert not written & SDA_SYSRSTC_SYSRESTREQ


def test_holding_touches_no_register_but_the_reset_control() -> None:
    t = FakeTransport(pipelined=False)
    gates.hold_in_functional_reset(Dap(t))
    written = {addr for op, _i, addr in t.log if op == "ap_write"}
    assert written == {AP_SDA + SDA_SYSRSTC}


# ---------------------------------------------------------------------------
# EDBGREQ latching for the RAM-load flow
# ---------------------------------------------------------------------------


def test_arm_edbgreq_writes_the_plugin_values_to_the_three_control_registers() -> None:
    """Verbatim replication of the reference plugin's _reset_to_first_instruction
    MDM writes (its write_mdm index*4 == the RM 135.10.1.x offsets)."""
    from s32edbg.data.ladder1 import (
        MDM_CONTROL,
        MDM_CONTROL2,
        MDM_CONTROL3,
        MDM_CONTROL_LOAD_ASSERT,
    )

    t = FakeTransport(pipelined=False)
    gates.arm_edbgreq(Dap(t))
    assert t.ap_space[AP_MDM + MDM_CONTROL] == MDM_CONTROL_LOAD_ASSERT == 0x6080_0700
    assert t.ap_space[AP_MDM + MDM_CONTROL2] == 0x000F_0000
    assert t.ap_space[AP_MDM + MDM_CONTROL3] == 0x000F_0000


def test_clear_core_edbgreq_is_a_true_read_modify_write() -> None:
    """Only the target core's request bit drops; the other cores' pending
    requests survive. Core 0 = bit 16 (guide §1.5, CR52_RTU0_{3..0} bits 19:16)."""
    from s32edbg.data.ladder1 import MDM_CONTROL2

    t = FakeTransport(pipelined=False)
    t.ap_space[AP_MDM + MDM_CONTROL2] = 0x000F_0000
    gates.clear_core_edbgreq(Dap(t), core=0)
    assert t.ap_space[AP_MDM + MDM_CONTROL2] == 0x000E_0000


def test_clear_core_edbgreq_rejects_an_impossible_core_index() -> None:
    with pytest.raises(ValueError, match="not 0-3"):
        gates.clear_core_edbgreq(Dap(FakeTransport(pipelined=False)), core=4)


def test_clear_smu_edbgreq_is_the_multitool_exact_rmw() -> None:
    """s32multitool's M33_0 de-assert, verbatim (s32z2e2_cores.py
    MDM_AP_DESC): RMW CONTROL clear 0x1000_0100. Bit 8 (the SMU latch) and
    the undocumented bit 28 drop; the CE latches (bits 10:9), MSK_DBG_FAULT
    and bits 30:29 survive; CONTROL2/CONTROL3 are untouched."""
    from s32edbg.data.ladder1 import MDM_CONTROL, MDM_CONTROL2, MDM_CONTROL3

    t = FakeTransport(pipelined=False)
    t.ap_space[AP_MDM + MDM_CONTROL] = 0x7080_0700
    t.ap_space[AP_MDM + MDM_CONTROL2] = 0x000F_0000
    t.ap_space[AP_MDM + MDM_CONTROL3] = 0x000F_0000
    gates.clear_smu_edbgreq(Dap(t))
    assert t.ap_space[AP_MDM + MDM_CONTROL] == 0x6080_0600
    assert t.ap_space[AP_MDM + MDM_CONTROL2] == 0x000F_0000
    assert t.ap_space[AP_MDM + MDM_CONTROL3] == 0x000F_0000

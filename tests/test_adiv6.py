"""ADIv6 DP/AP composition tests against a fake transport."""

from __future__ import annotations

import pytest
from conftest import FakeTransport

from s32edbg.dap import adiv6
from s32edbg.dap.adiv6 import Dap, twin_ap_l1
from s32edbg.data.expected_apmap import AP_MDM, DEBUG_SYSTEM_CORE_BASE
from s32edbg.data.ladder1 import MDM_IDR_EXPECTED
from s32edbg.errors import DapError, PowerUpError


def test_dp_address0_is_refused_over_jtag() -> None:
    """ADIv6 Figure A1-2 makes JTAG-DP DPACC address 0b00 Reserved.

    DPIDR, DPIDR1 and BASEPTR0/1 all live at DP address 0x0 and are selected by
    DPBANKSEL, so over JTAG none of them is reachable. Reading anyway does not
    fault -- it returns a plausible value that is not the register asked for. On
    silicon that was 0x4BA07477 against a real DPIDR of 0x4C013477, close enough
    to pass a glance, and it propagated into DPIDR1, BASEPTR and from there into
    the AP-space ROM walk.
    """
    from s32edbg.transport.base import Tif

    t = FakeTransport(pipelined=False)
    t.selected_tif = Tif.JTAG
    dap = Dap(t)

    assert not dap.dp_address0_readable
    with pytest.raises(DapError, match="Figure A1-2"):
        dap.read_dpidr()
    with pytest.raises(DapError, match="Figure A1-2"):
        dap.read_dpidr1()


def test_baseptr_is_absent_rather_than_wrong_over_jtag() -> None:
    """None, not an exception, because every caller already falls back to
    APBIC1_ROM_BASE when BASEPTR is absent. A *wrong* pointer instead sent the
    ROM walk to a garbage address -- `romdump --source apspace` and `report`
    both failed that way with "0x4BA07000 is not a ROM table"."""
    from s32edbg.transport.base import Tif

    t = FakeTransport(pipelined=False)
    t.selected_tif = Tif.JTAG
    assert Dap(t).read_baseptr() is None


def test_info_reports_identity_as_unavailable_over_jtag_but_still_reads_ctrl_stat() -> None:
    """CTRL/STAT is at DP address 0x4, which stays readable on both protocols.
    So even when identity is unreachable there is still evidence about whether
    the DAP is alive -- which is the question `connect` exists to answer."""
    from s32edbg.transport.base import Tif

    t = FakeTransport(pipelined=False)
    t.selected_tif = Tif.JTAG
    t.ctrl_stat = 0xF000_0000
    info = Dap(t).info()

    assert info.dpidr is None
    assert info.dpidr1 is None
    assert info.baseptr is None
    assert info.ctrl_stat == 0xF000_0000
    assert info.unavailable and "Figure A1-2" in info.unavailable


def test_dp_address0_is_readable_over_swd() -> None:
    """The suppression has to be interface-aware. Over SWD, DPACC address 0b00
    *is* DPIDR, so refusing there would break the protocol the switch exists to
    reach."""
    from s32edbg.transport.base import Tif

    t = FakeTransport(pipelined=False)
    t.selected_tif = Tif.SWD
    assert Dap(t).dp_address0_readable
    assert Dap(t).read_dpidr().raw == 0


def test_dp_address0_is_readable_when_no_interface_was_selected() -> None:
    """An unknown interface is not a reason to refuse a read that may be valid.
    This also keeps every existing test double working without opting in."""
    assert Dap(FakeTransport(pipelined=False)).dp_address0_readable


def test_ap_read_composes_select_and_apacc_index() -> None:
    t = FakeTransport(pipelined=False)
    t.ap_space[AP_MDM + adiv6.AP_IDR] = MDM_IDR_EXPECTED
    dap = Dap(t)
    assert dap.ap_read(AP_MDM, adiv6.AP_IDR) == MDM_IDR_EXPECTED

    writes = [e for e in t.log if e[0] == "dp_write" and e[1] == 2]
    assert writes, "SELECT was never written"
    # SELECT holds address bits [31:4]; DPBANKSEL (bits[3:0]) must be 0 so that
    # DPIDR/CTRL-STAT stay visible at DP 0x0/0x4.
    assert writes[-1][2] == ((AP_MDM + adiv6.AP_IDR) & 0xFFFF_FFF0)
    reads = [e for e in t.log if e[0] == "ap_read"]
    # IDR at 0xDFC is the 4th word of its 4-word bank -> APACC A[3:2] = 3
    assert reads[-1][1] == 3


def test_ap_read_bank_selection_for_each_word_in_bank() -> None:
    t = FakeTransport(pipelined=False)
    dap = Dap(t)
    for offset, want_index in ((0xDF0, 0), (0xDF4, 1), (0xDF8, 2), (0xDFC, 3)):
        t.log.clear()
        dap.ap_read(AP_MDM, offset)
        assert [e for e in t.log if e[0] == "ap_read"][-1][1] == want_index


def test_pipelined_ap_read_uses_rdbuff() -> None:
    t = FakeTransport(pipelined=True)
    t.ap_space[AP_MDM + adiv6.AP_IDR] = MDM_IDR_EXPECTED
    dap = Dap(t)
    assert dap.ap_read(AP_MDM, adiv6.AP_IDR) == MDM_IDR_EXPECTED
    assert any(e[0] == "dp_read" and e[1] == 3 for e in t.log), "RDBUFF was not read"


def test_calibrate_ap_read_finds_the_right_model() -> None:
    """The pipeline model is discovered, not assumed."""
    t = FakeTransport(pipelined=True)
    t.ap_space[AP_MDM + adiv6.AP_IDR] = MDM_IDR_EXPECTED
    dap = Dap(t)
    dap.ap_read_pipelined = False  # start with the wrong model
    assert dap.calibrate_ap_read(AP_MDM, MDM_IDR_EXPECTED)
    assert dap.ap_read_pipelined is True


def test_calibrate_reports_failure_when_idr_never_matches() -> None:
    t = FakeTransport(pipelined=False)  # AP space empty -> reads 0
    dap = Dap(t)
    assert not dap.calibrate_ap_read(AP_MDM, MDM_IDR_EXPECTED)


# --------------------------------------------------------------------------
# Power-up handshake
# --------------------------------------------------------------------------


def test_power_up_sets_req_and_waits_for_ack() -> None:
    t = FakeTransport(pipelined=False)
    dap = Dap(t)
    stat = dap.power_up()
    assert stat & adiv6.CDBGPWRUPREQ
    assert stat & adiv6.CDBGPWRUPACK
    assert not stat & adiv6.CSYSPWRUPREQ


def test_power_up_system_requests_both() -> None:
    t = FakeTransport(pipelined=False)
    dap = Dap(t)
    stat = dap.power_up(system=True)
    # ADIv6 p83: CSYSPWRUPREQ must never be asserted without CDBGPWRUPREQ.
    assert stat & adiv6.CDBGPWRUPREQ
    assert stat & adiv6.CSYSPWRUPREQ
    assert stat & adiv6.CSYSPWRUPACK


def test_power_up_times_out_when_never_acked() -> None:
    t = FakeTransport(pipelined=False)
    t.auto_ack = False
    dap = Dap(t)
    with pytest.raises(PowerUpError) as exc:
        dap.power_up(timeout_s=0.05)
    assert "CDBGPWRUPACK" in str(exc.value)


# --------------------------------------------------------------------------
# MEM-AP memory access and BASE
# --------------------------------------------------------------------------


def test_mem_read32_programs_csw_and_tar_then_reads_drw() -> None:
    t = FakeTransport(pipelined=False)
    dap = Dap(t)
    t.ap_space[AP_MDM + adiv6.AP_DRW] = 0x12345678
    value = dap.mem_read32(AP_MDM, 0x0041_0000)
    assert value == 0x12345678
    assert t.ap_space[AP_MDM + adiv6.AP_TAR_LO] == 0x0041_0000
    assert t.ap_space[AP_MDM + adiv6.AP_CSW] == adiv6.CSW_SIZE_32BIT


def test_read_ap_base_masks_and_reports_presence() -> None:
    t = FakeTransport(pipelined=False)
    dap = Dap(t)
    # BASEADDR 0x01BFE000 with Format=1 and P=1
    t.ap_space[AP_MDM + adiv6.AP_BASE_LO] = 0x01BF_E000 | adiv6.BASE_FORMAT | adiv6.BASE_PRESENT
    rom, present = dap.read_ap_base(AP_MDM)
    assert present and rom == 0x01BF_E000


def test_read_ap_base_absent_when_p_clear() -> None:
    t = FakeTransport(pipelined=False)
    dap = Dap(t)
    t.ap_space[AP_MDM + adiv6.AP_BASE_LO] = 0x01BF_E000 | adiv6.BASE_FORMAT
    rom, present = dap.read_ap_base(AP_MDM)
    assert not present and rom is None


def test_apspace_reader_refuses_addresses_outside_apbic1() -> None:
    from s32edbg.errors import DapError

    dap = Dap(FakeTransport(pipelined=False))
    reader = dap.apspace_reader()
    with pytest.raises(DapError) as exc:
        reader(0x0000_0000)
    assert "APBIC1" in str(exc.value)


# --------------------------------------------------------------------------
# The twin-AP explanation for the RM's own inconsistent addresses
# --------------------------------------------------------------------------


def test_twin_ap_reconciles_rm_table_957_core_addresses() -> None:
    """RM Table 957 lists MDM-AP at 0x01C10000 (debugger) and 0x4DC11000 (cores).

    The RM says the debug system sits at 0x4C000000 in system memory, which would
    make the cores-side address 0x4DC10000, not 0x4DC11000. SoC-600 TRM 2.2
    explains the extra 0x1000: the cores see AP-L1, the debugger sees AP-L0.
    """
    assert DEBUG_SYSTEM_CORE_BASE + twin_ap_l1(AP_MDM) == 0x4DC1_1000

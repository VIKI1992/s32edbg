"""Bus access: CSW handling, address stepping, byte lanes and safety policy.

No hardware required.
"""

from __future__ import annotations

import pytest
from conftest import FakeTransport, MemApFake

from s32edbg.dap import memaccess
from s32edbg.dap.adiv6 import (
    AP_CSW,
    AP_DRW,
    CSW_ADDRINC_SINGLE,
    CSW_SIZE_16BIT,
    CSW_SIZE_8BIT,
    TAR_AUTOINC_BOUNDARY,
    Dap,
)
from s32edbg.data.expected_apmap import AP_APB, AP_AXI, AP_SDA
from s32edbg.data.ladder1 import MDM_STATUS, MDM_STATUS_SYSTEM_RESET
from s32edbg.data.expected_apmap import AP_MDM
from s32edbg.errors import UnsafeOperation


def written(t: FakeTransport, ap_base: int, offset: int) -> int | None:
    return t.ap_space.get(ap_base + offset)


# ---------------------------------------------------------------------------
# CSW: the bits we do not own must survive
# ---------------------------------------------------------------------------


def test_a_read_preserves_the_csw_bits_it_does_not_own() -> None:
    """ADIv6 C2.6.4 leaves Prot/Type/Mode/DbgSwEnable implementation defined
    with an SoC-chosen reset value. Those bits set the bus attributes every
    access is issued with -- privilege, cacheability, secure state -- so
    overwriting CSW wholesale silently changes them. On the AXI-AP that is the
    class of mistake RM Table 957 says needs a destructive reset to undo.
    """
    t = FakeTransport(pipelined=False)
    t.ap_space[AP_AXI + AP_CSW] = 0x2300_0052  # Prot/DbgSwEnable bits set
    Dap(t).mem_read(AP_AXI, 0x4000_0000, size=32)

    csw = written(t, AP_AXI, AP_CSW)
    assert csw is not None
    assert csw & 0x2300_0000 == 0x2300_0000, "high CSW bits were clobbered"
    assert csw & 0b111 == 0b010, "Size was not set to 32-bit"


def test_a_byte_access_selects_the_8_bit_csw_size() -> None:
    t = FakeTransport(pipelined=False)
    Dap(t).mem_read(AP_APB, 0x0000_0000, size=8)
    assert written(t, AP_APB, AP_CSW) & 0b111 == CSW_SIZE_8BIT


def test_a_halfword_access_selects_the_16_bit_csw_size() -> None:
    t = FakeTransport(pipelined=False)
    Dap(t).mem_read(AP_APB, 0x0000_0000, size=16)
    assert written(t, AP_APB, AP_CSW) & 0b111 == CSW_SIZE_16BIT


def test_an_unsupported_access_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="8, 16, 32"):
        Dap(FakeTransport(pipelined=False)).mem_read(AP_APB, 0, size=64)


# ---------------------------------------------------------------------------
# Byte lanes -- ADIv6 C2.6.6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "addr,expected",
    [(0x1000, 0xAA), (0x1001, 0xBB), (0x1002, 0xCC), (0x1003, 0xDD)],
)
def test_a_byte_read_extracts_the_lane_the_low_address_bits_select(addr: int, expected: int) -> None:
    """ADIv6 C2.6.6: a sub-word transfer appears in the byte lane chosen by the
    low address bits, not right-aligned in DRW."""
    t = FakeTransport(pipelined=False)
    t.ap_space[AP_APB + AP_DRW] = 0xDDCC_BBAA
    assert Dap(t).mem_read(AP_APB, addr, size=8) == expected


def test_a_byte_write_is_placed_in_the_lane_the_address_selects() -> None:
    t = FakeTransport(pipelined=False)
    Dap(t).mem_write(AP_APB, 0x1002, 0x5A, size=8)
    assert written(t, AP_APB, AP_DRW) == 0x005A_0000


# ---------------------------------------------------------------------------
# Auto-increment and the 1 KB rule -- ADIv6 C2.6.4
# ---------------------------------------------------------------------------


def test_a_block_read_inside_one_window_returns_consecutive_words() -> None:
    t = MemApFake()
    for i in range(8):
        t.mem[0x2000 + i * 4] = 0xA000 + i
    assert Dap(t).mem_read_block(AP_APB, 0x2000, 8, size=32) == [0xA000 + i for i in range(8)]


def test_a_block_read_straddling_a_1kb_boundary_returns_the_right_words() -> None:
    """ADIv6 C2.6.4 only guarantees auto-increment within a 1 KB window, and the
    AP is free to wrap at the boundary rather than carry. A reader that does not
    rewrite TAR there silently returns words from the *start* of the same
    window -- correct-looking values from the wrong addresses, with no error
    raised anywhere. This is the failure this test exists to catch.
    """
    t = MemApFake()
    start = TAR_AUTOINC_BOUNDARY - 8  # two words below the boundary
    for i in range(4):
        t.mem[start + i * 4] = 0xB000 + i
    # Poison where a wrapped read would land, so a regression is unmistakable.
    t.mem[0x0000] = 0xDEAD
    t.mem[0x0004] = 0xDEAD

    assert Dap(t).mem_read_block(AP_APB, start, 4, size=32) == [0xB000 + i for i in range(4)]


def test_a_block_read_uses_the_auto_increment_csw_setting() -> None:
    t = MemApFake()
    Dap(t).mem_read_block(AP_APB, 0, 4, size=32)
    assert written(t, AP_APB, AP_CSW) & (0b11 << 4) == CSW_ADDRINC_SINGLE


def test_a_block_read_falls_back_to_explicit_tar_when_autoinc_is_unsupported() -> None:
    """Some APs implement CSW.AddrInc as RO 0b00. Without the read-back check
    every word after the first would come from the same address, so the caller
    would get N copies of one value and no indication anything was wrong."""
    t = MemApFake(autoinc_supported=False)
    for i in range(4):
        t.mem[0x3000 + i * 4] = 0xC000 + i
    assert Dap(t).mem_read_block(AP_APB, 0x3000, 4, size=32) == [0xC000 + i for i in range(4)]


def test_a_single_value_read_does_not_use_auto_increment() -> None:
    t = FakeTransport(pipelined=False)
    memaccess.read_words(Dap(t), AP_APB, 0x2000, 1, size=32)
    assert written(t, AP_APB, AP_CSW) & (0b11 << 4) == 0


# ---------------------------------------------------------------------------
# Policy: the two guards that exist for exactly this command
# ---------------------------------------------------------------------------


def test_writing_the_sda_ap_is_refused() -> None:
    """RM 135.13.4: "HSE allows one only attempt for C/R comparison. If it
    fails, no further attempts are accepted." A botched write is unrecoverable
    until reset, so the SDA-AP is read-only in this tool."""
    dap = Dap(FakeTransport(pipelined=False))
    with pytest.raises(UnsafeOperation, match="SDA-AP"):
        memaccess.preflight(dap, AP_SDA, write=True)


def test_reading_the_sda_ap_is_allowed() -> None:
    dap = Dap(FakeTransport(pipelined=False))
    report, _notes = memaccess.preflight(dap, AP_SDA, write=False)
    assert report is not None


def test_axi_access_is_refused_while_functional_reset_is_asserted() -> None:
    """RM Table 957: an AXI-AP access during reset can leave peripherals
    unreachable "until you issue a destructive reset"."""
    t = FakeTransport(pipelined=False)
    t.ap_space[AP_MDM + MDM_STATUS] = MDM_STATUS_SYSTEM_RESET
    with pytest.raises(UnsafeOperation, match="destructive reset"):
        memaccess.preflight(Dap(t), AP_AXI, write=False)


def test_axi_access_is_permitted_once_reset_is_deasserted() -> None:
    t = FakeTransport(pipelined=False)
    report, _notes = memaccess.preflight(Dap(t), AP_AXI, write=False)
    assert report is not None


def test_apb_access_is_unaffected_by_functional_reset() -> None:
    """The Table 957 warning is about the system interconnect, not the debug
    APB, so an APB-AP read must not be refused for the same reason."""
    t = FakeTransport(pipelined=False)
    t.ap_space[AP_MDM + MDM_STATUS] = MDM_STATUS_SYSTEM_RESET
    report, _notes = memaccess.preflight(Dap(t), AP_APB, write=False)
    assert report is not None


def test_a_faulted_read_raises_instead_of_returning_a_zero() -> None:
    """Defect D-3, found at the bench.

    A bus access that faults does not raise on its own: it returns 0x00000000
    and latches CTRL/STAT.STICKYERR. `mem read 0x76120000 --ap axi` reported
    0x00000000 for RTU0.GPR CFG_CORE -- a register XRDC denies the debugger
    entirely -- and that zero was then read as the register's value. Returning
    it is the confidently-wrong answer this tool exists to avoid.
    """
    from s32edbg.errors import DapError

    t = MemApFake()
    t.faulting.add(0x7612_0000)
    with pytest.raises(DapError, match="faulted"):
        memaccess.read_words(Dap(t), AP_AXI, 0x7612_0000, 1)


def test_a_faulted_read_names_the_address_and_the_likely_cause() -> None:
    from s32edbg.errors import DapError

    t = MemApFake()
    t.faulting.add(0x7612_0000)
    with pytest.raises(DapError, match="0x76120000.*XRDC"):
        memaccess.read_words(Dap(t), AP_AXI, 0x7612_0000, 1)


def test_a_block_read_that_faults_partway_is_rejected_whole() -> None:
    """The sticky flag latches, so every word after the faulting one reads as
    zero too. Returning the block would mix real values with fabricated ones,
    which is worse than returning nothing."""
    from s32edbg.errors import DapError

    t = MemApFake()
    for i in range(8):
        t.mem[0x2000 + i * 4] = 0xA000 + i
    t.faulting.add(0x2010)
    with pytest.raises(DapError, match="none of the block can be trusted"):
        memaccess.read_words(Dap(t), AP_APB, 0x2000, 8)


def test_a_faulted_write_raises_rather_than_being_blamed_on_the_register() -> None:
    """A discarded write otherwise surfaces as a read-back mismatch, which reads
    as "this register is write-1-to-clear" rather than "that access failed"."""
    from s32edbg.errors import DapError

    t = MemApFake()
    t.faulting.add(0x3000)
    with pytest.raises(DapError, match="faulted"):
        memaccess.write_word(Dap(t), AP_APB, 0x3000, 0x1234)


def test_a_normal_read_is_unaffected_by_the_fault_checking() -> None:
    t = MemApFake()
    t.mem[0x2000] = 0xCAFEBABE
    assert memaccess.read_words(Dap(t), AP_APB, 0x2000, 1) == [0xCAFEBABE]


def test_a_closed_debug_gate_warns_rather_than_refusing() -> None:
    """RM 135.11 warns that access with the clocks gated "might hang the debug
    APBIC bus", but the operator may be deliberately probing a cold part."""
    _report, notes = memaccess.preflight(Dap(FakeTransport(pipelined=False)), AP_APB, write=False)
    assert any("debug-enable gate is closed" in n for n in notes)


# ---------------------------------------------------------------------------
# AP naming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,base",
    [("axi", AP_AXI), ("apb", AP_APB), ("sda", AP_SDA), ("ahb-ce1", 0x01C9_2000)],
)
def test_an_ap_alias_resolves_to_its_rm_table_957_base(name: str, base: int) -> None:
    assert memaccess.resolve_ap(name) == base


def test_a_raw_base_address_is_accepted_as_well_as_a_name() -> None:
    assert memaccess.resolve_ap("0x01C20000") == AP_AXI


def test_an_unknown_ap_name_is_rejected_and_lists_the_valid_ones() -> None:
    with pytest.raises(ValueError, match="ahb-smu"):
        memaccess.resolve_ap("nonsense")


def test_a_bare_number_is_rejected_as_an_ap_base() -> None:
    """An AP base is an address: the 0x prefix is required, same rule as every
    other address argument. 29491200 is 0x01C20000 in decimal -- accepting it
    would bless exactly the hex/decimal guessing the rule exists to stop."""
    with pytest.raises(ValueError, match="0x-prefixed"):
        memaccess.resolve_ap("29491200")


# ---------------------------------------------------------------------------
# The write side of auto-increment -- mem_write_block / write_words
# ---------------------------------------------------------------------------


def test_a_block_write_inside_one_window_lands_consecutive_words() -> None:
    t = MemApFake()
    Dap(t).mem_write_block(AP_APB, 0x2000, [0xA000 + i for i in range(8)], size=32)
    assert [t.mem[0x2000 + i * 4] for i in range(8)] == [0xA000 + i for i in range(8)]


def test_a_block_write_straddling_a_1kb_boundary_lands_the_right_addresses() -> None:
    """The write-side twin of the wrap test: an AP may wrap TAR inside its 1 KB
    window, so a writer that does not rewrite TAR at the boundary stores the
    tail of the block over the *start* of the window -- clobbering memory it was
    never asked to touch, silently."""
    t = MemApFake()
    start = TAR_AUTOINC_BOUNDARY - 8  # two words below the boundary
    t.mem[0x0000] = 0x5AFE
    t.mem[0x0004] = 0x5AFE

    Dap(t).mem_write_block(AP_APB, start, [0xB000 + i for i in range(4)], size=32)

    assert [t.mem[start + i * 4] for i in range(4)] == [0xB000 + i for i in range(4)]
    assert t.mem[0x0000] == 0x5AFE, "a wrapped write clobbered the window start"
    assert t.mem[0x0004] == 0x5AFE


def test_a_block_write_uses_the_auto_increment_csw_setting() -> None:
    t = MemApFake()
    Dap(t).mem_write_block(AP_APB, 0, [1, 2, 3, 4], size=32)
    assert written(t, AP_APB, AP_CSW) & (0b11 << 4) == CSW_ADDRINC_SINGLE


def test_a_block_write_falls_back_to_explicit_tar_when_autoinc_is_unsupported() -> None:
    """With AddrInc RO 0b00 every word of the first chunk lands at its first
    address (last one wins). The fallback must notice via the TAR read-back and
    re-issue the chunk one TAR write per word, leaving the memory correct."""
    t = MemApFake(autoinc_supported=False)
    Dap(t).mem_write_block(AP_APB, 0x3000, [0xC000 + i for i in range(4)], size=32)
    assert [t.mem[0x3000 + i * 4] for i in range(4)] == [0xC000 + i for i in range(4)]


def test_a_block_write_reuses_select_instead_of_rewriting_it_per_word() -> None:
    """CSW/TAR/DRW share one 16-byte SELECT granule by design, so a burst needs
    one SELECT write, not one per word. On the DLL transport every SELECT write
    is a USB round trip; for a 3.6 MiB image the difference is minutes."""
    t = MemApFake()
    dap = Dap(t)
    dap.mem_write_block(AP_APB, 0x2000, [0] * 64, size=32)
    select_writes = [e for e in t.log if e[0] == "dp_write" and e[1] == 2]
    assert len(select_writes) <= 4, f"{len(select_writes)} SELECT writes for 64 words"


def test_write_words_raises_when_any_word_in_the_block_faults() -> None:
    """STICKYERR is latching: one check at the end covers the whole block."""
    t = MemApFake()
    t.faulting.add(0x2008)
    with pytest.raises(Exception, match="faulted"):
        memaccess.write_words(Dap(t), AP_APB, 0x2000, [1, 2, 3, 4], size=32)


def test_write_words_with_an_empty_block_touches_nothing() -> None:
    t = MemApFake()
    memaccess.write_words(Dap(t), AP_APB, 0x2000, [], size=32)
    assert t.mem == {}


def test_write_words_still_refuses_the_sda_ap() -> None:
    with pytest.raises(UnsafeOperation, match="SDA-AP"):
        memaccess.write_words(Dap(MemApFake()), AP_SDA, 0x0, [1], size=32)

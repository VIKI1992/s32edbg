"""External QSPI-NOR flash: data pins, planners, applet flows, trust machinery.

Driven against the SyntheticTarget's QSPI/applet model (conftest), which is an
oracle rather than a mirror: programming ANDs into a sparse NOR array so a
missing erase-before-write corrupts the verify data, the applet refuses to run
unless the physical LUT slots hold the opcodes the reference loads for each
op, the AHB read window answers only while slot 1 holds a READ sequence, LUT
writes are discarded while the LUTKEY/LCKCR lock is set, and a still-latched
M33 EDBGREQ re-halts the SMU with DFSR = "external debug request" before the
applet executes -- each a real way the ported flow can be wrong that a
storage-only fake would wave through.

What only silicon can settle (AP reachability per address class, the real AHB
window base, ReadMem's rc convention and budget) is recorded as bench items
u-f1..u-f9 in docs/test-plan.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import SyntheticTarget

from s32edbg import elf
from s32edbg.dap import flasher, gates, loader
from s32edbg.dap import m33 as m33_mod
from s32edbg.dap.adiv6 import Dap
from s32edbg.data import qspiflash as qf
from s32edbg.errors import FlashError, TransportError

MULTITOOL_FLASH_SETS = Path(r"C:\s32multitool\flash_address_sets.json")
MULTITOOL_BLOB = Path(
    r"C:\s32multitool\tests\fixtures\hw_flash_images\s32e2xx_bootloader_M33_v3.2.0_BLOB.bin"
)
BR1_REAR_ELF = Path(r"C:\br1_rear_ecu\build\bin\br1_rear.elf")


def _blob_path() -> Path | None:
    """The golden bootloader BLOB: the in-repo v3.2.0 fixture by default, or
    the operator's own file via S32EDBG_FLASH_BLOB (same 1,907,168-byte format
    as the user's v3.1.0 reference)."""
    override = os.environ.get("S32EDBG_FLASH_BLOB")
    if override:
        p = Path(override)
        return p if p.exists() else None
    return MULTITOOL_BLOB if MULTITOOL_BLOB.exists() else None


# ---------------------------------------------------------------------------
# Data pins: the vendored applet and the transcribed tables
# ---------------------------------------------------------------------------


def test_the_vendored_applet_matches_its_pinned_identity() -> None:
    """The SHA pin is what makes every address constant in qspiflash.py a fact
    about *this* binary rather than a hope; applet_bytes re-verifies on every
    load, so this doubles as the check that the vendored file is intact."""
    blob = qf.applet_bytes()
    assert len(blob) == qf.APPLET_SIZE == 2072
    import hashlib

    assert hashlib.sha256(blob).hexdigest() == qf.APPLET_SHA256
    assert blob[:4] != b"\x00\x00\x00\x00", "the entry must be Thumb code, not padding"


def test_a_touched_applet_binary_is_refused(monkeypatch) -> None:
    """The binary is toolchain-locked (docs/RAW_QSPI_APPLET.md): a modified file
    must never be handed to the SMU, whatever else looks plausible."""

    class Vendored:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def __truediv__(self, _name: str) -> "Vendored":
            return self

        def read_bytes(self) -> bytes:
            return self._data

    real = qf.applet_bytes()
    monkeypatch.setattr(qf.resources, "files", lambda pkg: Vendored(real[:-1] + b"\x00"))
    with pytest.raises(ValueError, match="SHA-256"):
        qf.applet_bytes()
    monkeypatch.setattr(qf.resources, "files", lambda pkg: Vendored(real[:100]))
    with pytest.raises(ValueError, match="2072"):
        qf.applet_bytes()


def test_the_lut_encoders_produce_the_reference_bit_layout() -> None:
    """instr[15:10] | pads[9:8] | operand[7:0], pairs packed low-half-first --
    the encoding load_virtual_lut writes into the LUT registers."""
    assert qf.lut_instr(1, 0, 0x9F) == 0x049F  # CMD 0x9F on 1 pad (SPI RDID)
    assert qf.lut_instr(7, 0, 4) == 0x1C04  # READ 4 bytes
    assert qf.lut_instr(2, 3, 32) == 0x0B20  # 32-bit ADDR on 8 pads
    assert qf.lut_instr(8, 3, 8) == 0x2308  # WRITE on 8 pads
    assert qf.lut_pair(0x049F, 0x1C04) == 0x1C04_049F
    assert qf.lut_pair(0x049F) == 0x0000_049F


def test_both_lut_sets_carry_the_same_nine_sequences_and_are_terminated() -> None:
    names = {
        qf.LUT_RDID, qf.LUT_WREN, qf.LUT_RDSR, qf.LUT_WRDI, qf.LUT_READ,
        qf.LUT_PAGE_PROGRAM, qf.LUT_SECTOR_ERASE, qf.LUT_BLOCK_ERASE_64K,
        qf.LUT_CHIP_ERASE,
    }
    assert set(qf.SPI_LUTS) == set(qf.OPI_LUTS) == names
    for seqs in (qf.SPI_LUTS, qf.OPI_LUTS):
        for name, seq in seqs.items():
            assert seq[-1] == 0, f"{name} must end in the STOP halfword"
    assert qf.luts_for_mode("opi") is qf.OPI_LUTS
    assert qf.luts_for_mode("spi") is qf.SPI_LUTS
    assert qf.luts_for_mode("anything else") is qf.SPI_LUTS  # the reference rule


def test_the_command_opcodes_pin_the_datasheet_values() -> None:
    """The opcodes the MX25UW25A45G actually decodes. The SyntheticTarget's
    applet oracle keys off these same values, so this pin is what keeps the
    fake and the tables honest together."""
    spi = {name: seq[0] & 0xFF for name, seq in qf.SPI_LUTS.items()}
    assert spi == {
        qf.LUT_RDID: 0x9F, qf.LUT_WREN: 0x06, qf.LUT_RDSR: 0x05,
        qf.LUT_WRDI: 0x04, qf.LUT_READ: 0x0B, qf.LUT_PAGE_PROGRAM: 0x02,
        qf.LUT_SECTOR_ERASE: 0x20, qf.LUT_BLOCK_ERASE_64K: 0xDC,
        qf.LUT_CHIP_ERASE: 0x60,
    }
    # Macronix OPI: every command is the opcode followed by its complement.
    for name, seq in qf.OPI_LUTS.items():
        assert seq[1] & 0xFF == (seq[0] & 0xFF) ^ 0xFF, name
    assert qf.OPI_LUTS[qf.LUT_READ][0] & 0xFF == 0xEC
    assert qf.OPI_LUTS[qf.LUT_PAGE_PROGRAM][0] & 0xFF == 0x12
    assert qf.OPI_LUTS[qf.LUT_SECTOR_ERASE][0] & 0xFF == 0x21


def test_the_raw_touchup_forces_sfacr_to_zero_after_the_s32ds_init() -> None:
    """The one deliberate divergence from the NXP init, and the one that must
    never be lost: SFACR = 0x00010803 silently transforms the SFAR offset the
    controller emits and aliased multi-page programs on the reference bench
    (AN13563 3.3.1.2). The S32DS value is still written first so a bench
    register-history comparison against the reference matches exactly."""
    sfacr = qf.QSPI_BASE + qf.QSPI_SFACR
    s32ds = [a for a in qf.ACTIONS_QSPI_CONTROLLER if a.op == "write32" and a.addr == sfacr]
    assert [a.value for a in s32ds] == [0x0001_0803]
    touchup = [a for a in qf.ACTIONS_RAW_TOUCHUP if a.op == "write32" and a.addr == sfacr]
    assert [a.value for a in touchup] == [0x0000_0000]
    # The touch-up ends by resetting the AHB sequence pointers, verified by poll.
    assert qf.ACTIONS_RAW_TOUCHUP[-1].op == "poll"
    assert qf.ACTIONS_RAW_TOUCHUP[-1].addr == qf.QSPI_BASE + qf.QSPI_SPTRCLR


def test_the_applet_call_contract_is_self_consistent() -> None:
    """The RAM layout the applet was linked against: code, mailbox, payload,
    stack, in that order and non-overlapping. A drift here means the .bin and
    the constants no longer describe the same applet."""
    assert (qf.OP_PROGRAM_BUFFER, qf.OP_READ_BUFFER) == (3, 4)
    assert (qf.OP_ERASE_4K_RANGE, qf.OP_ERASE_64K_RANGE) == (5, 6)
    assert len(qf.MAILBOX_LABELS) == 28
    assert qf.MAILBOX_LABELS[:4] == ("MAGIC", "OP", "RC", "PHASE")
    assert set(qf.RC_NAMES) == set(range(0x1001, 0x100A))
    assert qf.APPLET_BASE + qf.APPLET_SIZE <= qf.APPLET_STATUS
    assert qf.APPLET_STATUS + len(qf.MAILBOX_LABELS) * 4 <= qf.APPLET_DATA
    assert qf.APPLET_DATA + qf.APPLET_DATA_SIZE <= qf.APPLET_STACK
    assert qf.APPLET_DATA_SIZE == qf.PROGRAM_CHUNK
    assert qf.APPLET_ENTRY == qf.APPLET_BASE  # DCRSR pc convention: no Thumb bit


@pytest.mark.skipif(not MULTITOOL_FLASH_SETS.exists(), reason="s32multitool not on this machine")
def test_the_regions_match_the_reference_address_sets() -> None:
    """flash_address_sets.json is the source of the --area presets; the two
    must stay byte-identical or `--area app` writes somewhere the reference
    tooling would not."""
    sets = json.loads(MULTITOOL_FLASH_SETS.read_text(encoding="utf-8"))["flash_address_sets"]
    reference = {e["name"]: (int(e["start_offset"], 16), int(e["size"], 16)) for e in sets}
    ours = {label: (offset, size) for offset, size, label in qf.REGIONS.values()}
    assert ours == reference


# ---------------------------------------------------------------------------
# The erase planner and the blank-page-skip run builder (pure, offline)
# ---------------------------------------------------------------------------


def test_a_64k_aligned_stretch_is_one_block_erase_call() -> None:
    plan = flasher.build_erase_plan(0x0030_0000, 0x2_0000)
    assert plan == [
        (0x0030_0000, 0x2_0000, qf.BLOCK_ERASE_SIZE, qf.LUT_BLOCK_ERASE_64K, qf.OP_ERASE_64K_RANGE)
    ]


def test_a_ragged_range_gets_4k_head_and_tail_around_a_64k_middle() -> None:
    plan = flasher.build_erase_plan(0xF000, 0x1_2000)
    assert [(o, l, u) for o, l, u, _n, _op in plan] == [
        (0x0_F000, 0x1000, qf.SECTOR_SIZE),
        (0x1_0000, 0x1_0000, qf.BLOCK_ERASE_SIZE),
        (0x2_0000, 0x1000, qf.SECTOR_SIZE),
    ]


def test_erase_spans_are_capped_at_the_applet_chunk() -> None:
    """One applet call covers at most 0x100000 of flash (the reference's
    erase_applet_chunk_size); a 3 MiB range is three calls, not one."""
    plan = flasher.build_erase_plan(0, 0x30_0000)
    assert [(o, l) for o, l, _u, _n, _op in plan] == [
        (0x0, 0x10_0000), (0x10_0000, 0x10_0000), (0x20_0000, 0x10_0000)
    ]
    assert all(u == qf.BLOCK_ERASE_SIZE for _o, _l, u, _n, _op in plan)


@pytest.mark.parametrize(
    "offset,length",
    [(0, 0x1000), (0x1000, 0x2000), (0xF000, 0x1_2000), (0, 0x30_0000), (0x30_F000, 0x20_1000)],
    ids=lambda v: hex(v),
)
def test_the_erase_plan_covers_exactly_the_range_in_aligned_spans(offset, length) -> None:
    plan = flasher.build_erase_plan(offset, length)
    cursor = offset
    for span_offset, span_length, unit, _name, op in plan:
        assert span_offset == cursor
        assert span_offset % unit == 0 and span_length % unit == 0
        assert span_length <= qf.ERASE_CHUNK
        assert op in (qf.OP_ERASE_4K_RANGE, qf.OP_ERASE_64K_RANGE)
        cursor += span_length
    assert cursor == offset + length


def test_the_erase_plan_refuses_misalignment() -> None:
    with pytest.raises(FlashError, match="4 KiB aligned"):
        flasher.build_erase_plan(0x800, 0x1000)
    with pytest.raises(FlashError, match="multiple of 4 KiB"):
        flasher.build_erase_plan(0x1000, 0x800)


def test_blank_pages_are_skipped_and_neighbours_coalesce_into_runs() -> None:
    data = b"\x11" * 0x100 + b"\xff" * 0x100 + b"\x22" * 0x100
    runs, blank, non_blank = flasher.build_non_blank_program_runs(data, len(data), qf.PROGRAM_CHUNK)
    assert runs == [(0x000, 0x100), (0x200, 0x100)]
    assert (blank, non_blank) == (1, 2)


def test_an_all_ff_image_programs_nothing() -> None:
    """Correct only because erase-before-write guarantees the skipped pages
    already read 0xFF -- which the flow tests below prove end to end."""
    runs, blank, non_blank = flasher.build_non_blank_program_runs(b"\xff" * 0x400, 0x400, qf.PROGRAM_CHUNK)
    assert runs == []
    assert (blank, non_blank) == (4, 0)


def test_a_partial_tail_page_counts_via_its_ff_padding() -> None:
    """Ten real bytes then nothing: the tail page is padded with 0xFF before
    the blank test, so it programs; bytes past the input never invent data."""
    runs, blank, non_blank = flasher.build_non_blank_program_runs(b"\x55" * 10, 0x200, qf.PROGRAM_CHUNK)
    assert runs == [(0, 0x100)]
    assert (blank, non_blank) == (1, 1)  # the second page is pure padding: blank


def test_runs_are_capped_at_the_program_chunk() -> None:
    runs, _blank, _non_blank = flasher.build_non_blank_program_runs(b"\x11" * 0x300, 0x300, 0x200)
    assert runs == [(0x000, 0x200), (0x200, 0x100)]


def test_the_run_builder_refuses_a_misaligned_chunk() -> None:
    with pytest.raises(FlashError, match="256-byte aligned"):
        flasher.build_non_blank_program_runs(b"\x11" * 0x100, 0x100, 0x180)


# ---------------------------------------------------------------------------
# The SMU bring-up (Finding 15): s32multitool's _m33_smu(), verbatim
# ---------------------------------------------------------------------------


def test_the_smu_boot_table_is_the_multitool_sequence_verbatim() -> None:
    """Order-exact pin of data/smuboot.ACTIONS_SMU_BOOT against the reference
    (`_m33_smu()`, s32z2e2_init_sequence.py:210-250 in the NXP runtime that
    s32multitool ships): thirteen writes, the CCUPD read-modify-write, the two
    MC_ME key writes, the 20/10 ms pauses and the two gta-40 polls. A drifted
    number here is a drifted bring-up -- this table is why a wiped board
    flashes at all."""
    from s32edbg.data import smuboot

    expected = [
        ("write32", 0x2500_2400, 0xF800_F000, 0xFFFF_FFFF, 0),
        ("write32", 0x2500_2404, 0xE7FE_E7FE, 0xFFFF_FFFF, 0),
        ("write32", 0x2500_2000, 0x2500_4000, 0xFFFF_FFFF, 0),
        ("write32", 0x2500_2004, 0x2500_2401, 0xFFFF_FFFF, 0),
        ("write32", 0x2500_2008, 0x2500_2401, 0xFFFF_FFFF, 0),
        ("write32", 0x2500_200C, 0x2500_2401, 0xFFFF_FFFF, 0),
        ("write32", 0x2500_2010, 0x2500_2401, 0xFFFF_FFFF, 0),
        ("write32", 0x2500_2014, 0x2500_2401, 0xFFFF_FFFF, 0),
        ("write32", 0x2500_2018, 0x2500_2401, 0xFFFF_FFFF, 0),
        ("write32", 0x2500_4000, 0x0000_0000, 0xFFFF_FFFF, 0),
        ("write32", 0x2500_4004, 0x0000_0000, 0xFFFF_FFFF, 0),
        ("write32", 0x2500_4008, 0x0000_0000, 0xFFFF_FFFF, 0),
        ("write32", 0x2500_400C, 0x0000_0000, 0xFFFF_FFFF, 0),
        ("write32", 0x4106_004C, 0x0000_0000, 0xFFFF_FFFF, 0),
        ("write32", 0x4190_014C, 0x2500_2000, 0xFFFF_FFFF, 0),
        ("write32", 0x4190_0140, 0x0000_0001, 0xFFFF_FFFF, 0),
        ("rmw", 0x4190_0144, 0x0000_0001, 0x0000_0001, 0),
        ("write32", 0x4190_0000, 0x0000_5AF0, 0xFFFF_FFFF, 0),
        ("write32", 0x4190_0000, 0x0000_A50F, 0xFFFF_FFFF, 0),
        ("sleep", 0, 0, 0xFFFF_FFFF, 20),
        ("poll", 0x4190_0144, 0x0000_0000, 0x0000_0001, 40),
        ("poll", 0x4190_0148, 0x0000_0001, 0x0000_0001, 40),
        ("sleep", 0, 0, 0xFFFF_FFFF, 10),
    ]
    got = [
        (a.op, a.addr, a.value, a.mask, a.timeout_ms)
        for a in smuboot.ACTIONS_SMU_BOOT
    ]
    assert got == expected


def test_smu_boot_addresses_share_the_applet_map_deliberately() -> None:
    """The bring-up writes its vector table over qspiflash.APPLET_STATUS and
    zeroes stack words inside APPLET_DATA -- benign strictly by ordering (the
    bring-up lands first, the flasher clobbers them later; the reference uses
    the same memory map). Pinned so an applet-map move is noticed here rather
    than as a corrupted mailbox on the bench."""
    from s32edbg.data import smuboot

    assert smuboot.SMU_VECTOR_BASE == qf.APPLET_STATUS
    assert (
        qf.APPLET_DATA
        <= smuboot.SMU_STACK_TOP
        < qf.APPLET_DATA + qf.APPLET_DATA_SIZE
    )


def test_run_init_actions_boots_a_dest0_smu_halted_at_the_stub() -> None:
    """The Finding-15 mechanism against the DEST0 model: SMU unpowered
    (Finding 14 -- every private-bus read zero, and no reset changes that),
    EDBGREQ armed, then the four bring-up tables in the reference's order.
    The MC_ME PRTN0 core-0 release powers the core up halted at the
    debugger-written stub, identity readable again."""
    from s32edbg.dap import memaccess
    from s32edbg.data import rtuboot, smuboot
    from s32edbg.data.expected_apmap import AP_AXI

    target = SyntheticTarget()
    target.smu_unpowered = True
    dap = Dap(target)
    dap.power_up()
    gates.apply_ladder1(dap)
    assert not m33_mod.identify(dap, m33_mod.M33_APS["smu"]).is_cortex_m33
    gates.arm_edbgreq(dap)
    memaccess.preflight(dap, AP_AXI, write=True)
    loader.run_init_actions(dap, rtuboot.ACTIONS_RTU0_PARTITION)
    loader.run_init_actions(dap, rtuboot.ACTIONS_RTU1_PARTITION)
    loader.run_init_actions(dap, rtuboot.ACTIONS_GENERIC_SRAM)
    loader.run_init_actions(dap, smuboot.ACTIONS_SMU_BOOT)
    assert target.smu_unpowered is False
    assert target.m33["halted"] is True, "the still-latched EDBGREQ caught it"
    assert target.m33["pc"] == 0x2500_2400, "halted at the stub, not in BootROM"
    assert m33_mod.identify(dap, m33_mod.M33_APS["smu"]).is_cortex_m33


# ---------------------------------------------------------------------------
# Flow tests against the SyntheticTarget's QSPI + applet model
# ---------------------------------------------------------------------------


def make_flasher(
    device: SyntheticTarget | None = None,
    *,
    use_fast: bool = False,
    latch_edbgreq: bool = False,
    ahb_base: int | None = None,
    prepare: bool = True,
):
    """A QspiFlasher over a quiesced synthetic SoC.

    Mirrors the CLI bring-up's end state -- gates applied, every M33 pinned by
    its own DHCSR, EDBGREQ latches clear -- without the reset choreography
    (that is the CLI tests' job). ``latch_edbgreq`` leaves the MDM CONTROL
    latches set instead, which is the D6 trap the flow must survive.
    """
    device = SyntheticTarget() if device is None else device
    dap = Dap(device)
    dap.power_up()
    gates.apply_ladder1(dap)
    notes: list[str] = []
    loader.halt_m33s(dap, notes.append)
    if latch_edbgreq:
        gates.arm_edbgreq(dap)
    else:
        gates.clear_m33_edbgreq(dap)
    smu = m33_mod.resolve("smu", m33_mod.discover(dap))
    fl = flasher.QspiFlasher(
        dap,
        device,
        smu,
        note=notes.append,
        ahb_base=device.AHB_TEST_BASE if ahb_base is None else ahb_base,
        use_fast=use_fast,
    )
    if prepare:
        fl.preflight()
        fl.controller_init()
        fl.prepare()
    return device, dap, fl, notes


def test_controller_init_skips_the_tables_when_bootrom_left_it_alive() -> None:
    """Finding 11 (2026-08-05): the PLL block at 0x40220000 faults for the
    debugger on both candidate APs, so the cold tables cannot run on this
    silicon -- and they never need to on a booted board, because BootROM
    boots from this flash and leaves the whole tree configured. The init
    probes first and skips."""
    device, _dap, fl, notes = make_flasher()
    assert any("already alive" in n for n in notes)
    assert 0x4022_0020 not in device.mem, "the PLL table must not have run"
    assert (fl.mode, fl.jedec) == ("spi", 0xC239_89C2)


def test_controller_init_runs_the_full_tables_on_a_cold_controller() -> None:
    """The virgin-board path: a controller that answers nothing gets the full
    vendored bring-up, after which detection and a program round-trip work.
    (On the real part this path is gated on the Finding-11 XRDC question.)"""
    device = SyntheticTarget()
    device.qspi_cold = True
    device, _dap, fl, notes = make_flasher(device)
    assert any("running the full NXP bring-up" in n for n in notes)
    assert device.mem[0x4022_0020] == 0x0000_0001, "PLLCLKMUX written"
    assert device.mem[qf.QSPI_BASE + qf.QSPI_DLLSR] & 0x4000, "DLL locked"
    assert (fl.mode, fl.jedec) == ("spi", 0xC239_89C2)
    data = bytes(range(256))
    report = flasher.FlashReport(op="program")
    fl.program(data, 0x0030_0000, report)
    fl.verify(data, 0x0030_0000, report, mode="full")


def test_prepare_detects_spi_mode_and_arms_the_read_window() -> None:
    device, _dap, fl, _notes = make_flasher()
    assert (fl.mode, fl.jedec) == ("spi", 0xC239_89C2)
    # Slot 1 carries the READ sequence (the AHB window's LUT), slot 0 is clear.
    assert device._qspi_slot_opcode(qf.PHYS_AHB_LUT)[0] == 0x0B
    assert device._qspi_slot_opcode(qf.PHYS_COMMAND_LUT)[0] == 0
    # The raw touch-up ran: SFACR forced 0, AHB reads routed at LUT 1.
    assert device.mem[qf.QSPI_BASE + qf.QSPI_SFACR] == 0
    assert device.mem[qf.QSPI_BASE + qf.QSPI_BFGENCR] == qf.PHYS_AHB_LUT << 12
    assert device.qspi_lut_locked is True


def test_prepare_falls_back_to_opi_when_spi_reads_invalid() -> None:
    """A board whose firmware left the device in octal mode: the SPI RDID
    reads all-ones, the OPI probe answers -- the reference's per-operation
    detection order."""
    device = SyntheticTarget()
    device.flash_device_mode = "opi"
    device, _dap, fl, notes = make_flasher(device)
    assert (fl.mode, fl.jedec) == ("opi", 0xC239_89C2)
    assert any("SPI JEDEC probe read 0xFFFFFFFF" in n for n in notes)
    assert device._qspi_slot_opcode(qf.PHYS_AHB_LUT)[0] == 0xEC  # the OPI READ


def test_prepare_fails_loudly_when_no_mode_answers() -> None:
    device = SyntheticTarget()
    device.flash_device_mode = "neither"
    device, _dap, fl, _notes = make_flasher(device, prepare=False)
    fl.preflight()
    fl.controller_init()
    with pytest.raises(FlashError, match="no valid JEDEC"):
        fl.prepare()


def test_program_erases_first_and_programs_only_the_non_blank_pages() -> None:
    """The core contract in one flow: erase precedes every write (the model's
    NOR AND-semantics corrupts the data if it does not), the blank middle page
    is skipped, and the verify is byte-exact against the original input."""
    device, _dap, fl, notes = make_flasher()
    data = b"\x11" * 0x100 + b"\xff" * 0x100 + bytes(range(256))
    device.flash[0x0030_0000] = 0  # stale content the erase must lift
    report = flasher.FlashReport(op="program")
    fl.program(data, 0x0030_0000, report)
    assert device.applet_runs == [
        (qf.OP_ERASE_4K_RANGE, 0x0030_0000, 0x1000),
        (qf.OP_PROGRAM_BUFFER, 0x0030_0000, 0x100),
        (qf.OP_PROGRAM_BUFFER, 0x0030_0200, 0x100),
    ]
    assert device.flash[0x0030_0000] == 0x1111_1111
    assert 0x0030_0100 not in device.flash, "the blank page stays unprogrammed"
    fl.verify(data, 0x0030_0000, report, mode="full")
    assert report.skipped_pages == 1
    assert report.program_runs == 2
    assert report.programmed_bytes == 0x200
    assert report.erase_bytes == 0x1000
    assert any("blank-page skip: 1 of 3" in n for n in notes)
    # The whole flow respected the LUT lock: nothing was written while locked.
    assert device.qspi_lut_discards == 0
    assert device.qspi_lut_locked is True


def test_a_program_that_skips_the_erase_is_caught_by_verify() -> None:
    """Prove the model is an oracle: NOR can only clear bits, so stale content
    under a write without an erase corrupts the result -- and the byte-exact
    verify is what catches it."""
    device, _dap, fl, _notes = make_flasher()
    device.flash[0x0030_0000] = 0x0000_0000
    fl.erase = lambda *a, **k: None  # a flow that forgot erase-before-write
    report = flasher.FlashReport(op="program")
    fl.program(b"\x11" * 4, 0x0030_0000, report)
    with pytest.raises(FlashError, match="verify mismatched"):
        fl.verify(b"\x11" * 4, 0x0030_0000, report, mode="full")


def test_program_round_trips_in_opi_mode_too() -> None:
    """The OPI LUT set passes the applet's slot oracle end to end: PP 0x12,
    sector erase 0x21, window reads via 0xEC."""
    device = SyntheticTarget()
    device.flash_device_mode = "opi"
    device, _dap, fl, _notes = make_flasher(device)
    data = bytes(range(256))
    report = flasher.FlashReport(op="program")
    fl.program(data, 0x0030_0000, report)
    fl.verify(data, 0x0030_0000, report, mode="full")
    assert fl.read(0x0030_0000, len(data)) == data


def test_verify_names_the_first_differing_byte() -> None:
    device, _dap, fl, _notes = make_flasher()
    data = bytes((0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88))
    report = flasher.FlashReport(op="program")
    fl.program(data, 0x0030_0000, report)
    device.flash[0x0030_0004] &= ~0x0000_FF00  # byte 5 of the image: 0x66 -> 0x00
    with pytest.raises(FlashError, match="0x00300005.*expected 0x66, read 0x00"):
        fl.verify(data, 0x0030_0000, report, mode="full")


def test_erase_plans_64k_plus_4k_and_the_blank_check_passes() -> None:
    device, _dap, fl, _notes = make_flasher()
    for offset in (0x0030_0000, 0x0030_FFFC, 0x0031_0004):
        device.flash[offset] = 0
    report = flasher.FlashReport(op="erase")
    fl.erase(0x0030_0000, 0x1_1000, report)
    assert device.applet_runs == [
        (qf.OP_ERASE_64K_RANGE, 0x0030_0000, 0x1_0000),
        (qf.OP_ERASE_4K_RANGE, 0x0031_0000, 0x1000),
    ]
    assert (report.erase_spans_64k, report.erase_spans_4k) == (1, 1)
    assert not device.flash, "every word of the range must read erased"
    fl.blank_check(0x0030_0000, 0x1_1000, report, mode="full")


def test_a_blank_check_over_programmed_flash_names_the_offset() -> None:
    device, _dap, fl, _notes = make_flasher()
    report = flasher.FlashReport(op="program")
    fl.program(b"\x11" * 4, 0x0030_0000, report)
    with pytest.raises(FlashError, match="blank check failed at flash offset 0x00300000"):
        fl.blank_check(0x0030_0000, 0x1000, report, mode="full")


def test_chip_erase_is_host_driven_and_polls_wip_to_completion() -> None:
    """The one operation with no applet op: WREN until WEL latches, the slot-0
    CHIP_ERASE command, then RDSR until WIP clears -- the model answers busy
    three times so the poll loop actually loops."""
    device, _dap, fl, _notes = make_flasher()
    device.flash[0x0100_0000] = 0
    report = flasher.FlashReport(op="erase --all")
    fl.chip_erase(report)
    assert device.qspi_chip_erases == 1
    assert not device.flash
    assert device.applet_runs == [], "chip erase must never ride the applet"
    assert device._qspi_wip_polls == 0, "the WIP poll consumed the busy answers"
    assert device.qspi_wel is False, "WEL self-clears when the erase starts"
    assert report.erase_bytes == qf.DEVICE_SIZE
    fl.blank_check(0, 0x1000, report, mode="full")


def test_a_failed_applet_op_reports_the_rc_name_and_the_decoded_mailbox() -> None:
    """Wrong slot-0 LUT for the requested op -- the applet's own failure mode.
    The error must carry the rc name and the labelled 28-word mailbox, because
    at the bench that dump is the only visibility into the applet."""
    device, _dap, fl, _notes = make_flasher()
    with pytest.raises(FlashError) as excinfo:
        fl.run_applet(qf.OP_ERASE_4K_RANGE, 0x0030_0000, 0x1000)  # slot 0 is clear
    message = str(excinfo.value)
    assert "rc=0x00001006" in message and "QSPI_ERROR" in message
    assert "MAGIC = 0x42514347" in message
    assert "PHASE = 0x0000FF00" in message


def test_registers_are_saved_and_restored_around_applet_calls() -> None:
    """The applet borrows the SMU's whole register file; success or failure,
    the session must get back exactly the context it had."""
    device, dap, fl, _notes = make_flasher()
    sentinels = {
        name: 0xA000_0000 + i for i, name in enumerate(flasher.APPLET_SAVED_REGS)
    }
    for name, value in sentinels.items():
        m33_mod.write_register(dap, fl.smu, name, value)

    report = flasher.FlashReport(op="erase")
    fl.erase(0x0030_0000, 0x1000, report)  # a successful call
    for name, value in sentinels.items():
        assert m33_mod.read_register(dap, fl.smu, name) == value, name

    with pytest.raises(FlashError):
        fl.run_applet(9, 0, 0x1000)  # BAD_OP: the failure path restores too
    for name, value in sentinels.items():
        assert m33_mod.read_register(dap, fl.smu, name) == value, name


def test_a_latched_edbgreq_rehalts_into_external_and_the_error_names_the_cure() -> None:
    """Trap D6: ladder-1/arm_edbgreq latch M33 EDBGREQ in MDM CONTROL[10:8];
    resumed into the applet, the SMU re-halts before the first instruction
    with DFSR = external. The failure must point at clear_m33_edbgreq -- and
    clearing the latches must actually be the cure."""
    device, dap, fl, _notes = make_flasher(latch_edbgreq=True)
    report = flasher.FlashReport(op="erase")
    with pytest.raises(FlashError, match="clear_m33_edbgreq"):
        fl.erase(0x0030_0000, 0x1000, report)
    assert device.applet_runs == [], "the applet must not have executed"

    gates.clear_m33_edbgreq(dap)  # the ordering the CLI bring-up guarantees
    fl.erase(0x0030_0000, 0x1000, report)
    assert device.applet_runs == [(qf.OP_ERASE_4K_RANGE, 0x0030_0000, 0x1000)]


def test_the_applet_is_staged_once_per_flow() -> None:
    device, _dap, fl, _notes = make_flasher()
    staged: list[int] = []
    original = fl._stage
    fl._stage = lambda addr, data: (staged.append(addr), original(addr, data))[1]
    report = flasher.FlashReport(op="erase")
    fl.erase(0x0030_0000, 0x1000, report)
    fl.erase(0x0030_0000, 0x1000, report)
    assert staged.count(qf.APPLET_BASE) == 1
    assert fl.applet_calls == 2


def test_nothing_runs_before_prepare() -> None:
    """A flasher whose controller state is unknown must refuse every
    operation -- the LUT slots could hold anything."""
    _device, _dap, fl, _notes = make_flasher(prepare=False)
    report = flasher.FlashReport(op="x")
    with pytest.raises(FlashError, match="before prepare"):
        fl.erase(0x0030_0000, 0x1000, report)
    with pytest.raises(FlashError, match="before prepare"):
        fl.program(b"\x11", 0x0030_0000, report)
    with pytest.raises(FlashError, match="before prepare"):
        fl.read(0, 4)


def test_window_reads_and_applet_reads_agree() -> None:
    """Two independent read paths -- the memory-mapped window and applet op 4
    into the payload buffer -- must produce identical bytes, including the
    erased 0xFF past the programmed range."""
    device, _dap, fl, _notes = make_flasher()
    data = bytes(range(256)) * 2
    report = flasher.FlashReport(op="program")
    fl.program(data, 0x0030_0000, report)
    window = fl.read(0x0030_0000, 0x300)
    applet = fl.read_via_applet(0x0030_0000, 0x300)
    assert window == applet == data + b"\xff" * 0x100


def test_a_faulting_window_base_fails_over_to_the_alternate_once() -> None:
    """u-f1: the reference profile carries both 0x0 and 0x08000000 as window
    bases. A base that faults (wrong for this AP) must fail over loudly; the
    model serves the window at 0x08000000 only."""
    device, _dap, fl, notes = make_flasher(ahb_base=0x0)
    device.faulting.add(0x0000_1000)  # base 0x0 + offset: the first read faults
    data = b"\xa5" * 8
    report = flasher.FlashReport(op="program")
    fl.program(data, 0x0000_1000, report)
    assert fl.read(0x0000_1000, 8) == data
    assert fl.ahb_base == 0x0800_0000
    assert any("trying base 0x08000000" in n for n in notes)


def test_preflight_refuses_an_ap_that_cannot_reach_the_controller() -> None:
    device = SyntheticTarget()
    device.faulting.add(qf.QSPI_BASE + qf.QSPI_MCR)
    device, _dap, fl, _notes = make_flasher(device, prepare=False)
    with pytest.raises(FlashError, match="did not answer"):
        fl.preflight()


def test_preflight_refuses_applet_ram_that_drops_writes() -> None:
    """The wrong-AP signature on this SoC is not a fault but a silent
    nothing: writes vanish, reads return zero. The write/read-back probe is
    what turns that into a refusal before anything destructive."""

    class DeafAppletRam(SyntheticTarget):
        def on_mem_write(self, addr: int, value: int) -> bool | None:
            if addr == qf.APPLET_STATUS:
                return True  # claimed, never stored
            return super().on_mem_write(addr, value)

    device, _dap, fl, _notes = make_flasher(DeafAppletRam(), prepare=False)
    with pytest.raises(FlashError, match="did not hold a written word"):
        fl.preflight()


def test_check_jedec_refuses_an_unknown_part_unless_forced() -> None:
    device, _dap, fl, notes = make_flasher()
    fl.mode, fl.jedec = "spi", 0x0018_9981  # some other part answered
    with pytest.raises(FlashError, match="Refusing destructive work"):
        fl.check_jedec()
    fl.check_jedec(force=True)
    assert any("--force-jedec" in n for n in notes)


# ---------------------------------------------------------------------------
# The ReadMem fast path: trust is earned, lies are caught, budget is obeyed
# ---------------------------------------------------------------------------


class FastReadFlashTarget(SyntheticTarget):
    """A target whose transport-side ReadMem answers correctly, serving the
    same window/RBDR view as the ADIv6 path."""

    can_block_read = True

    def __init__(self) -> None:
        super().__init__()
        self.block_reads: list[tuple[int, int]] = []

    def read_mem_block(self, addr: int, count: int) -> bytes:
        self.block_reads.append((addr, count))
        out = bytearray()
        for i in range(0, count, 4):
            a = addr + i
            out += self.on_mem_read(a, self.mem.get(a, 0)).to_bytes(4, "little")
        return bytes(out[:count])


class LyingFirstReadTarget(FastReadFlashTarget):
    """ReadMem that corrupts its very first byte -- the full-proof block must
    catch it before any of its data is believed."""

    def read_mem_block(self, addr: int, count: int) -> bytes:
        data = bytearray(super().read_mem_block(addr, count))
        data[0] ^= 0xFF
        return bytes(data)


class LyingLaterReadTarget(FastReadFlashTarget):
    """ReadMem that lies only in a later block, at a sampled word."""

    lie_at: int | None = None

    def read_mem_block(self, addr: int, count: int) -> bytes:
        data = bytearray(super().read_mem_block(addr, count))
        if addr == self.lie_at:
            data[0] ^= 0xFF
        return bytes(data)


class FlakyReadTarget(FastReadFlashTarget):
    """ReadMem fails (rc-style) exactly once, then behaves -- the transient
    shape the writer's measured bus-contention policy expects."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = 1

    def read_mem_block(self, addr: int, count: int) -> bytes:
        if self.fail_next:
            self.fail_next -= 1
            raise TransportError(f"JLINK_ReadMem(0x{addr:08X}) returned -1")
        return super().read_mem_block(addr, count)


def _programmed_fast_target(target_cls):
    device, dap, fl, notes = make_flasher(target_cls(), use_fast=True)
    data = bytes(range(256)) * 32  # 0x2000: spans the proof block plus one more
    report = flasher.FlashReport(op="program")
    fl.program(data, 0x0030_0000, report)
    return device, fl, notes, data, report


def test_the_fast_read_path_proves_itself_and_is_used() -> None:
    device, fl, notes, data, report = _programmed_fast_target(FastReadFlashTarget)
    fl.verify(data, 0x0030_0000, report, mode="full")
    assert device.block_reads, "ReadMem was never called"
    assert any("proven against a full independent ADIv6 read" in n for n in notes)
    fl.finish_report(report)
    assert report.fast_read == "used"


def test_a_lying_first_read_block_is_demoted_and_verify_still_passes() -> None:
    device, fl, notes, data, report = _programmed_fast_target(LyingFirstReadTarget)
    fl.verify(data, 0x0030_0000, report, mode="full")  # correct via ADIv6 fallback
    assert fl.reader.fast_demoted is True
    assert any("fast read path demoted" in n for n in notes)
    fl.finish_report(report)
    assert report.fast_read == "demoted"


def test_a_lie_in_a_later_block_is_caught_by_the_sampled_cross_check() -> None:
    device, fl, notes, data, report = _programmed_fast_target(LyingLaterReadTarget)
    # Lie at the start of the second fast block (the first is fully proven).
    device.lie_at = device.AHB_TEST_BASE + 0x0030_0000 + flasher.READ_PROOF_BYTES
    fl.verify(data, 0x0030_0000, report, mode="full")
    assert fl.reader.fast_demoted is True
    assert any("sampled cross-check disagreed" in n for n in notes)


def test_a_transient_readmem_failure_falls_back_per_block_and_stays_on() -> None:
    device, fl, notes, data, report = _programmed_fast_target(FlakyReadTarget)
    fl.verify(data, 0x0030_0000, report, mode="full")
    assert fl.reader.fast_demoted is False
    assert fl.reader.fast_ever_used is True
    assert any("the fast path stays on" in n for n in notes)


class FastWriteFlashTarget(SyntheticTarget):
    """Transport-side WriteMem that works, for exercising the budget."""

    can_block_write = True

    def __init__(self) -> None:
        super().__init__()
        self.block_writes: list[tuple[int, int]] = []

    def write_mem_block(self, addr: int, data: bytes) -> None:
        self.block_writes.append((addr, len(data)))
        for i in range(0, len(data), 4):
            self.mem[addr + i] = int.from_bytes(data[i : i + 4], "little")


class ResumingWriteFlashTarget(FastWriteFlashTarget):
    """Models the measured 2026-08-05 DLL behaviour: every WriteMem burst
    leaves the SMU running again (Finding 6, flash edition)."""

    def write_mem_block(self, addr: int, data: bytes) -> None:
        super().write_mem_block(addr, data)
        self.m33["halted"] = False


def test_a_writemem_that_resumes_the_smu_is_repinned_before_the_applet(monkeypatch) -> None:
    """Without the re-pin, the register save after staging dies with S_REGRDY
    clear (DCRSR needs Debug state) -- the exact first-applet-call failure
    measured at the bench. With it, the flow completes and says what it did."""
    device, _dap, fl, notes = make_flasher(ResumingWriteFlashTarget(), use_fast=True)
    data = bytes(range(256))
    report = flasher.FlashReport(op="program")
    fl.program(data, 0x0030_0000, report)
    fl.verify(data, 0x0030_0000, report, mode="full")
    assert any("resumed the SMU behind our back" in n for n in notes)
    assert device.m33["halted"] is True, "the flow must end with the SMU pinned"


class FailingResumingWriteTarget(FastWriteFlashTarget):
    """The 2026-08-06 bench shape: every WriteMem burst FAILS (rc -1) -- and
    still resumes the SMU, because the DLL wraps even a failing access in its
    stop-mode halt+access+go."""

    def write_mem_block(self, addr: int, data: bytes) -> None:
        self.m33["halted"] = False
        raise TransportError(f"JLINK_WriteMem(0x{addr:08X}, {len(data)}) returned -1")


def test_a_failed_writemem_burst_still_gets_the_repin(monkeypatch) -> None:
    """Measured 2026-08-06 on the DEST0-recovered board: the first staging
    burst failed rc -1, fell back to ADIv6 -- and the DCRSR register save
    then died with S_REGRDY clear, because the resume side effect rides
    FAILED bursts too. The re-pin gates on attempted bursts, not successful
    ones."""
    monkeypatch.setattr(loader, "FAST_RETRY_PAUSE_S", 0)
    device, _dap, fl, notes = make_flasher(FailingResumingWriteTarget(), use_fast=True)
    data = bytes(range(256))
    report = flasher.FlashReport(op="program")
    fl.program(data, 0x0030_0000, report)
    fl.verify(data, 0x0030_0000, report, mode="full")
    assert fl.writer.fast_ever_used is False, "no burst ever succeeded"
    assert any("resumed the SMU behind our back" in n for n in notes)
    assert device.m33["halted"] is True, "the flow must end with the SMU pinned"


def test_the_finding9_budget_retires_both_fast_paths_mid_flow(monkeypatch) -> None:
    """Finding 9: the DLL's WriteMem wedges after ~4 MB per connection. The
    flow counts every fast-path byte and retires both directions *before* the
    wedge -- here the ceiling is lowered so the applet staging fits under it
    but the payload staging would cross it."""
    monkeypatch.setattr(flasher, "FAST_BUDGET_BYTES", 4096)
    device, _dap, fl, notes = make_flasher(FastWriteFlashTarget(), use_fast=True)
    data = bytes(range(256)) * 16  # 0x1000 of payload
    report = flasher.FlashReport(op="program")
    fl.program(data, 0x0030_0000, report)
    fl.verify(data, 0x0030_0000, report, mode="full")
    assert any("fast paths retired" in n for n in notes)
    assert not fl.writer.fast_alive and not fl.reader.fast_alive
    # The applet itself was still staged by WriteMem before the retirement...
    assert any(addr == qf.APPLET_BASE for addr, _n in device.block_writes)
    # ...and the payload was not: past the line everything rides ADIv6.
    assert not any(addr == qf.APPLET_DATA for addr, _n in device.block_writes)
    # Retirement is thrift, not mistrust: the report says used, not demoted.
    fl.finish_report(report)
    assert report.fast_write == "used"


# ---------------------------------------------------------------------------
# Golden inputs, when they exist on this machine
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_blob_path() is None, reason="no bootloader BLOB on this machine")
def test_golden_blob_identity_and_plans() -> None:
    """The numbers a bench `flash program <blob> --offset 0` must reproduce.
    S32EDBG_FLASH_BLOB points this at another BLOB of the same format (the
    v3.2.0-specific pins then step aside)."""
    path = _blob_path()
    blob = path.read_bytes()
    assert blob[0] == 0xD1, "a BLOB starts with the BootROM IVT tag"
    assert len(blob) <= qf.REGIONS["blob"][1], "it must fit the Blob Area"

    program_length = flasher.align_up(len(blob), qf.PAGE_SIZE)
    padded = blob + b"\xff" * (program_length - len(blob))
    runs, blank, non_blank = flasher.build_non_blank_program_runs(
        padded, program_length, qf.PROGRAM_CHUNK
    )
    assert sum(length for _o, length in runs) == non_blank * qf.PAGE_SIZE
    assert (blank + non_blank) * qf.PAGE_SIZE == program_length

    plan = flasher.build_erase_plan(0, flasher.align_up(len(blob), qf.SECTOR_SIZE))
    covered = sum(length for _o, length, _u, _n, _op in plan)
    assert covered == flasher.align_up(len(blob), qf.SECTOR_SIZE)

    if path == MULTITOOL_BLOB:
        assert len(blob) == 1_907_168
        assert (len(runs), blank, non_blank) == (10, 3372, 4078)
        assert [(o, length, u) for o, length, u, _n, _op in plan] == [
            (0x0, 0x10_0000, qf.BLOCK_ERASE_SIZE),
            (0x10_0000, 0xD_0000, qf.BLOCK_ERASE_SIZE),
            (0x1D_0000, 0x2000, qf.SECTOR_SIZE),
        ]


@pytest.mark.skipif(not BR1_REAR_ELF.exists(), reason="br1_rear.elf not on this machine")
def test_golden_br1_rear_flattens_into_the_app_area() -> None:
    """The application path: flatten spans ~5.8 MiB (fits the 6 MiB app area)
    but stages only ~3.5 MiB of real bytes thanks to the blank-page skip --
    the synergy that keeps the flow inside the Finding-9 WriteMem budget."""
    image, blob = elf.load(BR1_REAR_ELF)
    base, flat = elf.flatten(image, blob)
    assert base == 0x3210_0000
    assert len(flat) == 0x5C_BF9C
    assert len(flat) <= qf.REGIONS["app"][1]

    program_length = flasher.align_up(len(flat), qf.PAGE_SIZE)
    padded = flat + b"\xff" * (program_length - len(flat))
    runs, blank, non_blank = flasher.build_non_blank_program_runs(
        padded, program_length, qf.PROGRAM_CHUNK
    )
    assert (len(runs), blank, non_blank) == (22, 9374, 14370)
    assert non_blank * qf.PAGE_SIZE == 3_678_720  # what actually stages

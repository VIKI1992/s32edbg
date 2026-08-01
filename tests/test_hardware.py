"""Bench acceptance gates. Requires a probe and a powered target.

Excluded from the default run by ``addopts = "-m 'not hardware'"``. Run them
deliberately, **in file order**, and stop at the first failure:

    pytest -m hardware -v -x

File order is dependency order. Nothing below `test_the_debug_gate_opens` can
work until it passes, because RM 135.11 leaves the debug clocks off by default
and warns that touching debug components before they are enabled "might hang the
debug APBIC bus".

Before any of this: check the PMIC and SBC enable switches. A healthy 3.3 V
VTREF says nothing about the 0.8 V core rail, and an unpowered core means a
legitimately dead TAP (RM Table 295, Table 301).

What these tests will and will not change on the target
-------------------------------------------------------
Read-only, except for three deliberate exceptions, each marked in its docstring:

* `gates apply` writes the four LADDER-1 MDM-AP registers. Sticky until power
  loss, and the documented mechanism for enabling debug clocks.
* `attach`/`release` write SDASYSRSTC to hold and then release functional reset.
  **This stops the application.** RM Table 301 confirms the JTAGC survives it.
* The `mem write` test sets and then clears a CoreSight CLAIM tag, which is
  architecturally a software-only scratch field with no hardware effect.

Nothing here ever writes the SDA-AP authentication path, drives a boundary-scan
output, or switches to SWD.

Why several tests are xfail rather than deleted
-----------------------------------------------
The raw-JTAG path (`WJTAGRaw`) does not report TDO consistently on this
probe/target combination, so `calibrate_tdo_order()` correctly refuses and
everything built on it is unreachable. Boundary scan runs through SVF/JTAGLoad
instead. Those tests stay, marked non-strict xfail: on different probe firmware
they may start working, and if they do that is news rather than a failure.
"""

from __future__ import annotations

import pytest

from s32edbg.dap import apmap as apmap_mod
from s32edbg.dap import gates as gates_mod
from s32edbg.dap import memaccess
from s32edbg.dap import romtable
from s32edbg.dap.adiv6 import Dap
from s32edbg.data.bscan import BSCAN_DEFAULT_IRLEN
from s32edbg.data.expected_apmap import AP_APB, AP_SDA, APBIC2_ROM_BASE
from s32edbg.data.known_ids import JTAG_IDCODES
from s32edbg.data.ladder1 import SDA_SYSRSTC, SDA_SYSRSTC_SYSFUNCREQ
from s32edbg.errors import DapError, S32EError
from s32edbg.jtag import bscan as bscan_mod
from s32edbg.jtag import tap as tap_mod
from s32edbg.transport import make_transport
from s32edbg.transport.install import discover

pytestmark = pytest.mark.hardware

#: RM Table 961, S32E277/8/9 in the 27x27 package.
EXPECTED_IDCODE = 0x0831_F01D

#: Read off this silicon through J-Link's connect banner. DPv3 = ADIv6.
EXPECTED_DPIDR = 0x4C01_3477

#: What `romdump --source apb` found once the gate was open. Floors, not
#: equalities: a future silicon revision may expose more.
MIN_APB_ROM_TABLES = 12
MIN_APB_COMPONENTS = 74

#: CoreSight identification-block offsets (ADIv6 p280 onwards). CLAIMSET/CLAIMCLR
#: are architecturally software-use-only claim tags with no hardware effect,
#: which is what makes them the one safe write target on a live ECU.
OFF_CLAIMSET = 0xFA0
OFF_CLAIMCLR = 0xFA4
OFF_LSR = 0xFB4
CIDR_PREAMBLE = (0x0D, 0x05, 0xB1)  # CIDR0, CIDR2, CIDR3; CIDR1 carries the class

RAW_JTAG_REASON = (
    "WJTAGRaw does not report TDO consistently on this probe; boundary scan runs "
    "through SVF/JTAGLoad instead. See the module docstring."
)


@pytest.fixture()
def commander():
    t = make_transport("commander", install=discover(), device="CORTEX-R52", speed_khz=4000)
    t.open()
    try:
        yield t
    finally:
        t.close()


@pytest.fixture()
def dap():
    """A powered-up DAP over the in-process DLL backend.

    This is the backend that lands AP writes on this target. The batch backend
    can drive vectors but its WriteDP/ReadAP sequence returns zeros for every AP
    here, which is why `attach` was rebuilt on this path.
    """
    t = make_transport("dll", install=discover())
    t.open()
    try:
        t.select_interface(0)
        t.set_speed_khz(4000)
        try:
            t.set_jtag_config(-1, -1)
            t.select_device("CORTEX-R52")
            t.connect_target()
        except S32EError:
            pass  # the core is invisible behind a closed gate; the DAP still works
        d = Dap(t)
        d.power_up()
        yield d
    finally:
        t.close()


# ---------------------------------------------------------------------------
# 0-1. The probe and the wire
# ---------------------------------------------------------------------------


def test_the_probe_enumerates_and_reports_a_target_voltage(commander) -> None:
    """Checkpoint 0. VTREF near 3.3 V and a probe identity."""
    info = commander.probe_info()
    assert info.serial, "no probe enumerated"
    assert info.vtref_mv is not None and info.vtref_mv > 2000, (
        f"VTREF reads {info.vtref_mv} mV. Note this only proves the 3.3 V rail is up; "
        "the 0.8 V core rail comes from a different regulator, and an unpowered core "
        "is indistinguishable from a dead TAP at this level."
    )


def test_the_chain_scan_finds_the_expected_idcode(commander) -> None:
    """Checkpoint 1, via J-Link's own scan rather than a raw shift.

    A zero or all-ones IDCODE here almost always means JCOMP is low or the core
    rail is off, not that the tool is wrong (RM 60.5.4.1). If it is intermittent,
    the target is rebooting under you -- use `s32edbg attach`.
    """
    facts = commander.scan_chain()
    assert facts.get("idcode") == EXPECTED_IDCODE, (
        f"expected 0x{EXPECTED_IDCODE:08X}, got {facts.get('idcode')!r}. "
        f"Known IDCODEs: {[hex(k) for k in JTAG_IDCODES]}"
    )


# ---------------------------------------------------------------------------
# 2. DP identity, and the two things it is easy to conflate
# ---------------------------------------------------------------------------


def test_the_batched_identity_read_returns_the_documented_dpidr(commander) -> None:
    """Checkpoint 2. DPv3 / ADIv6 / SoC-600.

    The value comes from J-Link's connect banner, not from a DPACC transaction.
    Over JTAG that is the *only* correct source -- see the next test.
    """
    ident = commander.read_dp_identity()
    assert ident["dpidr"] == EXPECTED_DPIDR, (
        f"expected 0x{EXPECTED_DPIDR:08X}, got {ident['dpidr']!r}. "
        "0x00000000 and 0xFFFFFFFF are not DPIDR values, they are how a wire reads "
        "when nothing drives it."
    )


def test_reading_dpidr_through_dpacc_over_jtag_is_refused(dap) -> None:
    """Defect D-1, found here on 2026-08-01, fixed, and pinned so it stays fixed.

    ADIv6 Figure A1-2 makes JTAG-DP DPACC address 0b00 **Reserved**: there is no
    DPIDR to read there. Reading it anyway does not fault -- it returned
    0x4BA07477 on this part, close enough to the real 0x4C013477 to look right,
    and the same bad read propagated into DPIDR1 and BASEPTR because all four
    registers live at DP address 0x0 and are chosen by DPBANKSEL. The garbage
    BASEPTR then sent the AP-space ROM walk to 0x4BA07000.

    So the correct behaviour is to refuse, and to report identity as absent
    while still reading CTRL/STAT at DP 0x4 -- which is the register that
    actually answers "is the DAP alive".
    """
    with pytest.raises(DapError, match="Figure A1-2"):
        dap.read_dpidr()

    info = dap.info()
    assert info.dpidr is None
    assert info.baseptr is None
    assert info.ctrl_stat not in (0x0000_0000, 0xFFFF_FFFF), (
        f"CTRL/STAT reads 0x{info.ctrl_stat:08X}; the DAP is not answering at all, "
        "so this test cannot distinguish the fix from a dead link"
    )


# ---------------------------------------------------------------------------
# 3. The gate. Everything below this line depends on it.
# ---------------------------------------------------------------------------


def test_the_debug_gate_opens_and_reads_back_exactly(dap) -> None:
    """Checkpoint 3. **Writes the four LADDER-1 MDM-AP registers.**

    RM 135.11: the debug clocks are off by default and are enabled by "setting
    any enable bit from the MDM-AP DBGGENCNTRL, DBGGENCNTRL2, and DBGGENCNTRL3
    registers". Idempotent and sticky until power loss.

    A read-back of zero does not mean this sequence is wrong -- it means MDM-AP
    writes are being rejected, which points at the HSE gate instead.
    """
    report = gates_mod.apply_ladder1(dap)
    assert report.mdm.identity_ok, f"MDM-AP IDR is 0x{report.mdm.idr or 0:08X}"
    assert report.mdm.enables_set, (
        "DBGENCNTRL1-3 still read zero after the write. "
        f"warnings: {report.warnings}"
    )
    assert report.mdm.registers["DBGENCNTRL"] == 0x3CF3_CF00


def test_the_sda_ap_reports_its_identity_without_being_written(dap) -> None:
    """The HSE gate is read-only here. RM 135.13.4: "HSE allows one only attempt
    for C/R comparison. If it fails, no further attempts are accepted."
    """
    state = gates_mod.read_hse(dap)
    assert state.identity_ok, f"SDA-AP IDR is 0x{state.idr or 0:08X}"


# ---------------------------------------------------------------------------
# 4. The AP map and the ROM trees
# ---------------------------------------------------------------------------


def test_every_ap_rm_table_957_documents_is_present_with_the_right_idr(dap) -> None:
    """Checkpoint 4. Two independent discovery methods, merged, then diffed
    against the RM. Each method fails differently, so agreement is the evidence.
    """
    gates_mod.apply_ladder1(dap)
    result = apmap_mod.discover(dap)
    diff = result.diff_against_rm()
    assert not any(diff.values()), diff


def test_the_apb_rom_tree_enumerates_once_the_gate_is_open(dap) -> None:
    """Checkpoint 5. The APB-AP is the only route to APBIC2's 28 MB of debug
    components (RM 135.5.2), so this is the test that proves MEM-AP transfers
    work at all.
    """
    gates_mod.apply_ladder1(dap)
    rom, present = dap.read_ap_base(AP_APB)
    assert present and rom is not None, "APB-AP BASE.P is clear: no debug entry"
    root = romtable.walk(dap.mem_reader(AP_APB), rom)
    summary = romtable.summarise(root)
    assert summary["tables"] >= MIN_APB_ROM_TABLES, summary
    assert summary["components"] >= MIN_APB_COMPONENTS, summary
    # Deliberately not `errors == 0`. Measured: 12 error nodes, and all 12 are
    # correct. Eleven are unmapped APBIC2 regions whose reads echo address bits
    # (0x01050000 -> CIDR 05 05 05 05); the twelfth, 0x01190000, is a real
    # component whose CIDR1 is 0x09 when the low nibble must be zero. Recording
    # them and walking on is the designed behaviour -- demanding zero would make
    # a correct walk fail.


# ---------------------------------------------------------------------------
# 5. Bus access. Never exercised on silicon before this suite existed.
# ---------------------------------------------------------------------------


def _first_component(dap: Dap) -> int:
    """A component address the ROM walk actually found, to read known values from."""
    rom, _present = dap.read_ap_base(AP_APB)
    root = romtable.walk(dap.mem_reader(AP_APB), rom or APBIC2_ROM_BASE)
    for node in root.flatten():
        if node.ident is not None and node.ident.preamble_ok and not node.is_table:
            return node.base
    pytest.skip("no component with a valid ID block found behind the APB-AP")


def test_a_read_through_the_apb_ap_returns_the_coresight_id_preamble(dap) -> None:
    """The oracle for read correctness: a known answer reached by a new path.

    ADIv6 fixes CIDR0/2/3 at 0x0D/0x05/0xB1 for every CoreSight component, so a
    `mem read` of +0xFF0 has an answer that does not depend on this silicon.
    """
    gates_mod.apply_ladder1(dap)
    base = _first_component(dap)
    cidr = memaccess.read_words(dap, AP_APB, base + 0xFF0, 4, size=32)
    assert (cidr[0] & 0xFF, cidr[2] & 0xFF, cidr[3] & 0xFF) == CIDR_PREAMBLE, (
        f"component at 0x{base:08X} read back CIDR {[hex(c) for c in cidr]}"
    )


def test_a_block_read_across_a_1kb_boundary_agrees_with_individual_reads(dap) -> None:
    """The failure this exists to catch is silent.

    ADIv6 C2.6.4 only guarantees TAR auto-increment inside a 1 KB window; the AP
    may wrap at the boundary instead of carrying. A reader that does not rewrite
    TAR there returns words from the *start* of the window -- plausible values
    from the wrong addresses, with no fault raised anywhere. Comparing a block
    that straddles the boundary against word-at-a-time reads is the only check
    that sees it.

    The region has to be chosen with care, and the obvious choice is wrong. A
    block straddling the first boundary of a debug frame reads all zeros on both
    sides, so it agrees with single reads whether or not TAR was rewritten --
    the test passes vacuously. This one instead runs from +0xBF0 to the end of
    the 4 KB page, crossing the 0xC00 boundary and finishing on the
    identification block, whose values are known and non-zero. A reader that
    wrapped would take those last words from the +0x800 window and return zeros.
    """
    gates_mod.apply_ladder1(dap)
    base = _first_component(dap)
    start = base + 0xBF0
    count = (0x1000 - 0xBF0) // 4  # 260 words, ending at +0xFFC

    block = memaccess.read_words(dap, AP_APB, start, count, size=32)
    assert len(block) == count
    cidr = [v & 0xFF for v in block[-4:]]
    assert (cidr[0], cidr[2], cidr[3]) == CIDR_PREAMBLE, (
        f"the last four words of a boundary-crossing block read are {[hex(v) for v in cidr]}, "
        f"not the ID block at 0x{base + 0xFF0:08X} -- TAR is not being rewritten at the "
        "1 KB boundary, so these came from the wrong window"
    )

    singles = [
        memaccess.read_words(dap, AP_APB, base + 0xFF0 + i * 4, 1, size=32)[0] for i in range(4)
    ]
    assert block[-4:] == singles


def test_a_byte_read_agrees_with_the_word_that_contains_it(dap) -> None:
    """ADIv6 C2.6.6 puts a sub-word transfer in the byte lane the low address
    bits select, not right-aligned in DRW. Getting the lane wrong produces a
    valid-looking value from the wrong quarter of the word."""
    gates_mod.apply_ladder1(dap)
    base = _first_component(dap)
    word = memaccess.read_words(dap, AP_APB, base + 0xFF0, 1, size=32)[0]
    lanes = memaccess.read_words(dap, AP_APB, base + 0xFF0, 4, size=8)
    assert lanes == [(word >> (8 * i)) & 0xFF for i in range(4)]


def test_a_claim_tag_can_be_set_and_cleared(dap) -> None:
    """**Writes to the target**, but to the one field where that is free.

    CLAIMSET/CLAIMCLR are architecturally software-use-only tags -- they exist so
    a debugger can mark ownership of a component and have no hardware effect
    whatever. If the software lock (LSR) is engaged the write is ignored, which
    is a legitimate outcome and is skipped rather than failed.
    """
    gates_mod.apply_ladder1(dap)
    base = _first_component(dap)

    lsr = memaccess.read_words(dap, AP_APB, base + OFF_LSR, 1, size=32)[0]
    if lsr & 0b011 == 0b011:  # implemented and locked
        pytest.skip(f"component at 0x{base:08X} has its software lock engaged (LSR 0x{lsr:08X})")

    before = memaccess.read_words(dap, AP_APB, base + OFF_CLAIMCLR, 1, size=32)[0]
    try:
        memaccess.write_word(dap, AP_APB, base + OFF_CLAIMSET, 0x1, size=32)
        after = memaccess.read_words(dap, AP_APB, base + OFF_CLAIMCLR, 1, size=32)[0]
        assert after & 0x1, f"CLAIM bit 0 did not set (CLAIMCLR 0x{after:08X})"
    finally:
        memaccess.write_word(dap, AP_APB, base + OFF_CLAIMCLR, ~before & 0x1, size=32)


# ---------------------------------------------------------------------------
# 6. attach / release. Stops and restarts the application.
# ---------------------------------------------------------------------------


def test_the_hold_and_release_round_trip(dap) -> None:
    """**Stops the running application, then restarts it.**

    RM 135.11 names SDASYSRSTC[SYSFUNCREQ] as the debugger's own hold, and RM
    Table 301 is what makes it usable: a functional reset does not reset the
    JTAGC or the SWJ-DP, so the debug connection survives while the cores stop.

    The release half is the part that had never been confirmed. A hold with no
    proven release leaves a target stopped with no obvious way back.
    """
    gates_mod.hold_in_functional_reset(dap)
    held = dap.ap_read(AP_SDA, SDA_SYSRSTC)
    assert held & SDA_SYSRSTC_SYSFUNCREQ, f"hold did not take (SDASYSRSTC 0x{held:08X})"

    gates_mod.release_functional_reset(dap)
    freed = dap.ap_read(AP_SDA, SDA_SYSRSTC)
    assert not freed & SDA_SYSRSTC_SYSFUNCREQ, f"still held (SDASYSRSTC 0x{freed:08X})"


def test_the_axi_ap_is_refused_while_the_system_is_held(dap) -> None:
    """**Stops the application briefly.** The guard is worth proving on silicon
    rather than only against a fake, because RM Table 957 says the cost of it not
    firing is peripherals that stay unreachable "until you issue a destructive
    reset"."""
    from s32edbg.data.expected_apmap import AP_AXI
    from s32edbg.errors import UnsafeOperation

    gates_mod.hold_in_functional_reset(dap)
    try:
        with pytest.raises(UnsafeOperation, match="destructive reset"):
            memaccess.preflight(dap, AP_AXI, write=False)
    finally:
        gates_mod.release_functional_reset(dap)


# ---------------------------------------------------------------------------
# 7. Boundary scan, through the SVF path that works
# ---------------------------------------------------------------------------


def test_the_boundary_scan_preamble_completes_through_svf(tmp_path) -> None:
    """Checkpoint 6. The RM Table 984 sequence must play without error.

    IR width comes from `BSCAN_DEFAULT_IRLEN` rather than a literal, because the
    RM contradicts itself (Table 959 says 16, Table 984 uses 8) and silicon
    settled it at 16 -- at 8 bits SAMPLE/PRELOAD falls through to IDCODE and the
    BSR is never selected. A literal here is how the two drifted apart before.
    """
    from s32edbg.transport import svf as svf_mod

    path = tmp_path / "preamble.svf"
    path.write_text(
        svf_mod.emit_svf_for_preamble(BSCAN_DEFAULT_IRLEN, frequency_hz=4_000_000),
        encoding="ascii",
    )
    result = svf_mod.run_svf(path, install=discover())
    assert result.ok, result.stdout


def test_the_capture_is_stable_in_structure_but_not_frozen() -> None:
    """Checkpoint 7, and the proof that the scan reaches real boundary cells.

    Non-invasive: RM 136.4.4.2 says PRELOAD loads only the update latches, which
    drive nothing unless EXTEST or CLAMP is selected, and this tool selects
    neither.

    Equality between captures is the wrong assertion, and asserting it would
    have hidden the answer. Three outcomes have to be told apart:

      identical every time  -> could be live-and-quiet, could be a stuck path
      a few bits moving     -> live: mostly-static pins with one or two toggling
      everything moving     -> noise, not a capture

    Measured on a board running an application: 0x0035025555A02006 then
    0x0015025555A02006 -- one bit of 64, the other 63 held. A quiescent board
    gave 0x0000000000000006. So the capture tracks board state, which is what
    "the boundary scan works" actually means.

    **Run this last.** The preamble links JTAGC_AE into the chain and takes the
    DAP away until a power cycle -- see the module docstring for `bscan`.
    """
    captures = [bscan_mod.svf_sample(BSCAN_DEFAULT_IRLEN, 64, install=discover()) for _ in range(3)]
    raws = [c.raw for c in captures]

    assert not all(r in (0, (1 << 64) - 1) for r in raws), (
        f"every capture is degenerate ({[hex(r) for r in raws]}). An all-zero or all-ones "
        "capture usually means the preamble did not take and the BSR was never selected."
    )

    unstable = sum(
        1 for bit in range(64) if len({(r >> bit) & 1 for r in raws}) > 1
    )
    assert unstable <= 8, (
        f"{unstable} of 64 bits moved across three captures ({[hex(r) for r in raws]}). "
        "That is noise rather than pin state -- check the scan length and the preamble."
    )


def test_the_preamble_links_the_ae_tap_and_takes_the_dap_away(commander) -> None:
    """**Destructive to DAP access until a power cycle. Keep this last.**

    This is the fact that cost the most to find, so it is pinned here rather
    than left in prose. The RM Table 984 preamble's final step writes
    TLM_TAP_SELECT_TDR = 0x8000_0011, which links JTAGC_AE into the scan chain.
    Measured on 2026-08-01:

        before the preamble   TotalIRLen 8    CTRL/STAT 0xF0000000
        after the preamble    TotalIRLen 16   CTRL/STAT 0x00000001

    and the change persists. J-Link's DAP addressing is set up for the chain it
    scanned at connect, so once the chain grows a TAP it cannot identify, every
    DP/AP path stops working on both backends -- `attach` could not power up the
    DAP at all afterwards.

    Nothing is damaged, and the recovery is confirmed rather than assumed: after
    a power cycle `doctor` reported `TotalIRLen 8` again and `attach`, `release`
    and `report` all ran normally. That is why this test asserts a *cold* chain
    of 8 first -- running it twice without a power cycle in between would fail
    on the precondition, which is the correct outcome.

    It also settles the RM's apparent self-contradiction. Table 958's 8-bit
    opcodes describe the cold chain and Table 959's 16-bit opcodes describe the
    post-preamble chain, so both are right about different states. And "the
    board routes no AE JTAG port" was a red herring throughout: the AE TAP is
    internal to the SIP and linked by the TLM, never brought to a connector.
    """
    before = commander.scan_chain().get("total_ir_len")
    assert before == 8, f"expected a cold chain of 8 bits, got {before!r}"

    result = bscan_mod.run_preamble(commander, BSCAN_DEFAULT_IRLEN)
    if not result.ok:
        pytest.skip(f"preamble did not run on this backend: {result.steps}")

    after = commander.scan_chain().get("total_ir_len")
    assert after == 16, (
        f"the chain is still {after!r} bits after the preamble. Either "
        "TLM_TAP_SELECT_TDR did not land, or this part links its AE TAP differently."
    )


# ---------------------------------------------------------------------------
# 9. The raw-JTAG floor: present, refused, and left visible
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason=RAW_JTAG_REASON, strict=False)
def test_the_tdo_byte_order_can_be_established(commander) -> None:
    """The gate for every raw-JTAG result.

    Shifts a known pattern through the one-bit BYPASS register, whose answer is
    known in advance, and accepts the byte order that reproduces it. It refuses
    on this hardware, which is the correct behaviour -- do not "fix" the tests
    below by relaxing them.
    """
    assert commander.calibrate_tdo_order() in ("little", "big")


@pytest.mark.xfail(reason=RAW_JTAG_REASON, strict=False)
def test_the_raw_path_and_the_chain_scan_agree_on_the_idcode(commander) -> None:
    """Two independent routes to one known answer: a self-built TMS/TDI vector
    versus J-Link's own chain scan. Agreement on RM Table 961's value is what
    would make the raw path trustworthy."""
    commander.calibrate_tdo_order()
    raw = tap_mod.read_idcode(commander)
    assert raw == EXPECTED_IDCODE == commander.scan_chain().get("idcode")


@pytest.mark.xfail(reason=RAW_JTAG_REASON, strict=False)
def test_the_bypass_register_measures_exactly_one_bit(commander) -> None:
    """A BYPASS DR of anything but 1 means more than one TAP is in the chain,
    which the board schematic does not show (RM 136.3.2)."""
    commander.calibrate_tdo_order()
    assert tap_mod.measure_dr_length_bypass(commander, 8) == 1


@pytest.mark.xfail(reason=RAW_JTAG_REASON, strict=False)
def test_the_boundary_scan_register_length_can_be_measured(commander) -> None:
    """Blocked on the same capability. RM 136.3.4 puts the BSR length only in the
    BSDL file, which RM Table 984 names but does not supply, so measuring it is
    the only route -- and measurement needs a TDO-reporting shift."""
    commander.calibrate_tdo_order()
    assert bscan_mod.run_preamble(commander, BSCAN_DEFAULT_IRLEN).ok
    assert bscan_mod.measure_bsr_length(commander, BSCAN_DEFAULT_IRLEN) > 1

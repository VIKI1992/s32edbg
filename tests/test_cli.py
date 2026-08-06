"""The command-line surface: exit codes, refusals, and end-to-end wiring.

Every other test file exercises a library module. This one exercises the thing
the operator actually types, which matters because **the safety policy lives in
the CLI layer**: the SWD opt-in, the `mem write` confirmation, the dump
alignment check and the exit-code contract in `entry()` are all implemented
here and nowhere else.

Two halves:

* Tests that need no probe *and no patching*, because the path refuses before it
  ever opens a transport. These run on a machine with no SEGGER install.
* Tests that patch `discover`/`make_transport` to a `SyntheticTarget` and drive
  a whole command through to its JSON output. Those assert on the JSON file
  rather than console text, because the JSON is a contract and Rich's rendering
  is not.

`entry()` is called directly rather than through `CliRunner`, because
`CliRunner` re-implements the standalone-mode error handling and would not see
the 2/3/130 mapping that is the thing under test.
"""

from __future__ import annotations

import io
import json
import sys

import pytest
from conftest import DPIDR_S32E2, SyntheticTarget
from rich.console import Console

from s32edbg import cli
from s32edbg.data.expected_apmap import AP_ALIASES, EXPECTED_APS

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TOOL_ERROR = 2
EXIT_REFUSED = 3
EXIT_ABORTED = 130


@pytest.fixture()
def run(monkeypatch, capsys):
    """Invoke `entry()` as the console script would, and capture what it printed.

    Output arrives by two routes and both matter. The Rich consoles are replaced
    with wide ones writing to a buffer -- at the default terminal width Rich
    wraps mid-sentence, and a wrapped message fails a substring assertion for a
    reason that has nothing to do with the behaviour under test. Click's own
    output (`--help`, `--version`, the confirmation prompt) never goes through
    Rich, and Click caches its stdout wrapper so patching `sys.stdout` does not
    redirect it; that half comes from `capsys` instead.
    """
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=200, no_color=True))
    monkeypatch.setattr(cli, "err_console", Console(file=buffer, width=200, no_color=True))

    def invoke(*argv: str, stdin: str = "") -> tuple[int, str]:
        monkeypatch.setattr(sys, "argv", ["s32edbg", *argv])
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = cli.entry()
        captured = capsys.readouterr()
        return code, buffer.getvalue() + captured.out + captured.err

    return invoke


@pytest.fixture()
def target(monkeypatch) -> SyntheticTarget:
    """Patch the transport factory so commands run against a synthetic S32E2.

    One instance for the whole test, so a `gates apply` in one invocation is
    still in effect for the `mem read` in the next -- which is exactly how the
    sticky LADDER-1 bits behave on silicon (RM 135.11).
    """
    device = SyntheticTarget()
    monkeypatch.setattr(cli, "discover", lambda *a, **k: object())
    monkeypatch.setattr(cli, "make_transport", lambda kind, **kwargs: device)
    return device


def read_json(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def all_commands() -> list[list[str]]:
    """Every command path in the tool, discovered rather than listed.

    A hardcoded list goes stale the moment someone adds a subcommand; this
    cannot, so `--help` coverage stays complete by construction.
    """
    found: list[list[str]] = []

    def walk(group, prefix: list[str]) -> None:
        for name, command in sorted(group.commands.items()):
            path = prefix + [name]
            found.append(path)
            if hasattr(command, "commands"):
                walk(command, path)

    walk(cli.main, [])
    return found


# ---------------------------------------------------------------------------
# Reachability: no probe, no patching, no target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", all_commands(), ids=lambda p: " ".join(p))
def test_every_command_can_render_its_own_help(run, path: list[str]) -> None:
    """Catches a broken decorator or a bad import at the cheapest possible price.

    `--help` builds the whole parameter list, so a typo'd `click.Choice` or an
    `AP_ALIASES` key that no longer resolves shows up here rather than at the
    bench.
    """
    code, out = run(*path, "--help")
    assert code == EXIT_OK
    assert "Usage:" in out


def test_the_root_group_reports_a_version(run) -> None:
    code, out = run("--version")
    assert code == EXIT_OK
    assert "s32edbg" in out


def test_the_ap_option_offers_every_rm_table_957_bus(run) -> None:
    """All nine APs must be addressable by name, or a bus is unreachable without
    the operator looking up a base address in the RM."""
    _code, out = run("mem", "read", "--help")
    for alias in AP_ALIASES:
        assert alias in out
    assert len(AP_ALIASES) == len(EXPECTED_APS)


# ---------------------------------------------------------------------------
# Refusals that happen before a transport is ever opened
# ---------------------------------------------------------------------------


def test_switching_to_swd_without_the_opt_in_is_refused(run) -> None:
    """RM 135.5.1: "Switching back from SWD mode to JTAG mode requires POR."

    Exit 3, not 1: this is the safety-policy code, the same one the SDA-AP and
    AXI-AP guards use. Reporting it as a usage error would put a one-way
    protocol switch in the same bucket as a mistyped flag.
    """
    code, out = run("iface", "swd")
    assert code == EXIT_REFUSED
    assert "POR" in out
    assert cli.SWD_OPT_IN in out


def test_a_misaligned_dump_address_is_rejected(run) -> None:
    code, out = run("mem", "dump", "0x1", "4")
    assert code == EXIT_USAGE
    assert "4-byte aligned" in out


def test_a_dump_length_that_is_not_a_whole_number_of_words_is_rejected(run) -> None:
    code, out = run("mem", "dump", "0x0", "6")
    assert code == EXIT_USAGE
    assert "4-byte aligned" in out


def test_an_unknown_ap_name_is_rejected_and_names_the_valid_ones(run) -> None:
    code, out = run("mem", "read", "0x0", "--ap", "nonsense")
    assert code == EXIT_USAGE
    assert "ahb-smu" in out


def test_sampling_without_a_bsr_length_explains_why_it_cannot_be_measured(run) -> None:
    """RM 136.3.4 puts the BSR length only in the BSDL file, and measuring it
    needs a TDO-reporting raw shift that no backend here provides. The refusal
    has to say so, or it reads as a missing argument the operator forgot."""
    code, out = run("bscan", "sample")
    assert code == EXIT_USAGE
    assert "--bsr-length" in out
    assert "BSDL" in out


def test_declining_the_write_confirmation_aborts_before_touching_the_target(run) -> None:
    """The prompt is the last chance to stop; answering no must leave the target
    completely untouched, which is only true if it happens before the transport
    is opened. No transport is patched in this test, so reaching one would raise
    a discovery error instead of the abort."""
    code, out = run("mem", "write", "0x0", "0x1", stdin="n\n")
    assert code == EXIT_ABORTED
    assert "aborted" in out.lower()


# ---------------------------------------------------------------------------
# Policy refusals that need a live DAP to reach
# ---------------------------------------------------------------------------


def test_writing_the_sda_ap_is_refused_even_with_yes(run, target) -> None:
    """RM 135.13.4: the HSE challenge/response accepts one attempt ever. `--yes`
    skips the *prompt*, not the policy."""
    code, out = run("mem", "write", "0x0", "0x1", "--ap", "sda", "--yes")
    assert code == EXIT_REFUSED
    assert "SDA-AP" in out


def test_the_axi_ap_is_refused_while_functional_reset_is_asserted(run, target) -> None:
    """RM Table 957: an AXI-AP access during reset can leave peripherals
    unreachable "until you issue a destructive reset"."""
    from s32edbg.data.ladder1 import MDM_STATUS, MDM_STATUS_SYSTEM_RESET
    from s32edbg.data.expected_apmap import AP_MDM

    target.ap_space[AP_MDM + MDM_STATUS] = MDM_STATUS_SYSTEM_RESET
    code, out = run("mem", "read", "0x4000_0000".replace("_", ""), "--ap", "axi")
    assert code == EXIT_REFUSED
    assert "destructive reset" in out


def test_the_apb_ap_is_not_refused_for_the_axi_reason(run, target) -> None:
    """The Table 957 warning is about the system interconnect. Blocking the
    debug APB for it would make the tool unusable in exactly the held state that
    `attach` puts the target into."""
    from s32edbg.data.ladder1 import MDM_STATUS, MDM_STATUS_SYSTEM_RESET
    from s32edbg.data.expected_apmap import AP_MDM, APBIC2_ROM_BASE

    target.ap_space[AP_MDM + MDM_STATUS] = MDM_STATUS_SYSTEM_RESET
    code, _out = run("mem", "read", hex(APBIC2_ROM_BASE), "--ap", "apb")
    assert code == EXIT_OK


# ---------------------------------------------------------------------------
# End-to-end, against the synthetic target
# ---------------------------------------------------------------------------


def test_connect_reports_the_dpidr_and_recognises_adiv6(run, target, tmp_path) -> None:
    out_file = tmp_path / "connect.json"
    code, _out = run("--json", str(out_file), "connect")
    assert code == EXIT_OK
    payload = read_json(out_file)
    assert payload["dpidr"] == f"0x{DPIDR_S32E2:08X}"
    assert payload["is_adiv6"] is True


def test_connect_over_jtag_says_dpidr_is_unavailable_instead_of_printing_it(
    run, target, tmp_path
) -> None:
    """Defect D-1, found at the bench on 2026-08-01 and fixed here.

    ADIv6 Figure A1-2 makes JTAG-DP DPACC address 0b00 Reserved, and DPIDR,
    DPIDR1 and BASEPTR0/1 all live at DP address 0x0. `connect` on the dll
    backend read it anyway and printed 0x4BA07477 -- against a real DPIDR of
    0x4C013477 -- with version "reserved(0x7)", a nonsense DPIDR1 and a garbage
    BASEPTR, and exited 0.

    The bad BASEPTR was the expensive part: it sent the AP-space ROM walk to
    0x4BA07000, so `romdump --source apspace` and `report` both failed.
    """
    from s32edbg.transport.base import Tif

    target.selected_tif = Tif.JTAG
    out_file = tmp_path / "connect.json"
    code, out = run("--json", str(out_file), "connect")

    assert code == EXIT_OK
    payload = read_json(out_file)
    assert payload["dpidr"] is None
    assert payload["baseptr"] is None
    assert "Figure A1-2" in payload["unavailable"]
    # CTRL/STAT is at DP 0x4 and stays readable, so the liveness answer survives.
    assert payload["ctrl_stat"] is not None
    assert "unavailable" in out.lower()


def test_report_still_walks_the_apspace_rom_when_baseptr_is_unreachable(
    run, target, tmp_path
) -> None:
    """The other half of D-1. With a garbage BASEPTR the walk started at
    0x4BA07000 and died outside the APBIC1 window; with BASEPTR correctly absent
    it falls back to APBIC1_ROM_BASE, which is where RM Table 957's map lives."""
    from s32edbg.transport.base import Tif

    target.selected_tif = Tif.JTAG
    out_file = tmp_path / "report.json"
    code, _out = run("--irlen", "8", "--json", str(out_file), "report", "--apply-gates")

    assert code == EXIT_OK
    apspace = read_json(out_file)["romtables"]["apspace"]
    assert "error" not in apspace
    assert len(apspace["children"]) == len(EXPECTED_APS)


def test_a_dap_that_never_acknowledges_power_up_is_a_tool_error_not_a_traceback(
    run, target
) -> None:
    """`_prepare_dap` retries, then gives up. The give-up must travel the normal
    error route -- exit 2 with a message naming CTRL/STAT -- because a traceback
    tells a bench operator nothing about which rail is down."""
    target.auto_ack = False
    code, out = run("connect")
    assert code == EXIT_TOOL_ERROR
    assert "power-up not acknowledged" in out


def test_gates_apply_opens_the_gate_and_reads_every_ladder1_register_back(
    run, target, tmp_path
) -> None:
    """RM 135.11: the debug clocks are off by default and these are the bits that
    enable them. Verifying by read-back is the point -- if MDM-AP writes are
    being rejected the registers read zero and the gate is still shut."""
    out_file = tmp_path / "gates.json"
    code, _out = run("--json", str(out_file), "gates", "apply")
    assert code == EXIT_OK
    payload = read_json(out_file)
    assert payload["mdm"]["enables_set"] is True
    for name in ("CONTROL", "DBGENCNTRL", "DBGENCNTRL2", "DBGENCNTRL3"):
        assert name in payload["mdm"]["registers"]
    assert payload["mdm"]["registers"]["DBGENCNTRL"] == "0x3CF3CF00"


def test_the_sda_ap_is_reported_but_never_written_by_gates_apply(run, target) -> None:
    """`gates apply` touches the MDM-AP only. A write landing on the SDA-AP would
    be the unrecoverable one, so assert on the traffic log rather than trusting
    the code path."""
    from s32edbg.data.expected_apmap import AP_SDA

    run("gates", "apply")
    sda_writes = [
        entry for entry in target.log if entry[0] == "ap_write" and (entry[2] & 0xFFFF_0000) == AP_SDA
    ]
    assert sda_writes == []


def test_apmap_finds_every_ap_rm_table_957_documents(run, target, tmp_path) -> None:
    out_file = tmp_path / "apmap.json"
    code, _out = run("--json", str(out_file), "apmap")
    assert code == EXIT_OK
    payload = read_json(out_file)
    assert len(payload["aps"]) == len(EXPECTED_APS)
    diff = payload["diff_against_rm"]
    assert not any(diff.values()), diff


def test_romdump_walks_both_trees(run, target, tmp_path) -> None:
    """AP space and the APBIC2 tree behind the APB-AP are reached by different
    mechanisms -- direct AP reads versus MEM-AP transfers -- so a single command
    covering both is the one that proves `mem_reader` and `apspace_reader` agree
    on what a ROM table looks like."""
    out_file = tmp_path / "rom.json"
    run("gates", "apply")
    code, _out = run("--json", str(out_file), "romdump")
    assert code == EXIT_OK
    payload = read_json(out_file)
    assert len(payload["apspace"]["children"]) == len(EXPECTED_APS)
    assert len(payload["apb"]["children"]) == 2


def test_an_unreadable_component_becomes_an_error_node_instead_of_aborting_the_walk(
    run, target, tmp_path
) -> None:
    """The regression that motivated splitting DapError from TransportError.

    While every failed AP access raised `TransportError`, the `except DapError`
    recovery sites never fired, so the first unreadable component aborted the
    entire ROM walk. On silicon that turns a 74-component dump into nothing.
    """
    from s32edbg.data.expected_apmap import AP_AXI

    target.unreadable = {AP_AXI + off for off in (0xFF0, 0xFF4, 0xFF8, 0xFFC)}
    out_file = tmp_path / "rom.json"
    code, _out = run("--json", str(out_file), "romdump", "--source", "apspace")
    assert code == EXIT_OK
    payload = read_json(out_file)
    assert len(payload["apspace"]["children"]) == len(EXPECTED_APS), "the walk stopped early"
    assert any(child.get("error") for child in payload["apspace"]["children"])


def test_mem_info_reports_the_ap_identity_and_its_csw_as_found(run, target, tmp_path) -> None:
    """CSW is reported before any access because reading it afterwards would show
    what this tool set, not what the AP was configured with."""
    out_file = tmp_path / "info.json"
    code, _out = run("--json", str(out_file), "mem", "info", "--ap", "apb")
    assert code == EXIT_OK
    payload = read_json(out_file)
    assert payload["idr"] == "0x24770006"  # RM Table 957, APB-AP
    assert payload["base"] == "0x01BFE000"
    assert "big_endian" in payload["cfg"]


def test_mem_read_returns_the_requested_number_of_values(run, target, tmp_path) -> None:
    from s32edbg.data.expected_apmap import APBIC2_ROM_BASE

    out_file = tmp_path / "read.json"
    code, _out = run(
        "--json", str(out_file), "mem", "read", hex(APBIC2_ROM_BASE), "--ap", "apb", "--count", "4"
    )
    assert code == EXIT_OK
    payload = read_json(out_file)
    assert len(payload["values"]) == 4
    assert payload["size_bits"] == 32


def test_mem_read_warns_when_the_debug_gate_is_still_closed(run, target, tmp_path) -> None:
    """RM 135.11 warns that touching debug components with the clocks gated
    "might hang the debug APBIC bus". A warning and not a refusal, because the
    operator may be deliberately probing a cold part."""
    from s32edbg.data.expected_apmap import APBIC2_ROM_BASE

    out_file = tmp_path / "read.json"
    code, _out = run("--json", str(out_file), "mem", "read", hex(APBIC2_ROM_BASE), "--ap", "apb")
    assert code == EXIT_OK
    assert any("debug-enable gate is closed" in note for note in read_json(out_file)["notes"])


def test_mem_dump_reads_a_contiguous_block(run, target, tmp_path) -> None:
    from s32edbg.data.expected_apmap import APBIC2_ROM_BASE

    out_file = tmp_path / "dump.json"
    code, _out = run(
        "--json", str(out_file), "mem", "dump", hex(APBIC2_ROM_BASE), "64", "--ap", "apb"
    )
    assert code == EXIT_OK
    assert len(read_json(out_file)["words"]) == 16


def test_a_dump_that_straddles_a_1kb_boundary_returns_the_same_words_as_single_reads(
    run, target
) -> None:
    """ADIv6 C2.6.4 only guarantees auto-increment inside a 1 KB window. A reader
    that ignores the boundary returns words from the *start* of the window --
    plausible values from the wrong addresses, with no error anywhere. Comparing
    the block against individual reads is the only check that catches it, and it
    is the same check the bench procedure runs on silicon.
    """
    from s32edbg.dap.adiv6 import TAR_AUTOINC_BOUNDARY
    from s32edbg.data.expected_apmap import AP_APB, APBIC2_ROM_BASE

    start = APBIC2_ROM_BASE + TAR_AUTOINC_BOUNDARY - 16
    for i in range(8):
        target.mem[start + i * 4] = 0xBEEF_0000 + i

    from s32edbg.dap import memaccess
    from s32edbg.dap.adiv6 import Dap

    dap = Dap(target)
    block = memaccess.read_words(dap, AP_APB, start, 8, size=32)
    singles = [memaccess.read_words(dap, AP_APB, start + i * 4, 1, size=32)[0] for i in range(8)]
    assert block == singles
    assert block == [0xBEEF_0000 + i for i in range(8)]


def test_mem_write_reads_the_value_back(run, target) -> None:
    code, out = run("mem", "write", "0x00001000", "0x5A5A5A5A", "--ap", "apb", "--yes")
    assert code == EXIT_OK
    assert "reads back 0x5A5A5A5A" in out


def test_a_write_whose_readback_differs_says_so_rather_than_reporting_success(
    run, target, monkeypatch
) -> None:
    """Write-1-to-clear and read-only fields legitimately read back differently.
    Silently reporting the written value would make a rejected write look like a
    successful one, which is the failure mode that matters here."""
    from s32edbg.dap.adiv6 import Dap

    monkeypatch.setattr(Dap, "mem_write", lambda *a, **k: None)
    code, out = run("mem", "write", "0x00002000", "0xA5A5A5A5", "--ap", "apb", "--yes")
    assert code == EXIT_OK
    assert "read-back differs" in out


def test_bp_set_on_a_held_core_says_held_in_reset(run, target) -> None:
    """A core that never left reset reads OS-locked too, and 'clear OSLAR' is
    the wrong advice -- unlock refuses with the reset story and points at the
    catch mechanism instead."""
    from conftest import CORE1_DEBUG
    from s32edbg.data import r52debug as r52

    target.mem[CORE1_DEBUG + r52.EDPRSR] = (
        r52.EDPRSR_PU | r52.EDPRSR_R | r52.EDPRSR_OSLK
    )
    # Measured silicon behaviour: core-domain registers fault while held.
    target.faulting.add(CORE1_DEBUG + r52.EDSCR)
    code, out = run("bp", "set", "0x00400120", "--core", "rtu0.1")
    assert code == EXIT_TOOL_ERROR
    assert "held in reset" in out
    assert "core catch" in out


def test_status_notes_a_core_held_in_reset(run, target) -> None:
    from conftest import CORE1_DEBUG
    from s32edbg.data import r52debug as r52

    target.mem[CORE1_DEBUG + r52.EDPRSR] = (
        r52.EDPRSR_PU | r52.EDPRSR_R | r52.EDPRSR_OSLK
    )
    target.faulting.add(CORE1_DEBUG + r52.EDSCR)
    code, out = run("core", "status", "--core", "rtu0.1")
    assert code == EXIT_OK
    assert "held in reset" in out


def test_bp_list_on_a_locked_core_names_unlock(run, target) -> None:
    """Listing is read-only and must not unlock (that changes target state);
    the per-slot 'did not read' rows get a stated reason and the fix."""
    from conftest import CORE0_DEBUG

    target.lock_core(CORE0_DEBUG)
    code, out = run("bp", "list", "--core", "rtu0.0")
    assert code == EXIT_OK
    assert "comparator registers will not read" in out
    assert "Run `unlock`" in out


def test_arming_a_comparator_on_a_running_core_warns_but_arms(run, target) -> None:
    """Guide §5.7/§5.8: program comparators while the core is halted -- the
    change needs a context synchronization event to be guaranteed visible.
    A warning and not a refusal: arming-while-running has fired on this
    silicon, so refusing would remove a working workflow."""
    code, out = run("bp", "set", "0x00400120", "--core", "rtu0.0")
    assert code == EXIT_OK
    assert "is not halted" in out
    code, out = run("wp", "set", "0x00410FF0", "--core", "rtu0.0")
    assert code == EXIT_OK
    assert "is not halted" in out


def test_arming_on_a_halted_core_does_not_warn(run, target) -> None:
    code, out = run(
        "debug", "--core", "rtu0.0", "-c", "halt", "-c", "bp set 0x00400120"
    )
    assert code == EXIT_OK
    assert "is not halted" not in out


def test_core_list_finds_the_cores_and_reports_their_identity(run, target, tmp_path) -> None:
    out_file = tmp_path / "cores.json"
    code, out = run("--json", str(out_file), "core", "list")
    assert code == EXIT_OK
    cores = read_json(out_file)["cores"]
    assert [c["name"] for c in cores] == ["rtu0.0", "rtu0.1"]
    assert all(c["anchors_agree"] for c in cores)
    assert cores[0]["midr"]["part_name"] == "Cortex-R52"
    assert cores[0]["comparators"]["breakpoints"] == 8
    assert "Cortex-R52" in out


def test_core_list_says_what_to_do_when_no_frame_answers(run, target) -> None:
    """The debug clocks are off by default (RM 135.11), so "no cores" is the
    expected first experience on a cold part. It has to name the fix rather than
    just report emptiness."""
    target.mem.clear()
    code, out = run("core", "list")
    assert code == EXIT_TOOL_ERROR
    assert "gates apply" in out


def test_core_status_can_select_one_core(run, target, tmp_path) -> None:
    out_file = tmp_path / "status.json"
    code, _out = run("--json", str(out_file), "core", "status", "--core", "rtu0.1")
    assert code == EXIT_OK
    status = read_json(out_file)["status"]
    assert len(status) == 1
    assert status[0]["core"] == "rtu0.1"
    assert status[0]["halted"] is False


def test_core_status_rejects_an_unknown_core_and_names_the_real_ones(run, target) -> None:
    code, out = run("core", "status", "--core", "rtu9.9")
    assert code == EXIT_USAGE
    assert "rtu0.0" in out


def test_core_status_reports_a_bad_frame_rather_than_refusing_to_look(run, target) -> None:
    """`status` exists to diagnose. Refusing because the identity anchors
    disagree would withhold exactly the information needed."""
    from conftest import CORE0_DEBUG
    from s32edbg.data import r52debug as r52

    target.mem[CORE0_DEBUG + r52.MIDR] = 0x0000_0000
    code, out = run("core", "status", "--core", "0")
    assert code == EXIT_OK
    assert "does not identify as a Cortex-R52" in out


def test_report_collects_every_section_in_one_pass(run, target, tmp_path) -> None:
    """`report` is the only command that touches probe, TAP, DAP, gates, the AP
    map and both ROM trees in a single run, so it doubles as the smoke test for
    the whole tool."""
    out_file = tmp_path / "report.json"
    code, _out = run("--irlen", "8", "--json", str(out_file), "report", "--apply-gates")
    assert code == EXIT_OK
    payload = read_json(out_file)
    for section in ("tool_version", "probe", "tap", "dap", "gates", "apmap", "romtables"):
        assert section in payload, f"report is missing the {section} section"
    assert payload["gates"]["mdm"]["enables_set"] is True
    assert payload["romtables"]["apb"]["children"]


def test_report_records_a_tap_failure_instead_of_giving_up_on_the_whole_artifact(
    run, target, tmp_path
) -> None:
    """Without `--irlen` the fake cannot measure an IR width, which is the same
    position the Commander backend is in on silicon. The report must still be
    produced -- a diagnostic artifact that refuses to exist when one probe fails
    is worthless exactly when it is needed."""
    out_file = tmp_path / "report.json"
    code, _out = run("--json", str(out_file), "report")
    assert code == EXIT_OK
    payload = read_json(out_file)
    assert "error" in payload["tap"]
    assert payload["apmap"]["aps"]


def test_an_empty_device_is_refused_before_the_probe_is_touched(run, target) -> None:
    """Measured twice: a session that skips select_device leaves the probe
    unable to power the DAP, and every later session fails the same way -- new
    process, either backend -- until the board is power-cycled. The state lives
    in the probe, so the tool cannot retry its way out; the only defence is not
    to open such a session."""
    code, out = run("--device", "", "core", "list")
    assert code == EXIT_REFUSED
    assert "power-cycled" in out


# ---------------------------------------------------------------------------
# Parse safety: bad input costs nothing and kills nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["bp", "set", "32169874"],           # the reported killer: hex meant, no 0x
        ["bp", "set", "321D2EE6"],           # hex letters, no 0x: was an uncaught ValueError
        ["bp", "set", "zzz"],
        ["wp", "set", "999"],
        ["m33", "bp", "set", "999"],
        ["m33", "wp", "set", "999"],
        ["mem", "read", "123"],
        ["mem", "dump", "123", "16"],
        ["mem", "write", "123", "0x1", "--yes"],
        ["romdump", "--rom", "123"],
    ],
    ids=lambda a: " ".join(a),
)
def test_a_bare_or_garbage_address_is_rejected_before_the_probe_opens(run, argv) -> None:
    """Addresses require the 0x prefix. `32169874` could be hex or decimal:
    base-0 silently armed a comparator at the decimal reading of a hex address,
    and an unprefixed hex string raised a ValueError that no handler caught and
    took the whole session down. No transport is patched here, so reaching one
    would fail on discovery instead of exiting 1."""
    code, out = run(*argv)
    assert code == EXIT_USAGE
    assert "0x-prefixed" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["mem", "write", "0x1000", "banana", "--yes"],
        ["core", "reg", "r3", "banana"],
        ["tap", "ir", "zz"],
        ["tap", "dr", "zz", "8"],
    ],
    ids=lambda a: " ".join(a),
)
def test_a_garbage_value_is_rejected_before_the_probe_opens(run, argv) -> None:
    """Data values keep int(x, 0) semantics -- decimal or 0x hex -- but a
    non-number must exit 1 with a message, not escape as a ValueError."""
    code, out = run(*argv)
    assert code == EXIT_USAGE
    assert "is not a number" in out


def test_an_uppercase_0x_prefix_is_accepted(run, target, tmp_path) -> None:
    out_file = tmp_path / "read.json"
    code, _out = run("--json", str(out_file), "mem", "read", "0X00410FF0", "--ap", "apb")
    assert code == EXIT_OK
    assert read_json(out_file)["address"] == "0x00410FF0"


def test_a_decimal_value_is_still_legal_for_mem_write(run, target) -> None:
    """The 0x rule is for addresses. `mem write <addr> 57005` writes decimal
    57005 exactly as it always has."""
    code, out = run("mem", "write", "0x00410FF0", "57005", "--ap", "apb", "--yes")
    assert code == EXIT_OK
    assert "0x0000DEAD" in out


def test_m33_bp_clear_still_takes_a_bare_decimal_index(run, target) -> None:
    """`m33 bp clear 2` is a comparator index, not an address: bare decimal
    stays legal there."""
    code, out = run("m33", "bp", "clear", "2", "--core", "smu")
    assert code == EXIT_OK
    assert "cleared breakpoint 2" in out


def test_m33_bp_clear_rejects_a_non_decimal_index(run) -> None:
    code, out = run("m33", "bp", "clear", "x2")
    assert code == EXIT_USAGE
    assert "decimal index" in out


def test_an_escaped_exception_is_an_internal_error_with_exit_2(run, target, monkeypatch) -> None:
    """The exit-code contract has no traceback bucket. Anything that escapes a
    command is a tool bug: reported as `internal error`, exit 2, no traceback."""
    def boom(dap):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli.gates_mod, "status", boom)
    code, out = run("gates", "status")
    assert code == EXIT_TOOL_ERROR
    assert "internal error" in out
    assert "RuntimeError" in out
    assert "Traceback" not in out


# ---------------------------------------------------------------------------
# flash: argument surface, --yes gating, and the end-to-end flows.
#
# The SyntheticTarget serves the memory-mapped flash window at 0x08000000
# (base 0x0 would shadow its core debug frames), so every command that reads
# flash back passes --ahb-base 0x08000000 -- on silicon that base is a bench
# question (u-f1) and the default 0x0 fails over by itself.
# ---------------------------------------------------------------------------


def write_image(tmp_path, data: bytes, name: str = "image.bin"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_flash_needs_exactly_one_destination(run, tmp_path) -> None:
    """No implicit offset, ever (user decision 2026-08-04): a flash write with
    a guessed destination is how a bootloader gets overwritten by accident. No
    transport is patched here -- the refusal must precede the probe."""
    p = write_image(tmp_path, b"\x11" * 4)
    code, out = run("flash", "program", str(p))
    assert code == EXIT_USAGE
    assert "exactly one of --offset" in out
    code, out = run("flash", "program", str(p), "--offset", "0x0", "--area", "app")
    assert code == EXIT_USAGE
    assert "exactly one of --offset" in out


def test_flash_rejects_an_unknown_area_and_names_the_real_ones(run, tmp_path) -> None:
    p = write_image(tmp_path, b"\x11" * 4)
    code, out = run("flash", "program", str(p), "--area", "bootloader")
    assert code == EXIT_USAGE
    assert "selftestbck" in out


def test_flash_program_requires_a_page_aligned_offset(run, tmp_path) -> None:
    p = write_image(tmp_path, b"\x11" * 4)
    code, out = run("flash", "program", str(p), "--offset", "0x300080")
    assert code == EXIT_USAGE
    assert "not 256-byte aligned" in out


def test_flash_program_refuses_an_image_too_big_for_its_area(run, tmp_path) -> None:
    p = write_image(tmp_path, b"\x00" * 0x6000)
    code, out = run("flash", "program", str(p), "--area", "cert")  # cert holds 0x5000
    assert code == EXIT_USAGE
    assert "refusing to spill past the region" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["flash", "program", "IMAGE", "--offset", "0x300000"],
        ["flash", "erase", "--offset", "0x300000", "--length", "0x1000"],
        ["flash", "erase", "--all"],
    ],
    ids=lambda a: " ".join(a[:2] + a[3:4]) if isinstance(a, list) else "",
)
def test_flash_destructive_ops_require_yes_when_stdin_is_not_a_terminal(
    run, tmp_path, argv
) -> None:
    """--yes gates every destructive flash op. No transport is patched, so the
    refusal provably happens before the probe -- and before the JEDEC gate,
    the reset, or anything else that touches the board."""
    argv = [str(write_image(tmp_path, b"\x11" * 4)) if a == "IMAGE" else a for a in argv]
    code, out = run(*argv)
    assert code == EXIT_USAGE
    assert "--yes" in out


def test_flash_erase_all_does_not_combine_with_a_range(run) -> None:
    code, out = run("flash", "erase", "--all", "--offset", "0x0", "--yes")
    assert code == EXIT_USAGE
    assert "--all does not combine" in out


def test_flash_read_with_an_offset_needs_a_length(run, tmp_path) -> None:
    code, out = run("flash", "read", "--offset", "0x0", "--out", str(tmp_path / "d.bin"))
    assert code == EXIT_USAGE
    assert "--length is required" in out


def test_flash_jedec_reports_mode_and_identity(run, target, tmp_path) -> None:
    """The smoke test command, end to end: quiesce-reset, controller bring-up,
    mode detection, and the identity verdict -- plus the JSON contract."""
    out_file = tmp_path / "jedec.json"
    code, out = run("--json", str(out_file), "flash", "jedec")
    assert code == EXIT_OK
    assert "JEDEC 0xC23989C2" in out
    assert "matches the expected MX25UW25A45G" in out
    payload = read_json(out_file)
    assert payload["op"] == "jedec"
    assert payload["mode"] == "spi"
    assert payload["jedec"] == "0xC23989C2"


def test_flash_bringup_waits_out_the_post_reset_m33_dead_window(run, target) -> None:
    """Finding 10, measured 2026-08-05: right after the quiesce reset
    releases, the SMU AHB-AP answers all-zeros for a moment (identity reads
    zero; correct shortly after, with the EDBGREQ latch having caught the
    core). The bring-up polls identity until the AP is back instead of
    failing on the first read."""
    target.m33_dead_reads_after_reset = 12  # two full identity passes read dead
    code, out = run("flash", "jedec")
    assert code == EXIT_OK
    assert "JEDEC 0xC23989C2" in out


def test_flash_bringup_survives_a_blank_flash_board_dead_at_attach(run, target) -> None:
    """The virgin-board case, measured 2026-08-05 right after the first chip
    erase: with no bootloader the SoC sits in the BootROM fail cascade and
    the SMU AP answers nothing at attach. The pre-reset quiesce is then
    best-effort -- the reset with EDBGREQ latched is what catches the SMU."""
    target._m33_dead_reads = 10**6  # dead from attach...
    target.m33_dead_reads_after_reset = 0  # ...until the quiesce reset runs
    code, out = run("flash", "jedec")
    assert code == EXIT_OK
    assert "could not quiesce before the reset" in out
    assert "JEDEC 0xC23989C2" in out


def test_flash_reset_none_still_requires_a_live_smu(run, target) -> None:
    """--reset none has no stronger mechanism coming: a dead SMU at attach is
    fatal there, not best-effort."""
    target._m33_dead_reads = 10**6
    code, out = run("flash", "jedec", "--reset", "none")
    assert code == EXIT_TOOL_ERROR
    assert "could not halt the SMU M33" in out


def test_flash_bringup_boots_the_smu_out_of_dest0(run, target, monkeypatch) -> None:
    """Finding 15 end to end: a wiped (DEST0) board -- SMU unpowered, and no
    reset reviving it -- is brought up by the flash flow itself, s32multitool
    style (partition enables + SRAM scrubs + MC_ME PRTN0 core-0 release onto
    the debugger-written stub), and `flash jedec` completes. This is the exact
    state Finding 14 measured as unrecoverable."""
    target.smu_unpowered = True
    target.smu_sram_gated = True  # bench 2026-08-06: SRAM dead until config_m33
    monkeypatch.setattr(cli, "FLASH_SMU_SETTLE_TIMEOUT_S", 0.3)
    code, out = run("flash", "jedec")
    assert code == EXIT_OK
    assert "JEDEC 0xC23989C2" in out
    assert "SMU bring-up (s32multitool, Finding 15)" in out
    assert target.smu_unpowered is False, "the MC_ME release powered the SMU up"
    assert target.mem[0x4190_014C] == 0x2500_2000, "PRTN0_CORE0_ADDR = the stub vectors"
    assert target.mem[0x4103_0300] == 0x0F00_0004, "config_m33 ran: SRAM clock selected"


def test_flash_deassert_clears_only_the_smu_latch(run, target) -> None:
    """The multitool-exact de-assert: after a flash op, MDM CONTROL keeps the
    CE latches (bits 10:9) exactly as the reference leaves them and has
    dropped only the SMU's bit 8 (and the verbatim bit 28)."""
    from s32edbg.data.expected_apmap import AP_MDM
    from s32edbg.data.ladder1 import MDM_CONTROL

    code, out = run("flash", "jedec")
    assert code == EXIT_OK
    control = target.ap_space[AP_MDM + MDM_CONTROL]
    assert control & 0x700 == 0x600, "CE latches stay latched; the SMU's drops"
    assert control & 0x1000_0100 == 0


def test_flash_default_reset_is_the_pin(run, target) -> None:
    """Finding 15 aligns the flash reset default with the reference: RESET_B
    (probe nRESET) toggled once -- assert, then release. `--reset sda`
    remains the fallback and drives no pin traffic."""
    code, out = run("flash", "jedec")
    assert code == EXIT_OK
    assert target.pin_reset_calls == [True, False]
    target.pin_reset_calls.clear()
    code, out = run("flash", "jedec", "--reset", "sda")
    assert code == EXIT_OK
    assert target.pin_reset_calls == []


def test_flash_program_end_to_end_erases_writes_verifies_and_reports(
    run, target, tmp_path
) -> None:
    """The whole CLI program flow on the synthetic NOR: erase lands before the
    writes (the model's AND-semantics would corrupt the verify otherwise), the
    blank page is skipped, the verify is full, and the JSON says all of it."""
    image = write_image(tmp_path, b"\x11" * 0x100 + b"\xff" * 0x100 + bytes(range(256)))
    out_file = tmp_path / "program.json"
    code, out = run(
        "--json", str(out_file), "flash", "program", str(image),
        "--offset", "0x300000", "--ahb-base", "0x08000000", "--yes",
    )
    assert code == EXIT_OK
    assert "programmed" in out
    assert "power-cycle the board" in out
    assert target.flash[0x0030_0000] == 0x1111_1111
    assert 0x0030_0100 not in target.flash, "the blank page stayed unprogrammed"
    assert target.applet_runs[0][0] == 5, "the sector erase must precede the writes"
    payload = read_json(out_file)
    assert payload["op"] == "program"
    assert payload["program"]["runs"] == 2
    assert payload["program"]["skipped_pages"] == 1
    assert payload["erase"]["bytes"] == 0x1000
    assert payload["verify"] == "full"
    assert payload["applet_calls"] == 3
    assert payload["mode"] == "spi"


def test_flash_program_flattens_an_elf_and_writes_it_verbatim(run, target, tmp_path) -> None:
    """An ELF input is packed LMA-relative with 0xFF gaps and written like any
    .bin (user decision 2026-08-04) -- the gap page never gets programmed
    because it is indistinguishable from erased flash."""
    import struct
    from s32edbg import elf as elf_mod

    segments = [(0x3210_0000, b"\xaa" * 4), (0x3210_0200, b"\xbb" * 4)]
    phoff, phnum = 52, len(segments)
    data_off = phoff + phnum * elf_mod.PHDR_SIZE
    ident = elf_mod.ELF_MAGIC + bytes([1, 1, 1, 0]) + b"\x00" * 8
    ehdr = struct.pack(
        "<HHIIIIIHHHHHH", 2, elf_mod.EM_ARM, 1, 0x3210_0001, phoff, 0, 0, 52,
        elf_mod.PHDR_SIZE, phnum, 0, 0, 0,
    )
    phdrs, blobs, off = b"", b"", data_off
    for paddr, data in segments:
        phdrs += struct.pack(
            "<IIIIIIII", elf_mod.PT_LOAD, off, paddr, paddr, len(data), len(data), 5, 4
        )
        blobs += data
        off += len(data)
    image = write_image(tmp_path, ident + ehdr + phdrs + blobs, "app.elf")

    code, out = run(
        "flash", "program", str(image),
        "--area", "app", "--ahb-base", "0x08000000", "--yes",
    )
    assert code == EXIT_OK
    assert "flattened LMA-relative: base 0x32100000" in out
    app_offset = 0x0030_0000
    assert target.flash[app_offset] == 0xAAAA_AAAA
    assert app_offset + 0x100 not in target.flash, "the 0xFF gap page was skipped"
    assert target.flash[app_offset + 0x200] == 0xBBBB_BBBB


def test_flash_erase_all_is_the_host_driven_chip_erase(run, target, tmp_path) -> None:
    target.flash[0x0100_0000] = 0
    out_file = tmp_path / "erase.json"
    code, out = run(
        "--json", str(out_file), "flash", "erase", "--all",
        "--ahb-base", "0x08000000", "--yes",
    )
    assert code == EXIT_OK
    assert target.qspi_chip_erases == 1
    assert not target.flash
    assert target.applet_runs == [], "chip erase has no applet op"
    payload = read_json(out_file)
    assert payload["op"] == "erase --all"
    assert payload["erase"]["bytes"] == 32 * 1024 * 1024
    assert payload["verify"] == "blank-sampled"


def test_flash_erase_range_blank_checks_afterwards(run, target, tmp_path) -> None:
    target.flash[0x0030_0000] = 0
    out_file = tmp_path / "erase.json"
    code, out = run(
        "--json", str(out_file), "flash", "erase",
        "--offset", "0x300000", "--length", "0x11000",
        "--ahb-base", "0x08000000", "--yes",
    )
    assert code == EXIT_OK
    assert "erased" in out
    assert [op for op, _o, _s in target.applet_runs] == [6, 5], "64K middle + 4K tail"
    payload = read_json(out_file)
    assert payload["erase"]["spans_64k"] == 1
    assert payload["erase"]["spans_4k"] == 1
    assert payload["verify"] == "blank-full"


def test_flash_program_at_offset_zero_warns_without_the_ivt_tag(run, target, tmp_path) -> None:
    """Offset 0 is where the BootROM looks for the 0xD1 IVT tag; an image
    without one still programs (as instructed) but the operator is told the
    board will not boot from it."""
    image = write_image(tmp_path, b"\x00" * 0x100, "no_ivt.bin")
    code, out = run(
        "flash", "program", str(image),
        "--offset", "0x0", "--ahb-base", "0x08000000", "--yes",
    )
    assert code == EXIT_OK
    assert "0xD1" in out
    assert "Programming anyway" in out


def test_flash_program_at_offset_zero_reports_the_ivt_in_place(run, target, tmp_path) -> None:
    image = write_image(tmp_path, b"\xd1\x01\x00\x60" + b"\x00" * 0xFC, "blob.bin")
    code, out = run(
        "flash", "program", str(image),
        "--offset", "0x0", "--ahb-base", "0x08000000", "--yes",
    )
    assert code == EXIT_OK
    assert "boot IVT in place" in out


def test_flash_read_dumps_the_window_into_a_file(run, target, tmp_path) -> None:
    for i in range(4):
        target.flash[0x0030_0000 + i * 4] = 0x1122_3344 + i
    dump = tmp_path / "dump.bin"
    code, out = run(
        "flash", "read", "--offset", "0x300000", "--length", "0x20",
        "--out", str(dump), "--ahb-base", "0x08000000",
    )
    assert code == EXIT_OK
    data = dump.read_bytes()
    assert len(data) == 0x20
    assert data[:4] == (0x1122_3344).to_bytes(4, "little")
    assert data[0x10:] == b"\xff" * 0x10, "unprogrammed flash reads erased"


def test_flash_verify_passes_on_matching_content_and_exits_2_on_a_mismatch(
    run, target, tmp_path
) -> None:
    content = bytes(range(64))
    for i in range(0, 64, 4):
        target.flash[0x0030_0000 + i] = int.from_bytes(content[i : i + 4], "little")
    match = write_image(tmp_path, content, "match.bin")
    code, out = run(
        "flash", "verify", str(match),
        "--offset", "0x300000", "--ahb-base", "0x08000000",
    )
    assert code == EXIT_OK
    assert "verify passed" in out

    mismatch = write_image(tmp_path, b"\x00" * 16, "mismatch.bin")
    code, out = run(
        "flash", "verify", str(mismatch),
        "--offset", "0x300000", "--ahb-base", "0x08000000",
    )
    assert code == EXIT_TOOL_ERROR
    # Byte 0 matches (both are 0x00); the first real difference is byte 1.
    assert "verify mismatched at flash offset 0x00300001" in out

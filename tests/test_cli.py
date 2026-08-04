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

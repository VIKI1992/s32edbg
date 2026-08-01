"""s32edbg command-line interface."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import click
from rich.console import Console

from . import __version__
from .dap import adiv6
from .dap import apmap as apmap_mod
from .dap import gates as gates_mod
from .dap import iface as iface_mod
from .dap import memaccess
from .dap import romtable
from .dap import m33 as m33_mod
from .dap import runcontrol
from .dap.adiv6 import Dap
from .data import r52debug
from .data import swj
from .data.bscan import BSCAN_DEFAULT_IRLEN, BSCAN_PROBE_BITS
from .data.expected_apmap import AP_ALIASES, AP_APB, AP_MDM, AP_SDA, APBIC1_ROM_BASE
from .data.known_ids import decode_dpidr
from .data.ladder1 import MDM_IDR_EXPECTED, SDA_SYSRSTC, SDA_SYSRSTC_SYSFUNCREQ
from .errors import PowerUpError, S32EError, TransportError, UnsafeOperation
from .jtag import bscan as bscan_mod
from .jtag import tap as tap_mod
from .report import emit
from .transport import Tif, discover, list_installs, make_transport

console = Console()
err_console = Console(stderr=True)

SWD_OPT_IN = "--i-understand-swd-is-one-way"

#: Which backends can clock an arbitrary TMS/TDI vector. Kept here rather than
#: instantiating a transport to ask, because the choice has to be made before
#: one is built.
RAW_JTAG_BACKENDS = {"dll": False, "commander": True}


@dataclass
class Ctx:
    transport_kind: str
    serial: str | None
    speed_khz: int
    jlink_dir: str | None
    log: str | None
    json_out: str | None
    echo: bool
    irlen: int | None
    device: str | None


def _emit_json(target: str | None, payload: object) -> None:
    text = json.dumps(payload, indent=2, default=str)
    if target in (None, "-"):
        console.print_json(text)
    else:
        Path(target).write_text(text, encoding="utf-8")
        console.print(f"[green]wrote[/green] {target}")


@contextmanager
def open_transport(
    ctx: Ctx,
    *,
    tif: Tif | None = Tif.JTAG,
    connect_dap: bool = True,
    needs_raw_jtag: bool = False,
) -> Iterator[tuple[object, Dap | None]]:
    install = discover(ctx.jlink_dir)
    kind = ctx.transport_kind

    # Raw TMS/TDI shifting only exists on the commander backend: no JLink_x64.dll
    # export can do it with a discoverable ABI (see transport/dll.py). Rather
    # than fail a `tap` or `bscan` command because --transport defaults to dll,
    # switch backend and say so. `doctor` sets the same precedent.
    if needs_raw_jtag and not RAW_JTAG_BACKENDS.get(kind, False):
        console.print(
            f"[dim]note:[/dim] raw JTAG needs the commander backend; using it for this "
            f"command instead of --transport {kind}"
        )
        kind = "commander"

    kwargs: dict[str, object] = {"install": install, "serial": ctx.serial}
    if ctx.log:
        kwargs["log_file"] = ctx.log
    if kind == "commander":
        kwargs["echo"] = ctx.echo
        kwargs["device"] = ctx.device
        kwargs["speed_khz"] = ctx.speed_khz

    t = make_transport(kind, **kwargs)
    t.open()
    try:
        if tif is not None:
            t.select_interface(tif)
            t.set_speed_khz(ctx.speed_khz)
            _bring_up_link(t, ctx, tif)
        dap = Dap(t) if connect_dap else None
        yield t, dap
    finally:
        t.close()


def _bring_up_link(t: object, ctx: Ctx, tif: Tif) -> None:
    """Get the DAP link into a state where DP/AP reads actually return data.

    A bare session that has only had TIF_Select and SetSpeed is not enough: every
    ReadDP comes back 0 because J-Link has never scanned the chain. The sequence
    that works on this target is auto-detect IRPre/DRPre (negative values) plus a
    real connect. The connect may report failure while still leaving the DAP
    usable -- on this chip the core sits behind a closed debug gate until
    `gates apply` runs, so a failed core identification is expected and benign.

    Opening a session **without** a device name is not merely useless, it is
    destructive: measured twice on 2026-08-01, a session that skipped
    `select_device` left the probe unable to power the DAP, and every later
    session -- new process, full connect, either backend -- then failed the same
    way with `CTRL/STAT=0x00000000` and `doctor` reporting `chain: no response`,
    until the board was power-cycled. So an empty `--device` is refused up front
    rather than allowed to brick the link.
    """
    if not ctx.device:
        raise UnsafeOperation(
            "--device must not be empty: J-Link needs a device name to scan the "
            "chain, and a session opened without one leaves the probe unable to "
            "power the DAP until the board is power-cycled (measured twice). "
            "Use the default CORTEX-R52 unless you have a reason not to."
        )

    set_jtag = getattr(t, "set_jtag_config", None)
    if set_jtag is not None and tif is Tif.JTAG:
        set_jtag(-1, -1)

    select_device = getattr(t, "select_device", None)
    if select_device is not None:
        select_device(ctx.device)

    configure = getattr(t, "configure_coresight", None)
    if configure is not None and tif is Tif.SWD:
        # SEGGER's own sample comments the empty string as the SWD init sequence.
        try:
            configure("")
        except S32EError:
            pass

    connect_target = getattr(t, "connect_target", None)
    if connect_target is not None:
        try:
            connect_target()
        except S32EError:
            pass


def _prepare_dap(dap: Dap, *, system_power: bool = False, attempts: int = 4) -> list[str]:
    """Power up the debug domain and calibrate the AP-read model.

    The power-up is retried, because on this target it succeeds only in windows:
    the DAP can refuse for several seconds and then come up. A single attempt
    made every DAP command look permanently broken during a period when it was
    merely busy. This does not rescue a genuinely dead TAP -- if the JTAGC is
    held in reset the retries all fail and the error says so.
    """
    notes: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            dap.power_up(system=system_power)
            if attempt > 1:
                notes.append(f"debug power-up succeeded on attempt {attempt}")
            break
        except PowerUpError:
            if attempt == attempts:
                raise
    if not dap.calibrate_ap_read(AP_MDM, MDM_IDR_EXPECTED):
        notes.append(
            "could not confirm the AP-read pipeline model against the MDM-AP IDR "
            f"(RM Table 957 says 0x{MDM_IDR_EXPECTED:08X}). AP values below may be "
            "shifted by one transaction. If the debug gate is closed this is expected "
            "-- run `gates apply` and retry."
        )
    else:
        notes.append(
            f"AP reads are {'pipelined' if dap.ap_read_pipelined else 'direct'} "
            "on this transport (determined by reading a known IDR both ways)."
        )
    return notes


# ---------------------------------------------------------------------------
# root
# ---------------------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="s32edbg")
@click.option("--transport", "transport_kind", type=click.Choice(["dll", "commander"]), default="dll", show_default=True, help="Probe backend. 'dll' is in-process and interactive; 'commander' runs JLink.exe in batch mode as a cross-check oracle.")
@click.option("--serial", default=None, help="J-Link serial number, when more than one is attached.")
@click.option("--speed", "speed_khz", type=int, default=4000, show_default=True, help="Interface speed in kHz.")
@click.option("--jlink-dir", default=None, help="Pin a specific SEGGER install directory.")
@click.option("--log", default=None, help="Write a J-Link log file here (wire-level trace).")
@click.option("--json", "json_out", default=None, help="Write JSON output to this path, or '-' for stdout.")
@click.option("--echo", is_flag=True, help="Echo Commander traffic (commander transport only).")
@click.option("--irlen", type=int, default=None, help="Override the JTAG IR length instead of measuring it.")
@click.option("--device", default="CORTEX-R52", show_default=True, help="Device name for J-Link's connect sequence. J-Link needs one to scan the chain and init the DAP even for DAP-only work.")
@click.pass_context
def main(ctx: click.Context, transport_kind: str, serial: str | None, speed_khz: int, jlink_dir: str | None, log: str | None, json_out: str | None, echo: bool, irlen: int | None, device: str | None) -> None:
    """CoreSight/JTAG tool for NXP S32E27x over a SEGGER J-Link.

    Safety policy baked in: boundary scan is SAMPLE/PRELOAD only, the SDA-AP is
    never written, AXI-AP traffic is refused while functional reset is asserted,
    and switching to SWD needs an explicit opt-in because returning to JTAG
    requires a power-on reset.
    """
    ctx.obj = Ctx(transport_kind, serial, speed_khz, jlink_dir, log, json_out, echo, irlen, device)


# ---------------------------------------------------------------------------
# probe / connect
# ---------------------------------------------------------------------------


@main.command()
@click.pass_obj
def installs(ctx: Ctx) -> None:
    """List the SEGGER installations found on this machine."""
    found = list_installs()
    if not found:
        raise click.ClickException("no SEGGER J-Link installation found")
    chosen = discover(ctx.jlink_dir)
    for inst in found:
        mark = "*" if inst.path == chosen.path else " "
        has_cmd = "JLink.exe" if inst.commander.is_file() else "(no JLink.exe)"
        has_svf = "JTAGLoad.exe" if inst.jtagload.is_file() else "(no JTAGLoad.exe)"
        console.print(f"{mark} {inst.path}  v{inst.version_str}  {has_cmd}  {has_svf}")
    console.print("\n[dim]* = the install this tool will use[/dim]")


@main.command()
@click.pass_obj
def probe(ctx: Ctx) -> None:
    """Report probe identity, VTREF and pin states. Does not touch the DAP."""
    with open_transport(ctx, tif=None, connect_dap=False) as (t, _):
        info = t.probe_info()  # type: ignore[union-attr]

    table_rows = [
        ("serial", info.serial),
        ("hardware version", info.hardware_version),
        ("firmware", info.firmware),
        ("DLL version", info.dll_version),
        ("VTREF", f"{info.vtref_mv} mV" if info.vtref_mv is not None else None),
    ]
    for key, value in table_rows:
        console.print(f"{key:>18}: {value if value is not None else '[dim]unknown[/dim]'}")
    if info.pins:
        console.print(f"{'pins':>18}: " + " ".join(f"{k}={v}" for k, v in info.pins.items()))
    for note in info.notes:
        console.print(f"[dim]note:[/dim] {note}")
    for cap in info.capabilities:
        console.print(f"[red]probe limitation:[/red] {cap}")

    if info.vtref_mv is not None and info.vtref_mv < 2000:
        console.print(
            "[yellow]![/yellow] VTREF is low. On this board the debug connectors take it "
            "from +3V3 (J9 pin 2 directly, J3 pin 2 through ferrite L13). Check target power."
        )
    if ctx.json_out:
        _emit_json(ctx.json_out, info.as_dict())


@main.command()
@click.pass_obj
def doctor(ctx: Ctx) -> None:
    """Diagnose a target that will not answer, with the checks ranked.

    Runs a JTAG chain scan through JLink.exe (which is what reports IRPrint) and
    turns the result into a ranked list of board-level checks. Read-only.
    """
    from . import diagnose as diag
    from .transport.commander import CommanderTransport

    t = CommanderTransport(
        install=discover(ctx.jlink_dir),
        serial=ctx.serial,
        log_file=ctx.log,
        device=ctx.device,
        speed_khz=ctx.speed_khz,
        echo=ctx.echo,
    )
    t.open()
    info = t.probe_info()
    currents = t.sample_activity()
    scan = t.scan_chain()
    raw = str(scan.get("raw", ""))

    console.print(f"probe: {info.hardware_version or '?'}  firmware: {info.firmware or '?'}")
    console.print(f"VTREF: {info.vtref_mv} mV   pins: " + " ".join(f"{k}={v}" for k, v in info.pins.items()))
    if currents:
        console.print(f"ITarget samples: {currents} mA")
    idcode = scan.get("idcode")
    console.print(
        "chain: "
        + (f"IDCODE 0x{idcode:08X}" if isinstance(idcode, int) and idcode else "[red]no response[/red]")
        + (f", TotalIRLen {scan['total_ir_len']}" if scan.get("total_ir_len") else "")
    )
    console.rule("diagnosis")

    obs = diag.Observation(
        vtref_mv=info.vtref_mv,
        pins=info.pins,
        idcode=idcode if isinstance(idcode, int) else None,
        total_ir_len=scan.get("total_ir_len") if isinstance(scan.get("total_ir_len"), int) else None,
        ir_print_all_zero="IRPrint = 0x..0000" in raw or "IRPrint = 0x0000" in raw,
        connect_failed="Could not connect to the target device" in raw,
        capability_warnings=list(scan.get("capability_warnings") or []),
        itarget_samples=currents,
    )
    findings = diag.diagnose(obs)
    colours = {"blocker": "red", "warning": "yellow", "ok": "green"}
    for f in findings:
        console.print(f"[{colours[f.severity]}]{f.severity.upper()}[/{colours[f.severity]}]  {f.title}")
        if f.detail:
            console.print(f"        {f.detail}")
        for i, check in enumerate(f.checks, 1):
            console.print(f"        {i}. {check}")
        console.print()
    console.print(f"[bold]{diag.summarise(findings)}[/bold]")

    if ctx.json_out:
        _emit_json(
            ctx.json_out,
            {
                "probe": info.as_dict(),
                "chain": {k: v for k, v in scan.items() if k != "raw"},
                "findings": [
                    {"severity": f.severity, "title": f.title, "detail": f.detail, "checks": f.checks}
                    for f in findings
                ],
            },
        )


@main.command()
@click.option("--system-power", is_flag=True, help="Also request system power (CSYSPWRUPREQ).")
@click.pass_obj
def connect(ctx: Ctx, system_power: bool) -> None:
    """Bring up the DAP with no device selected and report its identity.

    On the commander backend this uses a single batched identity read, since
    that backend cannot serve individual DP reads. Running it on both backends
    and comparing DPIDR is what validates the hand-written ctypes signatures.
    """
    with open_transport(ctx) as (t, dap):
        assert dap is not None
        batch_identity = getattr(t, "read_dp_identity", None)
        if batch_identity is not None:
            ident = batch_identity()
            raw = ident.get("dpidr")
            # 0x00000000 and 0xFFFFFFFF are not DPIDR values, they are the two
            # ways a wire reads when nothing is driving it. ADIv6 makes DPIDR
            # bit 0 RAO, and this target's is 0x4C013477. Reporting either as a
            # successful connect would be the same confidently-wrong answer the
            # rest of this tool works to avoid.
            stat = ident.get("ctrl_stat")
            dap_alive = stat not in (None, 0x0000_0000, 0xFFFF_FFFF)
            if raw in (0x0000_0000, 0xFFFF_FFFF) or (raw is None and not dap_alive):
                raise TransportError(
                    "no DAP answered: the TAP is not responding. Run `s32edbg doctor`, "
                    "which ranks the causes. On this board the usual one is that the "
                    "0.8 V core rail is down while VTREF still reads a healthy 3.3 V "
                    "from a different regulator."
                )
            if raw is None:
                # The DAP is answering -- CTRL/STAT read -- but J-Link's connect
                # did not print a DPIDR line this run. Over JTAG that banner is
                # the only correct source (DPACC 0b00 is Reserved), so report
                # the gap rather than substituting the reserved register.
                console.print(
                    "[yellow]![/yellow] DPIDR unavailable: J-Link printed no DPIDR line, "
                    "and over JTAG it cannot be read through DPACC (ADIv6 Figure A1-2 "
                    "makes address 0b00 Reserved). CTRL/STAT below shows the DAP is alive."
                )
            if raw is not None:
                console.print(f"DPIDR      0x{raw:08X}  ({decode_dpidr(raw).version_name})")
            if ident.get("dpidr1") is not None:
                console.print(f"DPIDR1     0x{ident['dpidr1']:08X}")
            if ident.get("ctrl_stat") is not None:
                console.print(f"CTRL/STAT  0x{ident['ctrl_stat']:08X}")
            console.print(
                "[dim]batched read: this backend cannot do single DP transactions. "
                "Compare this DPIDR against --transport dll.[/dim]"
            )
            if ctx.json_out:
                _emit_json(ctx.json_out, {k: (f"0x{v:08X}" if v is not None else None)
                                          for k, v in ident.items()})
            return

        notes = _prepare_dap(dap, system_power=system_power)
        info = dap.info()

        if info.dpidr is None:
            # Over JTAG the identity registers are simply not reachable. Say so
            # and show CTRL/STAT, which is at DP 0x4 and stays readable -- that
            # is the evidence about whether the DAP is alive. Printing the
            # reserved register's value instead would look like an answer.
            console.print(f"[yellow]![/yellow] DPIDR unavailable. {info.unavailable}")
        else:
            console.print(f"      DPIDR: 0x{info.dpidr.raw:08X}")
            console.print(f"    version: {info.dpidr.version_name}" + ("  [green](ADIv6)[/green]" if info.dpidr.is_adiv6 else "  [yellow](pre-ADIv6)[/yellow]"))
            console.print(f"   designer: {info.dpidr.designer_name}")
            console.print(f"     partno: 0x{info.dpidr.partno:02X}  revision {info.dpidr.revision}  MINDP={info.dpidr.min_dp}")
            if info.dpidr1 is not None:
                console.print(f"     DPIDR1: 0x{info.dpidr1['raw']:08X}  AP address size {info.dpidr1['asize_bits']} bits  ERRMODE={info.dpidr1['errmode']}")
            console.print(f"    BASEPTR: " + (f"0x{info.baseptr:X}" if info.baseptr is not None else "[yellow]invalid/absent[/yellow]"))
        console.print(f"  CTRL/STAT: 0x{info.ctrl_stat:08X}")
        for note in notes:
            console.print(f"[dim]note:[/dim] {note}")

        caps = getattr(t, "capability_warnings", [])
        for cap in caps:
            console.print(f"[red]probe limitation:[/red] {cap}")

        if ctx.json_out:
            _emit_json(
                ctx.json_out,
                {
                    "dpidr": f"0x{info.dpidr.raw:08X}" if info.dpidr else None,
                    "version": info.dpidr.version_name if info.dpidr else None,
                    "is_adiv6": info.dpidr.is_adiv6 if info.dpidr else None,
                    "dpidr1": info.dpidr1,
                    "baseptr": f"0x{info.baseptr:X}" if info.baseptr is not None else None,
                    "ctrl_stat": f"0x{info.ctrl_stat:08X}",
                    "ap_read_pipelined": info.ap_read_pipelined,
                    "unavailable": info.unavailable,
                    "notes": notes,
                    "probe_limitations": caps,
                },
            )


# ---------------------------------------------------------------------------
# iface
# ---------------------------------------------------------------------------


@main.group()
def iface() -> None:
    """Show or switch the target interface (JTAG / SWD)."""


@iface.command("show")
@click.pass_obj
def iface_show(ctx: Ctx) -> None:
    """Detect the live interface from what actually answers.

    Never switches protocol: probing SWD would mean sending the switch
    sequence, and RM 135.5.1 makes that a one-way door.
    """
    with open_transport(ctx) as (t, dap):
        if dap is not None:
            try:
                dap.power_up()
            except S32EError:
                pass
        state = iface_mod.detect(t, dap)  # type: ignore[arg-type]

        colour = {"proven": "green", "inferred": "yellow", "none": "red"}[state.confidence]
        console.print(
            f"interface: [{colour}]{state.reported}[/{colour}] ({state.confidence})"
        )
        for line in state.evidence:
            console.print(f"  [dim]-[/dim] {line}")
        for line in state.limitations:
            console.print(f"  [red]![/red] {line}")
        console.print(
            "[dim]RM 135.5.1: the debug port comes out of reset in JTAG. In SWD mode "
            "TDI and TDO become GPIO (Table 980), and returning to JTAG needs POR.[/dim]"
        )
        if ctx.json_out:
            _emit_json(ctx.json_out, state.as_dict())


@iface.command("jtag")
@click.pass_obj
def iface_jtag(ctx: Ctx) -> None:
    """Select JTAG (the reset default; always safe)."""
    with open_transport(ctx, tif=Tif.JTAG) as (_t, dap):
        assert dap is not None
        dap.power_up()
        info = dap.info()
        console.print("[green]JTAG selected[/green]")
        if info.dpidr is not None:
            console.print(f"DPIDR 0x{info.dpidr.raw:08X}")
        else:
            # Expected, not a failure: over JTAG the identity registers are not
            # reachable through DPACC. CTRL/STAT is, and it is what shows the
            # DAP came up.
            console.print(f"CTRL/STAT 0x{info.ctrl_stat:08X}")
            console.print(f"[dim]{info.unavailable}[/dim]")


@iface.command("swd")
@click.option("--i-understand-swd-is-one-way", "opt_in", is_flag=True, help="Required. Acknowledges that returning to JTAG needs a power-on reset.")
@click.option("--dormant/--no-dormant", default=True, show_default=True, help="Use the dormant-state transition rather than the deprecated direct sequence.")
@click.pass_obj
def iface_swd(ctx: Ctx, opt_in: bool, dormant: bool) -> None:
    """Switch to SWD. Guarded, because this is a one-way door.

    RM 135.5.1: "Switching back from SWD mode to JTAG mode requires POR."
    RM 135.14.1.1: this SWJ-DP implements the dormant state, so a transition
    through dormant state is required; Arm deprecates the direct 0x79E7 sequence
    on parts that implement it.
    """
    if not opt_in:
        # UnsafeOperation, not ClickException: this is the safety policy exit
        # code (3), the same one the SDA-AP and AXI-AP guards use. A usage error
        # (1) would put it in the same bucket as a typo'd flag.
        raise UnsafeOperation(
            "refusing to switch to SWD without an explicit opt-in.\n"
            "RM 135.5.1 states: \"Switching back from SWD mode to JTAG mode requires POR.\"\n"
            f"Once in SWD, only a power cycle restores JTAG. Re-run with {SWD_OPT_IN} "
            "if that is acceptable."
        )

    console.print("[yellow]![/yellow] Switching to SWD. Recovery to JTAG requires a power-on reset.")
    console.print("[dim]what the probe is being asked to do:[/dim]")
    for i, step in enumerate(swj.describe_switch("jtag", "swd"), 1):
        console.print(f"  [dim]{i}.[/dim] {step}")

    install = discover(ctx.jlink_dir)
    kwargs: dict[str, object] = {"install": install, "serial": ctx.serial}
    if ctx.log:
        kwargs["log_file"] = ctx.log
    if ctx.transport_kind == "commander":
        kwargs["echo"] = ctx.echo
        kwargs["device"] = ctx.device
        kwargs["speed_khz"] = ctx.speed_khz

    t = make_transport(ctx.transport_kind, **kwargs)
    t.open()
    try:
        set_dormant = getattr(t, "set_dormant_mode_handling", None)
        if dormant and set_dormant is not None:
            set_dormant(True)
            console.print("[dim]enabled SetDormantModeHandling[/dim]")
        elif dormant:
            console.print(
                "[yellow]![/yellow] this transport cannot enable dormant-mode handling; "
                "use --transport dll for the dormant-aware path"
            )
        elif not dormant:
            console.print(
                "[yellow]![/yellow] --no-dormant selected. Arm deprecates the direct "
                "0x79E7 sequence on parts that implement the dormant state, and RM "
                "135.14.1.1 says this one does."
            )
        t.select_interface(Tif.SWD)
        t.set_speed_khz(ctx.speed_khz)
        configure = getattr(t, "configure_coresight", None)
        if configure is not None:
            configure("")  # SEGGER's own sample comments this as the SWD init sequence

        caps = list(getattr(t, "capability_warnings", []))
        if caps:
            for cap in caps:
                console.print(f"[red]probe limitation:[/red] {cap}")
            raise TransportError(
                "the probe declined the protocol switch. The sequences run on probe "
                "firmware and no exported DLL symbol can drive them from here, so "
                "there is no fallback: this needs a probe or firmware that supports "
                "CoreSight SoC-600 dormant handling."
            )

        dap = Dap(t)
        # ADIv6 B5.2: DP register state is UNKNOWN after a switch, so program it
        # absolutely rather than reading it first.
        dap.reinitialise_after_protocol_switch()
        dpidr = dap.read_dpidr()
        console.print(f"[green]SWD selected[/green]; DPIDR 0x{dpidr.raw:08X} ({dpidr.version_name})")
        console.print("TDI and TDO are now GPIO (RM Table 980). Power-cycle to return to JTAG.")
    finally:
        t.close()


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


@main.group()
def gates() -> None:
    """Inspect and open the debug-enable gates."""


@gates.command("status")
@click.pass_obj
def gates_status(ctx: Ctx) -> None:
    """Read the MDM-AP gate and the SDA-AP (HSE) gate. Read-only."""
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        _prepare_dap(dap)
        report = gates_mod.status(dap)
        emit.print_gates(report, console)
        if ctx.json_out:
            _emit_json(ctx.json_out, _gates_json(report))


@gates.command("apply")
@click.pass_obj
def gates_apply(ctx: Ctx) -> None:
    """Write the LADDER-1 debug-enable sequence (idempotent).

    Needed before the APB ROM will enumerate. RM 135.11 warns that touching debug
    components with the clocks still gated "might hang the debug APBIC bus".
    """
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        _prepare_dap(dap)
        report = gates_mod.apply_ladder1(dap)
        emit.print_gates(report, console)
        if report.mdm.enables_set:
            console.print("[green]debug-enable gate is open[/green] (sticky until power loss)")
        if ctx.json_out:
            _emit_json(ctx.json_out, _gates_json(report))


def _gates_json(report: gates_mod.GateReport) -> dict[str, object]:
    return {
        "mdm": {
            "idr": f"0x{report.mdm.idr:08X}" if report.mdm.idr is not None else None,
            "identity_ok": report.mdm.identity_ok,
            "registers": {k: f"0x{v:08X}" for k, v in report.mdm.registers.items()},
            "status_flags": report.mdm.status_flags(),
            "enables_set": report.mdm.enables_set,
            "error": report.mdm.error,
        },
        "hse": {
            "idr": f"0x{report.hse.idr:08X}" if report.hse.idr is not None else None,
            "authsts": f"0x{report.hse.authsts:08X}" if report.hse.authsts is not None else None,
            "app_debug_enabled": report.hse.app_debug_enabled,
            "challenge_pending": report.hse.challenge_pending,
            "reset_flags": report.hse.reset_flags(),
            "uid": f"0x{report.hse.uid:016X}" if report.hse.uid is not None else None,
            "error": report.hse.error,
        },
        "warnings": report.warnings,
    }


@main.command()
@click.option("--attempts", type=int, default=20, show_default=True, help="How many times to retry the chain scan while waiting for a live window.")
@click.option("--hold/--no-hold", default=True, show_default=True, help="Hold the system in functional reset once attached, so it stops rebooting.")
@click.option("--gate/--no-gate", default=True, show_default=True, help="Apply the LADDER-1 debug-enable sequence after attaching.")
@click.pass_obj
def attach(ctx: Ctx, attempts: int, hold: bool, gate: bool) -> None:
    """Get and keep a debug foothold on a target that will not stay still.

    Built for a virgin board. With blank flash the BootROM fails, the failure
    cascade escalates to a POR, and RM Table 301 makes POR one of exactly two
    things that hold the JTAGC in reset -- so the TAP is only alive in brief
    windows. With software running the problem is different but the symptom is
    the same: RM 135.15.1 says the debug pads "operate in JTAG functionality out
    of reset but can be later reassigned", so an application that remuxes them
    takes the TAP away.

    So: retry until a window opens, then immediately hold the system in
    functional reset (RM 135.11), which stops the reboot and stops software
    reaching the pad configuration. RM Table 301 confirms a functional reset
    leaves the JTAGC and SWJ-DP running, which is what makes this safe.
    """
    from .transport.commander import CommanderTransport

    install = discover(ctx.jlink_dir)
    t = CommanderTransport(
        install=install, serial=ctx.serial, log_file=ctx.log,
        device=ctx.device, speed_khz=ctx.speed_khz, echo=ctx.echo,
    )
    t.open()
    held = False
    for i in range(1, attempts + 1):
        if not hold:
            scan = t.scan_chain()
            idcode = scan.get("idcode")
            if isinstance(idcode, int) and idcode:
                console.print(f"[green]window caught[/green] on attempt {i}: IDCODE 0x{idcode:08X}")
                break
            console.print(f"[dim]attempt {i}/{attempts}: no response[/dim]")
            continue

        # The hold goes through the DLL backend, because that is the path proven
        # to land AP writes on this target -- it reads all nine APs and applies
        # LADDER-1 successfully, whereas the batch backend's WriteDP/ReadAP
        # sequence returns zeros for every AP here.
        #
        # Retry the *whole* sequence rather than just the scan: the TAP
        # answering does not mean the DAP will power up, and the DAP powering up
        # does not mean the write will land. Only the completed hold counts.
        try:
            with open_transport(ctx) as (_t2, dap):
                assert dap is not None
                dap.power_up()
                gates_mod.hold_in_functional_reset(dap)
                back = dap.ap_read(AP_SDA, SDA_SYSRSTC)
                if back & SDA_SYSRSTC_SYSFUNCREQ:
                    console.print(
                        f"[green]attached and holding[/green] on attempt {i} "
                        f"(SDASYSRSTC 0x{back:08X})"
                    )
                    held = True
                    break
                why = f"hold did not take (SDASYSRSTC reads 0x{back:08X})"
        except S32EError as exc:
            why = str(exc).splitlines()[0]
        console.print(f"[dim]attempt {i}/{attempts}: {why}[/dim]")
    else:
        raise TransportError(
            f"no usable window in {attempts} attempts. The TAP may answer while the DAP "
            "still refuses to power up, which is what J-Link reports as 'Failed to power "
            "up DAP'. If the flash is blank, let FREC reach 8 so the part lands in "
            "Fail-Safe serial boot where it stops looping (RM 62.12), or assert WDGDIS on "
            "J9 pin 28 to stop the SBC and PMIC watchdogs. `s32edbg doctor` ranks the causes."
        )

    if held:
        console.print(
            "[dim]The cores are stopped and the JTAGC stays up -- RM Table 301: a "
            "functional reset does not reset the JTAGC.[/dim]"
        )
    if gate:
        with open_transport(ctx) as (_t2, dap):
            assert dap is not None
            _prepare_dap(dap)
            report = gates_mod.apply_ladder1(dap)
            state = "open" if report.mdm.enables_set else "still gated"
            console.print(f"debug-enable gate: {state}")
            for w in report.warnings:
                console.print(f"[yellow]![/yellow] {w}")
    console.print(
        "[dim]Release with `s32edbg release` when done. The gate bits are sticky "
        "across functional resets and are lost only on power loss.[/dim]"
    )


@main.command()
@click.option("--attempts", type=int, default=10, show_default=True, help="How many times to retry while waiting for the DAP to power up.")
@click.pass_obj
def release(ctx: Ctx, attempts: int) -> None:
    """Let a held system run again. Never issues a destructive reset.

    Retries like `attach` does, for the same reason: the DAP powers up only in
    windows, and a release that gives up after one try leaves the target
    stopped with no obvious way to recover.
    """
    for i in range(1, attempts + 1):
        try:
            with open_transport(ctx) as (_t, dap):
                assert dap is not None
                dap.power_up()
                gates_mod.release_functional_reset(dap)
                back = dap.ap_read(AP_SDA, SDA_SYSRSTC)
                if not back & SDA_SYSRSTC_SYSFUNCREQ:
                    console.print(
                        f"[green]released[/green] on attempt {i}; the system is running again."
                    )
                    return
                why = f"still held (SDASYSRSTC 0x{back:08X})"
        except S32EError as exc:
            why = str(exc).splitlines()[0]
        console.print(f"[dim]attempt {i}/{attempts}: {why}[/dim]")
    raise TransportError(
        "could not reach the DAP to release the hold. If the target is already "
        "running, the hold was probably cleared by a reset -- a power cycle clears "
        "it unconditionally."
    )


# ---------------------------------------------------------------------------
# mem -- bus access through any of the RM Table 957 APs
# ---------------------------------------------------------------------------


def _ap_option(fn):
    return click.option(
        "--ap",
        "ap_spec",
        default="apb",
        show_default=True,
        help="Which bus to go through: " + ", ".join(sorted(AP_ALIASES)) + ", or a base address.",
    )(fn)


def _resolve_ap_or_fail(spec: str) -> int:
    try:
        return memaccess.resolve_ap(spec)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@main.group()
def mem() -> None:
    """Read and write memory through the debug access ports.

    The APB-AP reaches the CoreSight debug components on APBIC2; the AXI-AP
    reaches the whole system memory map and is the one RM Table 957 warns must
    not be touched while functional reset is asserted.
    """


@mem.command("info")
@_ap_option
@click.pass_obj
def mem_info(ctx: Ctx, ap_spec: str) -> None:
    """Report an AP's identity, capabilities and CSW as found."""
    ap_base = _resolve_ap_or_fail(ap_spec)
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        for note in _prepare_dap(dap):
            console.print(f"[dim]note:[/dim] {note}")
        idr = dap.ap_read(ap_base, adiv6.AP_IDR)
        cfg = dap.read_ap_cfg(ap_base)
        csw = dap.ap_read(ap_base, adiv6.AP_CSW)
        rom, present = dap.read_ap_base(ap_base)

        console.print(f"AP        {memaccess.ap_label(ap_base)}")
        console.print(f"IDR       0x{idr:08X}")
        console.print(f"CFG       0x{cfg.raw:08X}  "
                      f"big_endian={cfg.big_endian} large_address={cfg.large_address} "
                      f"large_data={cfg.large_data}")
        console.print(f"CSW       0x{csw:08X} (as found, before any access)")
        console.print(f"BASE      {'0x%08X' % rom if rom is not None else 'no debug entry'}"
                      f"{'' if present else '  (P bit clear)'}")
        if ctx.json_out:
            _emit_json(ctx.json_out, {
                "ap": f"0x{ap_base:08X}",
                "idr": f"0x{idr:08X}",
                "cfg": cfg.as_dict(),
                "csw_as_found": f"0x{csw:08X}",
                "base": f"0x{rom:08X}" if rom is not None else None,
            })


@mem.command("read")
@click.argument("addr")
@_ap_option
@click.option("--size", type=click.Choice(["8", "16", "32"]), default="32", show_default=True)
@click.option("--count", type=int, default=1, show_default=True, help="How many values to read.")
@click.pass_obj
def mem_read(ctx: Ctx, addr: str, ap_spec: str, size: str, count: int) -> None:
    """Read one or more values from a bus."""
    ap_base = _resolve_ap_or_fail(ap_spec)
    start = int(addr, 0)
    width = int(size)
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        for note in _prepare_dap(dap):
            console.print(f"[dim]note:[/dim] {note}")
        _report, notes = memaccess.preflight(dap, ap_base, write=False)
        range_note = memaccess.apb_range_note(ap_base, start)
        for note in notes + ([range_note] if range_note else []):
            console.print(f"[yellow]![/yellow] {note}")

        values = memaccess.read_words(dap, ap_base, start, count, size=width)
        stride = width // 8
        digits = width // 4
        for i, value in enumerate(values):
            console.print(f"0x{start + i * stride:08X}  0x{value:0{digits}X}")
        if ctx.json_out:
            _emit_json(ctx.json_out, {
                "ap": f"0x{ap_base:08X}",
                "address": f"0x{start:08X}",
                "size_bits": width,
                "values": [f"0x{v:0{digits}X}" for v in values],
                "notes": notes,
            })


@mem.command("dump")
@click.argument("addr")
@click.argument("nbytes", type=int)
@_ap_option
@click.pass_obj
def mem_dump(ctx: Ctx, addr: str, nbytes: int, ap_spec: str) -> None:
    """Hex-dump a region of a bus."""
    ap_base = _resolve_ap_or_fail(ap_spec)
    start = int(addr, 0)
    if start % 4 or nbytes % 4:
        raise click.ClickException("dump address and length must both be 4-byte aligned")
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        for note in _prepare_dap(dap):
            console.print(f"[dim]note:[/dim] {note}")
        _report, notes = memaccess.preflight(dap, ap_base, write=False)
        for note in notes:
            console.print(f"[yellow]![/yellow] {note}")

        words = memaccess.read_words(dap, ap_base, start, nbytes // 4, size=32)
        emit.print_hexdump(start, words, console)
        if ctx.json_out:
            _emit_json(ctx.json_out, {
                "ap": f"0x{ap_base:08X}",
                "address": f"0x{start:08X}",
                "words": [f"0x{w:08X}" for w in words],
            })


@mem.command("write")
@click.argument("addr")
@click.argument("value")
@_ap_option
@click.option("--size", type=click.Choice(["8", "16", "32"]), default="32", show_default=True)
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@click.pass_obj
def mem_write(ctx: Ctx, addr: str, value: str, ap_spec: str, size: str, yes: bool) -> None:
    """Write one value to a bus."""
    ap_base = _resolve_ap_or_fail(ap_spec)
    target = int(addr, 0)
    data = int(value, 0)
    width = int(size)
    if not yes:
        click.confirm(
            f"write 0x{data:0{width // 4}X} to 0x{target:08X} via {memaccess.ap_label(ap_base)}?",
            abort=True,
        )
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        for note in _prepare_dap(dap):
            console.print(f"[dim]note:[/dim] {note}")
        _report, notes = memaccess.preflight(dap, ap_base, write=True)
        for note in notes:
            console.print(f"[yellow]![/yellow] {note}")

        memaccess.write_word(dap, ap_base, target, data, size=width)
        readback = dap.mem_read(ap_base, target, size=width)
        console.print(f"0x{target:08X}  wrote 0x{data:0{width // 4}X}, "
                      f"reads back 0x{readback:0{width // 4}X}")
        if readback != data:
            console.print(
                "[yellow]![/yellow] read-back differs. That is normal for a "
                "write-1-to-clear or read-only field, but check it is what you meant."
            )


# ---------------------------------------------------------------------------
# core -- run-control against the per-core Cortex-R52 debug frames
# ---------------------------------------------------------------------------


def _core_option(fn):
    return click.option(
        "--core",
        "core_spec",
        default=None,
        help="Which core: an index, a name such as rtu0.0, or a debug-frame base address. "
        "Defaults to every core for read-only commands.",
    )(fn)


@main.group()
def core() -> None:
    """Inspect and control the Cortex-R52 cores.

    Every core has its own 4 KB external debug frame on APBIC2, reached through
    the APB-AP, plus a CTI at +0x10000 that carries the halt and restart
    requests. `gates apply` is a prerequisite: RM 135.11 leaves the debug clocks
    off by default.
    """


def _discover_cores(ctx: Ctx, dap: Dap) -> list[runcontrol.Core]:
    for note in _prepare_dap(dap):
        console.print(f"[dim]note:[/dim] {note}")
    cores = runcontrol.discover(dap)
    if not cores:
        raise TransportError(
            "no Cortex-R52 debug frames answered. The debug clocks are off by default "
            "(RM 135.11) -- run `s32edbg gates apply` first. If the gate is already "
            "open, `s32edbg romdump --source apb` will show whether the APBIC2 tree "
            "enumerates at all."
        )
    return cores


@core.command("list")
@click.pass_obj
def core_list(ctx: Ctx) -> None:
    """Discover every core debug frame and report what it is. Read-only.

    Cores are found two ways, because the two disagree on this part: the ROM
    table enumerates the frames the design exposes, and a by-stride probe finds
    any that are present but unlisted. The datasheet gives four Cortex-R52 cores
    per RTU while the ROM table lists two frames per RTU, so anything found only
    by probing is marked rather than quietly merged.
    """
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        emit.print_cores(cores, console)
        if ctx.json_out:
            _emit_json(ctx.json_out, {"cores": [c.as_dict() for c in cores]})


@core.command("status")
@_core_option
@click.pass_obj
def core_status(ctx: Ctx, core_spec: str | None) -> None:
    """Decode EDSCR and EDPRSR for one core, or all of them. Read-only."""
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        if core_spec is not None:
            try:
                cores = [runcontrol.resolve(core_spec, cores)]
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc

        payload: list[dict[str, object]] = []
        for c in cores:
            console.rule(f"{c.name}  (debug frame 0x{c.debug_base:08X})")
            for note in runcontrol.preflight_notes(c):
                console.print(f"[yellow]![/yellow] {note}")

            state = c.state.as_dict()
            edscr = state.get("edscr")
            edprsr = state.get("edprsr")
            if isinstance(edscr, dict):
                console.print(f"EDSCR   {edscr['raw']}  {edscr['status']}")
                console.print(
                    f"        halting_debug_enabled={edscr['halting_debug_enabled']} "
                    f"sticky_error={edscr['sticky_error']} "
                    f"instruction_transfer_empty={edscr['instruction_transfer_empty']}"
                )
            if isinstance(edprsr, dict):
                console.print(
                    f"EDPRSR  {edprsr['raw']}  powered_up={edprsr['powered_up']} "
                    f"halted={edprsr['halted']} os_locked={edprsr['os_locked']} "
                    f"in_reset={edprsr['in_reset']}"
                )
            if state.get("warning"):
                console.print(f"[red]![/red] {state['warning']}")
            if c.state.error:
                console.print(f"[red]{c.state.error}[/red]")

            where: dict[str, object] = {}
            if c.state.halted:
                where = runcontrol.halt_location(dap, c)
                if where.get("known"):
                    console.print(
                        f"stopped at {where['address']}  [dim]({where['source']})[/dim]"
                    )
                elif where.get("candidates"):
                    console.print(f"stopped at one of {where['candidates']}")
                    console.print(f"[dim]{where['source']}[/dim]")
            payload.append({"core": c.name, **state, "location": where})

        if ctx.json_out:
            _emit_json(ctx.json_out, {"status": payload})


def _select_cores(
    core_spec: str | None, all_cores: bool, cores: list[runcontrol.Core]
) -> list[runcontrol.Core]:
    if all_cores:
        return cores
    if core_spec is None:
        raise click.ClickException(
            "say which core: --core <index|name|address>, or --all. Names on this "
            "target are " + ", ".join(c.name for c in cores) + "."
        )
    try:
        return [runcontrol.resolve(core_spec, cores)]
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


#: Printed before every halt. The policy here is warn, not gate -- consistent
#: with `attach --hold`, which already stops the cores with no opt-in flag.
HALT_WARNING = (
    "halting stops the application on this core, and its lockstep partner stops "
    "with it in hardware. Peripherals may keep running: the MDM-AP IP_DBG controls "
    "decide what freezes and this tool does not touch them, so a watchdog can "
    "expire and reset the board while the core is stopped."
)


@core.command("unlock")
@_core_option
@click.option("--all", "all_cores", is_flag=True, help="Unlock every core.")
@click.pass_obj
def core_unlock(ctx: Ctx, core_spec: str | None, all_cores: bool) -> None:
    """Clear the OS lock and software lock on a core's debug frame.

    Usually not needed on its own -- `core halt` does it. Useful when you want
    to see whether the locks will clear before stopping anything.
    """
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        payload: list[dict[str, object]] = []
        for c in _select_cores(core_spec, all_cores, cores):
            runcontrol.preflight(c, write=True)
            notes = runcontrol.unlock(dap, c)
            for note in notes:
                console.print(f"[dim]{c.name}:[/dim] {note}")
            if not notes:
                console.print(f"[dim]{c.name}:[/dim] already unlocked")
            state = runcontrol.read_state(dap, c.debug_base)
            console.print(f"[green]{c.name}[/green] EDPRSR 0x{state.edprsr or 0:08X}")
            payload.append({"core": c.name, "notes": notes, **state.as_dict()})
        if ctx.json_out:
            _emit_json(ctx.json_out, {"unlock": payload})


@core.command("halt")
@_core_option
@click.option("--all", "all_cores", is_flag=True, help="Halt every reachable core.")
@click.pass_obj
def core_halt(ctx: Ctx, core_spec: str | None, all_cores: bool) -> None:
    """Stop a core. Clears its locks first if it needs it.

    Armv8 has no halt bit -- the request is a CTI trigger, so this programs the
    core's cross-trigger interface and pulses the channel wired to the debug
    request. Success requires EDPRSR.HALTED and EDSCR.STATUS to agree.
    """
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        chosen = _select_cores(core_spec, all_cores, cores)
        console.print(f"[yellow]![/yellow] {HALT_WARNING}")

        payload: list[dict[str, object]] = []
        for c in chosen:
            for note in runcontrol.preflight(c, write=True):
                console.print(f"[yellow]![/yellow] {c.name}: {note}")
            for note in runcontrol.unlock(dap, c):
                console.print(f"[dim]{c.name}:[/dim] {note}")

            state = runcontrol.halt(dap, c)
            console.print(
                f"[green]{c.name} halted[/green] -- "
                f"{r52debug.edscr_status_name(state.edscr or 0)} "
                f"(EDSCR 0x{state.edscr or 0:08X}, EDPRSR 0x{state.edprsr or 0:08X})"
            )
            payload.append({"core": c.name, **state.as_dict()})
        if ctx.json_out:
            _emit_json(ctx.json_out, {"halt": payload})


@core.command("resume")
@_core_option
@click.option("--all", "all_cores", is_flag=True, help="Resume every core.")
@click.pass_obj
def core_resume(ctx: Ctx, core_spec: str | None, all_cores: bool) -> None:
    """Let a halted core run again.

    Acknowledges the debug request before restarting. Skipping that leaves the
    request asserted at the trigger matrix, so the core re-enters Debug state
    immediately and the resume looks like it did nothing.
    """
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        payload: list[dict[str, object]] = []
        for c in _select_cores(core_spec, all_cores, cores):
            runcontrol.preflight(c, write=True)
            state = runcontrol.resume(dap, c)
            if state.rehalted:
                where = runcontrol.halt_location(dap, c)
                console.print(
                    f"[yellow]{c.name} ran and immediately re-halted[/yellow] -- "
                    f"{r52debug.edscr_status_name(state.edscr or 0)}"
                    + (f" at {where['address']}" if where.get("address") else "")
                )
                console.print(
                    "[dim]That is a working breakpoint, not a failed resume. To get "
                    "past it, clear the comparator or step over it first.[/dim]"
                )
            else:
                console.print(
                    f"[green]{c.name} running[/green] -- "
                    f"{r52debug.edscr_status_name(state.edscr or 0)}"
                )
            payload.append({"core": c.name, **state.as_dict()})
        if ctx.json_out:
            _emit_json(ctx.json_out, {"resume": payload})


@core.command("step")
@_core_option
@click.option("--over/--into", default=False, show_default="--into",
              help="--over runs a call to completion; --into steps one instruction.")
@click.option("--count", type=int, default=1, show_default=True)
@click.option("--mask-interrupts/--allow-interrupts", default=True, show_default=True,
              help="Masking stops a step landing in an interrupt handler instead of "
                   "the next instruction.")
@click.pass_obj
def core_step(ctx: Ctx, core_spec: str | None, over: bool, count: int,
              mask_interrupts: bool) -> None:
    """Step a halted core one instruction, or over a call.

    `--into` is an architectural halting step: one instruction, whatever it is.
    `--over` reads the instruction at the PC first, and if it is a
    branch-with-link it puts a temporary breakpoint at the return address and
    lets the call run, so you do not step through the whole callee.
    """
    with _one_core(ctx, core_spec) as (dap, c):
        for n in range(count):
            before = runcontrol.read_pc(dap, c)
            if over:
                state, info = runcontrol.step_over(dap, c, mask_interrupts=mask_interrupts)
                console.print(
                    f"0x{before:08X}  {info['mnemonic']}  -> {info['action']}"
                )
            else:
                state = runcontrol.step(dap, c, mask_interrupts=mask_interrupts)
            after = runcontrol.read_pc(dap, c)
            console.print(
                f"[green]step {n + 1}[/green]  0x{before:08X} -> 0x{after:08X}  "
                f"({r52debug.edscr_status_name(state.edscr or 0)})"
            )
        if ctx.json_out:
            _emit_json(ctx.json_out, {"core": c.name, "pc": f"0x{after:08X}"})


@core.command("xtrigger")
@click.argument("state", type=click.Choice(["on", "off"]))
@click.pass_obj
def core_xtrigger(ctx: Ctx, state: str) -> None:
    """Halt every core together, or stop doing so.

    With cross-trigger on, one core entering Debug state pulls the others in
    through the CTI matrix, so you get a coherent multicore snapshot. Without it
    the other cores keep running and can corrupt the state you stopped to look
    at -- the breakpoints guide covers this in section 7.3.

    It is off by default and single-core halts deliberately do not propagate.
    """
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        usable = [c for c in cores if not runcontrol.identity_problems(c)]
        notes = runcontrol.set_cross_trigger(dap, usable, state == "on")
        for note in notes:
            console.print(f"[dim]{note}[/dim]")
        console.print(
            f"[green]cross-trigger {state}[/green] across {len(usable)} core(s)"
        )
        if state == "on":
            console.print(
                "[yellow]![/yellow] halting any one core now stops them all, including "
                "the one running your application."
            )
        if ctx.json_out:
            _emit_json(ctx.json_out, {"cross_trigger": state, "cores": notes})


@core.command("sample")
@_core_option
@click.option("--count", type=int, default=200, show_default=True, help="How many samples.")
@click.pass_obj
def core_sample(ctx: Ctx, core_spec: str | None, count: int) -> None:
    """Sample where a running core is executing. Never stops it.

    EDPCSR is a profiling aid: valid while the core runs, 0xFFFFFFFF while it is
    halted. This is the safe way to find an address worth breakpointing --
    halting repeatedly to hunt for a hot spot risks deadlocking the application,
    and this touches nothing.
    """
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        c = _select_cores(core_spec, False, cores)[0]
        result = runcontrol.sample_pc(dap, c, count)

        console.print(
            f"{c.name}: {result['samples']} samples, {result['invalid']} invalid, "
            f"{result['distinct']} distinct addresses"
        )
        for row in result["top"]:  # type: ignore[union-attr]
            bar = "#" * int(row["percent"] / 2)
            console.print(f"  {row['address']}  {row['hits']:5d}  {row['percent']:5.1f}%  {bar}")
        if result["invalid"] == result["samples"]:
            console.print(f"[yellow]![/yellow] {result['note']}")
        else:
            console.print(f"[dim]{result['note']}[/dim]")
        if ctx.json_out:
            _emit_json(ctx.json_out, result)


@core.command("regs")
@_core_option
@click.pass_obj
def core_regs(ctx: Ctx, core_spec: str | None) -> None:
    """Read a halted core's registers.

    There is no register file to read: in Debug state the core executes
    instructions written to EDITR, so every value is fetched by making the core
    move it into the debug transfer register. That path is proved against a
    known pattern first, and if the proof fails no values are reported at all.
    """
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        c = _select_cores(core_spec, False, cores)[0]
        runcontrol.preflight(c, write=True)

        regs = runcontrol.read_registers(dap, c)
        if not regs.path_verified:
            for note in regs.notes:
                console.print(f"[red]![/red] {note}")
            raise TransportError(
                "the register-transfer path did not verify, so no register values are "
                "reported. See data/r52debug.py -- the AArch32 instruction encodings "
                "are derived, not documented, and this is the check that gates them."
            )

        emit.print_registers(c.name, regs, console)
        if ctx.json_out:
            _emit_json(ctx.json_out, {"core": c.name, **regs.as_dict()})


@core.command("reg")
@click.argument("name")
@click.argument("value", required=False)
@_core_option
@click.pass_obj
def core_reg(ctx: Ctx, name: str, value: str | None, core_spec: str | None) -> None:
    """Read or write one register of a halted core: `core reg r3`, `core reg r3 0x1234`."""
    key = name.strip().lower()
    if not (key.startswith("r") and key[1:].isdigit() and 0 <= int(key[1:]) <= 14):
        raise click.ClickException(
            f"{name!r} is not a readable core register. Use r0-r14; PC is reported "
            "separately by `core regs` because it does not come from the register file."
        )
    n = int(key[1:])

    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        c = _select_cores(core_spec, False, cores)[0]
        runcontrol.preflight(c, write=True)

        if not runcontrol.verify_register_path(dap, c):
            raise TransportError(
                "the register-transfer round trip failed; refusing to report or write "
                "a register value."
            )
        if value is not None:
            runcontrol.write_reg(dap, c, n, int(value, 0))
        got = runcontrol.read_reg(dap, c, n)
        console.print(f"{c.name}  r{n} = 0x{got:08X}")
        if ctx.json_out:
            _emit_json(ctx.json_out, {"core": c.name, "register": f"r{n}", "value": f"0x{got:08X}"})


# ---------------------------------------------------------------------------
# m33 -- the three Cortex-M33 subsystems
# ---------------------------------------------------------------------------


def _m33_option(fn):
    return click.option(
        "--core",
        "core_spec",
        default=None,
        help="Which M33: " + ", ".join(m33_mod.M33_APS) + ", or an AP base address.",
    )(fn)


@main.group()
def m33() -> None:
    """Run-control for the Cortex-M33 subsystems.

    A different mechanism from the R52 cores. An M33 is reached through its own
    AHB-AP as ordinary memory -- the debug registers live in the processor's own
    address map at 0xE000E000 -- so halting is a single write and reading a
    register is two, with no instruction-transfer register and no cross-trigger
    interface involved.

    Three of them on this part (RM Table 957): the System Manager, and FlexLLCE
    CE cores 0 and 1.
    """


@contextmanager
def _one_m33(ctx: Ctx, core_spec: str | None, *, write: bool = True):
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        for note in _prepare_dap(dap):
            console.print(f"[dim]note:[/dim] {note}")
        cores = m33_mod.discover(dap)
        if core_spec is None:
            raise click.ClickException(
                "say which core: --core " + "|".join(m33_mod.M33_APS)
            )
        try:
            c = m33_mod.resolve(core_spec, cores)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        for note in m33_mod.preflight(c, write=write):
            console.print(f"[yellow]![/yellow] {c.name}: {note}")
        yield dap, c


@m33.command("list")
@click.pass_obj
def m33_list(ctx: Ctx) -> None:
    """Identify all three M33 subsystems. Read-only."""
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        for note in _prepare_dap(dap):
            console.print(f"[dim]note:[/dim] {note}")
        cores = m33_mod.discover(dap)
        emit.print_m33_cores(cores, console)
        if ctx.json_out:
            _emit_json(ctx.json_out, {"m33": [c.as_dict() for c in cores]})


@m33.command("halt")
@_m33_option
@click.pass_obj
def m33_halt(ctx: Ctx, core_spec: str | None) -> None:
    """Stop an M33 core."""
    with _one_m33(ctx, core_spec) as (dap, c):
        console.print(f"[yellow]![/yellow] {HALT_WARNING}")
        dhcsr = m33_mod.halt(dap, c)
        pc = m33_mod.read_register(dap, c, "pc")
        console.print(
            f"[green]{c.name} halted[/green]  PC 0x{pc:08X}  DHCSR 0x{dhcsr:08X}"
        )
        if ctx.json_out:
            _emit_json(ctx.json_out, {"core": c.name, "pc": f"0x{pc:08X}"})


@m33.command("resume")
@_m33_option
@click.pass_obj
def m33_resume(ctx: Ctx, core_spec: str | None) -> None:
    """Let a halted M33 core run again."""
    with _one_m33(ctx, core_spec) as (dap, c):
        dhcsr, rehalted = m33_mod.resume(dap, c)
        if rehalted:
            pc = m33_mod.read_register(dap, c, "pc")
            console.print(
                f"[yellow]{c.name} ran and immediately re-halted[/yellow] at "
                f"0x{pc:08X}  DHCSR 0x{dhcsr:08X}"
            )
            console.print(
                "[dim]That is a working breakpoint, not a failed resume. Clear the "
                "comparator or step past it first.[/dim]"
            )
        else:
            console.print(f"[green]{c.name} running[/green]  DHCSR 0x{dhcsr:08X}")


@m33.command("step")
@_m33_option
@click.option("--count", type=int, default=1, show_default=True)
@click.option("--mask-interrupts/--allow-interrupts", default=True, show_default=True,
              help="Masking keeps a step from landing in an interrupt handler.")
@click.pass_obj
def m33_step(ctx: Ctx, core_spec: str | None, count: int, mask_interrupts: bool) -> None:
    """Step a halted M33 core one instruction at a time."""
    with _one_m33(ctx, core_spec) as (dap, c):
        for n in range(count):
            before = m33_mod.read_register(dap, c, "pc")
            m33_mod.step(dap, c, mask_interrupts=mask_interrupts)
            after = m33_mod.read_register(dap, c, "pc")
            console.print(f"[green]step {n + 1}[/green]  0x{before:08X} -> 0x{after:08X}")


@m33.command("regs")
@_m33_option
@click.pass_obj
def m33_regs(ctx: Ctx, core_spec: str | None) -> None:
    """Read a halted M33 core's registers.

    No instruction execution and no scratch register: the core register file is
    reached through a selector and a data register, so reading it cannot disturb
    the context you halted to inspect.
    """
    with _one_m33(ctx, core_spec) as (dap, c):
        values = m33_mod.read_registers(dap, c)
        emit.print_m33_registers(c.name, values, console)
        if ctx.json_out:
            _emit_json(
                ctx.json_out,
                {"core": c.name, "registers": {k: f"0x{v:08X}" for k, v in values.items()}},
            )


@m33.command("sample")
@_m33_option
@click.option("--count", type=int, default=200, show_default=True)
@click.pass_obj
def m33_sample(ctx: Ctx, core_spec: str | None, count: int) -> None:
    """Sample where a running M33 is executing. Never stops it."""
    with _one_m33(ctx, core_spec, write=False) as (dap, c):
        result = m33_mod.sample_pc(dap, c, count)
        console.print(
            f"{c.name}: {result['samples']} samples, {result['invalid']} invalid, "
            f"{result['distinct']} distinct addresses"
        )
        for row in result["top"]:  # type: ignore[union-attr]
            console.print(
                f"  {row['address']}  {row['hits']:5d}  {row['percent']:5.1f}%  "
                + "#" * int(row["percent"] / 2)
            )
        if result.get("note"):
            console.print(f"[yellow]![/yellow] {result['note']}")
        if ctx.json_out:
            _emit_json(ctx.json_out, result)


@m33.command("bp")
@click.argument("action", type=click.Choice(["list", "set", "clear"]))
@click.argument("address", required=False)
@_m33_option
@click.pass_obj
def m33_bp(ctx: Ctx, action: str, address: str | None, core_spec: str | None) -> None:
    """Manage M33 hardware breakpoints: `m33 bp list`, `bp set <addr>`, `bp clear <n>`.

    These come from the Breakpoint Unit, not the R52's DBGBVR comparators -- four
    or eight of them, read from FP_CTRL rather than assumed.
    """
    with _one_m33(ctx, core_spec, write=action != "list") as (dap, c):
        if action == "list":
            entries = m33_mod.list_breakpoints(dap, c)
            emit.print_comparators(f"{c.name} breakpoints", entries, console)
        elif action == "set":
            if address is None:
                raise click.ClickException("`bp set` needs an address")
            entry = m33_mod.set_breakpoint(dap, c, int(address, 0))
            console.print(
                f"[green]breakpoint {entry['index']}[/green] at {entry['address']} on {c.name}"
            )
        else:
            if address is None:
                raise click.ClickException("`bp clear` needs a comparator index")
            m33_mod.clear_breakpoint(dap, c, int(address, 0))
            console.print(f"cleared breakpoint {address} on {c.name}")


# ---------------------------------------------------------------------------
# bp / wp -- hardware comparators
# ---------------------------------------------------------------------------


@contextmanager
def _one_core(ctx: Ctx, core_spec: str | None, *, write: bool = True):
    """Open a session, discover, select one core, preflight it.

    Clears the OS lock when the command is going to write. Most cores come up
    locked after a power cycle, and while the lock is set every core-domain
    register access faults -- so without this a `bp set` on a freshly booted
    board failed with "a comparator write faulted", which describes the symptom
    and not the cause. `core halt` already unlocked; this makes the comparator
    commands behave the same way.
    """
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        c = _select_cores(core_spec, False, cores)[0]
        for note in runcontrol.preflight(c, write=write):
            console.print(f"[yellow]![/yellow] {c.name}: {note}")
        if write and c.state.unreachable_reason:
            for note in runcontrol.unlock(dap, c):
                console.print(f"[dim]{c.name}:[/dim] {note}")
            c.state = runcontrol.read_state(dap, c.debug_base)
        yield dap, c


@main.group()
def bp() -> None:
    """Hardware instruction breakpoints.

    Eight comparators per core on this part, read from EDDFR rather than
    assumed. They are per core: a breakpoint on rtu0.0 does not catch code
    running on rtu0.1, and in lockstep the pair is one logical target.
    """


@bp.command("list")
@_core_option
@click.pass_obj
def bp_list(ctx: Ctx, core_spec: str | None) -> None:
    """Show every breakpoint comparator, programmed or not."""
    with _one_core(ctx, core_spec, write=False) as (dap, c):
        entries = runcontrol.list_breakpoints(dap, c)
        emit.print_comparators(f"{c.name} breakpoints", entries, console)
        if ctx.json_out:
            _emit_json(ctx.json_out, {"core": c.name, "breakpoints": entries})


@bp.command("set")
@click.argument("address")
@_core_option
@click.option("--index", type=int, default=None, help="Use this comparator; default is the first free one.")
@click.option("--arm", "arm_state", is_flag=True, help="The target is A32 code, not T32.")
@click.pass_obj
def bp_set(ctx: Ctx, address: str, core_spec: str | None, index: int | None, arm_state: bool) -> None:
    """Set a hardware breakpoint at an instruction address.

    A Thumb function pointer has bit 0 set to mark the instruction set; that bit
    is not part of the address and is stripped. Writing it into the comparator
    would give one that can never match.
    """
    with _one_core(ctx, core_spec) as (dap, c):
        entry = runcontrol.set_breakpoint(
            dap, c, int(address, 0), index=index, thumb=not arm_state
        )
        console.print(
            f"[green]breakpoint {entry['index']}[/green] at {entry['address']} on {c.name} "
            f"(DBGBVR {entry['dbgbvr']}, DBGBCR {entry['dbgbcr']}, BAS {entry['bas']})"
        )
        if ctx.json_out:
            _emit_json(ctx.json_out, {"core": c.name, "breakpoint": entry})


@bp.command("clear")
@click.argument("index", type=int, required=False)
@_core_option
@click.option("--all", "all_slots", is_flag=True, help="Clear every comparator on this core.")
@click.pass_obj
def bp_clear(ctx: Ctx, index: int | None, core_spec: str | None, all_slots: bool) -> None:
    """Clear one breakpoint comparator, or all of them."""
    if index is None and not all_slots:
        raise click.ClickException("give a comparator index, or --all")
    with _one_core(ctx, core_spec) as (dap, c):
        total, _ = runcontrol.comparator_counts(c)
        targets = range(total) if all_slots else [index]
        for n in targets:  # type: ignore[arg-type]
            runcontrol.clear_breakpoint(dap, c, int(n))
        console.print(f"cleared {'all ' if all_slots else ''}breakpoint(s) on {c.name}")


@main.group()
def wp() -> None:
    """Hardware data watchpoints. Separate comparators from breakpoints."""


@wp.command("list")
@_core_option
@click.pass_obj
def wp_list(ctx: Ctx, core_spec: str | None) -> None:
    with _one_core(ctx, core_spec, write=False) as (dap, c):
        entries = runcontrol.list_watchpoints(dap, c)
        emit.print_comparators(f"{c.name} watchpoints", entries, console)
        if ctx.json_out:
            _emit_json(ctx.json_out, {"core": c.name, "watchpoints": entries})


@wp.command("set")
@click.argument("address")
@_core_option
@click.option("--size", type=click.Choice(["1", "2", "4", "8"]), default="4", show_default=True)
@click.option("--on", "access", type=click.Choice(["load", "store", "both"]), default="both", show_default=True)
@click.option("--index", type=int, default=None)
@click.pass_obj
def wp_set(ctx: Ctx, address: str, core_spec: str | None, size: str, access: str, index: int | None) -> None:
    """Watch a data address for load, store or both."""
    with _one_core(ctx, core_spec) as (dap, c):
        try:
            entry = runcontrol.set_watchpoint(
                dap, c, int(address, 0), size=int(size), on=access, index=index
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        console.print(
            f"[green]watchpoint {entry['index']}[/green] at {entry['address']} on {c.name}, "
            f"{entry['bytes']} byte(s), on {entry['on']}"
        )
        if ctx.json_out:
            _emit_json(ctx.json_out, {"core": c.name, "watchpoint": entry})


@wp.command("clear")
@click.argument("index", type=int, required=False)
@_core_option
@click.option("--all", "all_slots", is_flag=True)
@click.pass_obj
def wp_clear(ctx: Ctx, index: int | None, core_spec: str | None, all_slots: bool) -> None:
    if index is None and not all_slots:
        raise click.ClickException("give a comparator index, or --all")
    with _one_core(ctx, core_spec) as (dap, c):
        _, total = runcontrol.comparator_counts(c)
        for n in (range(total) if all_slots else [index]):  # type: ignore[arg-type]
            runcontrol.clear_watchpoint(dap, c, int(n))
        console.print(f"cleared {'all ' if all_slots else ''}watchpoint(s) on {c.name}")


# ---------------------------------------------------------------------------
# apmap / romdump
# ---------------------------------------------------------------------------


@main.command("apmap")
@click.option("--method", type=click.Choice(["both", "direct", "walk"]), default="both", show_default=True)
@click.pass_obj
def apmap_cmd(ctx: Ctx, method: str) -> None:
    """Discover access ports and diff them against RM Table 957."""
    methods = ("direct", "walk") if method == "both" else (method,)
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        _prepare_dap(dap)
        result = apmap_mod.discover(dap, methods=methods)
        emit.print_apmap(result, console)
        if ctx.json_out:
            _emit_json(
                ctx.json_out,
                {
                    "baseptr": f"0x{result.baseptr:X}" if result.baseptr is not None else None,
                    "rom_base_used": f"0x{result.rom_base_used:08X}",
                    "walk_error": result.walk_error,
                    "notes": result.notes,
                    "aps": [
                        {
                            "base": f"0x{ap.base:08X}",
                            "idr": f"0x{ap.idr.raw:08X}" if ap.idr and ap.idr.present else None,
                            "kind": ap.describe(),
                            "expected_role": ap.expected_name,
                            "idr_matches_rm": ap.idr_matches_rm,
                            "found_by": sorted(ap.found_by),
                            "error": ap.error,
                        }
                        for ap in result.aps
                    ],
                    "diff_against_rm": result.diff_against_rm(),
                },
            )


@main.command("romdump")
@click.option("--source", type=click.Choice(["both", "apspace", "apb"]), default="both", show_default=True, help="apspace = the AP-map ROM; apb = the CoreSight tree behind the APB-AP.")
@click.option("--ap", "ap_base", default=None, help="Override the MEM-AP to walk through, e.g. 0x01C30000.")
@click.option("--rom", "rom_base", default=None, help="Override the ROM table address to start from.")
@click.option("--max-depth", type=int, default=romtable.DEFAULT_MAX_DEPTH, show_default=True)
@click.pass_obj
def romdump_cmd(ctx: Ctx, source: str, ap_base: str | None, rom_base: str | None, max_depth: int) -> None:
    """Walk and dump every reachable CoreSight ROM table."""
    payload: dict[str, object] = {}
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        _prepare_dap(dap)

        gate = gates_mod.status(dap)
        if not gate.mdm.enables_set:
            console.print(
                "[yellow]![/yellow] the debug-enable gate is still closed, so the APB ROM "
                "will very likely read as empty. Run `s32edbg gates apply` first."
            )

        if source in ("both", "apspace"):
            base = int(rom_base, 0) if rom_base else (dap.read_baseptr() or APBIC1_ROM_BASE) & 0xFFFFFFFF
            console.rule(f"AP-space ROM table at 0x{base:08X}")
            root = romtable.walk(dap.apspace_reader(), base, max_depth=max_depth)
            emit.print_rom_tree(root, console)
            console.print(_summary_line(root))
            payload["apspace"] = emit.node_to_dict(root)

        if source in ("both", "apb"):
            mem_ap = int(ap_base, 0) if ap_base else AP_APB
            rom, present = dap.read_ap_base(mem_ap)
            if not present or rom is None:
                console.rule(f"MEM-AP 0x{mem_ap:08X}")
                console.print(
                    "[yellow]BASE.P is clear[/yellow]: this AP reports no debug entry. "
                    "If the debug-enable gate was just opened, retry."
                )
                payload["apb"] = {"error": "BASE.P clear, no debug entry"}
            else:
                console.rule(f"APB ROM table at 0x{rom:08X} (via MEM-AP 0x{mem_ap:08X})")
                root = romtable.walk(dap.mem_reader(mem_ap), rom, max_depth=max_depth)
                emit.print_rom_tree(root, console)
                console.print(_summary_line(root))
                payload["apb"] = emit.node_to_dict(root)

    if ctx.json_out:
        _emit_json(ctx.json_out, payload)


def _summary_line(root: romtable.Node) -> str:
    s = romtable.summarise(root)
    colour = "green" if s["errors"] == 0 else "yellow"
    return (
        f"[{colour}]{s['tables']} tables, {s['components']} components, "
        f"{s['errors']} errors[/{colour}]"
    )


# ---------------------------------------------------------------------------
# tap
# ---------------------------------------------------------------------------


@main.group()
def tap() -> None:
    """Raw JTAG TAP operations."""


@tap.command("info")
@click.pass_obj
def tap_info(ctx: Ctx) -> None:
    """Read IDCODE and measure the IR length."""
    with open_transport(ctx, tif=Tif.JTAG, connect_dap=False) as (t, _):
        info = tap_mod.probe(t, expect_ir=ctx.irlen)  # type: ignore[arg-type]
        emit.print_tap(info, console)
        hint = bscan_mod.bsdl_hint(info.idcode)
        if hint:
            console.print(f"[dim]{hint}[/dim]")
        if ctx.json_out:
            _emit_json(ctx.json_out, info.as_dict())


@tap.command("ir")
@click.argument("value")
@click.option("--bits", type=int, default=None, help="IR width; measured if omitted.")
@click.pass_obj
def tap_ir(ctx: Ctx, value: str, bits: int | None) -> None:
    """Shift a value into the instruction register."""
    with open_transport(ctx, tif=Tif.JTAG, connect_dap=False, needs_raw_jtag=True) as (t, _):
        width = bits or ctx.irlen or tap_mod.measure_ir_length(t)  # type: ignore[arg-type]
        tdo = t.ir_shift(int(value, 0), width)  # type: ignore[union-attr]
        console.print(f"IR {width} bits <- 0x{int(value, 0):X}, TDO 0x{tdo:X}")


@tap.command("dr")
@click.argument("value")
@click.argument("bits", type=int)
@click.pass_obj
def tap_dr(ctx: Ctx, value: str, bits: int) -> None:
    """Shift a value through the data register (instruction must already be loaded)."""
    with open_transport(ctx, tif=Tif.JTAG, connect_dap=False, needs_raw_jtag=True) as (t, _):
        tdo = t.dr_shift(int(value, 0), bits)  # type: ignore[union-attr]
        console.print(f"DR {bits} bits <- 0x{int(value, 0):X}, TDO 0x{tdo:X}")


# ---------------------------------------------------------------------------
# bscan
# ---------------------------------------------------------------------------


@main.group()
def bscan() -> None:
    """Boundary scan (SAMPLE/PRELOAD only).

    EXTEST, HIGHZ and CLAMP are deliberately not implemented: RM 136.4.4 says
    each asserts the internal MCU system reset, and HIGHZ additionally floats
    every output driver.
    """


def _resolve_irlen(t: object, ctx: Ctx) -> int:
    if ctx.irlen:
        return ctx.irlen
    return tap_mod.measure_ir_length(t)  # type: ignore[arg-type]


#: Measured on silicon, and the reason boundary scan has to go last.
#:
#: The RM Table 984 preamble's last step writes TLM_TAP_SELECT_TDR = 0x80000011,
#: which links JTAGC_AE into the scan chain: `doctor` reports TotalIRLen 8 before
#: it and 16 after, and the change persists. J-Link's DAP addressing is set up
#: for the chain it scanned at connect, so once the chain grows a TAP it cannot
#: identify, DP access stops working -- CTRL/STAT went from 0xF0000000 to
#: 0x00000001 on both backends, and `attach` could not power up the DAP at all.
#: Nothing is damaged; a power cycle restores the 8-bit chain.
PREAMBLE_CHAIN_WARNING = (
    "the preamble links JTAGC_AE into the scan chain (TLM_TAP_SELECT_TDR), which "
    "takes the JTAG IR from 8 bits to 16 and persists. J-Link's DAP addressing "
    "assumes the chain it scanned at connect, so DP/AP access -- connect, gates, "
    "apmap, romdump, mem, attach -- will stop working until you power-cycle. "
    "Run boundary scan last."
)


@bscan.command("preamble")
@click.option("--svf", "svf_path", default=None, help="Write an SVF file instead of shifting directly.")
@click.option("--run-svf", is_flag=True, help="Play the generated SVF with JTAGLoad.exe.")
@click.pass_obj
def bscan_preamble(ctx: Ctx, svf_path: str | None, run_svf: bool) -> None:
    """Shift the RM Table 984 preamble that selects the BSR segments."""
    if svf_path or run_svf:
        import tempfile

        from .transport import svf as svf_mod

        irlen = ctx.irlen or BSCAN_DEFAULT_IRLEN
        text = svf_mod.emit_svf_for_preamble(irlen, frequency_hz=ctx.speed_khz * 1000)
        # --run-svf on its own still needs a file to hand JTAGLoad, so make a
        # throwaway one rather than refusing or silently falling through to the
        # raw path, which needs a capability the SVF route exists to avoid.
        if not svf_path:
            svf_path = str(Path(tempfile.gettempdir()) / "s32edbg-preamble.svf")
        Path(svf_path).write_text(text, encoding="utf-8")
        console.print(f"[green]wrote[/green] {svf_path} (IR width {irlen})")
        if not ctx.irlen:
            console.print(
                f"[dim]IR width {irlen} (RM Table 959), measured on silicon -- see "
                "data/bscan.py. Override with --irlen if your part differs.[/dim]"
            )
        if run_svf:
            result = svf_mod.run_svf(svf_path, install=discover(ctx.jlink_dir), serial=ctx.serial, log_file=ctx.log)
            console.print(result.stdout.strip() or "[dim](no output)[/dim]")
            for line in result.failures():
                console.print(f"[red]{line}[/red]")
            console.print("[green]SVF ok[/green]" if result.ok else "[red]SVF failed[/red]")
            if result.ok:
                console.print(f"[yellow]![/yellow] {PREAMBLE_CHAIN_WARNING}")
        return

    with open_transport(ctx, tif=Tif.JTAG, connect_dap=False, needs_raw_jtag=True) as (t, _):
        irlen = _resolve_irlen(t, ctx)
        result = bscan_mod.run_preamble(t, irlen)  # type: ignore[arg-type]
        for step in result.steps:
            mark = "[green]ok[/green]" if step.get("ok") else "[red]failed[/red]"
            console.print(f"{step['step']:>20}: IR {step['ir']} ({step['ir_bits']}b), DR {step['dr_bits']}b  {mark}")
            if step.get("error"):
                console.print(f"    [red]{step['error']}[/red]")
        console.print("[green]preamble complete[/green]" if result.ok else "[red]preamble failed[/red]")
        if result.ok:
            console.print(f"[yellow]![/yellow] {PREAMBLE_CHAIN_WARNING}")
        if ctx.json_out:
            _emit_json(ctx.json_out, {"ir_length": irlen, **result.as_dict()})


@bscan.command("length")
@click.option("--max-bits", type=int, default=4096, show_default=True)
@click.option("--skip-preamble", is_flag=True, help="Assume the preamble has already been shifted.")
@click.pass_obj
def bscan_length(ctx: Ctx, max_bits: int, skip_preamble: bool) -> None:
    """Measure the boundary-scan register length.

    The RM does not state it: "The size of the boundary scan register and bit
    ordering is device-dependent and can be found in the device BSDL file."
    """
    with open_transport(ctx, tif=Tif.JTAG, connect_dap=False, needs_raw_jtag=True) as (t, _):
        irlen = _resolve_irlen(t, ctx)
        if not skip_preamble:
            pre = bscan_mod.run_preamble(t, irlen)  # type: ignore[arg-type]
            if not pre.ok:
                raise click.ClickException("preamble failed; cannot measure the BSR")
        length = bscan_mod.measure_bsr_length(t, irlen, max_bits=max_bits)  # type: ignore[arg-type]
        console.print(f"boundary-scan register length: [bold]{length}[/bold] bits")
        if ctx.json_out:
            _emit_json(ctx.json_out, {"ir_length": irlen, "bsr_length": length})


@bscan.command("sample")
@click.option("--bsr-length", type=int, default=None, help="Skip measurement and use this length.")
@click.option("--skip-preamble", is_flag=True)
@click.option("--watch", is_flag=True, help="Take a second capture and report which bits changed.")
@click.pass_obj
def bscan_sample(ctx: Ctx, bsr_length: int | None, skip_preamble: bool, watch: bool) -> None:
    """Capture pin states with SAMPLE/PRELOAD. Non-invasive.

    Runs through SVF/JTAGLoad, which is the route proven on hardware. It costs
    one JTAGLoad invocation per byte, because JTAGLoad stops at the first TDO
    mismatch and that mismatch report is the only way it reveals received data.
    """
    irlen = ctx.irlen or BSCAN_DEFAULT_IRLEN
    console.print(f"[yellow]![/yellow] {PREAMBLE_CHAIN_WARNING}")
    if bsr_length is None:
        raise click.ClickException(
            "--bsr-length is required for now: measuring it needs a TDO-reporting raw "
            "shift, which no backend provides here. RM 136.3.4 puts the length only in "
            f"the BSDL file. Start with a guess ({BSCAN_PROBE_BITS}) and widen it."
        )
    install = discover(ctx.jlink_dir)

    def capture(label: str):
        seen = 0

        def on_byte(i: int, total: int, _v: int) -> None:
            nonlocal seen
            seen = i + 1
            if seen % 8 == 0 or seen == total:
                console.print(f"[dim]{label}: {seen}/{total} bytes[/dim]", end="\r")

        r = bscan_mod.svf_sample(
            irlen, bsr_length, install=install, serial=ctx.serial,
            log_file=ctx.log, frequency_hz=ctx.speed_khz * 1000, on_byte=on_byte,
        )
        console.print(" " * 40, end="\r")
        return r

    first = capture("capture 1")
    console.print(f"captured {first.bsr_length} bits: {first.as_dict()['raw_hex']}")

    payload: dict[str, object] = {"ir_length": irlen, "first": first.as_dict()}
    if watch:
        click.pause("change an input pin on the board, then press any key...")
        second = capture("capture 2")
        changed = first.diff(second)
        console.print(f"second capture: {second.as_dict()['raw_hex']}")
        if changed:
            console.print(f"[green]{len(changed)} bit(s) changed:[/green] {changed}")
        else:
            console.print(
                "[yellow]no bits changed.[/yellow] Either nothing moved, or the capture "
                "is not live -- check the preamble took."
            )
        payload["second"] = second.as_dict()
        payload["changed_bits"] = changed
    console.print(f"[dim]{first.as_dict()['note']}[/dim]")
    if ctx.json_out:
        _emit_json(ctx.json_out, payload)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@main.command("report")
@click.option("--apply-gates", is_flag=True, help="Open the debug-enable gate before dumping.")
@click.pass_obj
def report_cmd(ctx: Ctx, apply_gates: bool) -> None:
    """Collect everything into one artifact: probe, DAP, gates, APs, ROM tree, TAP."""
    payload: dict[str, object] = {"tool_version": __version__}

    with open_transport(ctx, tif=Tif.JTAG, connect_dap=False) as (t, _):
        payload["probe"] = t.probe_info().as_dict()  # type: ignore[union-attr]
        try:
            payload["tap"] = tap_mod.probe(t, expect_ir=ctx.irlen).as_dict()  # type: ignore[arg-type]
        except S32EError as exc:
            payload["tap"] = {"error": str(exc)}

    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        payload["dap_notes"] = _prepare_dap(dap)
        info = dap.info()
        payload["dap"] = {
            "dpidr": f"0x{info.dpidr.raw:08X}" if info.dpidr else None,
            "version": info.dpidr.version_name if info.dpidr else None,
            "is_adiv6": info.dpidr.is_adiv6 if info.dpidr else None,
            "dpidr1": info.dpidr1,
            "baseptr": f"0x{info.baseptr:X}" if info.baseptr is not None else None,
            "unavailable": info.unavailable,
        }

        report = gates_mod.apply_ladder1(dap) if apply_gates else gates_mod.status(dap)
        payload["gates"] = _gates_json(report)

        result = apmap_mod.discover(dap)
        payload["apmap"] = {
            "aps": [
                {
                    "base": f"0x{ap.base:08X}",
                    "idr": f"0x{ap.idr.raw:08X}" if ap.idr and ap.idr.present else None,
                    "kind": ap.describe(),
                    "expected_role": ap.expected_name,
                    "idr_matches_rm": ap.idr_matches_rm,
                }
                for ap in result.aps
            ],
            "diff_against_rm": result.diff_against_rm(),
        }

        roms: dict[str, object] = {}
        base = (info.baseptr or APBIC1_ROM_BASE) & 0xFFFFFFFF
        roms["apspace"] = emit.node_to_dict(romtable.walk(dap.apspace_reader(), base))
        rom, present = dap.read_ap_base(AP_APB)
        if present and rom is not None:
            roms["apb"] = emit.node_to_dict(romtable.walk(dap.mem_reader(AP_APB), rom))
        else:
            roms["apb"] = {"error": "BASE.P clear, no debug entry"}
        payload["romtables"] = roms

    _emit_json(ctx.json_out or "-", payload)


def entry() -> int:
    """Console entry point: turns tool exceptions into exit codes.

    2 = tool error, 3 = refused by safety policy, 130 = aborted.
    """
    try:
        main.main(standalone_mode=False)
    except UnsafeOperation as exc:
        err_console.print(f"[red]refused:[/red] {exc}")
        return 3
    except S32EError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        return 2
    except click.ClickException as exc:
        err_console.print(f"[red]error:[/red] {exc.format_message()}")
        return exc.exit_code
    except click.Abort:
        err_console.print("[yellow]aborted[/yellow]")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(entry())

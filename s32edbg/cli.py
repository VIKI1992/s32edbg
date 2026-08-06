"""s32edbg command-line interface."""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import click
from rich.console import Console

from . import __version__
from . import elf as elf_mod
from .dap import adiv6
from .dap import apmap as apmap_mod
from .dap import flasher as flasher_mod
from .dap import gates as gates_mod
from .dap import iface as iface_mod
from .dap import loader as loader_mod
from .dap import memaccess
from .dap import romtable
from .dap import m33 as m33_mod
from .dap import runcontrol
from .dap.adiv6 import Dap
from .data import qspiflash
from .data import r52debug
from .data import rtuboot
from .data import smuboot
from .data import swj
from .data.bscan import BSCAN_DEFAULT_IRLEN, BSCAN_PROBE_BITS
from .data.expected_apmap import AP_ALIASES, AP_APB, AP_AXI, AP_MDM, AP_SDA, APBIC1_ROM_BASE
from .data.known_ids import decode_dpidr
from .data.ladder1 import MDM_IDR_EXPECTED, SDA_SYSRSTC, SDA_SYSRSTC_SYSFUNCREQ
from .errors import ElfError, PowerUpError, S32EError, TransportError, UnsafeOperation
from .jtag import bscan as bscan_mod
from .jtag import tap as tap_mod
from .report import emit
from .session import DebugSession
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
    #: The active debug session, when running inside the `debug` REPL. While
    #: set, `open_transport` yields the held connection instead of opening a
    #: fresh one -- which is the whole point of a session on a board where
    #: every connect pulses reset into the SBC/PMIC (test-plan defect D-2).
    session: DebugSession | None = None


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
    # Inside a debug session the connection already exists and is shared:
    # yield it and do not close it. Commands that genuinely cannot share (raw
    # JTAG needs the commander backend) are refused rather than silently
    # opening a second connection under the held one.
    if ctx.session is not None:
        if needs_raw_jtag:
            raise UnsafeOperation(
                "raw JTAG needs the commander backend, and the debug session holds "
                "the probe through the dll backend. Quit the session first."
            )
        yield ctx.session.transport, (ctx.session.dap if connect_dap else None)
        return

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


def _prepare_dap(
    dap: Dap, *, ctx: Ctx | None = None, system_power: bool = False, attempts: int = 4
) -> list[str]:
    """Power up the debug domain and calibrate the AP-read model.

    The power-up is retried, because on this target it succeeds only in windows:
    the DAP can refuse for several seconds and then come up. A single attempt
    made every DAP command look permanently broken during a period when it was
    merely busy. This does not rescue a genuinely dead TAP -- if the JTAGC is
    held in reset the retries all fail and the error says so.

    Inside a debug session this runs once: the power-up bits and the pipeline
    calibration hold for the life of the connection, so repeating them per
    command would only add traffic and repeat the notes.
    """
    session = ctx.session if ctx is not None else None
    if session is not None and session.prepared:
        return []
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
    if session is not None:
        session.prepared = True
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
    """CoreSight/JTAG debugger for NXP S32E27x over a SEGGER J-Link.

    Three layers of capability: bring-up and survey (doctor, gates, apmap,
    romdump, mem, report), run control for the Cortex-R52 and Cortex-M33
    cores (core, m33, bp, wp -- halt, step, resume, breakpoints, watchpoints,
    reset-and-halt), and `debug`, a persistent session that holds one probe
    connection for a whole halt/step/resume workflow and restores the target
    when it closes. `load` writes a build's ELF straight into RTU0 RAM and
    parks rtu0.0 halted at its entry -- the fast build/debug loop. `flash`
    programs, erases, verifies and reads the external QSPI NOR through the
    s32multitool-derived SMU-M33 applet route.

    First command on a freshly power-cycled board: `gates apply` -- the debug
    clocks are off by default (RM 135.11) and the enables are lost on power
    loss. Everything else fails politely until it has run.

    Safety policy baked in: boundary scan is SAMPLE/PRELOAD only, the SDA-AP
    authentication path is never written (the reset-control registers are the
    sole, allowlisted exception), AXI-AP traffic is refused while functional
    reset is asserted, and switching to SWD needs an explicit opt-in because
    returning to JTAG requires a power-on reset.
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

        notes = _prepare_dap(dap, ctx=ctx, system_power=system_power)
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
        _prepare_dap(dap, ctx=ctx)
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
        _prepare_dap(dap, ctx=ctx)
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
            _prepare_dap(dap, ctx=ctx)
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


def _parse_address(text: str, *, what: str = "address") -> int:
    """Parse an ADDRESS argument: 0x-prefixed hex only.

    A bare number is rejected rather than guessed at. `32169874` could be hex
    or decimal, and both guesses have burned users: base-0 silently read it as
    decimal and armed a comparator at the wrong address, while an unprefixed
    hex string such as `321D2EE6` raised a ValueError that no handler caught
    and took the whole debug session down with it (reconnecting pulses reset
    into the SBC/PMIC -- test-plan defect D-2).
    """
    s = text.strip()
    if s[:2].lower() == "0x":
        try:
            return int(s, 16)
        except ValueError:
            pass
    raise click.ClickException(
        f"{what} {text!r} must be 0x-prefixed hex, e.g. 0x321D2EE6. A bare "
        "number is rejected rather than guessed at: 32169874 could be hex or "
        "decimal."
    )


def _parse_value(text: str, *, what: str = "value") -> int:
    """Parse a data VALUE argument: decimal, or 0x/0o/0b-prefixed (int base 0)."""
    try:
        return int(text.strip(), 0)
    except ValueError:
        raise click.ClickException(
            f"{what} {text!r} is not a number. Use decimal (57005) or 0x-prefixed "
            "hex (0xDEAD)."
        ) from None


def _parse_index(text: str, *, what: str = "comparator index") -> int:
    """Parse a comparator-index argument: bare decimal."""
    try:
        return int(text.strip(), 10)
    except ValueError:
        raise click.ClickException(
            f"{what} {text!r} is not a decimal index (0, 1, ...)."
        ) from None


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
        for note in _prepare_dap(dap, ctx=ctx):
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
    start = _parse_address(addr)
    width = int(size)
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        for note in _prepare_dap(dap, ctx=ctx):
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
    start = _parse_address(addr)
    if start % 4 or nbytes % 4:
        raise click.ClickException("dump address and length must both be 4-byte aligned")
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        for note in _prepare_dap(dap, ctx=ctx):
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
    target = _parse_address(addr)
    data = _parse_value(value)
    width = int(size)
    if not yes:
        click.confirm(
            f"write 0x{data:0{width // 4}X} to 0x{target:08X} via {memaccess.ap_label(ap_base)}?",
            abort=True,
        )
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        for note in _prepare_dap(dap, ctx=ctx):
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
    session = ctx.session
    if session is not None and session.cores is not None:
        # Identity is stable for the life of the connection; run state is not.
        # Serving the cache with stale state made `status` report a core as
        # running that the previous session command had halted.
        for c in session.cores:
            c.state = runcontrol.read_state(dap, c.debug_base)
            # A core this session already unlocked cannot legitimately read
            # OS-locked or in-reset again except through a core-local cold
            # reset (the application cycling its RGM reset -- guide §3.6:
            # comparators wiped, OS lock back). The session's records about it
            # describe a machine that no longer exists; drop them so the next
            # touch re-unlocks and re-sanitises (§5.7).
            if (
                c.name in session.sanitized
                and c.state.edprsr is not None
                and c.state.edprsr & (r52debug.EDPRSR_R | r52debug.EDPRSR_OSLK)
            ):
                session.note_core_reset(c.name)
                console.print(
                    f"[yellow]![/yellow] {c.name} was reset since this session "
                    "last touched it (identity cache kept; frame addresses are "
                    "stable). Its session breakpoint/halt records were dropped; "
                    "it will be re-unlocked and re-sanitised on next touch."
                )
            if (
                session.sync
                and c.state.halted is True
                and c.name not in session.halted_r52
            ):
                # A hardware-initiated stop (a breakpoint hit propagated by
                # the armed group) with nobody watching: take ownership so
                # restore-on-close resumes everything sync stopped.
                session.note_halted(c.name)
                console.print(
                    f"[dim]sync:[/dim] {c.name} halted by the cross-trigger group"
                )
        return session.cores

    for note in _prepare_dap(dap, ctx=ctx):
        console.print(f"[dim]note:[/dim] {note}")
    cores = runcontrol.discover(dap)
    if not cores:
        raise TransportError(
            "no Cortex-R52 debug frames answered. The debug clocks are off by default "
            "(RM 135.11) -- run `s32edbg gates apply` first. If the gate is already "
            "open, `s32edbg romdump --source apb` will show whether the APBIC2 tree "
            "enumerates at all."
        )
    if session is not None:
        session.cores = cores
    return cores


def _focus(ctx: Ctx) -> str | None:
    """The session's default core, when a command names none."""
    return ctx.session.focus if ctx.session is not None else None


def _session_touch(ctx: Ctx, dap: Dap, c: runcontrol.Core) -> None:
    """First write-operation touch of an R52 core inside a session.

    Sanitises the comparators once per core per session -- guide §5.7: every
    DBGB/DBGW register field is UNKNOWN after cold reset, *including the enable
    bit*, so the session starts each core it drives from a known state.
    `--keep-breakpoints` skips this for workflows that deliberately carry
    comparators from one session to the next.
    """
    session = ctx.session
    if session is None or session.keep_breakpoints or c.name in session.sanitized:
        return
    try:
        armed = runcontrol.sanitize_comparators(dap, c)
    except S32EError as exc:
        console.print(f"[yellow]![/yellow] {c.name}: comparator sanitation failed: {exc}")
        return
    session.sanitized.add(c.name)
    if armed:
        console.print(
            f"[dim]{c.name}:[/dim] disabled {armed} pre-existing comparator(s) at "
            "session start (guide §5.7; `debug --keep-breakpoints` keeps them)"
        )


def _m33_session_touch(ctx: Ctx, dap: Dap, c) -> None:
    """M33 counterpart of `_session_touch` -- FPB and DWT slots."""
    session = ctx.session
    key = f"m33:{c.name}"
    if session is None or session.keep_breakpoints or key in session.sanitized:
        return
    try:
        armed = m33_mod.sanitize_comparators(dap, c)
    except S32EError as exc:
        console.print(f"[yellow]![/yellow] {c.name}: comparator sanitation failed: {exc}")
        return
    session.sanitized.add(key)
    if armed:
        console.print(
            f"[dim]{c.name}:[/dim] disabled {armed} pre-existing comparator(s) at "
            "session start (`debug --keep-breakpoints` keeps them)"
        )


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
        if core_spec is None:
            core_spec = _focus(ctx)  # inside a session, `status` means the focused core
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
    core_spec: str | None,
    all_cores: bool,
    cores: list[runcontrol.Core],
    *,
    default: str | None = None,
) -> list[runcontrol.Core]:
    if all_cores:
        return cores
    if core_spec is None:
        core_spec = default  # the session's focused core, when one is set
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
        for c in _select_cores(core_spec, all_cores, cores, default=_focus(ctx)):
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
        chosen = _select_cores(core_spec, all_cores, cores, default=_focus(ctx))
        console.print(f"[yellow]![/yellow] {HALT_WARNING}")

        payload: list[dict[str, object]] = []
        for c in chosen:
            for note in runcontrol.preflight(c, write=True):
                console.print(f"[yellow]![/yellow] {c.name}: {note}")
            for note in runcontrol.unlock(dap, c):
                console.print(f"[dim]{c.name}:[/dim] {note}")
            _session_touch(ctx, dap, c)

            state = runcontrol.halt(dap, c)
            if ctx.session is not None:
                ctx.session.note_halted(c.name)
            console.print(
                f"[green]{c.name} halted[/green] -- "
                f"{r52debug.edscr_status_name(state.edscr or 0)} "
                f"(EDSCR 0x{state.edscr or 0:08X}, EDPRSR 0x{state.edprsr or 0:08X})"
            )
            payload.append({"core": c.name, **state.as_dict()})

        session = ctx.session
        if session is not None and session.sync:
            # The armed group propagates the halt in hardware; observe the
            # partners into Debug state and take session ownership so the
            # restore-on-close resumes everything sync stopped. Deliberately
            # NOT acknowledged here: the requests are level-held by the armed
            # group and only the group resume's teardown can lower them.
            others = [
                c
                for c in cores
                if c not in chosen and not runcontrol.identity_problems(c)
            ]
            stopped: list[str] = []
            last: dict[str, runcontrol.CoreState] = {}
            pending = {c.name: c for c in others}
            deadline = time.monotonic() + 1.0
            while pending and time.monotonic() < deadline:
                for name, c in list(pending.items()):
                    st = runcontrol.read_state(dap, c.debug_base)
                    last[name] = st
                    if st.halted is True:
                        session.note_halted(name)
                        stopped.append(name)
                        del pending[name]
                if pending:
                    time.sleep(0.02)
            if stopped:
                console.print(
                    f"[dim]sync: {', '.join(sorted(stopped))} stopped with the group[/dim]"
                )
            for name in pending:
                if last.get(name) is not None and last[name].halted is False:
                    console.print(
                        f"[yellow]![/yellow] sync: {name} did not stop with the group"
                    )
        if ctx.json_out:
            _emit_json(ctx.json_out, {"halt": payload})


@core.command("resume")
@_core_option
@click.option("--all", "all_cores", is_flag=True, help="Resume every core.")
@click.option("--sync", is_flag=True,
              help="Restart every selected halted core on a single CTM pulse "
                   "(guide §5.9), instead of one at a time.")
@click.pass_obj
def core_resume(ctx: Ctx, core_spec: str | None, all_cores: bool, sync: bool) -> None:
    """Let a halted core run again.

    Acknowledges the debug request -- and confirms the trigger deasserted --
    before restarting, then confirms the restart through EDPRSR.SDR. Skipping
    the acknowledge leaves the request asserted at the trigger matrix, so the
    core re-enters Debug state immediately and the resume looks like it did
    nothing.

    `--sync` is the guide's §5.9 synchronized restart: each participating
    core's restart channel is gated onto the cross-trigger matrix and one pulse
    restarts them all together, then the gates are closed again.
    """
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        chosen = _select_cores(core_spec, all_cores, cores, default=_focus(ctx))
        payload: list[dict[str, object]] = []

        def report(name: str, state: runcontrol.CoreState) -> None:
            if ctx.session is not None and not state.rehalted:
                ctx.session.note_resumed(name)
            if state.rehalted:
                c = runcontrol.resolve(name, cores)
                where = runcontrol.halt_location(dap, c)
                console.print(
                    f"[yellow]{name} ran and immediately re-halted[/yellow] -- "
                    f"{r52debug.edscr_status_name(state.edscr or 0)}"
                    + (f" at {where['address']}" if where.get("address") else "")
                )
                console.print(
                    "[dim]That is a working breakpoint, not a failed resume. To get "
                    "past it, clear the comparator or step over it first.[/dim]"
                )
            else:
                console.print(
                    f"[green]{name} running[/green] -- "
                    f"{r52debug.edscr_status_name(state.edscr or 0)}"
                )
            payload.append({"core": name, **state.as_dict()})

        session = ctx.session
        session_sync = session is not None and session.sync
        if session_sync and core_spec is None and not all_cores:
            # sync mode: a bare `continue` releases the whole stopped group,
            # not just the focused core -- that is the mode's contract.
            halted_usable = [
                c
                for c in cores
                if not runcontrol.identity_problems(c)
                and runcontrol.read_state(dap, c.debug_base).halted is True
            ]
            if halted_usable:
                chosen = halted_usable

        if sync or session_sync:
            for c in chosen:
                runcontrol.preflight(c, write=True)
            # Under an armed group the halted members' DBGTRIGGER levels
            # would swallow the restart; unarm_cross_halt is the teardown
            # (mask CTIINEN ch2, restart, re-arm).
            results = runcontrol.resume_group(
                dap, chosen, unarm_cross_halt=session_sync
            )
            console.print(
                f"[dim]synchronized restart: one pulse released "
                f"{len(results)} core(s)[/dim]"
            )
            for name, state in results.items():
                report(name, state)
        else:
            for c in chosen:
                runcontrol.preflight(c, write=True)
                report(c.name, runcontrol.resume(dap, c))
        if ctx.json_out:
            _emit_json(ctx.json_out, {"resume": payload})


@core.command("step")
@_core_option
@click.option("--over/--into", default=False, show_default="--into",
              help="--over runs a call to completion; --into steps one instruction.")
@click.option("--count", type=click.IntRange(min=1), default=1, show_default=True)
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
        session = ctx.session
        if session is not None:
            # The session now owns this core's run state: it is halted at the
            # end of every step, and restore-on-close should resume it.
            session.note_halted(c.name)
        saved_sync: list[tuple[int, int]] = []
        if session is not None and session.sync:
            # A step is per-core by design; masking the armed group's inputs
            # keeps the stepped core's restart from being swallowed by its
            # halted partners' levels (and its own), and the partners stay
            # halted through it.
            usable = [
                x for x in (session.cores or []) if not runcontrol.identity_problems(x)
            ]
            saved_sync = runcontrol.suspend_cross_halt(dap, usable)
            console.print(
                "[dim]sync: cross-halt masked for the step; the other cores "
                "stay halted[/dim]"
            )
        try:
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
        finally:
            if saved_sync:
                runcontrol.restore_cross_halt(dap, saved_sync)
        if ctx.json_out:
            _emit_json(ctx.json_out, {"core": c.name, "pc": f"0x{after:08X}"})


@core.command("catch")
@_core_option
@click.option("--wait", is_flag=True,
              help="Wait for the catch to fire, then convert the hold into a "
                   "real acknowledged halt (arms first if needed).")
@click.option("--timeout", "timeout_s", type=float, default=30.0, show_default=True,
              help="How long --wait polls for the halt.")
@click.option("--disarm", is_flag=True, help="Drop this core's armed latch instead.")
@click.pass_obj
def core_catch(
    ctx: Ctx, core_spec: str | None, wait: bool, timeout_s: float, disarm: bool
) -> None:
    """Halt a core at its first instruction when something else releases it.

    Arms the core's MDM EDBGREQ latch -- a level debug request that lives in
    the MDM-AP and therefore survives the core's own cold reset. That is the
    property EDECR.RCE lacks here: the application asserts core-1's reset
    before releasing it (BrsHwEarlyInitPowerOn), which wipes EDECR (guide
    §3.6; §5.10 covers only debugger-driven releases). The latch halting a
    core on the application's own release is measured silicon behaviour --
    Finding 7, there as the bug, here as the feature.

    An EDBGREQ hold is NOT architecturally Debug state (registers refuse, and
    dropping the latch would resume the core), so `--wait` polls for the halt
    and CONVERTS it: unlock (the cold reset re-set the OS lock), a real
    acknowledged CTI halt, drop the latch, re-sanitise (the reset wiped this
    core's comparators). The typical wiped-flash session:

    \b
        load app.elf
        catch --core rtu0.1
        continue                     (core0 boots; its firmware releases core1)
        catch --core rtu0.1 --wait   (core1 halted at its first instruction)
    """
    if wait and disarm:
        raise click.ClickException("--wait and --disarm do not combine")
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        c = _select_cores(core_spec, False, cores, default=_focus(ctx))[0]
        runcontrol.preflight(c, write=True)
        rtu, index = runcontrol.core_rtu_index(c)
        session = ctx.session

        if disarm:
            gates_mod.clear_r52_edbgreq(dap, rtu, index)
            if session is not None:
                session.edbgreq_armed.discard(c.name)
            console.print(f"[green]dropped[/green] the catch latch for {c.name}")
            return

        if session is None and not wait:
            raise click.ClickException(
                "arming a catch without waiting needs a debug session: an armed "
                "latch left behind halts the core on the application's own "
                "release with nobody there to convert it, and a stale latch is "
                "exactly the Finding-7 failure. Open `s32edbg debug` and run "
                "`catch` there, or use --wait."
            )

        armed_now = session is None or c.name not in session.edbgreq_armed
        if armed_now:
            doomed = sorted(
                i for name, i in (session.bp_set | session.wp_set) if name == c.name
            ) if session is not None else []
            if doomed:
                console.print(
                    f"[yellow]![/yellow] {c.name} has session comparator(s) "
                    f"{doomed} armed -- the application's cold reset of this "
                    "core will wipe them before the catch fires."
                )
            gates_mod.arm_r52_edbgreq(dap, rtu, index)
            if session is not None:
                session.edbgreq_armed.add(c.name)
            console.print(
                f"[green]catch armed[/green] on {c.name} (MDM EDBGREQ latched)."
            )
            if not wait:
                console.print(
                    f"[dim]now `continue` the boot core; when the application "
                    f"releases {c.name} it halts at its first instruction -- "
                    f"then `catch --core {c.name} --wait` converts the hold.[/dim]"
                )

        if not wait:
            return

        fired = False
        try:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                state = runcontrol.read_state(dap, c.debug_base)
                if (
                    state.edprsr is not None
                    and state.edprsr & r52debug.EDPRSR_HALTED
                    and not state.edprsr & r52debug.EDPRSR_R
                ):
                    fired = True
                    break
                time.sleep(0.02)
        finally:
            if not fired and session is None:
                # Standalone invocation: nothing owns the latch after this
                # process exits, so it must not outlive the wait.
                gates_mod.clear_r52_edbgreq(dap, rtu, index)
        if not fired:
            hint = (
                "the latch was dropped (no session to own it)"
                if session is None
                else "the latch stays armed -- the session close drops it, or "
                     "`catch --disarm`"
            )
            raise click.ClickException(
                f"{c.name} did not halt within {timeout_s:.0f}s; {hint}"
            )

        # Conversion. The core just came through its cold reset: the session's
        # records for it describe wiped state, the OS lock is back, and the
        # hold is not Debug state until the real halt lands.
        if session is not None:
            session.note_core_reset(c.name)
        for note in runcontrol.unlock(dap, c):
            console.print(f"[dim]{c.name}:[/dim] {note}")
        state = runcontrol.pin_down_edbgreq_hold(dap, c)
        if session is not None:
            session.edbgreq_armed.discard(c.name)
            session.note_halted(c.name)
        _session_touch(ctx, dap, c)
        try:
            pc = runcontrol.read_pc(dap, c)
            where = f"  PC 0x{pc:08X}"
        except S32EError as exc:
            pc = None
            where = f"  (PC unreadable: {exc})"
        console.print(
            f"[green]{c.name} caught[/green] at its first instruction -- "
            f"{r52debug.edscr_status_name(state.edscr or 0)}{where}"
        )
        if ctx.json_out:
            _emit_json(
                ctx.json_out,
                {
                    "core": c.name,
                    "pc": f"0x{pc:08X}" if pc is not None else None,
                    **state.as_dict(),
                },
            )


@core.command("reset-halt")
@_core_option
@click.pass_obj
def core_reset_halt(ctx: Ctx, core_spec: str | None) -> None:
    """Functional-reset the system and halt a core at its first instruction.

    The guide's §5.10.2 hold flow, the one that works on this silicon: the
    core's SDARSTCTRL release bit is cleared so it stays in reset while the
    system functional-resets and boots normally, EDECR.RCE is armed on the
    held core, and releasing it fires the Reset Catch before the first
    instruction -- DLR sits at the reset vector, nothing has been fetched.

    THE RESET IS SYSTEM-WIDE: every core reboots; only the chosen one is
    caught, and comparators on the other cores are wiped by the reset. (The
    guide's §5.10.3 SDARSTMASK protection measurably does not work on this
    part -- see the test plan's finding 1.)
    """
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        c = _select_cores(core_spec, False, cores, default=_focus(ctx))[0]
        runcontrol.preflight(c, write=True)
        console.print(
            "[yellow]![/yellow] this asserts a SYSTEM-WIDE functional reset: every "
            f"core reboots, and only {c.name} is caught at its reset vector. The "
            "debug connection survives (RM Table 301)."
        )
        state = runcontrol.reset_halt(dap, c)
        if ctx.session is not None:
            ctx.session.note_halted(c.name)
        _session_touch(ctx, dap, c)
        try:
            pc = runcontrol.read_pc(dap, c)
            where = f"  PC 0x{pc:08X}"
        except S32EError as exc:
            pc = None
            where = f"  (PC unreadable: {exc})"
        console.print(
            f"[green]{c.name} caught[/green] -- "
            f"{r52debug.edscr_status_name(state.edscr or 0)}{where}"
        )
        if ctx.json_out:
            _emit_json(
                ctx.json_out,
                {
                    "core": c.name,
                    "pc": f"0x{pc:08X}" if pc is not None else None,
                    **state.as_dict(),
                },
            )


@core.command("xtrigger")
@click.argument("state", type=click.Choice(["on", "off"]))
@click.pass_obj
def core_xtrigger(ctx: Ctx, state: str) -> None:
    """Halt every core together, or stop doing so.

    With cross-trigger on, one core entering Debug state pulls the others in
    through the CTI matrix, so you get a coherent multicore snapshot. Without it
    the other cores keep running and can corrupt the state you stopped to look
    at -- the breakpoints guide covers this in section 5.9.

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
        c = _select_cores(core_spec, False, cores, default=_focus(ctx))[0]
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
        c = _select_cores(core_spec, False, cores, default=_focus(ctx))[0]
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
    """Read or write one register of a halted core: `core reg r3`, `core reg pc 0x321D2EE6`.

    `pc` (an alias of `dlr`) and `dspsr` are the Debug-state PC and saved PSR,
    reached through MRC/MCR p15 (Cortex-R52 TRM §4.2.31). Guide §5.5: the core
    restarts from DLR, so writing `pc` is how execution is redirected before a
    resume; which instruction set it resumes in comes from DSPSR.T.
    """
    key = name.strip().lower()
    special = {
        "pc": (runcontrol.read_pc, runcontrol.write_pc, "pc (DLR)"),
        "dlr": (runcontrol.read_pc, runcontrol.write_pc, "pc (DLR)"),
        "dspsr": (runcontrol.read_dspsr, runcontrol.write_dspsr, "dspsr"),
    }
    is_gp = key.startswith("r") and key[1:].isdigit() and 0 <= int(key[1:] or "99") <= 14
    if not is_gp and key not in special:
        raise click.ClickException(
            f"{name!r} is not a supported register. Use r0-r14, pc (= dlr), or dspsr."
        )
    data = _parse_value(value, what=f"value for {key}") if value is not None else None

    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        c = _select_cores(core_spec, False, cores, default=_focus(ctx))[0]
        runcontrol.preflight(c, write=True)

        if not runcontrol.verify_register_path(dap, c):
            raise TransportError(
                "the register-transfer round trip failed; refusing to report or write "
                "a register value."
            )
        if is_gp:
            n = int(key[1:])
            if data is not None:
                runcontrol.write_reg(dap, c, n, data)
            got = runcontrol.read_reg(dap, c, n)
            label = f"r{n}"
        else:
            reader, writer, label = special[key]
            if data is not None:
                writer(dap, c, data)
            got = reader(dap, c)
        console.print(f"{c.name}  {label} = 0x{got:08X}")
        if ctx.json_out:
            _emit_json(ctx.json_out, {"core": c.name, "register": label, "value": f"0x{got:08X}"})


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
        for note in _prepare_dap(dap, ctx=ctx):
            console.print(f"[dim]note:[/dim] {note}")
        cores = m33_mod.discover(dap)
        if ctx.session is not None:
            # The session's restore-on-close needs live core objects.
            ctx.session.m33_cores = cores
            if core_spec is None:
                core_spec = ctx.session.m33_focus
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
        if write:
            _m33_session_touch(ctx, dap, c)
        yield dap, c


@m33.command("list")
@click.pass_obj
def m33_list(ctx: Ctx) -> None:
    """Identify all three M33 subsystems. Read-only."""
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        for note in _prepare_dap(dap, ctx=ctx):
            console.print(f"[dim]note:[/dim] {note}")
        cores = m33_mod.discover(dap)
        if ctx.session is not None:
            ctx.session.m33_cores = cores
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
        if ctx.session is not None:
            ctx.session.note_m33_halted(c.name)
        cause = m33_mod.halt_cause(dap, c)
        pc = m33_mod.read_register(dap, c, "pc")
        console.print(
            f"[green]{c.name} halted[/green]  PC 0x{pc:08X}  DHCSR 0x{dhcsr:08X}"
        )
        causes = cause.get("causes")
        if causes:
            console.print(f"[dim]cause: {', '.join(causes)} (DFSR {cause['raw']})[/dim]")
        if ctx.json_out:
            _emit_json(
                ctx.json_out, {"core": c.name, "pc": f"0x{pc:08X}", "cause": cause}
            )


@m33.command("resume")
@_m33_option
@click.pass_obj
def m33_resume(ctx: Ctx, core_spec: str | None) -> None:
    """Let a halted M33 core run again."""
    with _one_m33(ctx, core_spec) as (dap, c):
        dhcsr, rehalted = m33_mod.resume(dap, c)
        if ctx.session is not None and not rehalted:
            ctx.session.note_m33_resumed(c.name)
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
        if ctx.session is not None:
            ctx.session.note_m33_halted(c.name)
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
    # Parse before the probe opens: a typo must cost nothing.
    target: int | None = None
    slot: int | None = None
    if action == "set":
        if address is None:
            raise click.ClickException("`bp set` needs an address")
        target = _parse_address(address)
    elif action == "clear":
        if address is None:
            raise click.ClickException("`bp clear` needs a comparator index")
        slot = _parse_index(address)
    with _one_m33(ctx, core_spec, write=action != "list") as (dap, c):
        if action == "list":
            entries = m33_mod.list_breakpoints(dap, c)
            emit.print_comparators(f"{c.name} breakpoints", entries, console)
        elif action == "set":
            assert target is not None
            entry = m33_mod.set_breakpoint(dap, c, target)
            if ctx.session is not None:
                ctx.session.note_m33_bp(c.name, int(entry["index"]))
            console.print(
                f"[green]breakpoint {entry['index']}[/green] at {entry['address']} on {c.name}"
            )
        else:
            assert slot is not None
            m33_mod.clear_breakpoint(dap, c, slot)
            if ctx.session is not None:
                ctx.session.note_m33_bp_cleared(c.name, slot)
            console.print(f"cleared breakpoint {slot} on {c.name}")


@m33.command("wp")
@click.argument("action", type=click.Choice(["list", "set", "clear"]))
@click.argument("address", required=False)
@click.option("--size", type=click.Choice(["1", "2", "4"]), default="4", show_default=True,
              help="Watched object size; the address must be aligned to it.")
@click.option("--on", "access", type=click.Choice(["load", "store", "both"]),
              default="both", show_default=True)
@click.option("--index", type=int, default=None,
              help="Use this DWT comparator; default is the first free one.")
@_m33_option
@click.pass_obj
def m33_wp(
    ctx: Ctx,
    action: str,
    address: str | None,
    size: str,
    access: str,
    index: int | None,
    core_spec: str | None,
) -> None:
    """Manage M33 data watchpoints: `m33 wp list`, `wp set <addr>`, `wp clear <n>`.

    These come from the DWT comparators (four on these cores, read from
    DWT_CTRL rather than assumed), armed as halting debug events per the guide
    §6.7. DWT watchpoints are asynchronous: the core halts *near* the access,
    not at it, and DFSR reports DWTTRAP.
    """
    # Parse before the probe opens: a typo must cost nothing.
    target: int | None = None
    slot: int | None = None
    if action == "set":
        if address is None:
            raise click.ClickException("`wp set` needs an address")
        target = _parse_address(address)
    elif action == "clear":
        if address is None:
            raise click.ClickException("`wp clear` needs a comparator index")
        slot = _parse_index(address)
    with _one_m33(ctx, core_spec, write=action != "list") as (dap, c):
        if action == "list":
            entries = m33_mod.list_watchpoints(dap, c)
            emit.print_comparators(f"{c.name} watchpoints", entries, console)
            if ctx.json_out:
                _emit_json(ctx.json_out, {"core": c.name, "watchpoints": entries})
        elif action == "set":
            assert target is not None
            try:
                entry = m33_mod.set_watchpoint(
                    dap, c, target, size=int(size), on=access, index=index
                )
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
            if ctx.session is not None:
                ctx.session.note_m33_wp(c.name, int(entry["index"]))
            console.print(
                f"[green]watchpoint {entry['index']}[/green] at {entry['address']} on "
                f"{c.name}, {entry['bytes']} byte(s), on {entry['on']}"
            )
            if ctx.json_out:
                _emit_json(ctx.json_out, {"core": c.name, "watchpoint": entry})
        else:
            assert slot is not None
            m33_mod.clear_watchpoint(dap, c, slot)
            if ctx.session is not None:
                ctx.session.note_m33_wp_cleared(c.name, slot)
            console.print(f"cleared watchpoint {slot} on {c.name}")


@m33.command("reset-halt")
@_m33_option
@click.pass_obj
def m33_reset_halt(ctx: Ctx, core_spec: str | None) -> None:
    """Warm-reset an M33 and halt it at its reset handler's first instruction.

    The architected flow (guide §6.8): C_DEBUGEN + DEMCR.VC_CORERESET survive
    the warm reset, AIRCR.SYSRESETREQ requests it, and the core halts before
    the first handler instruction with DFSR.VCATCH set.

    KNOWN BLOCKED ON THIS SILICON (measured 2026-08-02): the AIRCR write is
    accepted but SYSRESETREQ takes no effect -- nothing resets and the core
    keeps running. The command fails cleanly and disarms the vector catch.
    The SoC gates M33 SYSRESETREQ outside the guide's sources; the SDA-AP
    RSTWAIT* CPUWAIT hold is the documented candidate for a future flow.
    """
    with _one_m33(ctx, core_spec) as (dap, c):
        console.print(
            "[yellow]![/yellow] SYSRESETREQ is a warm reset request; how far it "
            "reaches on this SoC (this core or wider) is not settled -- treat the "
            "system as rebooted."
        )
        dhcsr, pc = m33_mod.reset_halt(dap, c)
        if ctx.session is not None:
            ctx.session.note_m33_halted(c.name)
        cause = m33_mod.halt_cause(dap, c)
        console.print(
            f"[green]{c.name} caught[/green] at reset  PC 0x{pc:08X}  DHCSR 0x{dhcsr:08X}"
        )
        causes = cause.get("causes")
        if causes:
            console.print(f"[dim]cause: {', '.join(causes)} (DFSR {cause['raw']})[/dim]")
        if ctx.json_out:
            _emit_json(
                ctx.json_out, {"core": c.name, "pc": f"0x{pc:08X}", "cause": cause}
            )


# ---------------------------------------------------------------------------
# bp / wp -- hardware comparators
# ---------------------------------------------------------------------------


def _attach_core(ctx: Ctx, dap: Dap, c: runcontrol.Core, *, write: bool = True) -> None:
    """Preflight one already-selected core, unlocking and sanitising for writes.

    Clears the OS lock when the command is going to write. Most cores come up
    locked after a power cycle, and while the lock is set every core-domain
    register access faults -- so without this a `bp set` on a freshly booted
    board failed with "a comparator write faulted", which describes the symptom
    and not the cause. Shared between `_one_core` (every bp/wp command) and the
    `load` flow, which reaches the same state through its own bring-up and must
    not re-open the transport to get the unlock and §5.7 sanitation.
    """
    for note in runcontrol.preflight(c, write=write):
        console.print(f"[yellow]![/yellow] {c.name}: {note}")
    session = ctx.session
    if (
        write
        and session is not None
        and c.name in session.edbgreq_armed
        and c.state.edprsr is not None
        and c.state.edprsr & r52debug.EDPRSR_HALTED
        and not c.state.edprsr & r52debug.EDPRSR_R
    ):
        # A fired, unconverted `catch`: the EDBGREQ hold is not Debug state,
        # registers refuse, and a resume against it would be swallowed --
        # convert exactly as `catch --wait` would before the write proceeds.
        console.print(
            f"[dim]{c.name}:[/dim] a fired catch is holding this core; "
            "converting it into a real acknowledged halt first"
        )
        session.note_core_reset(c.name)
        for note in runcontrol.unlock(dap, c):
            console.print(f"[dim]{c.name}:[/dim] {note}")
        runcontrol.pin_down_edbgreq_hold(dap, c)
        session.edbgreq_armed.discard(c.name)
        session.note_halted(c.name)
        c.state = runcontrol.read_state(dap, c.debug_base)
    if write and c.state.unreachable_reason:
        for note in runcontrol.unlock(dap, c):
            console.print(f"[dim]{c.name}:[/dim] {note}")
        c.state = runcontrol.read_state(dap, c.debug_base)
    if write:
        _session_touch(ctx, dap, c)


@contextmanager
def _one_core(ctx: Ctx, core_spec: str | None, *, write: bool = True):
    """Open a session, discover, select one core, preflight it via _attach_core."""
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        cores = _discover_cores(ctx, dap)
        c = _select_cores(core_spec, False, cores, default=_focus(ctx))[0]
        _attach_core(ctx, dap, c, write=write)
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
        _note_unreadable_comparators(c)
        entries = runcontrol.list_breakpoints(dap, c)
        emit.print_comparators(f"{c.name} breakpoints", entries, console)
        if ctx.json_out:
            _emit_json(ctx.json_out, {"core": c.name, "breakpoints": entries})


def _note_unreadable_comparators(c: runcontrol.Core) -> None:
    """Listing is read-only, so it never unlocks (that changes target state);
    on a locked or held core the per-slot "did not read" rows below are
    expected -- this note is what turns them into a stated reason."""
    reason = c.state.unreachable_reason
    if not reason:
        return
    hint = (
        " Run `unlock` (or any write command, e.g. `bp set`) first."
        if "lock" in reason
        else ""
    )
    console.print(
        f"[yellow]![/yellow] {c.name}: comparator registers will not read -- "
        f"{reason}.{hint}"
    )


def _warn_comparator_arm_state(c: runcontrol.Core) -> None:
    """Guide §5.7 (and §5.8 for watchpoints): program comparators while the
    core is halted whenever possible -- the update needs a context
    synchronization event to be guaranteed visible. Arming against a running
    core has fired on this silicon, so this is a warning, not a refusal."""
    if c.state.halted is not True:
        console.print(
            f"[yellow]![/yellow] {c.name} is not halted. The guide (§5.7/§5.8) "
            "recommends programming comparators while the core is halted -- "
            "the change needs a context synchronization event to be guaranteed "
            "to take effect. It is armed; halt-first is the supported order."
        )


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
    target = _parse_address(address)  # before the probe opens: a typo must cost nothing
    with _one_core(ctx, core_spec) as (dap, c):
        entry = runcontrol.set_breakpoint(
            dap, c, target, index=index, thumb=not arm_state
        )
        _warn_comparator_arm_state(c)
        if ctx.session is not None:
            ctx.session.note_bp(c.name, int(entry["index"]))
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
        if ctx.session is not None:
            ctx.session.note_bp_cleared(c.name, None if all_slots else int(index))  # type: ignore[arg-type]
        console.print(f"cleared {'all ' if all_slots else ''}breakpoint(s) on {c.name}")


@main.group()
def wp() -> None:
    """Hardware data watchpoints -- halt when an address is loaded or stored.

    Separate comparators from breakpoints: eight per R52 core, watching data
    accesses rather than instruction fetches. A hit halts on the exact access
    (R52 watchpoints are synchronous) and `core status` reports the faulting
    data address from EDWAR -- the answer to "who is writing this variable".
    The watch covers 1, 2, 4 or 8 bytes inside one aligned 8-byte window.
    """


@wp.command("list")
@_core_option
@click.pass_obj
def wp_list(ctx: Ctx, core_spec: str | None) -> None:
    with _one_core(ctx, core_spec, write=False) as (dap, c):
        _note_unreadable_comparators(c)
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
    target = _parse_address(address)  # before the probe opens: a typo must cost nothing
    with _one_core(ctx, core_spec) as (dap, c):
        try:
            entry = runcontrol.set_watchpoint(
                dap, c, target, size=int(size), on=access, index=index
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        _warn_comparator_arm_state(c)
        if ctx.session is not None:
            ctx.session.note_wp(c.name, int(entry["index"]))
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
        if ctx.session is not None:
            ctx.session.note_wp_cleared(c.name, None if all_slots else int(index))  # type: ignore[arg-type]
        console.print(f"cleared {'all ' if all_slots else ''}watchpoint(s) on {c.name}")


# ---------------------------------------------------------------------------
# debug -- one session, many commands
# ---------------------------------------------------------------------------

#: Prompt aliases: the run-control commands people type all day, without their
#: group prefix. `continue` is the name every other debugger uses for resume.
SESSION_ALIASES: dict[str, tuple[str, ...]] = {
    "halt": ("core", "halt"),
    "resume": ("core", "resume"),
    "continue": ("core", "resume"),
    "step": ("core", "step"),
    "regs": ("core", "regs"),
    "reg": ("core", "reg"),
    "status": ("core", "status"),
    "unlock": ("core", "unlock"),
    "sample": ("core", "sample"),
    "reset-halt": ("core", "reset-halt"),
    "catch": ("core", "catch"),
}

#: Commands that cannot run inside a session, and why the refusal is safety
#: rather than convenience.
SESSION_BLOCKED: dict[str, str] = {
    "doctor": "it opens its own commander transport under the held connection",
    "attach": "it opens and closes many probe sessions while retrying",
    "release": "it opens and closes probe sessions while retrying",
    "iface": "a protocol switch would tear down the held connection",
    "bscan": (
        "the RM Table 984 preamble changes the JTAG chain (IR 8 -> 16) and takes "
        "the DAP away until a power cycle -- test-plan ordering rule 3"
    ),
    "debug": "a session is already open",
}


def _print_session_help() -> None:
    console.print(
        "Session commands: any normal s32edbg command works here without its\n"
        "global options -- `bp set 0x321D2EE6`, `wp list`, `mem read 0x00410FF0`,\n"
        "`m33 halt --core smu`, `gates status`, ...\n"
        "\n"
        "Addresses are 0x-prefixed hex (`bp set 0x321D2EE6`); a bare number\n"
        "like `32169874` is rejected rather than guessed at.\n"
        "\n"
        "Aliases for the focused core: halt, resume/continue, step, regs,\n"
        "reg, status, unlock, sample, reset-halt, catch.\n"
        "\n"
        "`catch --core rtu0.1` arms a halt at that core's FIRST instruction\n"
        "for when the application releases it; `catch --core rtu0.1 --wait`\n"
        "converts the hold once it fires. `sync on` makes any halt stop all\n"
        "cores together and bare `continue` restart them together; `sync off`\n"
        "(or quitting) puts run-control back to per-core.\n"
        "\n"
        "`load <app.elf>` writes a build straight into RAM and parks rtu0.0\n"
        "halted at its entry (resets the whole SoC first) -- the fast\n"
        "build/debug loop. Then `bp set 0x...` and `continue`.\n"
        "\n"
        "`flash program <img> --offset 0x0|--area app`, `flash erase`,\n"
        "`flash verify`, `flash read`, `flash jedec` work the external QSPI\n"
        "NOR (each quiesces the SoC with a reset first; power-cycle to boot\n"
        "the flashed image).\n"
        "\n"
        "Session builtins:\n"
        "  use <core>   focus a core (rtu0.0, rtu0.1, ... or smu/ce0/ce1)\n"
        "  help         this text\n"
        "  quit         close the session (clears session breakpoints and\n"
        "               resumes session-halted cores unless --keep-on-exit)\n"
        "\n"
        "Not available in a session: doctor, attach, release, iface, bscan."
    )


def _session_use(ctx: Ctx, args: list[str]) -> None:
    session = ctx.session
    assert session is not None
    if not args:
        console.print(
            f"focus: {session.focus or '-'} (R52), {session.m33_focus or '-'} (M33)"
        )
        return
    spec = args[0]
    if spec.lower() in m33_mod.M33_APS:
        session.m33_focus = spec.lower()
        console.print(f"M33 focus: {session.m33_focus}")
        return
    if session.cores is None:
        # `use` right after an in-session `gates apply` on a power-cycled
        # board: discovery has not run yet -- run it now rather than telling
        # the operator to issue `core list` first.
        try:
            _discover_cores(ctx, session.dap)
        except S32EError as exc:
            console.print(f"[red]error:[/red] {exc}")
            return
    try:
        c = runcontrol.resolve(spec, session.cores or [])
    except ValueError as exc:
        console.print(f"[red]error:[/red] {exc}")
        return
    session.focus = c.name
    console.print(f"focus: {c.name}")


def _session_sync(ctx: Ctx, args: list[str]) -> None:
    """The `sync on|off|status` session builtin -- stop-the-world mode.

    Armed, the guide-§5.9 cross-halt group makes ANY halt (manual or a
    breakpoint hit) stop every usable core together, and a bare `continue`
    releases the whole group on one pulse -- the mode for an application
    whose cores are software-coupled (the measured pair-deadlock). Session
    close disarms it whatever happens.
    """
    session = ctx.session
    assert session is not None
    if not args or args[0] not in ("on", "off", "status"):
        console.print("usage: sync on|off|status")
        return
    if args[0] == "status":
        console.print(f"sync: {'on' if session.sync else 'off'}")
        return
    want = args[0] == "on"
    try:
        cores = _discover_cores(ctx, session.dap)
    except S32EError as exc:
        console.print(f"[red]error:[/red] {exc}")
        return
    usable = [c for c in cores if not runcontrol.identity_problems(c)]
    if want and not usable:
        console.print("[red]error:[/red] no usable cores to arm")
        return
    try:
        notes = runcontrol.set_cross_trigger(session.dap, usable, want)
    except S32EError as exc:
        console.print(f"[red]error:[/red] {exc}")
        return
    for note in notes:
        console.print(f"[dim]{note}[/dim]")
    session.sync = want
    if want:
        console.print(
            "[green]sync on[/green]: any halt -- manual or a breakpoint hit -- "
            "now stops all cores together; bare `continue` restarts the whole "
            "group; `quit` disarms."
        )
    else:
        console.print("[green]sync off[/green]")


def _session_tokens(line: str) -> list[str] | None:
    """Split a session line into argv tokens. None on a lexing error."""
    try:
        raw = shlex.split(line, posix=False)
    except ValueError as exc:
        console.print(f"[red]error:[/red] {exc}")
        return None
    # posix=False keeps Windows paths intact but leaves surrounding quotes on;
    # strip them so `--json "out file.json"` arrives as one clean token.
    return [
        t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t
        for t in raw
    ]


def _session_dispatch(ctx: Ctx, line: str) -> bool:
    """Run one session command line. Returns False when the session should end."""
    tokens = _session_tokens(line)
    if not tokens:
        return True
    name = tokens[0].lower()
    if name in ("quit", "exit", "q"):
        return False
    if name in ("help", "?"):
        _print_session_help()
        return True
    if name == "use":
        _session_use(ctx, tokens[1:])
        return True
    if name == "sync":
        _session_sync(ctx, tokens[1:])
        return True
    if name in SESSION_ALIASES:
        tokens = [*SESSION_ALIASES[name], *tokens[1:]]
        name = tokens[0]
    reason = SESSION_BLOCKED.get(name)
    if reason is not None:
        console.print(
            f"[red]refused:[/red] `{name}` cannot run inside a debug session: {reason}. "
            "Quit the session first."
        )
        return True
    command = main.commands.get(name)
    if command is None:
        console.print(
            f"[red]error:[/red] unknown command {name!r}. `help` lists what works here."
        )
        return True
    try:
        with command.make_context(name, tokens[1:], obj=ctx) as command_ctx:
            command.invoke(command_ctx)
    except click.exceptions.Exit:
        pass
    except click.ClickException as exc:
        console.print(f"[red]error:[/red] {exc.format_message()}")
    except click.Abort:
        console.print("[yellow]aborted[/yellow]")
    except UnsafeOperation as exc:
        console.print(f"[red]refused:[/red] {exc}")
    except S32EError as exc:
        console.print(f"[red]error:[/red] {exc}")
    except KeyboardInterrupt:
        console.print(
            "\n[yellow]interrupted[/yellow] -- the session is still open; `quit` leaves it"
        )
    except Exception as exc:  # defence in depth: no input may end the session
        console.print(f"[red]internal error:[/red] {type(exc).__name__}: {exc}")
        console.print(
            "[dim]This is a bug in s32edbg, not target state -- the session is "
            "still open. Please report it with the command that caused it.[/dim]"
        )
    return True


def _session_repl(ctx: Ctx) -> None:
    console.print("[dim]type commands (`help` lists them); `quit` closes the session[/dim]")
    while True:
        session = ctx.session
        assert session is not None
        focus = session.focus or session.m33_focus or "-"
        try:
            line = console.input(f"[bold cyan]s32edbg\\[{focus}]>[/bold cyan] ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not _session_dispatch(ctx, line):
            break


@main.command("debug")
@click.option("--core", "core_spec", default=None,
              help="Focus core: an R52 name/index/address, or smu/ce0/ce1.")
@click.option("--keep-on-exit", is_flag=True,
              help="Leave the target exactly as the session left it: breakpoints "
                   "stay armed, halted cores stay halted.")
@click.option("--keep-breakpoints", is_flag=True,
              help="Skip the comparator sanitation on first touch of each core "
                   "(guide §5.7 initialises them because their reset state is UNKNOWN).")
@click.option("-c", "--command", "batch", multiple=True,
              help="Run this session command and exit; repeatable, runs in order.")
@click.pass_obj
def debug_cmd(
    ctx: Ctx,
    core_spec: str | None,
    keep_on_exit: bool,
    keep_breakpoints: bool,
    batch: tuple[str, ...],
) -> None:
    """Open one debug session: breakpoints, halt, step, resume -- then close it.

    Every other s32edbg invocation opens and closes its own probe connection,
    and on this board every connect pulses reset through R439 into the SBC and
    PMIC -- test-plan defect D-2 -- while halts held *across* invocations
    deadlocked the application. A session opens the connection once, caches
    core discovery, keeps run-control state coherent between commands, and
    closes once.

    On close (quit, end of input, or Ctrl-C at the prompt) the session
    restores the target: the breakpoints and watchpoints it set are cleared
    and the cores it halted are resumed, unless --keep-on-exit.

    On a freshly power-cycled board the session opens with no cores
    discovered -- run `gates apply` inside it, then `use <core>`, and carry
    on; discovery reruns by itself.

    Non-interactive use: pipe lines into stdin, or give -c commands:

        s32edbg debug --core rtu0.1 -c "halt" -c "regs" -c "resume"
    """
    if ctx.transport_kind != "dll":
        raise UnsafeOperation(
            "a debug session needs the dll backend: the commander backend runs one "
            "JLink.exe process per operation, so it cannot hold a connection open."
        )
    if ctx.session is not None:
        raise click.ClickException("a debug session is already open.")

    with open_transport(ctx) as (t, dap):
        assert dap is not None
        session = DebugSession(
            transport=t, dap=dap, keep_breakpoints=keep_breakpoints
        )
        ctx.session = session
        try:
            try:
                cores = _discover_cores(ctx, dap)
                console.print(
                    f"[green]session open[/green] -- {len(cores)} R52 core(s): "
                    + " ".join(c.name for c in cores)
                    + "; M33: " + " ".join(m33_mod.M33_APS)
                )
            except S32EError as exc:
                # A fresh power cycle loses the LADDER-1 enables, so discovery
                # can legitimately fail before `gates apply` has run. The
                # session stays open so it can be run *inside* -- rediscovery
                # happens on the next core command.
                console.print(f"[yellow]![/yellow] {exc}")
                console.print(
                    "[dim]session open with no cores discovered yet -- run "
                    "`gates apply`, then `core list`.[/dim]"
                )
            if core_spec:
                _session_use(ctx, [core_spec])

            if batch:
                for line in batch:
                    if not _session_dispatch(ctx, line):
                        break
            elif not sys.stdin.isatty():
                for line in sys.stdin:
                    if not _session_dispatch(ctx, line.rstrip("\n")):
                        break
            else:
                _session_repl(ctx)
        finally:
            ctx.session = None
            if keep_on_exit:
                console.print(
                    "[yellow]--keep-on-exit:[/yellow] breakpoints stay armed and "
                    "halted cores stay halted."
                )
            else:
                for note in session.restore():
                    console.print(f"[dim]restore:[/dim] {note}")
            console.print("[green]session closed[/green]")


# ---------------------------------------------------------------------------
# load -- write an ELF straight into RAM and park rtu0.0 at its entry
# ---------------------------------------------------------------------------


@main.command("load")
@click.argument("elf_path")
@click.option("--verify", "verify_full", is_flag=True,
              help="Read every word back and compare (slow on the pure ADIv6 "
                   "path -- roughly the write time again). Sampled verification "
                   "always runs regardless.")
@click.option("--no-fast", is_flag=True,
              help="Never use the J-Link WriteMem fast path; pure ADIv6 word "
                   "bursts only.")
@click.option("--reset", "reset_via", type=click.Choice(["sda", "pin"]),
              default="sda", show_default=True,
              help="Reset mechanism: 'sda' = chip-internal SDA-AP functional "
                   "reset (bench-validated, default); 'pin' = the probe's "
                   "nRESET line, the reference plugin's literal mechanism -- "
                   "on this board the pulse reaches the SBC/PMIC through R439.")
@click.option("--core1", "core1_mode", type=click.Choice(["leave", "park", "release"]),
              default="leave", show_default=True,
              help="rtu0.1 handling. 'leave' (default): untouched -- br1_rear's "
                   "core0 firmware does the whole core-1 bring-up itself "
                   "(verified in BrsHw.c). 'park': bring it up halted at the "
                   "entry. 'release': bring it up and let it free-run.")
@click.pass_obj
def load_cmd(
    ctx: Ctx,
    elf_path: str,
    verify_full: bool,
    no_fast: bool,
    reset_via: str,
    core1_mode: str,
) -> None:
    """Load an ELF into RTU0 RAM and leave rtu0.0 halted at its entry.

    The fast build/debug loop: no flashing. Replicates the reference plugin's
    debugger-does-everything flow -- system reset with debug requests latched,
    MC_ME/RGM partition bring-up, SRAMC ECC init (with the edge-trigger fix
    and witness verification), park rtu0.0, write every PT_LOAD segment at its
    LMA, verify, set PC = entry. You set breakpoints and `resume` when ready.

    This resets the WHOLE SoC: every core reboots and everything running
    stops. Run it inside a `debug` session -- a one-shot invocation works, but
    the next connect pulses reset into the SBC/PMIC (defect D-2) and undoes
    the load.
    """
    # Parse and sanity-check before the probe opens: a bad path or a
    # flash-linked image must cost nothing.
    try:
        image, blob = elf_mod.load(elf_path)
        loader_mod.check_windows(image)
    except S32EError as exc:
        raise click.ClickException(str(exc)) from exc

    console.print(
        f"[yellow]![/yellow] `load` resets the whole SoC: every core reboots. "
        f"Image: {image.name}, {image.total_filesz:,} bytes in "
        f"{len(image.segments)} segments, entry 0x{image.entry_pc:08X}"
        + (" (Thumb)" if image.entry_thumb else "")
    )
    if ctx.session is None:
        console.print(
            "[yellow]![/yellow] one-shot load: the NEXT s32edbg connect will "
            "pulse reset into the SBC/PMIC (defect D-2) and reboot away what "
            "was just loaded. Run `load` inside `s32edbg debug` instead."
        )

    with open_transport(ctx) as (t, dap):
        assert dap is not None

        def note(text: str) -> None:
            console.print(f"[dim]load:[/dim] {text}")

        session = ctx.session
        if session is not None:
            # Finding 9: the J-Link DLL's WriteMem wedges after roughly
            # 3.5-5 MB of traffic in one DLL session -- every later call
            # returns -1 in microseconds with no wire traffic, and neither
            # retries nor the DLL's own Halt() cure it. Only a fresh DLL
            # session does. A load is the one safe moment to pay for one:
            # the reopen's connect pulse lands moments before this flow's
            # own commanded SoC reset, so nothing of value is disturbed.
            note("fresh probe connection for this load (Finding 9: the DLL's "
                 "WriteMem wedges after ~4 MB per connection)")
            t.close()
            t.open()
            t.select_interface(Tif.JTAG)
            t.set_speed_khz(ctx.speed_khz)
            _bring_up_link(t, ctx, Tif.JTAG)
            dap.invalidate_select_cache()
            dap.invalidate_csw_cache()
            session.prepared = False

        for prep_note in _prepare_dap(dap, ctx=ctx):
            console.print(f"[dim]note:[/dim] {prep_note}")
        report = gates_mod.apply_ladder1(dap)
        for w in report.warnings:
            console.print(f"[yellow]![/yellow] {w}")

        # Best-effort: the load's own reset (P1/P2, EDBGREQ latched) is the
        # quiesce that persists, and on a blank-flash board the SMU AP answers
        # nothing at attach (BootROM fail cascade) until that reset runs.
        loader_mod.halt_m33s(dap, note, smu_fatal=False)

        if reset_via == "pin":
            if not getattr(t, "can_pin_reset", False):
                raise click.ClickException(
                    "--reset pin needs the DLL's SetRESET/ClrRESET exports, "
                    "which this transport does not provide"
                )
            driver: loader_mod.ResetDriver = loader_mod.PinReset(t)
        else:
            driver = loader_mod.SdaReset(dap)

        session = ctx.session
        hooks = loader_mod.LoadHooks(
            note=note,
            attach_core=lambda c: _attach_core(ctx, dap, c),
            discover_cores=lambda: _discover_cores(ctx, dap),
            on_system_reset=(
                session.note_system_reset if session is not None else (lambda: None)
            ),
            note_halted=(
                session.note_halted if session is not None else (lambda name: None)
            ),
        )
        load_report = loader_mod.load_image(
            dap,
            t,
            image,
            blob,
            hooks,
            reset=driver,
            verify_full=verify_full,
            use_fast=not no_fast,
            core1=core1_mode,
        )

        emit.print_load_report(load_report, console)
        if session is not None:
            session.focus = "rtu0.0"
            console.print(
                "[dim]focus: rtu0.0 -- set breakpoints, then `resume` (or "
                "`continue`) when ready. Quitting the session resumes the "
                "core unless --keep-on-exit.[/dim]"
            )
        else:
            console.print(
                "[dim]rtu0.0 stays halted at the entry after this process "
                "exits; resume it from a debug session.[/dim]"
            )
        if ctx.json_out:
            _emit_json(ctx.json_out, load_report.as_dict())


# ---------------------------------------------------------------------------
# flash -- external QSPI NOR (s32multitool raw-QSPI route, SMU M33 applet)
# ---------------------------------------------------------------------------


def _flash_note(text: str) -> None:
    console.print(f"[dim]flash:[/dim] {text}")


def _flash_resolve_target(offset: str | None, area: str | None) -> tuple[int, int | None, str]:
    """Resolve (--offset | --area) into (flash offset, capacity or None, label)."""
    if (offset is None) == (area is None):
        raise click.ClickException(
            "pick a destination: exactly one of --offset 0x... or --area "
            f"{{{'|'.join(qspiflash.REGIONS)}}}"
        )
    if offset is not None:
        value = _parse_address(offset, what="--offset")
        if value >= qspiflash.DEVICE_SIZE:
            raise click.ClickException(
                f"--offset 0x{value:08X} is past the end of the "
                f"{qspiflash.DEVICE_SIZE // (1024 * 1024)} MiB device"
            )
        return value, None, f"0x{value:08X}"
    key = area.strip().lower()
    if key not in qspiflash.REGIONS:
        raise click.ClickException(
            f"unknown --area {area!r}. Known: {', '.join(qspiflash.REGIONS)}"
        )
    off, size, label = qspiflash.REGIONS[key]
    return off, size, f"--area {key} ({label}, 0x{off:08X}+0x{size:X})"


def _flash_read_input(path: str, *, force_bin: bool) -> tuple[bytes, str]:
    """The program/verify input layer: .bin verbatim, ELF flattened.

    ELF detection is by magic, not extension -- the user decision (2026-08-04)
    is that a RAM-linked ELF is packed LMA-relative into one flat image with
    0xFF gaps and written verbatim like any .bin. --format bin forces raw
    treatment for the pathological file that starts with the ELF magic.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise click.ClickException(f"cannot read {p}: {exc}") from None
    if not raw:
        raise click.ClickException(f"{p} is empty; nothing to do")
    if force_bin or raw[:4] != elf_mod.ELF_MAGIC:
        return raw, p.name
    try:
        image = elf_mod.parse(raw, name=p.name)
        base, flat = elf_mod.flatten(image, raw)
    except S32EError as exc:
        raise click.ClickException(str(exc)) from exc
    _flash_note(
        f"{p.name} is an ELF -- flattened LMA-relative: base 0x{base:08X}, "
        f"span {len(flat):,} bytes ({image.total_filesz:,} bytes of file "
        "content, gaps filled 0xFF)"
    )
    return flat, f"{p.name} (ELF, flattened from LMA 0x{base:08X})"


def _flash_check_fit(data_len: int, offset: int, capacity: int | None, label: str) -> None:
    if capacity is not None and data_len > capacity:
        raise click.ClickException(
            f"the image is {data_len:,} bytes but {label} holds only "
            f"{capacity:,} -- refusing to spill past the region"
        )
    if offset + data_len > qspiflash.DEVICE_SIZE:
        raise click.ClickException(
            f"{data_len:,} bytes at 0x{offset:08X} runs past the end of the "
            f"{qspiflash.DEVICE_SIZE // (1024 * 1024)} MiB device"
        )


def _flash_confirm(what: str, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise click.ClickException(
            f"{what} is destructive; pass --yes to run it without a prompt "
            "(stdin is not a terminal, so there is nobody to ask)"
        )
    if not click.confirm(f"{what} -- proceed?", default=False):
        raise click.ClickException("aborted")


def _flash_options(fn):
    """Options every flash subcommand shares (connection + AP routing)."""
    fn = click.option(
        "--ahb-base", "ahb_base", default=None,
        help="Memory-mapped flash window base for reads/verify. Default 0x0 "
             "with automatic one-time failover to 0x08000000 -- both appear "
             "in the reference profile (bench item u-f1).",
    )(fn)
    fn = click.option(
        "--ap", "ap_spec", default=None,
        help="Memory AP for the whole flash flow: 'smu' (default -- every "
             "address involved is an M33-view address), 'axi', or a "
             "0x-prefixed AP base.",
    )(fn)
    fn = click.option(
        "--no-fast", is_flag=True,
        help="Never use the J-Link WriteMem/ReadMem fast paths; pure ADIv6 "
             "word traffic only (slow: ~4 KB/s).",
    )(fn)
    fn = click.option(
        "--reset", "reset_via", type=click.Choice(["sda", "pin", "none"]),
        default="pin", show_default=True,
        help="Quiesce mechanism before touching QSPI. 'pin' = probe nRESET, "
             "the s32multitool-exact RESET_B toggle (default for flash; it "
             "reaches the SBC/PMIC through R439, defect D-2 -- the same path "
             "every connect pulse already takes). 'sda' = chip-internal "
             "functional reset (the load flow's default), the fallback if "
             "the pin path misbehaves. 'none' = attach to the SoC as-is -- "
             "cannot recover a wiped board (the SMU bring-up needs the "
             "EDBGREQ-latched reset) and unproven while firmware may own "
             "the controller (bench item u-f9).",
    )(fn)
    return fn


#: Finding-10 settle bound: how long the flash flow waits for the SMU AHB-AP
#: to answer identity after the reset release before proceeding. On a healthy
#: board the AP is back in ~0.5 s; on a wiped (DEST0) board it stays dead for
#: the full wait and the SMU bring-up below revives it (Finding 15).
#: Module-level so tests can shrink the wait.
FLASH_SMU_SETTLE_TIMEOUT_S = 10.0


def _flash_bringup(
    ctx: Ctx,
    t: object,
    dap: Dap,
    *,
    reset_via: str,
    no_fast: bool,
    ap_spec: str | None,
    ahb_base: str | None,
    force_jedec: bool,
    destructive: bool,
) -> flasher_mod.QspiFlasher:
    """F0-F6: fresh connection, gates, reset-quiesce, SMU bring-up, QSPI up.

    Since Finding 15 the flow boots the SMU itself, s32multitool-style:
    EDBGREQ-latched reset, MC_ME/RGM partition enables, the NXP SRAM scrubs,
    then the MC_ME PRTN0 core-0 release onto a debugger-written stub -- so a
    wiped (DEST0) board is brought up the same way as a healthy one. Ends
    with the controller initialised, the LUTs armed, the JEDEC identity
    checked (for destructive callers), and the SMU halted by its own DHCSR
    with its EDBGREQ latch cleared -- the exact state `run_applet` requires.
    """
    note = _flash_note
    session = ctx.session
    if session is not None:
        # Finding 9, same policy as `load`: the DLL's WriteMem has a ~4 MB
        # per-connection budget, and a flash op stages the applet plus up to
        # megabytes of payload through it. The reopen's connect pulse lands
        # moments before this flow's own quiesce reset, so nothing of value
        # is disturbed.
        note(
            "fresh probe connection for this flash op (Finding 9: the DLL's "
            "WriteMem wedges after ~4 MB per connection)"
        )
        t.close()
        t.open()
        t.select_interface(Tif.JTAG)
        t.set_speed_khz(ctx.speed_khz)
        _bring_up_link(t, ctx, Tif.JTAG)
        dap.invalidate_select_cache()
        dap.invalidate_csw_cache()
        session.prepared = False

    for prep_note in _prepare_dap(dap, ctx=ctx):
        console.print(f"[dim]note:[/dim] {prep_note}")
    gate_report = gates_mod.apply_ladder1(dap)
    for w in gate_report.warnings:
        console.print(f"[yellow]![/yellow] {w}")
    # Pre-reset quiesce: best-effort whenever the EDBGREQ-latched reset
    # follows (a blank-flash board answers nothing here, measured 2026-08-05);
    # with --reset none there is no stronger mechanism coming, so a dead SMU
    # is fatal right here.
    loader_mod.halt_m33s(dap, note, smu_fatal=reset_via == "none")

    if reset_via != "none":
        if reset_via == "pin":
            if not getattr(t, "can_pin_reset", False):
                raise click.ClickException(
                    "--reset pin needs the DLL's SetRESET/ClrRESET exports, "
                    "which this transport does not provide"
                )
            driver: loader_mod.ResetDriver = loader_mod.PinReset(t)
        else:
            driver = loader_mod.SdaReset(dap)
        note(
            f"reset-quiesce: asserting {driver.name} functional reset with "
            "every debug request latched"
        )
        driver.assert_reset()
        gates_mod.arm_edbgreq(dap)
        driver.release_reset()
        loader_mod.wait_reset_deasserted(dap, note)
        if session is not None:
            session.note_system_reset()
        # The reference's own post-release settle: s32multitool pauses a flat
        # 200 ms after RESET_B goes high (_reset_to_first_instruction) before
        # any AXI traffic. Kept verbatim on top of the flag polls above.
        time.sleep(0.2)
        # Every core woke into a pending EDBGREQ; re-pin the M33s by their
        # own DHCSR before the latches can be dropped. Not instantly, though:
        # measured 2026-08-05 (Finding 10), the SMU AHB-AP answers all-zeros
        # for a moment after the reset release (identity reads zero, correct
        # again shortly after, with the EDBGREQ latch having caught the
        # core) -- so poll identity until the AP is back before the loud
        # one-shot halt. On a wiped (DEST0) board the poll times out with the
        # AP still dead -- expected; the bring-up below is what revives it.
        settle_t0 = time.monotonic()
        while True:
            ident = m33_mod.identify(dap, m33_mod.M33_APS["smu"])
            smu_alive = ident.is_cortex_m33
            if smu_alive or time.monotonic() - settle_t0 > FLASH_SMU_SETTLE_TIMEOUT_S:
                break
            time.sleep(0.05)
        settled = time.monotonic() - settle_t0
        if settled > 0.1:
            note(
                f"SMU AHB-AP {'came back' if smu_alive else 'still dead'} "
                f"{settled:.2f}s after the reset release"
            )
        # The s32multitool bring-up, verbatim tables, always on (Finding 15):
        # MC_ME/RGM partition enables, the NXP generic SRAM scrubs, then the
        # MC_ME PRTN0 core-0 release that boots the SMU onto a two-word stub
        # in its own SRAM -- flash-independent, which is what makes a wiped
        # (DEST0) board flashable. All of it is AXI traffic and all of it
        # runs only after the reset release, matching both the reference
        # ordering and the RM's no-AXI-during-reset rule.
        note(
            "SMU bring-up (s32multitool, Finding 15): partitions + SRAM "
            "scrubs + MC_ME PRTN0 core-0 release"
        )
        _axi_report, pf_notes = memaccess.preflight(dap, AP_AXI, write=True)
        for n in pf_notes:
            note(n)
        loader_mod.run_init_actions(dap, rtuboot.ACTIONS_RTU0_PARTITION, note=note)
        loader_mod.run_init_actions(dap, rtuboot.ACTIONS_RTU1_PARTITION, note=note)
        loader_mod.run_init_actions(dap, rtuboot.ACTIONS_GENERIC_SRAM, note=note)
        loader_mod.run_init_actions(dap, smuboot.ACTIONS_SMU_BOOT, note=note)
        # The reference's wiped-board guarantee: config_m33 -- the M33
        # subsystem clock select (MC_CGM2 MUX0) -- always runs in its full
        # prepare before anything touches SMU SRAM (its applet staging comes
        # only after prepare). This flow's first SMU-SRAM touch is the
        # flasher preflight, so the same write must land here: measured
        # 2026-08-06 on the DEST0-recovered bench board, the mux reads
        # 0x0F000000 (no source selected) and SMU SRAM discards writes --
        # the MC_ME release brings up the core and its debug view, not the
        # SRAM fabric. Idempotent on a healthy board (BootROM selects the
        # same source; the reference re-runs it on every full prepare).
        loader_mod.run_init_actions(dap, qspiflash.ACTIONS_M33_CLOCK, note=note)
        if not smu_alive:
            # The AP was dead at the settle poll; give the just-released core
            # a short beat to present its debug face before the loud halt.
            revive_t0 = time.monotonic()
            while True:
                ident = m33_mod.identify(dap, m33_mod.M33_APS["smu"])
                if ident.is_cortex_m33 or time.monotonic() - revive_t0 > 2.0:
                    break
                time.sleep(0.05)
        loader_mod.halt_m33s(dap, note)
    else:
        note(
            "--reset none: attaching to the SoC as-is. If firmware owns the "
            "QSPI controller this flow may misprogram or hang (u-f9). No "
            "reset also means no SMU bring-up -- recovering a wiped board "
            "needs --reset pin (default) or sda."
        )

    # The trap this ordering exists for: a latched SMU EDBGREQ re-halts the
    # core the instant it is resumed into the applet. Drop the latch only
    # after the DHCSR pin-down, then prove the halt survived the drop. (The
    # reference de-asserts before its halt; the end state -- SMU halted via
    # DHCSR, CONTROL bit 8 clear -- is identical, and this order is what the
    # survival check below is built on.)
    gates_mod.clear_smu_edbgreq(dap)
    cores = m33_mod.discover(dap)
    smu = m33_mod.resolve("smu", cores)
    for n in m33_mod.preflight(smu, write=True):
        note(f"smu: {n}")
    if smu.halted is not True:
        raise click.ClickException(
            "the SMU M33 did not stay halted once its EDBGREQ latch was "
            "cleared; refusing to run the flash applet on a running core"
        )
    note(
        "SMU EDBGREQ latch cleared (multitool-exact: CONTROL &= ~0x10000100; "
        "CE latches left as the reference leaves them); the SMU stays pinned "
        "by DHCSR"
    )

    ap_mem = memaccess.resolve_ap(ap_spec) if ap_spec else None
    window_base = (
        _parse_address(ahb_base, what="--ahb-base")
        if ahb_base
        else qspiflash.AHB_WINDOW_BASES[0]
    )
    fl = flasher_mod.QspiFlasher(
        dap,
        t,
        smu,
        note=note,
        ap_mem=ap_mem,
        ahb_base=window_base,
        use_fast=not no_fast,
    )
    for n in fl.preflight():
        console.print(f"[dim]note:[/dim] {n}")
    fl.controller_init()
    mode, jedec = fl.prepare()
    console.print(f"[dim]flash:[/dim] mode [bold]{mode}[/bold], JEDEC 0x{jedec:08X}")
    if destructive:
        fl.check_jedec(force=force_jedec)
    return fl


def _flash_finish(
    ctx: Ctx, fl: flasher_mod.QspiFlasher, report: flasher_mod.FlashReport, t0: float
) -> None:
    report.seconds = time.monotonic() - t0
    fl.finish_report(report)
    emit.print_flash_report(report, console)
    if ctx.json_out:
        _emit_json(ctx.json_out, report.as_dict())


@main.group("flash")
def flash_group() -> None:
    """External QSPI NOR flash: program, erase, verify, read, jedec.

    The s32multitool raw-QSPI route ported onto this tool: the QuadSPI
    controller at 0x42320000 is driven directly and every write/erase runs in
    a vendored 2 KiB applet on the SMU M33 (data/qspiflash.py carries the
    citations). Every subcommand resets the SoC with all debug requests
    latched, then boots the SMU itself exactly as the reference does
    (partitions + SRAM scrubs + MC_ME core release, Finding 15) -- so a
    completely wiped board is flashable -- and leaves it halted afterwards.
    Power-cycle the board to boot what was flashed; the reference project
    documents that a debug reset is not equivalent.
    """


@flash_group.command("jedec")
@_flash_options
@click.pass_obj
def flash_jedec(
    ctx: Ctx, reset_via: str, no_fast: bool, ap_spec: str | None, ahb_base: str | None
) -> None:
    """Identify the flash device: access mode (SPI/OPI) and JEDEC ID.

    The smoke test: a healthy MX25UW25A45G answers 0xC23989C2. Run this
    before the first destructive command on any board.
    """
    t0 = time.monotonic()
    with open_transport(ctx) as (t, dap):
        assert dap is not None
        fl = _flash_bringup(
            ctx, t, dap, reset_via=reset_via, no_fast=no_fast, ap_spec=ap_spec,
            ahb_base=ahb_base, force_jedec=False, destructive=False,
        )
        report = flasher_mod.FlashReport(op="jedec")
        assert fl.jedec is not None
        verdict = (
            "matches the expected MX25UW25A45G"
            if fl.jedec in qspiflash.EXPECTED_JEDEC_IDS
            else "does NOT match the expected MX25UW25A45G (0xC23989C2)"
        )
        colour = "green" if fl.jedec in qspiflash.EXPECTED_JEDEC_IDS else "yellow"
        console.print(f"[{colour}]JEDEC 0x{fl.jedec:08X} ({fl.mode}) -- {verdict}[/{colour}]")
        _flash_finish(ctx, fl, report, t0)


@flash_group.command("read")
@click.option("--offset", default=None, help="Flash offset, 0x-prefixed hex.")
@click.option("--area", default=None, help=f"Named region: {', '.join(qspiflash.REGIONS)}.")
@click.option("--length", default=None,
              help="Bytes to read (decimal or 0x-hex). Defaults to the --area size.")
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False),
              help="Write the dump to this file.")
@_flash_options
@click.pass_obj
def flash_read(
    ctx: Ctx, offset: str | None, area: str | None, length: str | None, out_path: str,
    reset_via: str, no_fast: bool, ap_spec: str | None, ahb_base: str | None,
) -> None:
    """Dump a flash range into a file, via the memory-mapped read window."""
    off, capacity, label = _flash_resolve_target(offset, area)
    if length is not None:
        nbytes = _parse_value(length, what="--length")
    elif capacity is not None:
        nbytes = capacity
    else:
        raise click.ClickException("--length is required when using --offset")
    if nbytes <= 0:
        raise click.ClickException("--length must be positive")
    if off + nbytes > qspiflash.DEVICE_SIZE:
        raise click.ClickException(
            f"reading {nbytes:,} bytes at 0x{off:08X} runs past the device end"
        )
    t0 = time.monotonic()
    with open_transport(ctx) as (t, dap):
        assert dap is not None
        fl = _flash_bringup(
            ctx, t, dap, reset_via=reset_via, no_fast=no_fast, ap_spec=ap_spec,
            ahb_base=ahb_base, force_jedec=False, destructive=False,
        )
        report = flasher_mod.FlashReport(op="read", offset=off, length=nbytes)
        data = fl.read(off, nbytes)
        Path(out_path).write_bytes(data)
        console.print(
            f"[green]read {nbytes:,} bytes[/green] from {label} into {out_path}"
        )
        _flash_finish(ctx, fl, report, t0)


@flash_group.command("verify")
@click.argument("input_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--offset", default=None, help="Flash offset, 0x-prefixed hex.")
@click.option("--area", default=None, help=f"Named region: {', '.join(qspiflash.REGIONS)}.")
@click.option("--format", "format_", type=click.Choice(["bin"]), default=None,
              help="Force raw-binary treatment even if the file starts with the ELF magic.")
@_flash_options
@click.pass_obj
def flash_verify(
    ctx: Ctx, input_path: str, offset: str | None, area: str | None, format_: str | None,
    reset_via: str, no_fast: bool, ap_spec: str | None, ahb_base: str | None,
) -> None:
    """Byte-exact compare of a file (or flattened ELF) against flash content."""
    data, name = _flash_read_input(input_path, force_bin=format_ == "bin")
    off, capacity, label = _flash_resolve_target(offset, area)
    _flash_check_fit(len(data), off, capacity, label)
    t0 = time.monotonic()
    with open_transport(ctx) as (t, dap):
        assert dap is not None
        fl = _flash_bringup(
            ctx, t, dap, reset_via=reset_via, no_fast=no_fast, ap_spec=ap_spec,
            ahb_base=ahb_base, force_jedec=False, destructive=False,
        )
        report = flasher_mod.FlashReport(op="verify", offset=off, length=len(data))
        fl.verify(data, off, report, mode="full")
        console.print(
            f"[green]verify passed[/green]: {name} matches flash at {label}, byte-exact"
        )
        _flash_finish(ctx, fl, report, t0)


@flash_group.command("erase")
@click.option("--offset", default=None, help="Flash offset, 0x-prefixed hex, 4 KiB aligned.")
@click.option("--area", default=None, help=f"Named region: {', '.join(qspiflash.REGIONS)}.")
@click.option("--length", default=None,
              help="Bytes to erase (4 KiB multiple). Defaults to the --area size.")
@click.option("--all", "erase_all", is_flag=True,
              help="Erase the WHOLE 32 MiB device (host-driven chip erase; minutes).")
@click.option("--no-verify", is_flag=True, help="Skip the blank check afterwards.")
@click.option("--force-jedec", is_flag=True,
              help="Proceed even if the JEDEC ID is not the expected MX25UW25A45G.")
@click.option("--yes", is_flag=True, help="Skip the interactive confirmation.")
@_flash_options
@click.pass_obj
def flash_erase(
    ctx: Ctx, offset: str | None, area: str | None, length: str | None, erase_all: bool,
    no_verify: bool, force_jedec: bool, yes: bool,
    reset_via: str, no_fast: bool, ap_spec: str | None, ahb_base: str | None,
) -> None:
    """Erase a flash range (4K/64K plan) or the whole device (--all).

    Erased flash reads 0xFF. Erasing the blob area (offset 0) removes the
    boot IVT: the board will no longer boot until something is programmed
    back, and a blank boot flash escalates the BootROM failure cascade
    (docs/virgin-board.md) -- recovery is `flash program` at offset 0 (the
    flow boots the SMU itself, Finding 15).
    """
    if erase_all:
        if offset is not None or area is not None or length is not None:
            raise click.ClickException("--all does not combine with --offset/--area/--length")
        _flash_confirm(
            "flash erase --all: erase the ENTIRE 32 MiB device, including the "
            "bootloader and IVT at offset 0. The board will not boot again "
            "until something is programmed back -- with no bootable image the "
            "BootROM fail cascade escalates to a destructive reset (DEST0, "
            "Finding 14) and only debug access remains. Since Finding 15 this "
            "tool CAN reflash that state (the flash flow boots the SMU "
            "itself, s32multitool-style); still, erase a RANGE instead if "
            "the bootloader must survive",
            yes,
        )
        off, nbytes, label = 0, qspiflash.DEVICE_SIZE, "the whole device"
    else:
        off, capacity, label = _flash_resolve_target(offset, area)
        if length is not None:
            nbytes = _parse_value(length, what="--length")
        elif capacity is not None:
            nbytes = capacity
        else:
            raise click.ClickException("--length is required when using --offset")
        _flash_confirm(f"flash erase: {nbytes:,} bytes at {label}", yes)
    t0 = time.monotonic()
    with open_transport(ctx) as (t, dap):
        assert dap is not None
        fl = _flash_bringup(
            ctx, t, dap, reset_via=reset_via, no_fast=no_fast, ap_spec=ap_spec,
            ahb_base=ahb_base, force_jedec=force_jedec, destructive=True,
        )
        report = flasher_mod.FlashReport(
            op="erase --all" if erase_all else "erase", offset=off, length=nbytes
        )
        if erase_all:
            fl.chip_erase(report)
            if not no_verify:
                fl.blank_check(off, nbytes, report, mode="sampled")
        else:
            fl.erase(off, nbytes, report)
            if not no_verify:
                # A full read of a multi-MiB erase is minutes on a demoted
                # read path; past 1 MiB the check samples one word per sector.
                mode = "full" if nbytes <= 0x10_0000 else "sampled"
                fl.blank_check(off, nbytes, report, mode=mode)
        console.print(f"[green]erased[/green] {nbytes:,} bytes at {label}")
        _flash_finish(ctx, fl, report, t0)


@flash_group.command("program")
@click.argument("input_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--offset", default=None, help="Flash offset, 0x-prefixed hex, 256-byte aligned.")
@click.option("--area", default=None, help=f"Named region: {', '.join(qspiflash.REGIONS)}.")
@click.option("--format", "format_", type=click.Choice(["bin"]), default=None,
              help="Force raw-binary treatment even if the file starts with the ELF magic.")
@click.option("--no-skip-blank", is_flag=True,
              help="Program all-0xFF pages too instead of skipping them (they "
                   "read 0xFF either way after the erase).")
@click.option("--verify", "verify_mode", type=click.Choice(["full", "sampled", "none"]),
              default="full", show_default=True,
              help="Post-program read-back compare through the flash window.")
@click.option("--force-jedec", is_flag=True,
              help="Proceed even if the JEDEC ID is not the expected MX25UW25A45G.")
@click.option("--yes", is_flag=True, help="Skip the interactive confirmation.")
@_flash_options
@click.pass_obj
def flash_program(
    ctx: Ctx, input_path: str, offset: str | None, area: str | None, format_: str | None,
    no_skip_blank: bool, verify_mode: str, force_jedec: bool, yes: bool,
    reset_via: str, no_fast: bool, ap_spec: str | None, ahb_base: str | None,
) -> None:
    """Erase-and-program an image into external flash, then verify.

    A .bin is written verbatim at the offset (the s32multitool contract: the
    image already carries whatever IVT/headers it needs). An ELF is flattened
    LMA-relative first, 0xFF in the gaps. Erase always precedes programming,
    sector-aligned around the target range.
    """
    data, name = _flash_read_input(input_path, force_bin=format_ == "bin")
    off, capacity, label = _flash_resolve_target(offset, area)
    _flash_check_fit(len(data), off, capacity, label)
    if off % qspiflash.PAGE_SIZE:
        raise click.ClickException(
            f"program offset 0x{off:08X} is not 256-byte aligned"
        )
    if off == 0 and data[0] != 0xD1:
        console.print(
            "[yellow]![/yellow] this image does not start with the BootROM IVT "
            "tag 0xD1, and offset 0 is where the BootROM looks for one -- the "
            "board will not boot from it. Programming anyway as instructed."
        )
    erase_span = flasher_mod.align_up(
        off + flasher_mod.align_up(len(data), qspiflash.PAGE_SIZE), qspiflash.SECTOR_SIZE
    ) - flasher_mod.align_down(off, qspiflash.SECTOR_SIZE)
    _flash_confirm(
        f"flash program: erase 0x{erase_span:X} bytes around {label} and write "
        f"{len(data):,} bytes from {name}",
        yes,
    )
    t0 = time.monotonic()
    with open_transport(ctx) as (t, dap):
        assert dap is not None
        fl = _flash_bringup(
            ctx, t, dap, reset_via=reset_via, no_fast=no_fast, ap_spec=ap_spec,
            ahb_base=ahb_base, force_jedec=force_jedec, destructive=True,
        )
        report = flasher_mod.FlashReport(op="program", offset=off, length=len(data))
        try:
            fl.program(data, off, report, skip_blank=not no_skip_blank)
            fl.verify(data, off, report, mode=verify_mode)
        except S32EError:
            console.print(
                "[red]![/red] the program did not complete: flash content at "
                f"{label} is now UNDEFINED. Do not boot from it -- re-run "
                "`flash program`, and if this was offset 0, keep the board "
                "powered (a blank/partial boot flash escalates the BootROM "
                "failure cascade, docs/virgin-board.md)."
            )
            raise
        console.print(
            f"[green]programmed[/green] {len(data):,} bytes from {name} at {label}"
            + (
                "  [dim](byte 0 reads 0xD1 -- boot IVT in place)[/dim]"
                if off == 0 and verify_mode != "none" and data[0] == 0xD1
                else ""
            )
        )
        console.print(
            "[dim]power-cycle the board to boot the flashed image -- the "
            "reference project documents that a debug reset is not "
            "equivalent to the cold boot the BootROM needs.[/dim]"
        )
        _flash_finish(ctx, fl, report, t0)


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
        _prepare_dap(dap, ctx=ctx)
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
    rom_override = _parse_address(rom_base, what="--rom") if rom_base else None
    ap_override = _resolve_ap_or_fail(ap_base) if ap_base else None
    payload: dict[str, object] = {}
    with open_transport(ctx) as (_t, dap):
        assert dap is not None
        _prepare_dap(dap, ctx=ctx)

        gate = gates_mod.status(dap)
        if not gate.mdm.enables_set:
            console.print(
                "[yellow]![/yellow] the debug-enable gate is still closed, so the APB ROM "
                "will very likely read as empty. Run `s32edbg gates apply` first."
            )

        if source in ("both", "apspace"):
            base = rom_override if rom_override is not None else (dap.read_baseptr() or APBIC1_ROM_BASE) & 0xFFFFFFFF
            console.rule(f"AP-space ROM table at 0x{base:08X}")
            root = romtable.walk(dap.apspace_reader(), base, max_depth=max_depth)
            emit.print_rom_tree(root, console)
            console.print(_summary_line(root))
            payload["apspace"] = emit.node_to_dict(root)

        if source in ("both", "apb"):
            mem_ap = ap_override if ap_override is not None else AP_APB
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
    v = _parse_value(value, what="IR value")
    with open_transport(ctx, tif=Tif.JTAG, connect_dap=False, needs_raw_jtag=True) as (t, _):
        width = bits or ctx.irlen or tap_mod.measure_ir_length(t)  # type: ignore[arg-type]
        tdo = t.ir_shift(v, width)  # type: ignore[union-attr]
        console.print(f"IR {width} bits <- 0x{v:X}, TDO 0x{tdo:X}")


@tap.command("dr")
@click.argument("value")
@click.argument("bits", type=int)
@click.pass_obj
def tap_dr(ctx: Ctx, value: str, bits: int) -> None:
    """Shift a value through the data register (instruction must already be loaded)."""
    v = _parse_value(value, what="DR value")
    with open_transport(ctx, tif=Tif.JTAG, connect_dap=False, needs_raw_jtag=True) as (t, _):
        tdo = t.dr_shift(v, bits)  # type: ignore[union-attr]
        console.print(f"DR {bits} bits <- 0x{v:X}, TDO 0x{tdo:X}")


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
        payload["dap_notes"] = _prepare_dap(dap, ctx=ctx)
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
    except KeyboardInterrupt:
        # A Ctrl-C that escaped the session prompt (e.g. during discovery or
        # restore). The context managers above have already closed the probe.
        err_console.print("[yellow]aborted[/yellow]")
        return 130
    except Exception as exc:
        # An escaped exception is a tool bug. The exit-code contract
        # (test-plan §5) has no traceback bucket, so it degrades to a tool
        # error. S32EDBG_TRACE=1 re-raises for debugging.
        err_console.print(f"[red]internal error:[/red] {type(exc).__name__}: {exc}")
        if os.environ.get("S32EDBG_TRACE"):
            raise
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(entry())

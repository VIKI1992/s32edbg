"""Console and JSON rendering."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from ..dap.apmap import ApMap
from ..dap.gates import GateReport
from ..dap.romtable import Node
from ..data.expected_apmap import APBIC2_EXPECTED_ROMS
from ..jtag.tap import TapInfo


def print_hexdump(base: int, words: list[int], console: Console | None = None) -> None:
    """Classic four-words-per-line hex dump with an ASCII gutter.

    Words are little-endian on the wire, so the ASCII column shows bytes in
    memory order rather than in the order the hex digits read.
    """
    con = console or Console()
    for row in range(0, len(words), 4):
        chunk = words[row : row + 4]
        hexpart = " ".join(f"{w:08X}" for w in chunk)
        raw = b"".join(w.to_bytes(4, "little") for w in chunk)
        text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in raw)
        con.print(f"0x{base + row * 4:08X}  {hexpart:<35}  |{text}|")


def node_to_dict(node: Node) -> dict[str, object]:
    ident = node.ident
    out: dict[str, object] = {
        "base": f"0x{node.base:08X}",
        "depth": node.depth,
        "is_table": node.is_table,
    }
    if node.error:
        out["error"] = node.error
    if node.revisit_of is not None:
        out["revisit_of"] = f"0x{node.revisit_of:08X}"
    if ident is not None:
        out["identity"] = {
            "class": ident.klass,
            "class_name": ident.class_name,
            "part_number": f"0x{ident.part_number:03X}",
            "designer": ident.designer_name,
            "jep106": {
                "continuation": ident.jep106_continuation,
                "identity": f"0x{ident.jep106_identity:02X}",
                "jedec_valid": ident.jedec_valid,
            },
            "revision": ident.revision,
            "revand": ident.revand,
            "customer_modified": ident.customer_modified,
            "cidr": [f"0x{v:02X}" for v in ident.cidr],
            "pidr": [f"0x{v:02X}" for v in ident.pidr],
            "description": ident.describe(),
        }
        if ident.devarch is not None:
            out["identity"]["devarch"] = {  # type: ignore[index]
                "raw": f"0x{ident.devarch.raw:08X}",
                "present": ident.devarch.present,
                "architect": ident.devarch.architect_name,
                "archid": f"0x{ident.devarch.archid:04X}",
                "archid_name": ident.devarch.archid_name,
                "revision": ident.devarch.revision,
            }
        if ident.devtype_raw is not None:
            out["identity"]["devtype"] = ident.devtype  # type: ignore[index]
        if ident.devid_raw is not None:
            out["identity"]["devid"] = f"0x{ident.devid_raw:08X}"  # type: ignore[index]
    if node.entries:
        out["entries"] = [
            {
                "index": e.index,
                "raw": f"0x{e.raw:08X}",
                "present": e.present,
                "final": e.final,
                "offset_pages": e.offset_pages,
                "component": f"0x{e.component_addr:08X}" if e.component_addr is not None else None,
                "powerid": e.powerid,
            }
            for e in node.entries
        ]
    if node.children:
        out["children"] = [node_to_dict(c) for c in node.children]
    return out


def _node_label(node: Node) -> str:
    addr = f"[bold]0x{node.base:08X}[/bold]"
    if node.revisit_of is not None:
        return f"{addr} [yellow]already visited (cycle guard)[/yellow]"
    if node.error:
        return f"{addr} [red]{node.error}[/red]"
    if node.ident is None:
        return f"{addr} [dim]unidentified[/dim]"
    known = APBIC2_EXPECTED_ROMS.get(node.base)
    suffix = f" [dim]({known})[/dim]" if known else ""
    style = "cyan" if node.is_table else "white"
    return f"{addr} [{style}]{node.ident.describe()}[/{style}]{suffix}"


def print_rom_tree(root: Node, console: Console | None = None) -> None:
    console = console or Console()
    tree = Tree(_node_label(root))

    def add(parent: Tree, node: Node) -> None:
        for child in node.children:
            branch = parent.add(_node_label(child))
            add(branch, child)

    add(tree, root)
    console.print(tree)


def print_apmap(apmap: ApMap, console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="Access ports", show_lines=False)
    table.add_column("AP base", style="bold")
    table.add_column("IDR")
    table.add_column("RM")
    table.add_column("Kind")
    table.add_column("Expected role")
    table.add_column("Found by")

    for ap in apmap.aps:
        idr = f"0x{ap.idr.raw:08X}" if ap.idr and ap.idr.present else "-"
        match = ap.idr_matches_rm
        if match is None:
            rm = "[dim]n/a[/dim]"
        elif match:
            rm = "[green]match[/green]"
        else:
            rm = "[red]differs[/red]"
        table.add_row(
            f"0x{ap.base:08X}",
            idr,
            rm,
            ap.describe(),
            ap.expected_name or "[yellow]undocumented[/yellow]",
            ",".join(sorted(ap.found_by)) or "-",
        )
    console.print(table)

    if apmap.baseptr is not None:
        console.print(f"BASEPTR (top-level AP-space pointer): 0x{apmap.baseptr:X}")
    console.print(f"Top-level ROM table scanned at: 0x{apmap.rom_base_used:08X}")
    if apmap.walk_error:
        console.print(f"[yellow]ROM walk:[/yellow] {apmap.walk_error}")
    for note in apmap.notes:
        console.print(f"[dim]note:[/dim] {note}")

    diff = apmap.diff_against_rm()
    for key, colour in (("missing", "red"), ("mismatched", "yellow"), ("unexpected", "yellow")):
        for line in diff[key]:
            console.print(f"[{colour}]{key}:[/{colour}] {line}")
    if not any(diff.values()):
        console.print("[green]AP map matches RM Table 957 exactly.[/green]")


def print_cores(cores: list, console: Console | None = None) -> None:
    """One row per discovered core debug frame.

    ``Identity`` is the three-anchor check from ``runcontrol.identify()`` -- if
    it does not say ok, nothing should be written to that frame, because a wrong
    base address reads and writes some other component instead of faulting.
    """
    console = console or Console()
    if not cores:
        console.print(
            "[yellow]no core debug frames found.[/yellow] The debug clocks are off by "
            "default (RM 135.11) -- run `s32edbg gates apply` first."
        )
        return

    table = Table(title="Cortex-R52 cores")
    table.add_column("#", style="bold")
    table.add_column("Name")
    table.add_column("Debug frame")
    table.add_column("CTI")
    table.add_column("Part")
    table.add_column("Identity")
    table.add_column("BP/WP")
    table.add_column("Invasive")
    table.add_column("State")

    for core in cores:
        ident = core.identity
        midr = r52_decode(ident.midr)
        comparators = ident.comparators
        if comparators is None:
            counts = "-"
        elif comparators["plausible"]:
            counts = f"{comparators['breakpoints']}/{comparators['watchpoints']}"
        else:
            counts = f"[yellow]{comparators['breakpoints']}/{comparators['watchpoints']}?[/yellow]"

        granted = ident.invasive_debug_granted
        invasive = (
            "[dim]?[/dim]"
            if granted is None
            else ("[green]granted[/green]" if granted else "[red]denied[/red]")
        )

        halted = core.state.halted
        if core.state.status_registers_disagree:
            state = "[red]registers disagree[/red]"
        elif core.state.unreachable_reason:
            # A locked or reset core is a normal condition, not a fault -- most
            # of the cores on a running ECU are in exactly this state.
            state = "[yellow]locked[/yellow]"
        elif halted is None:
            state = "[dim]unknown[/dim]"
        elif halted:
            state = "[yellow]halted[/yellow]"
        else:
            state = "[green]running[/green]"

        table.add_row(
            str(core.index),
            core.name + ("" if core.found_in_rom else " [dim](probed)[/dim]"),
            f"0x{core.debug_base:08X}",
            f"0x{core.cti_base:08X}",
            str(midr.get("part_name", "-")) if midr else "-",
            "[green]ok[/green]" if ident.anchors_agree else "[red]mismatch[/red]",
            counts,
            invasive,
            state,
        )
    console.print(table)

    for core in cores:
        if core.state.edscr is not None:
            from ..data.r52debug import edscr_status_name

            console.print(f"[dim]{core.name}:[/dim] {edscr_status_name(core.state.edscr)}")
        elif core.state.unreachable_reason:
            console.print(
                f"[dim]{core.name}:[/dim] core-domain debug registers unreadable -- "
                f"{core.state.unreachable_reason}"
            )
        if core.identity.error:
            console.print(f"[red]{core.name}:[/red] {core.identity.error}")


def print_registers(core_name: str, regs, console: Console | None = None) -> None:
    """r0-r14 and CPSR in four columns, with the sampled PC called out separately.

    PC is kept out of the table on purpose: it does not come from the register
    file like the others, and mixing it in would imply the same confidence.
    """
    console = console or Console()
    table = Table(title=f"{core_name} registers")
    for _ in range(4):
        table.add_column("Reg", style="bold")
        table.add_column("Value")

    names = [f"r{n}" for n in range(15)] + (["dspsr"] if "dspsr" in regs.values else [])
    rows = (len(names) + 3) // 4
    for row in range(rows):
        cells: list[str] = []
        for col in range(4):
            i = col * rows + row
            if i < len(names):
                cells += [names[i], f"0x{regs.values[names[i]]:08X}"]
            else:
                cells += ["", ""]
        table.add_row(*cells)
    console.print(table)

    if regs.pc is not None:
        from ..data.r52debug import decode_psr

        line = f"[bold]PC[/bold] 0x{regs.pc:08X}  [dim](DLR)[/dim]"
        dspsr = regs.values.get("dspsr")
        if dspsr is not None:
            psr = decode_psr(dspsr)
            line += f"   mode {psr['mode']}, {'Thumb' if psr['thumb'] else 'ARM'}, flags {psr['flags']}"
        console.print(line)
    if regs.pc_sampled is not None:
        console.print(f"PC (sampled, EDPCSR)  0x{regs.pc_sampled:08X}")
    for note in regs.notes:
        console.print(f"[dim]note:[/dim] {note}")


def print_comparators(title: str, entries: list, console: Console | None = None) -> None:
    """Breakpoint or watchpoint comparators, including the unused ones.

    Empty slots are shown rather than filtered out, because "how many are left"
    is the question that matters when a breakpoint cannot be inserted -- the
    breakpoints guide lists a full comparator bank as the first cause to check.
    """
    console = console or Console()
    table = Table(title=title)
    table.add_column("#", style="bold")
    table.add_column("State")
    table.add_column("Address")
    table.add_column("Detail")

    for entry in entries:
        if entry.get("error"):
            table.add_row(str(entry["index"]), "[red]error[/red]", "-", str(entry["error"]))
            continue
        if entry.get("enabled"):
            detail = (
                f"BAS {entry['bas']}, type {entry['type']}"
                if "bas" in entry
                else f"{entry.get('bytes')} byte(s), on {entry.get('on')}"
            )
            table.add_row(
                str(entry["index"]), "[green]enabled[/green]", str(entry["address"]), detail
            )
        else:
            table.add_row(str(entry["index"]), "[dim]free[/dim]", "-", "")
    console.print(table)

    used = sum(1 for e in entries if e.get("enabled"))
    console.print(f"[dim]{used} of {len(entries)} comparators in use[/dim]")


def r52_decode(midr: int | None) -> dict | None:
    from ..data.r52debug import decode_midr

    return decode_midr(midr) if midr is not None else None


def print_gates(report: GateReport, console: Console | None = None) -> None:
    console = console or Console()

    table = Table(title="MDM-AP (APB debug-enable gate)")
    table.add_column("Register", style="bold")
    table.add_column("Value")
    if report.mdm.idr is not None:
        table.add_row("IDR", f"0x{report.mdm.idr:08X}" + ("" if report.mdm.identity_ok else "  (unexpected)"))
    for name, value in report.mdm.registers.items():
        table.add_row(name, f"0x{value:08X}")
    console.print(table)
    if report.mdm.error:
        console.print(f"[red]MDM-AP:[/red] {report.mdm.error}")
    else:
        flags = report.mdm.status_flags()
        console.print(
            "STATUS: "
            + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in flags.items())
        )
        state = "[green]open[/green]" if report.mdm.enables_set else "[red]gated[/red]"
        console.print(f"Debug-enable gate: {state}")

    hse = Table(title="SDA-AP (HSE security gate, read-only)")
    hse.add_column("Field", style="bold")
    hse.add_column("Value")
    if report.hse.idr is not None:
        hse.add_row("IDR", f"0x{report.hse.idr:08X}")
    if report.hse.authsts is not None:
        hse.add_row("AUTHSTTS", f"0x{report.hse.authsts:08X}")
        hse.add_row("APPDBGEN", "yes" if report.hse.app_debug_enabled else "no")
        hse.add_row("CHALRDY", "yes" if report.hse.challenge_pending else "no")
    if report.hse.sysrsts is not None:
        hse.add_row("SDASYSRSTS", f"0x{report.hse.sysrsts:08X}")
        for k, v in report.hse.reset_flags().items():
            hse.add_row(k, "yes" if v else "no")
    if report.hse.rstctrl is not None:
        hse.add_row("SDARSTCTRL", f"0x{report.hse.rstctrl:08X}")
    if report.hse.uid is not None:
        hse.add_row("UID", f"0x{report.hse.uid:016X}")
    console.print(hse)
    if report.hse.error:
        console.print(f"[red]SDA-AP:[/red] {report.hse.error}")

    for warning in report.warnings:
        console.print(f"[yellow]![/yellow] {warning}")


def print_tap(info: TapInfo, console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="JTAG TAP")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    if info.idcode is not None:
        table.add_row("IDCODE", f"0x{info.idcode:08X}")
        table.add_row("Part", info.part_name or "[yellow]unrecognised[/yellow]")
    if info.ir_length is not None:
        table.add_row("IR length", f"{info.ir_length} ({info.ir_length_method})")
    if info.dr_length_bypass is not None:
        table.add_row("BYPASS DR length", str(info.dr_length_bypass))
    console.print(table)
    for note in info.notes:
        console.print(f"[dim]note:[/dim] {note}")
    for warning in info.warnings:
        console.print(f"[yellow]![/yellow] {warning}")


def print_m33_cores(cores: list, console: Console | None = None) -> None:
    """One row per Cortex-M33 subsystem."""
    console = console or Console()
    table = Table(title="Cortex-M33 subsystems")
    table.add_column("Name", style="bold")
    table.add_column("AP")
    table.add_column("Part")
    table.add_column("BP/WP")
    table.add_column("Debug")
    table.add_column("State")

    for core in cores:
        ident = core.identity
        if ident.error:
            table.add_row(core.name, f"0x{core.ap_base:08X}", "[red]-[/red]", "-", "-", "[red]no answer[/red]")
            continue
        from ..data.m33debug import decode_cpuid

        cpuid = decode_cpuid(ident.cpuid) if ident.cpuid is not None else {}
        counts = (
            f"{ident.breakpoints}/{ident.watchpoints}"
            if ident.breakpoints is not None
            else "-"
        )
        enabled = ident.dhcsr is not None and ident.dhcsr & 0x1
        halted = core.halted
        table.add_row(
            core.name,
            f"0x{core.ap_base:08X}",
            str(cpuid.get("part_name", "-")),
            counts,
            "[green]enabled[/green]" if enabled else "[dim]off[/dim]",
            "[dim]unknown[/dim]" if halted is None
            else ("[yellow]halted[/yellow]" if halted else "[green]running[/green]"),
        )
    console.print(table)
    for core in cores:
        if core.identity.error:
            console.print(f"[red]{core.name}:[/red] {core.identity.error}")


def print_m33_registers(name: str, values: dict, console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title=f"{name} registers")
    for _ in range(4):
        table.add_column("Reg", style="bold")
        table.add_column("Value")
    names = list(values)
    rows = (len(names) + 3) // 4
    for row in range(rows):
        cells: list[str] = []
        for col in range(4):
            i = col * rows + row
            cells += [names[i], f"0x{values[names[i]]:08X}"] if i < len(names) else ["", ""]
        table.add_row(*cells)
    console.print(table)


def print_flash_report(report, console: Console | None = None) -> None:
    """The `flash` group's closing summary: what happened, through what."""
    console = console or Console()
    table = Table(title=f"flash {report.op}")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("offset", f"0x{report.offset:08X}")
    table.add_row("length", f"{report.length:,} bytes")
    if report.mode is not None and report.jedec is not None:
        table.add_row("mode / JEDEC", f"{report.mode}  0x{report.jedec:08X}")
    if report.erase_bytes:
        table.add_row(
            "erased",
            f"{report.erase_bytes:,} bytes in {report.erase_seconds:.2f}s"
            + (
                f" ({report.erase_spans_64k}×64K + {report.erase_spans_4k}×4K spans)"
                if report.erase_spans_64k or report.erase_spans_4k
                else "  (chip erase)"
            ),
        )
    if report.program_runs or report.programmed_bytes:
        skipped = (
            f", {report.skipped_pages} blank pages skipped" if report.skipped_pages else ""
        )
        table.add_row(
            "programmed",
            f"{report.programmed_bytes:,} bytes in {report.program_seconds:.2f}s "
            f"({report.program_runs} run(s){skipped})",
        )
    verify = report.verify
    if report.verify_seconds:
        verify += f" in {report.verify_seconds:.2f}s"
    table.add_row("verify", verify)
    table.add_row(
        "paths",
        f"WriteMem {report.fast_write}, ReadMem {report.fast_read}, "
        f"{report.applet_calls} applet call(s)",
    )
    table.add_row(
        "via", f"AP 0x{report.ap_mem:08X}, AHB window base 0x{report.ahb_base:08X}"
    )
    table.add_row("total", f"{report.seconds:.2f}s")
    console.print(table)
    for note in report.notes:
        console.print(f"[dim]note: {note}[/dim]")


def print_load_report(report, console: Console | None = None) -> None:
    """The `load` command's closing table: what landed where, how, how fast."""
    console = console or Console()
    table = Table(title=f"loaded {report.image}")
    table.add_column("LMA", style="bold")
    table.add_column("Bytes", justify="right")
    table.add_column("Path")
    table.add_column("Verify")
    table.add_column("Time", justify="right")
    for seg in report.segments:
        table.add_row(
            f"0x{seg.paddr:08X}",
            f"{seg.nbytes:,}",
            seg.path,
            seg.verified,
            f"{seg.seconds:.2f}s",
        )
    console.print(table)
    console.print(
        f"total [bold]{report.total_bytes:,}[/bold] bytes in "
        f"{report.seconds:.2f}s ({report.kb_per_s:,.0f} KB/s), "
        f"fast path {report.fast_path}, verify {report.verify}"
    )
    for note in report.notes:
        console.print(f"[dim]note: {note}[/dim]")
    console.print(
        f"[green]rtu0.0 halted at the entry[/green]  "
        f"PC 0x{report.pc:08X}  DSPSR 0x{report.dspsr:08X}"
        + ("  [dim](T bit corrected)[/dim]" if report.dspsr_t_fixed else "")
    )

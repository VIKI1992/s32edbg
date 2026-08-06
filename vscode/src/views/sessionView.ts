import * as vscode from "vscode";
import { backendConfig } from "../config";
import { ElfState } from "../state/elf";
import { DebugState } from "../state/session";

interface Row {
  label: string;
  description?: string;
  icon?: string;
  command?: string;
  tooltip?: string;
}

export class SessionView implements vscode.TreeDataProvider<Row> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(
    private readonly state: DebugState,
    private readonly elf: ElfState,
  ) {
    state.onDidChange(() => this.changeEmitter.fire());
    elf.onDidSetElf(() => this.changeEmitter.fire());
  }

  refresh(): void {
    this.changeEmitter.fire();
  }

  getTreeItem(row: Row): vscode.TreeItem {
    const item = new vscode.TreeItem(row.label);
    item.description = row.description;
    item.tooltip = row.tooltip ?? row.description;
    if (row.icon) {
      item.iconPath = new vscode.ThemeIcon(row.icon);
    }
    if (row.command) {
      item.command = { command: row.command, title: row.label };
    }
    return item;
  }

  getChildren(row?: Row): Row[] {
    if (row) {
      return [];
    }
    const cfg = backendConfig();
    const rows: Row[] = [];
    if (this.state.connected) {
      rows.push({
        label: "Connected",
        description: `backend ${this.state.backendVersion} @ ${cfg.speedKhz} kHz`,
        icon: "plug",
        tooltip:
          "One persistent probe session (each fresh connect pulses the board's " +
          "reset line -- defect D-2 -- so it stays open until you disconnect).",
      });
      rows.push({
        label: "Sync mode",
        description: this.state.sync
          ? "ON -- any halt stops all cores"
          : "off -- halts are per-core",
        icon: "link",
        command: "s32e.toggleSync",
        tooltip:
          "Stop-the-world cross-trigger. Turn ON for software-coupled cores: " +
          "halting one core of a coupled pair otherwise deadlocks the app.",
      });
    } else {
      rows.push({
        label: this.state.connecting ? "Connecting..." : "Disconnected",
        icon: this.state.connecting ? "sync~spin" : "debug-disconnect",
        command: this.state.connecting ? undefined : "s32e.connect",
      });
    }
    rows.push({
      label: "Active ELF",
      description: this.elf.basename ?? "none -- click to choose",
      icon: "file-code",
      command: "s32e.chooseElf",
      tooltip: this.elf.active ?? undefined,
    });
    if (this.state.connected) {
      rows.push({
        label: "Load ELF to RAM",
        description: "resets the whole SoC",
        icon: "desktop-download",
        command: "s32e.loadElf",
        tooltip:
          "Writes the active ELF into RTU0 RAM and parks rtu0.0 halted at " +
          "its entry. Resets the whole SoC first (~35 s at 16 MHz).",
      });
    } else {
      rows.push({
        label: "Diagnostics: Doctor",
        description: "board diagnosis",
        icon: "pulse",
        command: "s32e.doctor",
      });
      rows.push({
        label: "Diagnostics: Probe info",
        icon: "search",
        command: "s32e.probe",
      });
      rows.push({
        label: "Diagnostics: Full report",
        icon: "notebook",
        command: "s32e.report",
      });
    }
    rows.push({
      label: "Output log",
      icon: "output",
      command: "s32e.showOutput",
    });
    return rows;
  }
}

import * as vscode from "vscode";
import { ElfState } from "../state/elf";
import { DebugState } from "../state/session";

/** Three always-available items: connection, focused core, active ELF. */
export class StatusBar implements vscode.Disposable {
  private readonly connection: vscode.StatusBarItem;
  private readonly core: vscode.StatusBarItem;
  private readonly elf: vscode.StatusBarItem;

  constructor(
    private readonly state: DebugState,
    private readonly elfState: ElfState,
  ) {
    this.connection = vscode.window.createStatusBarItem(
      "s32e.connection", vscode.StatusBarAlignment.Left, 102,
    );
    this.core = vscode.window.createStatusBarItem(
      "s32e.focusCore", vscode.StatusBarAlignment.Left, 101,
    );
    this.elf = vscode.window.createStatusBarItem(
      "s32e.elf", vscode.StatusBarAlignment.Left, 100,
    );
    this.connection.name = "S32E connection";
    this.core.name = "S32E focused core";
    this.elf.name = "S32E active ELF";
    this.connection.command = "s32e.connectOrDisconnect";
    this.core.command = "s32e.pickFocusCore";
    this.elf.command = "s32e.chooseElf";
    this.refresh();
    this.connection.show();
    this.elf.show();
  }

  refresh(): void {
    if (this.state.connecting) {
      this.connection.text = "$(sync~spin) S32E";
      this.connection.tooltip = "Connecting to the board...";
    } else if (this.state.connected) {
      this.connection.text = "$(plug) S32E";
      this.connection.tooltip = "S32E Debug: connected (click to disconnect)";
    } else {
      this.connection.text = "$(debug-disconnect) S32E";
      this.connection.tooltip = "S32E Debug: disconnected (click to connect)";
    }

    const focus = this.state.focusInfo;
    if (this.state.connected && this.state.focus) {
      const icon =
        focus?.halted === true
          ? "$(debug-pause)"
          : focus?.halted === false
            ? "$(play)"
            : "$(question)";
      this.core.text = `$(record) ${this.state.focus} ${icon}`;
      this.core.tooltip =
        `Focused core: ${this.state.focus} (${focus?.detail ?? "?"}).\n` +
        "Gutter breakpoints arm on this core. Click to switch.";
      this.core.show();
    } else {
      this.core.hide();
    }

    const base = this.elfState.basename;
    this.elf.text = base ? `$(file-code) ${base}` : "$(file-code) no ELF";
    this.elf.tooltip = this.elfState.active
      ? `Active ELF: ${this.elfState.active}\nDrives symbols, breakpoints, disassembly and Load to RAM. Click to change.`
      : "No active ELF -- click to choose (symbols, breakpoints, Load to RAM)";
  }

  dispose(): void {
    this.connection.dispose();
    this.core.dispose();
    this.elf.dispose();
  }
}

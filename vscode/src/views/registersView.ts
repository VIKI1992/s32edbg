import * as vscode from "vscode";
import { DebugState } from "../state/session";

interface RegNode {
  name: string;
  value: string;
  changed: boolean;
}

export class RegistersView implements vscode.TreeDataProvider<RegNode> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(private readonly state: DebugState) {
    state.onDidChange(() => this.changeEmitter.fire());
  }

  getTreeItem(node: RegNode): vscode.TreeItem {
    const item = new vscode.TreeItem(node.name);
    item.description = node.value + (node.changed ? "  ●" : "");
    item.tooltip = node.changed
      ? `${node.name} = ${node.value} (changed since the previous halt)`
      : `${node.name} = ${node.value}`;
    item.contextValue = "register";
    item.iconPath = node.changed
      ? new vscode.ThemeIcon("circle-filled", new vscode.ThemeColor("charts.yellow"))
      : new vscode.ThemeIcon("symbol-variable");
    item.command = {
      command: "s32e.editRegister",
      title: "Edit",
      arguments: [{ regName: node.name }],
    };
    return item;
  }

  getChildren(node?: RegNode): RegNode[] {
    if (node || !this.state.connected || !this.state.focus) {
      return [];
    }
    const regs = this.state.lastRegs.get(this.state.focus);
    if (!regs) {
      return [];
    }
    const prev = this.state.prevRegs.get(this.state.focus) ?? {};
    return Object.entries(regs).map(([name, value]) => ({
      name,
      value,
      changed: prev[name] !== undefined && prev[name] !== value,
    }));
  }
}

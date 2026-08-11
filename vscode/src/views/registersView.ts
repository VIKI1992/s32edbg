import * as vscode from "vscode";
import { DebugState } from "../state/session";
import {
  AutoRefreshNode,
  autoRefreshItem,
  isAutoRefreshNode,
  ViewPolling,
} from "../state/viewPolling";

interface RegNode {
  name: string;
  value: string;
  changed: boolean;
}

type Node = RegNode | AutoRefreshNode;

export class RegistersView implements vscode.TreeDataProvider<Node> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(
    private readonly state: DebugState,
    private readonly polling: ViewPolling,
  ) {
    state.onDidChange(() => this.changeEmitter.fire());
    polling.onDidChange((view) => {
      if (view === "registers") {
        this.changeEmitter.fire();
      }
    });
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if (isAutoRefreshNode(node)) {
      return autoRefreshItem("registers", this.polling.on("registers"));
    }
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

  getChildren(node?: Node): Node[] {
    if (node || !this.state.connected || !this.state.focus) {
      return [];
    }
    const toggle: Node = { autoRefresh: "registers" };
    const regs = this.state.lastRegs.get(this.state.focus);
    if (!regs) {
      return [toggle];
    }
    const prev = this.state.prevRegs.get(this.state.focus) ?? {};
    return [
      toggle,
      ...Object.entries(regs).map(([name, value]) => ({
        name,
        value,
        changed: prev[name] !== undefined && prev[name] !== value,
      })),
    ];
  }
}

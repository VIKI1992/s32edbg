import * as vscode from "vscode";
import { toHex } from "../backend/types";
import { MemProvider, MemView, memUri } from "../editor/memProvider";
import { DebugState } from "../state/session";
import {
  AutoRefreshNode,
  autoRefreshItem,
  isAutoRefreshNode,
  ViewPolling,
} from "../state/viewPolling";

type Node = MemView | { hint: true } | AutoRefreshNode;

export class MemoryView implements vscode.TreeDataProvider<Node> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(
    private readonly provider: MemProvider,
    private readonly state: DebugState,
    private readonly polling: ViewPolling,
  ) {
    provider.onDidChange(() => this.changeEmitter.fire());
    state.onDidChange(() => this.changeEmitter.fire());
    polling.onDidChange((view) => {
      if (view === "memory") {
        this.changeEmitter.fire();
      }
    });
  }

  refresh(): void {
    this.changeEmitter.fire();
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if (isAutoRefreshNode(node)) {
      return autoRefreshItem("memory", this.polling.on("memory"));
    }
    if ("hint" in node) {
      const item = new vscode.TreeItem("Read memory...");
      item.iconPath = new vscode.ThemeIcon("add");
      item.command = { command: "s32e.readMemory", title: "Read memory" };
      return item;
    }
    const item = new vscode.TreeItem(toHex(node.address));
    item.description = `${node.length} B via ${node.ap}`;
    item.iconPath = new vscode.ThemeIcon("file-binary");
    item.command = {
      command: "vscode.open",
      title: "Open",
      arguments: [memUri(node)],
    };
    item.contextValue = "memregion";
    return item;
  }

  getChildren(node?: Node): Node[] {
    if (node) {
      return [];
    }
    const toggle: Node[] = this.state.connected
      ? [{ autoRefresh: "memory" }]
      : [];
    return [...toggle, ...this.provider.open, { hint: true }];
  }
}

import * as vscode from "vscode";
import { toHex } from "../backend/types";
import { S32Watchpoint, WatchpointStore } from "../state/watchpoints";

export class WatchpointsView implements vscode.TreeDataProvider<S32Watchpoint> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(private readonly store: WatchpointStore) {
    store.onDidChange(() => this.changeEmitter.fire());
  }

  getTreeItem(wp: S32Watchpoint): vscode.TreeItem {
    const item = new vscode.TreeItem(toHex(wp.address));
    item.description = `${wp.core}  ${wp.size}B ${wp.on}  ${
      wp.state === "armed"
        ? `slot ${wp.slot}`
        : wp.state === "failed"
          ? "failed"
          : "pending"
    }`;
    item.tooltip =
      `watch ${wp.size} byte(s) at ${toHex(wp.address)} for ${wp.on} on ${wp.core}\n` +
      (wp.state === "failed"
        ? `FAILED: ${wp.error}`
        : wp.state === "armed"
          ? `armed in comparator ${wp.slot}. A hit halts on the exact access; the faulting address shows in the Cores view.`
          : "pending");
    item.iconPath =
      wp.state === "armed"
        ? new vscode.ThemeIcon("eye", new vscode.ThemeColor("charts.red"))
        : wp.state === "failed"
          ? new vscode.ThemeIcon("error", new vscode.ThemeColor("charts.red"))
          : new vscode.ThemeIcon("eye-closed");
    item.contextValue = "watchpoint";
    return item;
  }

  getChildren(node?: S32Watchpoint): S32Watchpoint[] {
    return node ? [] : this.store.all;
  }
}

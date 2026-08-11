import * as vscode from "vscode";
import { toHex } from "../backend/types";
import { ComparatorReconciler, RowVerdict } from "../state/comparators";
import {
  AutoRefreshNode,
  autoRefreshItem,
  isAutoRefreshNode,
  ViewPolling,
} from "../state/viewPolling";
import { S32Watchpoint, WatchpointStore } from "../state/watchpoints";

type Node =
  | { kind: "wp"; wp: S32Watchpoint }
  | { kind: "orphan"; unit: "wp"; core: string; index: number; address: string }
  | AutoRefreshNode;

export class WatchpointsView implements vscode.TreeDataProvider<Node> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(
    private readonly store: WatchpointStore,
    private readonly polling: ViewPolling,
    private readonly comparators: ComparatorReconciler,
  ) {
    store.onDidChange(() => this.changeEmitter.fire());
    comparators.onDidChange(() => this.changeEmitter.fire());
    polling.onDidChange((view) => {
      if (view === "watchpoints") {
        this.changeEmitter.fire();
      }
    });
  }

  private verdictFor(wp: S32Watchpoint): RowVerdict | undefined {
    return this.comparators.results.get(`${wp.core}#wp`)?.rows.get(wp.id);
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if (isAutoRefreshNode(node)) {
      return autoRefreshItem("watchpoints", this.polling.on("watchpoints"));
    }
    if (node.kind === "orphan") {
      const item = new vscode.TreeItem(`slot ${node.index}  ${node.address}`);
      item.description = `${node.core}  armed in hardware — not set by this session`;
      item.tooltip =
        `Hardware watchpoint comparator ${node.index} on ${node.core} is ` +
        `enabled at ${node.address}, but no watchpoint in this session ` +
        "claims it. Right-click to clear it.";
      item.iconPath = new vscode.ThemeIcon(
        "warning", new vscode.ThemeColor("charts.orange"),
      );
      item.contextValue = "hwOrphanWp";
      return item;
    }
    const wp = node.wp;
    const verdict = this.verdictFor(wp);
    const item = new vscode.TreeItem(toHex(wp.address));
    const hwBit =
      verdict?.verdict === "verified"
        ? "  ✓ hw"
        : verdict?.verdict === "mismatch"
          ? `  ⚠ hw: ${verdict.reason}`
          : verdict?.verdict === "unknown"
            ? "  hw: unreadable"
            : "";
    item.description = `${wp.core}  ${wp.size}B ${wp.on}  ${
      wp.state === "armed"
        ? `slot ${wp.slot}`
        : wp.state === "failed"
          ? "failed"
          : "pending"
    }${hwBit}`;
    item.tooltip =
      `watch ${wp.size} byte(s) at ${toHex(wp.address)} for ${wp.on} on ${wp.core}\n` +
      (wp.state === "failed"
        ? `FAILED: ${wp.error}`
        : wp.state === "armed"
          ? `armed in comparator ${wp.slot}. A hit halts on the exact access; the faulting address shows in the Cores view.`
          : "pending") +
      (verdict ? `\nhardware check: ${verdict.reason}` : "");
    item.iconPath =
      verdict?.verdict === "mismatch"
        ? new vscode.ThemeIcon("warning", new vscode.ThemeColor("charts.yellow"))
        : wp.state === "armed"
          ? new vscode.ThemeIcon("eye", new vscode.ThemeColor("charts.red"))
          : wp.state === "failed"
            ? new vscode.ThemeIcon("error", new vscode.ThemeColor("charts.red"))
            : new vscode.ThemeIcon("eye-closed");
    item.contextValue = "watchpoint";
    return item;
  }

  getChildren(node?: Node): Node[] {
    if (node) {
      return [];
    }
    const rows: Node[] = [
      { autoRefresh: "watchpoints" },
      ...this.store.all.map((wp): Node => ({ kind: "wp", wp })),
    ];
    for (const [key, verdicts] of this.comparators.results) {
      const [core, kind] = key.split("#");
      if (kind === "wp") {
        for (const orphan of verdicts.orphans) {
          rows.push({ kind: "orphan", unit: "wp", core, ...orphan });
        }
      }
    }
    return rows;
  }
}

import * as path from "path";
import * as vscode from "vscode";
import { toHex } from "../backend/types";
import { BreakpointStore, S32Breakpoint } from "../state/breakpoints";
import { ComparatorReconciler, RowVerdict } from "../state/comparators";
import {
  AutoRefreshNode,
  autoRefreshItem,
  isAutoRefreshNode,
  ViewPolling,
} from "../state/viewPolling";

type Node =
  | { kind: "group"; core: string }
  | { kind: "bp"; bp: S32Breakpoint }
  | { kind: "orphan"; unit: "bp"; core: string; index: number; address: string }
  | AutoRefreshNode;

export class BreakpointsView implements vscode.TreeDataProvider<Node> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(
    private readonly store: BreakpointStore,
    private readonly polling: ViewPolling,
    private readonly comparators: ComparatorReconciler,
  ) {
    store.onDidChange(() => this.changeEmitter.fire());
    comparators.onDidChange(() => this.changeEmitter.fire());
    polling.onDidChange((view) => {
      if (view === "breakpoints") {
        this.changeEmitter.fire();
      }
    });
  }

  private verdictFor(bp: S32Breakpoint): RowVerdict | undefined {
    return this.comparators.results.get(`${bp.core}#bp`)?.rows.get(bp.id);
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if (isAutoRefreshNode(node)) {
      return autoRefreshItem("breakpoints", this.polling.on("breakpoints"));
    }
    if (node.kind === "group") {
      const item = new vscode.TreeItem(
        node.core,
        vscode.TreeItemCollapsibleState.Expanded,
      );
      item.iconPath = new vscode.ThemeIcon("chip");
      item.contextValue = "bpgroup";
      return item;
    }
    if (node.kind === "orphan") {
      const item = new vscode.TreeItem(`slot ${node.index}  ${node.address}`);
      item.description = "armed in hardware — not set by this session";
      item.tooltip =
        `Hardware breakpoint comparator ${node.index} on ${node.core} is ` +
        `enabled at ${node.address}, but no breakpoint in this session ` +
        "claims it (a leftover from another tool or session). " +
        "Right-click to clear it.";
      item.iconPath = new vscode.ThemeIcon(
        "warning", new vscode.ThemeColor("charts.orange"),
      );
      item.contextValue = "hwOrphanBp";
      return item;
    }
    const bp = node.bp;
    const where = bp.source
      ? `${path.basename(bp.source.fsPath)}:${bp.source.line}`
      : bp.address !== null
        ? toHex(bp.address)
        : "?";
    const item = new vscode.TreeItem(where);
    const bits: string[] = [];
    if (bp.address !== null && bp.source) {
      bits.push(toHex(bp.address));
    }
    bits.push(
      bp.state === "armed"
        ? `slot ${bp.slot}`
        : bp.state === "failed"
          ? `failed`
          : "pending",
    );
    const verdict = this.verdictFor(bp);
    if (verdict?.verdict === "verified") {
      bits.push("✓ hw");
    } else if (verdict?.verdict === "mismatch") {
      bits.push(`⚠ hw: ${verdict.reason}`);
    } else if (verdict?.verdict === "unknown") {
      bits.push("hw: unreadable");
    }
    if (bp.adopted) {
      bits.push("adopted");
    }
    item.description = bits.join("  ");
    item.tooltip =
      `${where} on ${bp.core} (${bp.origin})\n` +
      (bp.adopted
        ? "ADOPTED: found armed in hardware, not set by this session -- " +
          "delete it like any breakpoint to clear the comparator\n"
        : "") +
      (bp.address !== null ? `address ${toHex(bp.address)}\n` : "") +
      (bp.state === "armed"
        ? `armed in hardware comparator ${bp.slot}`
        : bp.state === "failed"
          ? `FAILED: ${bp.error}`
          : "pending -- armed on next connect/symbols") +
      (verdict ? `\nhardware check: ${verdict.reason}` : "");
    item.iconPath =
      verdict?.verdict === "mismatch"
        ? new vscode.ThemeIcon("warning", new vscode.ThemeColor("charts.yellow"))
        : bp.state === "armed"
          ? new vscode.ThemeIcon("circle-filled", new vscode.ThemeColor("debugIcon.breakpointForeground"))
          : bp.state === "failed"
            ? new vscode.ThemeIcon("error", new vscode.ThemeColor("charts.red"))
            : new vscode.ThemeIcon("circle-outline");
    item.contextValue = "breakpoint";
    if (bp.source) {
      item.command = {
        command: "s32e.openFrameSource",
        title: "Open",
        arguments: [bp.source.fsPath, bp.source.line],
      };
    }
    return item;
  }

  getChildren(node?: Node): Node[] {
    if (!node) {
      const cores = [...new Set(this.store.all.map((b) => b.core))];
      // Orphan slots can exist on a core with no local rows (the focus is
      // always swept); its group must still show.
      for (const key of this.comparators.results.keys()) {
        const [core, kind] = key.split("#");
        if (kind === "bp" && this.comparators.results.get(key)!.orphans.length) {
          cores.push(core);
        }
      }
      const groups: Node[] = [...new Set(cores)]
        .sort()
        .map((core) => ({ kind: "group", core }));
      return [{ autoRefresh: "breakpoints" }, ...groups];
    }
    if (!isAutoRefreshNode(node) && node.kind === "group") {
      const rows: Node[] = this.store.all
        .filter((b) => b.core === node.core)
        .map((bp): Node => ({ kind: "bp", bp }));
      const orphans =
        this.comparators.results.get(`${node.core}#bp`)?.orphans ?? [];
      for (const orphan of orphans) {
        rows.push({ kind: "orphan", unit: "bp", core: node.core, ...orphan });
      }
      return rows;
    }
    return [];
  }
}

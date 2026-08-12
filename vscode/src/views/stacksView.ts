import * as vscode from "vscode";
import { StackScanRow } from "../backend/types";
import { DebugState } from "../state/session";
import { StacksState } from "../state/stacks";

type Node = StackScanRow | { hint: true } | { header: true };

/** Ten-cell text bar -- the tree has no real progress widget. */
export function stackBar(pct: number | null): string {
  if (pct === null) {
    return "";
  }
  const filled = Math.round(Math.min(100, Math.max(0, pct)) / 10);
  return "▰".repeat(filled) + "▱".repeat(10 - filled);
}

export function stackSeverity(
  row: StackScanRow,
): "error" | "warning" | "ok" | "unknown" {
  if (row.error !== null || row.pct === null) {
    return "unknown";
  }
  if (row.overflow === true || row.pct >= 95) {
    return "error";
  }
  if (row.pct >= 80) {
    return "warning";
  }
  return "ok";
}

export function stackDescription(row: StackScanRow): string {
  if (row.error !== null) {
    return row.error;
  }
  if (row.pct === null) {
    return "";
  }
  const over = row.overflow ? "  OVERFLOW" : "";
  return (
    `${stackBar(row.pct)} ${row.pct.toFixed(1)}%  ` +
    `${row.used}/${row.size} B${over}`
  );
}

export function stackTooltip(row: StackScanRow): string {
  const lines = [`${row.name}${row.type ? `  (${row.type})` : ""}`];
  lines.push(`core: ${row.core ?? "?"}`);
  if (row.users.length > 0) {
    lines.push(`users: ${row.users.join(", ")}`);
  }
  if (row.address !== null) {
    lines.push(`range: ${row.address} + ${row.size} B`);
  }
  if (row.watermark !== null) {
    lines.push(`high-water mark: ${row.watermark}`);
  }
  if (row.used !== null) {
    lines.push(`used ${row.used} B, free ${row.free} B (${row.pct}%)`);
  }
  if (row.overflow === true) {
    lines.push("OVERFLOW: the canary word no longer holds the fill pattern");
  }
  if (row.note !== null) {
    lines.push(`note: ${row.note}`);
  }
  if (row.error !== null) {
    lines.push(`error: ${row.error}`);
  }
  return lines.join("\n");
}

/** Refresh-button-only stack utilization: no polling, no probe traffic on
 * its own. Rows come from the last `stacks scan`; the header names the
 * ARXML the numbers are configured by. */
export class StacksView implements vscode.TreeDataProvider<Node> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(state: DebugState, private readonly stacks: StacksState) {
    stacks.onDidChange(() => this.changeEmitter.fire());
    state.onDidChange(() => this.changeEmitter.fire());
  }

  refresh(): void {
    this.changeEmitter.fire();
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if ("hint" in node) {
      const item = new vscode.TreeItem("Select project (ARXML stack config)…");
      item.iconPath = new vscode.ThemeIcon("folder-opened");
      item.command = {
        command: "s32e.stacks.selectProject",
        title: "Select project",
      };
      return item;
    }
    if ("header" in node) {
      const item = new vscode.TreeItem(this.stacks.arxmlBasename ?? "");
      item.description = this.stacks.scanning
        ? "scanning…"
        : this.stacks.scannedAt
          ? `scanned ${this.stacks.scannedAt.toLocaleTimeString()}`
          : "not scanned yet — press ⟳";
      item.iconPath = new vscode.ThemeIcon("file-code");
      item.tooltip = this.stacks.arxml ?? "";
      item.contextValue = "stacksHeader";
      return item;
    }
    const item = new vscode.TreeItem(node.name);
    item.description = stackDescription(node);
    const severity = stackSeverity(node);
    item.iconPath =
      severity === "error"
        ? new vscode.ThemeIcon(
            "error", new vscode.ThemeColor("errorForeground"),
          )
        : severity === "warning"
          ? new vscode.ThemeIcon(
              "warning", new vscode.ThemeColor("editorWarning.foreground"),
            )
          : severity === "unknown"
            ? new vscode.ThemeIcon("question")
            : new vscode.ThemeIcon("pass");
    item.tooltip = stackTooltip(node);
    item.contextValue = "stackRow";
    return item;
  }

  getChildren(node?: Node): Node[] {
    if (node) {
      return [];
    }
    if (this.stacks.arxml === null) {
      return [{ hint: true }];
    }
    const rows = [...this.stacks.rows].sort(
      (a, b) =>
        (a.core ?? "").localeCompare(b.core ?? "") ||
        a.name.localeCompare(b.name),
    );
    return [{ header: true }, ...rows];
  }
}

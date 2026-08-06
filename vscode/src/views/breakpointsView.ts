import * as path from "path";
import * as vscode from "vscode";
import { toHex } from "../backend/types";
import { BreakpointStore, S32Breakpoint } from "../state/breakpoints";

type Node = { kind: "group"; core: string } | { kind: "bp"; bp: S32Breakpoint };

export class BreakpointsView implements vscode.TreeDataProvider<Node> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(private readonly store: BreakpointStore) {
    store.onDidChange(() => this.changeEmitter.fire());
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if (node.kind === "group") {
      const item = new vscode.TreeItem(
        node.core,
        vscode.TreeItemCollapsibleState.Expanded,
      );
      item.iconPath = new vscode.ThemeIcon("chip");
      item.contextValue = "bpgroup";
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
    item.description = bits.join("  ");
    item.tooltip =
      `${where} on ${bp.core} (${bp.origin})\n` +
      (bp.address !== null ? `address ${toHex(bp.address)}\n` : "") +
      (bp.state === "armed"
        ? `armed in hardware comparator ${bp.slot}`
        : bp.state === "failed"
          ? `FAILED: ${bp.error}`
          : "pending -- armed on next connect/symbols");
    item.iconPath =
      bp.state === "armed"
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
      const cores = [...new Set(this.store.all.map((b) => b.core))].sort();
      return cores.map((core) => ({ kind: "group", core }));
    }
    if (node.kind === "group") {
      return this.store.all
        .filter((b) => b.core === node.core)
        .map((bp) => ({ kind: "bp", bp }));
    }
    return [];
  }
}

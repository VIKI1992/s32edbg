import * as vscode from "vscode";
import { hexToInt, StackFrame } from "../backend/types";
import { DebugState } from "../state/session";

type Node =
  | { kind: "frame"; frame: StackFrame }
  | { kind: "raw" }
  | { kind: "rawline"; text: string }
  | { kind: "note"; text: string };

export class StackView implements vscode.TreeDataProvider<Node> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;
  rawLines: string[] = [];
  /** Set by commands.ts: fetches the raw SP window lazily. */
  fetchRaw: (() => Promise<string[]>) | null = null;

  constructor(private readonly state: DebugState) {
    state.onDidChange(() => this.changeEmitter.fire());
  }

  refresh(): void {
    this.rawLines = [];
    this.changeEmitter.fire();
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if (node.kind === "frame") {
      const f = node.frame;
      const item = new vscode.TreeItem(
        `#${f.index}  ${f.func ?? "?"}`,
        vscode.TreeItemCollapsibleState.None,
      );
      item.description = `${f.pc}${f.file ? `  ${basename(f.file)}:${f.line}` : ""}  (${f.via})`;
      item.tooltip = `${f.pc}\n${f.file ?? "no source info"}${f.line ? ":" + f.line : ""}\nsp ${f.sp ?? "?"}  fp ${f.fp ?? "?"}\nunwound via ${f.via}`;
      item.iconPath = new vscode.ThemeIcon(
        f.index === 0 ? "debug-stackframe-focused" : "debug-stackframe",
      );
      if (f.file && f.line) {
        item.command = {
          command: "s32e.openFrameSource",
          title: "Open source",
          arguments: [f.file, f.line],
        };
      }
      return item;
    }
    if (node.kind === "raw") {
      const item = new vscode.TreeItem(
        "Raw stack memory @ SP",
        vscode.TreeItemCollapsibleState.Collapsed,
      );
      item.iconPath = new vscode.ThemeIcon("file-binary");
      return item;
    }
    if (node.kind === "rawline") {
      const item = new vscode.TreeItem(node.text);
      return item;
    }
    const item = new vscode.TreeItem(node.text);
    item.iconPath = new vscode.ThemeIcon("info");
    return item;
  }

  async getChildren(node?: Node): Promise<Node[]> {
    if (node?.kind === "raw") {
      if (this.rawLines.length === 0 && this.fetchRaw) {
        try {
          this.rawLines = await this.fetchRaw();
        } catch (err) {
          this.rawLines = [`could not read stack memory: ${err}`];
        }
      }
      return this.rawLines.map((text) => ({ kind: "rawline", text }));
    }
    if (node) {
      return [];
    }
    const focus = this.state.focusInfo;
    if (!this.state.connected || !focus) {
      return [];
    }
    if (focus.halted !== true) {
      return [{ kind: "note", text: `${focus.name} is running -- halt to see the stack` }];
    }
    const nodes: Node[] = this.state.lastStack.map((frame) => ({
      kind: "frame",
      frame,
    }));
    if (nodes.length > 0) {
      nodes.push({ kind: "note", text: `unwind stopped: ${this.state.lastStackReason}` });
      const sp = hexToInt(this.state.lastStack[0]?.sp ?? null);
      if (sp !== null) {
        nodes.push({ kind: "raw" });
      }
    }
    return nodes;
  }
}

function basename(p: string): string {
  return p.split(/[\\/]/).pop() ?? p;
}

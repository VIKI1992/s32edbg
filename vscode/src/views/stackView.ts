import * as vscode from "vscode";
import { hexToInt, StackFrame } from "../backend/types";
import { DebugState } from "../state/session";
import { VariablesState } from "../state/variables";
import {
  AutoRefreshNode,
  autoRefreshItem,
  isAutoRefreshNode,
  ViewPolling,
} from "../state/viewPolling";

type Node =
  | { kind: "frame"; frame: StackFrame }
  | { kind: "raw" }
  | { kind: "rawline"; text: string }
  | { kind: "note"; text: string }
  | AutoRefreshNode;

export class StackView implements vscode.TreeDataProvider<Node> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;
  rawLines: string[] = [];
  /** Set by commands.ts: fetches the raw SP window lazily. */
  fetchRaw: (() => Promise<string[]>) | null = null;

  constructor(
    private readonly state: DebugState,
    private readonly vars: VariablesState,
    private readonly polling: ViewPolling,
  ) {
    state.onDidChange(() => this.changeEmitter.fire());
    vars.onDidChange(() => this.changeEmitter.fire());
    polling.onDidChange((view) => {
      if (view === "callStack") {
        this.changeEmitter.fire();
      }
    });
  }

  refresh(): void {
    this.rawLines = [];
    this.changeEmitter.fire();
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if (isAutoRefreshNode(node)) {
      return autoRefreshItem("callStack", this.polling.on("callStack"));
    }
    if (node.kind === "frame") {
      const f = node.frame;
      const item = new vscode.TreeItem(
        `#${f.index}  ${f.func ?? "?"}`,
        vscode.TreeItemCollapsibleState.None,
      );
      item.description = `${f.pc}${f.file ? `  ${basename(f.file)}:${f.line}` : ""}  (${f.via})`;
      item.tooltip =
        `${f.pc}\n${f.file ?? "no source info"}${f.line ? ":" + f.line : ""}\n` +
        `sp ${f.sp ?? "?"}  fp ${f.fp ?? "?"}\nunwound via ${f.via}\n` +
        "click: select this frame (Variables follow) and open its source";
      item.iconPath = new vscode.ThemeIcon(
        f.index === this.vars.selectedFrame
          ? "debug-stackframe-focused"
          : "debug-stackframe",
      );
      item.command = {
        command: "s32e.selectFrame",
        title: "Select frame",
        arguments: [f.index, f.file, f.line],
      };
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
    if (node && isAutoRefreshNode(node)) {
      return [];
    }
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
      return [
        { autoRefresh: "callStack" },
        { kind: "note", text: `${focus.name} is running -- halt to see the stack` },
      ];
    }
    const nodes: Node[] = [
      { autoRefresh: "callStack" },
      ...this.state.lastStack.map(
        (frame): Node => ({ kind: "frame", frame }),
      ),
    ];
    if (this.state.lastStack.length > 0) {
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

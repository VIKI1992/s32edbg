import * as vscode from "vscode";
import { VarRow } from "../backend/types";
import { DebugState } from "../state/session";
import { VariablesState } from "../state/variables";
import {
  AutoRefreshNode,
  autoRefreshItem,
  isAutoRefreshNode,
  ViewPolling,
} from "../state/viewPolling";
import { isEditable, rowDescription, rowKey } from "./variablesFormat";

export type VarNode =
  | { kind: "section"; id: "locals" | "globals" | "watch" }
  | { kind: "row"; row: VarRow; section: string; watchPath?: string }
  | { kind: "more"; parentHandle: string; from: number; section: string }
  | { kind: "note"; text: string }
  | AutoRefreshNode;

export class VariablesView implements vscode.TreeDataProvider<VarNode> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(
    private readonly state: DebugState,
    private readonly vars: VariablesState,
    private readonly polling: ViewPolling,
  ) {
    state.onDidChange(() => this.changeEmitter.fire());
    vars.onDidChange(() => this.changeEmitter.fire());
    polling.onDidChange((view) => {
      if (view === "variables") {
        this.changeEmitter.fire();
      }
    });
  }

  getTreeItem(node: VarNode): vscode.TreeItem {
    if (isAutoRefreshNode(node)) {
      return autoRefreshItem("variables", this.polling.on("variables"));
    }
    if (node.kind === "section") {
      return this.sectionItem(node.id);
    }
    if (node.kind === "note") {
      const item = new vscode.TreeItem(node.text);
      item.iconPath = new vscode.ThemeIcon("info");
      return item;
    }
    if (node.kind === "more") {
      const item = new vscode.TreeItem(`more from [${node.from}]…`);
      item.iconPath = new vscode.ThemeIcon("ellipsis");
      item.command = {
        command: "s32e.expandMoreChildren",
        title: "More",
        arguments: [node],
      };
      return item;
    }
    return this.rowItem(node);
  }

  private sectionItem(id: "locals" | "globals" | "watch"): vscode.TreeItem {
    let label: string;
    let expanded = vscode.TreeItemCollapsibleState.Expanded;
    if (id === "locals") {
      const func = this.vars.localsFunc ? ` (${this.vars.localsFunc})` : "";
      const approx = this.vars.cfaSource === "fp" ? " · frame base ~fp" : "";
      label = `Locals — frame #${this.vars.selectedFrame}${func}${approx}`;
    } else if (id === "globals") {
      const filter = this.vars.globalsFilter
        ? ` (filter: ${this.vars.globalsFilter})`
        : "";
      label = this.vars.globalsOpened
        ? `Globals — ${this.vars.globals.length} of ${this.vars.globalsTotal}${filter}`
        : `Globals${filter}`;
      // A production image carries tens of thousands: closed until asked.
      expanded = this.vars.globalsOpened
        ? vscode.TreeItemCollapsibleState.Expanded
        : vscode.TreeItemCollapsibleState.Collapsed;
    } else {
      label = `Watch — ${this.vars.watches.length}`;
    }
    const item = new vscode.TreeItem(label, expanded);
    item.id = `s32e-vars-${id}`;
    item.contextValue = `varSection ${id}`;
    item.iconPath = new vscode.ThemeIcon(
      id === "locals" ? "symbol-namespace" : id === "globals" ? "globe" : "eye",
    );
    return item;
  }

  private rowItem(node: { row: VarRow; section: string; watchPath?: string }): vscode.TreeItem {
    const row = node.row;
    const expandable = row.expandable && row.handle !== null;
    const item = new vscode.TreeItem(
      row.name,
      expandable
        ? vscode.TreeItemCollapsibleState.Collapsed
        : vscode.TreeItemCollapsibleState.None,
    );
    const changed =
      row.value !== null &&
      this.wasChanged(node.section, row);
    item.description =
      rowDescription(row, this.vars.hexMode) + (changed ? "  ●" : "");
    item.tooltip = new vscode.MarkdownString(
      [
        `**${row.path}** — \`${row.type}\``,
        row.address ? `address ${row.address}` : null,
        row.register ? `register ${row.register}` : null,
        row.value ? `value ${row.value.display} = ${row.value.hex}` : null,
        row.static ? "`static`" : null,
        row.error ? `⚠ ${row.error}` : null,
        changed ? "_changed since the previous halt_" : null,
      ]
        .filter(Boolean)
        .join("\n\n"),
    );
    item.iconPath = row.error
      ? new vscode.ThemeIcon("warning", new vscode.ThemeColor("charts.yellow"))
      : changed
        ? new vscode.ThemeIcon("circle-filled", new vscode.ThemeColor("charts.yellow"))
        : new vscode.ThemeIcon("symbol-field");
    const ctx: string[] = ["variable"];
    if (isEditable(row)) {
      ctx.push("editable");
      item.command = {
        command: "s32e.editVariable",
        title: "Edit",
        arguments: [node],
      };
    }
    if (node.watchPath !== undefined) {
      ctx.push("watchRoot");
    }
    item.contextValue = ctx.join(" ");
    return item;
  }

  private changedKeys = new Map<string, boolean>();

  private wasChanged(section: string, row: VarRow): boolean {
    // vars.changed() records values as fetches happen; here we only read the
    // verdict it computed, caching per render pass to keep getTreeItem cheap.
    const key = rowKey(section, row.path);
    let verdict = this.changedKeys.get(key);
    if (verdict === undefined) {
      verdict = this.vars.changed(section, row);
      this.changedKeys.set(key, verdict);
    }
    return verdict;
  }

  async getChildren(node?: VarNode): Promise<VarNode[]> {
    this.changedKeys.clear();
    if (!node) {
      if (!this.state.connected) {
        return [];
      }
      return [
        { autoRefresh: "variables" },
        { kind: "section", id: "locals" },
        { kind: "section", id: "globals" },
        { kind: "section", id: "watch" },
      ];
    }
    if (isAutoRefreshNode(node)) {
      return [];
    }
    if (node.kind === "section") {
      return this.sectionChildren(node.id);
    }
    if (node.kind === "row" && node.row.handle !== null && node.row.expandable) {
      return this.expandChildren(node.row, node.section);
    }
    return [];
  }

  private async sectionChildren(
    id: "locals" | "globals" | "watch",
  ): Promise<VarNode[]> {
    const focus = this.state.focusInfo;
    if (id === "locals") {
      if (focus?.halted !== true) {
        return [{
          kind: "note",
          text: `${focus?.name ?? "the core"} is running — locals appear on halt`,
        }];
      }
      if (this.vars.localsError) {
        return [{ kind: "note", text: this.vars.localsError }];
      }
      return this.vars.locals.map((row) => ({
        kind: "row", row, section: "locals",
      }));
    }
    if (id === "globals") {
      if (!this.vars.globalsOpened) {
        this.vars.globalsOpened = true;
        void vscode.commands.executeCommand("s32e.refreshGlobalsSection");
        return [{ kind: "note", text: "loading…" }];
      }
      const nodes: VarNode[] = this.vars.globals.map((row) => ({
        kind: "row", row, section: "globals",
      }));
      if (this.vars.globalsTotal > this.vars.globals.length) {
        nodes.push({
          kind: "note",
          text: `…${this.vars.globalsTotal - this.vars.globals.length} more — use the filter`,
        });
      }
      return nodes;
    }
    // watch
    const nodes: VarNode[] = [];
    for (const path of this.vars.watches) {
      const row = this.vars.watchRows.get(path);
      if (row === undefined) {
        nodes.push({
          kind: "row",
          row: placeholderRow(path, "not evaluated yet"),
          section: "watch",
          watchPath: path,
        });
      } else if ("error" in row && !("handle" in row)) {
        nodes.push({
          kind: "row",
          row: placeholderRow(path, row.error),
          section: "watch",
          watchPath: path,
        });
      } else {
        nodes.push({
          kind: "row", row: row as VarRow, section: "watch", watchPath: path,
        });
      }
    }
    if (nodes.length === 0) {
      nodes.push({ kind: "note", text: "no watches — add one with +" });
    }
    return nodes;
  }

  private async expandChildren(row: VarRow, section: string): Promise<VarNode[]> {
    const handle = row.handle!;
    let children = this.vars.children.get(handle);
    if (children === undefined) {
      try {
        await this.vars.expand(this.state.requireClient(), handle);
        children = this.vars.children.get(handle) ?? [];
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        return [{
          kind: "note",
          text: message.startsWith("stale-handle:")
            ? "the target ran — refresh the view"
            : message,
        }];
      }
    }
    const nodes: VarNode[] = children.map((child) => ({
      kind: "row", row: child, section,
    }));
    const more = this.vars.moreFrom.get(handle);
    if (more !== undefined) {
      nodes.push({ kind: "more", parentHandle: handle, from: more, section });
    }
    return nodes;
  }
}

function placeholderRow(path: string, error: string): VarRow {
  return {
    handle: null, name: path, path, type: "", kind: "other", value: null,
    address: null, register: null, size: null, expandable: false,
    count: null, static: false, error,
  };
}

import * as vscode from "vscode";
import { CoreInfo, DebugState } from "../state/session";
import {
  AutoRefreshNode,
  autoRefreshItem,
  isAutoRefreshNode,
  ViewPolling,
} from "../state/viewPolling";

type CoresNode = CoreInfo | AutoRefreshNode;

export class CoreItem extends vscode.TreeItem {
  constructor(info: CoreInfo, focused: boolean) {
    super(
      (focused ? "⦿ " : "") + info.name,
      vscode.TreeItemCollapsibleState.None,
    );
    this.id = `core:${info.name}`;
    this.description =
      info.detail + (info.catchArmed ? "  ⚡catch armed" : "");
    this.tooltip =
      `${info.name} (${info.family === "r52" ? "Cortex-R52" : "Cortex-M33"})\n` +
      `${info.detail}\n` +
      (info.catchArmed ? "catch-at-first-instruction armed\n" : "") +
      (focused
        ? "FOCUSED: gutter breakpoints and step/halt/continue act here"
        : "click the target icon to focus");
    const ctx: string[] = ["core", `${info.family}core`];
    if (info.halted === true) {
      ctx.push("halted");
      this.iconPath = new vscode.ThemeIcon(
        "debug-pause",
        new vscode.ThemeColor("charts.red"),
      );
    } else if (info.halted === false) {
      ctx.push("running");
      this.iconPath = new vscode.ThemeIcon(
        "play-circle",
        new vscode.ThemeColor("charts.green"),
      );
    } else {
      ctx.push("unknown");
      this.iconPath = new vscode.ThemeIcon(
        "question",
        new vscode.ThemeColor("charts.yellow"),
      );
    }
    if (info.catchArmed) {
      ctx.push("catcharmed");
    }
    this.contextValue = ctx.join(" ");
    this.command = {
      command: "s32e.focusCore",
      title: "Focus",
      arguments: [{ coreName: info.name }],
    };
  }
}

export class CoresView implements vscode.TreeDataProvider<CoresNode> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(
    private readonly state: DebugState,
    private readonly polling: ViewPolling,
  ) {
    state.onDidChange(() => this.changeEmitter.fire());
    polling.onDidChange((view) => {
      if (view === "cores") {
        this.changeEmitter.fire();
      }
    });
  }

  getTreeItem(node: CoresNode): vscode.TreeItem {
    if (isAutoRefreshNode(node)) {
      return autoRefreshItem("cores", this.polling.on("cores"));
    }
    return new CoreItem(node, node.name === this.state.focus);
  }

  getChildren(node?: CoresNode): CoresNode[] {
    if (node) {
      return [];
    }
    const r52 = [...this.state.cores.values()].filter((c) => c.family === "r52");
    const m33 = [...this.state.cores.values()].filter((c) => c.family === "m33");
    // The checkbox only once connected: an empty tree shows the welcome
    // content (the Connect button), which any row would suppress.
    const toggle: CoresNode[] = this.state.connected
      ? [{ autoRefresh: "cores" }]
      : [];
    return [...toggle, ...r52, ...m33];
  }
}

/** The name a command handler should act on: the tree item arg or the focus. */
export function coreArg(
  state: DebugState,
  item?: CoreInfo | { coreName?: string },
): string | null {
  if (item && "coreName" in item && item.coreName) {
    return item.coreName;
  }
  if (item && "name" in item && (item as CoreInfo).name) {
    return (item as CoreInfo).name;
  }
  return state.focus;
}

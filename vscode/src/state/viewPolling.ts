import * as vscode from "vscode";

/** The sidebar views that generate probe traffic. Session renders local
 * state only, so it carries no toggle; Breakpoints and Watchpoints gate the
 * hardware-comparator verification sweep. */
export type PolledView =
  | "cores"
  | "variables"
  | "callStack"
  | "registers"
  | "memory"
  | "breakpoints"
  | "watchpoints";

const KEY = "s32e.viewPolling";

const DEFAULTS: Record<PolledView, boolean> = {
  cores: true,
  variables: true,
  callStack: true,
  registers: true,
  // A dump per open region per stop -- opt-in rather than a surprise.
  memory: false,
  breakpoints: true,
  watchpoints: true,
};

/** What each checkbox gates, shown as the row's description. */
const GATES: Record<PolledView, string> = {
  cores: "status poll",
  variables: "on-stop + live refresh",
  callStack: "on-stop unwind",
  registers: "on-stop read",
  memory: "re-read regions on stop",
  breakpoints: "verify hardware comparators",
  watchpoints: "verify hardware comparators",
};

/** Per-view auto-refresh switches, persisted in workspaceState.
 *
 * Checked means the view's data refreshes on its usual trigger (the periodic
 * poll for Cores, the on-stop fetch chain for the rest); unchecked keeps the
 * view quiet -- it shows its last data and only a manual refresh touches the
 * probe. The checkbox row itself lives in each view's provider via
 * autoRefreshNode/autoRefreshItem below.
 */
export class ViewPolling implements vscode.Disposable {
  private readonly changeEmitter = new vscode.EventEmitter<PolledView>();
  readonly onDidChange = this.changeEmitter.event;

  constructor(private readonly memento: vscode.Memento) {}

  private stored(): Partial<Record<PolledView, boolean>> {
    return this.memento.get<Partial<Record<PolledView, boolean>>>(KEY, {});
  }

  on(view: PolledView): boolean {
    return this.stored()[view] ?? DEFAULTS[view];
  }

  async set(view: PolledView, value: boolean): Promise<void> {
    if (this.on(view) === value) {
      return;
    }
    await this.memento.update(KEY, { ...this.stored(), [view]: value });
    this.changeEmitter.fire(view);
  }

  dispose(): void {
    this.changeEmitter.dispose();
  }
}

/** The node each provider prepends; the tree checkbox event hands it back. */
export interface AutoRefreshNode {
  autoRefresh: PolledView;
}

export function isAutoRefreshNode(node: unknown): node is AutoRefreshNode {
  return (
    typeof node === "object" && node !== null && "autoRefresh" in node
  );
}

/** The "Auto-refresh" checkbox row every polled view renders first. */
export function autoRefreshItem(
  view: PolledView, enabled: boolean,
): vscode.TreeItem {
  const item = new vscode.TreeItem("Auto-refresh");
  item.id = `s32e.autorefresh.${view}`;
  item.description = GATES[view] + (enabled ? "" : " — paused");
  item.checkboxState = enabled
    ? vscode.TreeItemCheckboxState.Checked
    : vscode.TreeItemCheckboxState.Unchecked;
  item.tooltip = enabled
    ? `This view refreshes automatically (${GATES[view]}). Uncheck to stop ` +
      "its probe traffic; the last values stay visible and the view's " +
      "refresh button still works."
    : "Auto-refresh is off: the values shown are from the last refresh. " +
      "Check to resume, or use the view's refresh button for a one-shot.";
  item.contextValue = "autorefresh";
  return item;
}

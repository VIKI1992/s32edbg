import * as vscode from "vscode";
import { SessionClient } from "../backend/client";
import {
  VarExpandResult,
  VarGlobalsResult,
  VarLocalsResult,
  VarRefreshResult,
  VarRow,
  VarWatchResult,
} from "../backend/types";
import { setting } from "../config";
import { Output } from "../ui/output";
import { isStaleHandleError, rowKey } from "../views/variablesFormat";
import { DebugState } from "./session";

const WATCHES_KEY = "s32e.variables.watches";

/** Everything the Variables view renders, plus the fetch/refresh plumbing. */
export class VariablesState implements vscode.Disposable {
  selectedFrame = 0;
  hexMode = false;
  globalsFilter = "";
  livePaused = false;
  /** Backend generation the caches belong to; -1 = nothing fetched. */
  generation = -1;
  /** Set once the Globals section has been expanded at least once. */
  globalsOpened = false;

  locals: VarRow[] = [];
  localsFunc: string | null = null;
  localsError: string | null = null;
  cfaSource: string | null = null;
  globals: VarRow[] = [];
  globalsTotal = 0;
  watchRows = new Map<string, VarRow | { error: string }>();
  children = new Map<string, VarRow[]>();
  moreFrom = new Map<string, number>();

  private prev = new Map<string, string>();
  private current = new Map<string, string>();

  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changeEmitter.event;

  private batchDepth = 0;
  private batchDirty = false;
  private inflightExpand = new Map<string, Promise<void>>();

  constructor(private readonly memento: vscode.Memento) {}

  fire(): void {
    if (this.batchDepth > 0) {
      this.batchDirty = true;
      return;
    }
    this.changeEmitter.fire();
  }

  /** Coalesce the per-fetch fires of one stop refresh into a single render.
   * Each fire re-renders the whole tree (and re-enters the lazy fetch paths
   * in getChildren), so a stop that fetches locals + N watches + globals
   * used to render 5-8 times. */
  async batch<T>(work: () => Promise<T>): Promise<T> {
    this.batchDepth += 1;
    try {
      return await work();
    } finally {
      this.batchDepth -= 1;
      if (this.batchDepth === 0 && this.batchDirty) {
        this.batchDirty = false;
        this.changeEmitter.fire();
      }
    }
  }

  get watches(): string[] {
    return this.memento.get<string[]>(WATCHES_KEY, []);
  }

  async addWatch(path: string): Promise<void> {
    const list = this.watches;
    if (!list.includes(path)) {
      await this.memento.update(WATCHES_KEY, [...list, path]);
    }
    this.fire();
  }

  async removeWatch(path: string): Promise<void> {
    await this.memento.update(
      WATCHES_KEY,
      this.watches.filter((p) => p !== path),
    );
    this.watchRows.delete(path);
    this.fire();
  }

  /** Halt boundary: rotate the changed-highlight maps (registersView style). */
  noteHalt(): void {
    this.prev = this.current;
    this.current = new Map();
  }

  /** Record a row's display and say whether it changed since the last halt. */
  changed(section: string, row: VarRow): boolean {
    if (!row.value) {
      return false;
    }
    const key = rowKey(section, row.path);
    this.current.set(key, row.value.display);
    const before = this.prev.get(key);
    return before !== undefined && before !== row.value.display;
  }

  invalidate(): void {
    this.locals = [];
    this.localsFunc = null;
    this.localsError = null;
    this.children.clear();
    this.moreFrom.clear();
    this.generation = -1;
    this.fire();
  }

  reset(): void {
    this.invalidate();
    this.globals = [];
    this.globalsTotal = 0;
    this.watchRows.clear();
    this.selectedFrame = 0;
    this.fire();
  }

  // -- fetches (all sequential over the shared client queue) ----------------

  async fetchLocals(client: SessionClient, core: string, frame: number): Promise<void> {
    this.selectedFrame = frame;
    try {
      const result = (await client.run(
        `var locals --core ${core} --frame ${frame}`,
      )) as VarLocalsResult;
      this.locals = result.variables;
      this.localsFunc = result.func;
      this.localsError = result.note;
      this.cfaSource = result.cfa_source;
      this.generation = result.generation;
      for (const row of result.variables) {
        this.changed("locals", row);
      }
    } catch (err) {
      this.locals = [];
      this.localsFunc = null;
      this.localsError = err instanceof Error ? err.message : String(err);
    }
    this.fire();
  }

  async fetchGlobals(client: SessionClient, core: string): Promise<void> {
    const limit = setting<number>("variables.childrenCap", 100);
    const filter = this.globalsFilter
      ? ` --filter "${this.globalsFilter}"`
      : "";
    try {
      const result = (await client.run(
        `var globals --core ${core} --limit ${limit}${filter}`,
      )) as VarGlobalsResult;
      this.globals = result.variables;
      this.globalsTotal = result.total;
      this.generation = result.generation;
      for (const row of result.variables) {
        this.changed("globals", row);
      }
    } catch (err) {
      this.globals = [];
      this.globalsTotal = 0;
      void err;
    }
    this.fire();
  }

  async fetchWatches(
    client: SessionClient, core: string, frame: number | null,
  ): Promise<void> {
    for (const path of this.watches) {
      const frameArg = frame !== null ? ` --frame ${frame}` : "";
      try {
        const result = (await client.run(
          `var watch "${path}" --core ${core}${frameArg}`,
        )) as VarWatchResult;
        this.watchRows.set(path, result.row);
        this.generation = result.generation;
        this.changed("watch", result.row);
      } catch (err) {
        this.watchRows.set(path, {
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }
    this.fire();
  }

  async expand(client: SessionClient, handle: string, from = 0): Promise<void> {
    // Issued from inside getChildren: concurrent re-renders of the same node
    // would otherwise fire duplicate backend requests for identical data.
    const key = `${handle}@${from}`;
    const inflight = this.inflightExpand.get(key);
    if (inflight) {
      return inflight;
    }
    const work = (async () => {
      const limit = setting<number>("variables.childrenCap", 100);
      const result = (await client.run(
        `var expand ${handle} --children-from ${from} --limit ${limit}`,
      )) as VarExpandResult;
      const existing = from > 0 ? (this.children.get(handle) ?? []) : [];
      this.children.set(handle, [...existing, ...result.children]);
      if (result.more_from !== null) {
        this.moreFrom.set(handle, result.more_from);
      } else {
        this.moreFrom.delete(handle);
      }
      this.fire();
    })();
    this.inflightExpand.set(key, work);
    try {
      return await work;
    } finally {
      this.inflightExpand.delete(key);
    }
  }

  applyRefresh(result: VarRefreshResult): void {
    const byHandle = new Map(result.rows.map((r) => [r.handle, r]));
    const patch = (rows: VarRow[], section: string) => {
      for (let i = 0; i < rows.length; i++) {
        const fresh = rows[i].handle !== null ? byHandle.get(rows[i].handle) : undefined;
        if (fresh) {
          rows[i] = fresh;
          this.changed(section, fresh);
        }
      }
    };
    patch(this.globals, "globals");
    for (const [path, row] of this.watchRows) {
      if ("handle" in row && row.handle !== null) {
        const fresh = byHandle.get(row.handle);
        if (fresh) {
          this.watchRows.set(path, fresh);
          this.changed("watch", fresh);
        }
      }
    }
    for (const [handle, rows] of this.children) {
      patch(rows, `children:${handle}`);
    }
    this.fire();
  }

  /** Handles worth re-reading live: visible leaf rows under Globals/Watch. */
  collectLiveHandles(): string[] {
    const out: string[] = [];
    const take = (row: VarRow) => {
      if (row.handle !== null && row.address !== null && !row.error) {
        out.push(row.handle);
      }
    };
    for (const row of this.globals) {
      take(row);
    }
    for (const row of this.watchRows.values()) {
      if ("handle" in row) {
        take(row);
      }
    }
    for (const rows of this.children.values()) {
      for (const row of rows) {
        take(row);
      }
    }
    return out;
  }

  dispose(): void {
    this.changeEmitter.dispose();
  }
}

/** The running-core live loop: globals + watches at a configurable cadence.
 * Same guard discipline as poll/poller.ts -- skip while anything is busy,
 * stop after repeated failures, never overlap. */
export class VariablesLive implements vscode.Disposable {
  private timer: ReturnType<typeof setInterval> | null = null;
  private inFlight = false;
  private failures = 0;
  private lastGeneration = -2;

  constructor(
    private readonly state: DebugState,
    private readonly vars: VariablesState,
    private readonly output: Output,
    private readonly treeVisible: () => boolean,
    private readonly pollingOn: () => boolean = () => true,
  ) {}

  start(): void {
    this.stop();
    const interval = setting<number>("variables.liveRefreshMs", 1000);
    if (interval <= 0) {
      return;
    }
    this.timer = setInterval(() => void this.tick(), Math.max(interval, 200));
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  private async tick(): Promise<void> {
    const focus = this.state.focusInfo;
    if (
      !this.state.connected ||
      this.state.busyOp ||
      this.inFlight ||
      (this.state.client?.busy ?? true) ||
      this.vars.livePaused ||
      !this.pollingOn() ||  // the view's Auto-refresh checkbox
      !this.treeVisible() ||
      focus?.halted !== false  // live mode is only for a RUNNING focus
    ) {
      return;
    }
    this.inFlight = true;
    try {
      const client = this.state.client!;
      const core = this.state.focus!;
      if (
        this.vars.generation !== this.lastGeneration ||
        (this.vars.globals.length === 0 && this.vars.watches.length > 0)
      ) {
        // Fresh generation (the core ran): whole-sale refetch, frameless.
        if (this.vars.globalsOpened) {
          await this.vars.fetchGlobals(client, core);
        }
        await this.vars.fetchWatches(client, core, null);
        this.lastGeneration = this.vars.generation;
      } else {
        const handles = this.vars.collectLiveHandles();
        for (let i = 0; i < handles.length; i += 64) {
          const chunk = handles.slice(i, i + 64);
          const result = (await client.run(
            `var refresh ${chunk.join(" ")}`,
          )) as VarRefreshResult;
          this.vars.applyRefresh(result);
        }
      }
      this.failures = 0;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (isStaleHandleError(message)) {
        // The target ran between fetch and refresh: drop and refetch next tick.
        this.vars.invalidate();
        this.lastGeneration = -2;
        this.failures = 0;
      } else {
        this.failures += 1;
        if (this.failures === 3) {
          this.output.line(`!! live variables failing repeatedly: ${message}`);
        }
        if (this.failures >= 10) {
          this.stop();
          void vscode.window.showWarningMessage(
            "S32E: live variable refresh stopped after repeated failures -- " +
              "see the output log. Toggle it in the Variables view to resume.",
          );
        }
      }
    } finally {
      this.inFlight = false;
    }
  }

  dispose(): void {
    this.stop();
  }
}

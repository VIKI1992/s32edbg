import * as vscode from "vscode";
import { SessionClient } from "../backend/client";
import {
  hexToInt,
  PollResult,
  StackFrame,
  StoppedEvent,
} from "../backend/types";

export type Family = "r52" | "m33";

export interface CoreInfo {
  name: string;
  family: Family;
  halted: boolean | null;
  detail: string;
  location?: { known?: boolean; address?: string; source?: string };
  unreachable?: string | null;
  catchArmed: boolean;
}

export const M33_NAMES = ["smu", "ce0", "ce1"];

export function familyOf(name: string): Family {
  return M33_NAMES.includes(name.toLowerCase()) ? "m33" : "r52";
}

/** Everything the views render, in one observable store. */
export class DebugState implements vscode.Disposable {
  client: SessionClient | null = null;
  connected = false;
  connecting = false;
  /** True while a long operation owns the session (load, catch-convert). */
  busyOp = false;

  cores = new Map<string, CoreInfo>();
  focus: string | null = null;
  sync = false;
  backendVersion = "";
  symbolsLoaded = false;

  /** Focused core's latest fetch results. */
  lastRegs = new Map<string, Record<string, string>>();
  prevRegs = new Map<string, Record<string, string>>();
  lastStack: StackFrame[] = [];
  lastStackReason = "";
  lastPc = new Map<string, number>();

  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changeEmitter.event;
  private readonly haltEmitter = new vscode.EventEmitter<CoreInfo>();
  /** Fired on every observed running->halted transition. */
  readonly onCoreHalted = this.haltEmitter.event;

  get focusInfo(): CoreInfo | null {
    return this.focus ? (this.cores.get(this.focus) ?? null) : null;
  }

  get focusFamily(): Family {
    return this.focus ? familyOf(this.focus) : "r52";
  }

  requireClient(): SessionClient {
    if (!this.client || !this.connected) {
      throw new Error("not connected -- run 'S32E: Connect' first");
    }
    return this.client;
  }

  fireChange(): void {
    this.updateContexts();
    this.changeEmitter.fire();
  }

  /** Merge one poll result; fires onCoreHalted for fresh halts. */
  applyPoll(poll: PollResult): void {
    const freshHalts: CoreInfo[] = [];
    const seen = new Set<string>();
    for (const row of poll.r52) {
      const prev = this.cores.get(row.core);
      const info: CoreInfo = {
        name: row.core,
        family: "r52",
        halted: row.halted,
        detail: row.unreachable
          ? `locked: ${row.unreachable}`
          : row.halted
            ? (row.location?.known && row.location.address
                ? `halted at ${row.location.address}`
                : `halted${row.status ? `: ${row.status}` : ""}`)
            : row.halted === false
              ? "running"
              : "unknown",
        location: row.location,
        unreachable: row.unreachable,
        catchArmed: poll.catch_armed.includes(row.core),
      };
      this.cores.set(row.core, info);
      seen.add(row.core);
      if (prev && prev.halted === false && info.halted === true) {
        freshHalts.push(info);
      }
    }
    for (const row of poll.m33) {
      const prev = this.cores.get(row.name);
      const info: CoreInfo = {
        name: row.name,
        family: "m33",
        halted: row.halted,
        detail: !row.answered
          ? "no answer"
          : row.halted
            ? "halted"
            : row.halted === false
              ? "running"
              : "unknown",
        catchArmed: false,
      };
      this.cores.set(row.name, info);
      seen.add(row.name);
      if (prev && prev.halted === false && info.halted === true) {
        freshHalts.push(info);
      }
    }
    this.sync = poll.sync;
    this.fireChange();
    for (const info of freshHalts) {
      this.haltEmitter.fire(info);
    }
  }

  /** Ingest a pushed stopped event; fires onCoreHalted unless the slow poll
   * already marked this core halted (never react to one stop twice). */
  noteStopped(ev: StoppedEvent): void {
    const prev = this.cores.get(ev.core);
    if (prev?.halted === true) {
      return;
    }
    const info: CoreInfo = {
      name: ev.core,
      family: ev.family,
      halted: true,
      detail:
        ev.location?.known && ev.location.address
          ? `halted at ${ev.location.address}`
          : ev.status ?? "halted",
      location: ev.location,
      unreachable: null,
      catchArmed: prev?.catchArmed ?? false,
    };
    this.cores.set(ev.core, info);
    this.fireChange();
    this.haltEmitter.fire(info);
  }

  notePc(core: string, pcHex: string | null | undefined): number | null {
    const pc = hexToInt(pcHex ?? null);
    if (pc !== null) {
      this.lastPc.set(core, pc);
    }
    return pc;
  }

  markHalted(core: string, halted: boolean, detail?: string): void {
    const info = this.cores.get(core);
    if (info) {
      info.halted = halted;
      info.detail = detail ?? (halted ? "halted" : "running");
      this.fireChange();
    }
  }

  resetForDisconnect(): void {
    this.client = null;
    this.connected = false;
    this.connecting = false;
    this.busyOp = false;
    this.symbolsLoaded = false;
    this.cores.clear();
    this.lastRegs.clear();
    this.prevRegs.clear();
    this.lastStack = [];
    this.lastPc.clear();
    this.sync = false;
    this.fireChange();
  }

  updateContexts(): void {
    const set = (key: string, value: unknown) =>
      void vscode.commands.executeCommand("setContext", key, value);
    set("s32e.connected", this.connected);
    set("s32e.busy", this.busyOp);
    set("s32e.focusHalted", this.focusInfo?.halted === true);
    set("s32e.focusIsR52", this.focusFamily === "r52");
  }

  dispose(): void {
    this.changeEmitter.dispose();
    this.haltEmitter.dispose();
  }
}

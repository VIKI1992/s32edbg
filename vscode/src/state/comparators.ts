import * as vscode from "vscode";
import { hexToInt } from "../backend/types";
import { setting } from "../config";
import { Output } from "../ui/output";
import { BreakpointStore } from "./breakpoints";
import { DebugState, familyOf } from "./session";
import { ViewPolling } from "./viewPolling";
import { WatchpointStore } from "./watchpoints";

/** One hardware comparator slot as `bp list` / `wp list` reports it.
 * An entry is either an error row or a decoded one; the R52 and M33 decoders
 * emit different extras, but index/error/enabled/address are common.
 * Watchpoint rows additionally carry size (`bytes`) and access (`on`). */
export interface HwSlot {
  index: number;
  error?: string;
  enabled?: boolean;
  address?: string;
  bytes?: number;
  on?: string;
}

export type Verdict = "verified" | "mismatch" | "unknown";

export interface RowVerdict {
  verdict: Verdict;
  reason: string;
}

/** An enabled hardware slot no session row claims -- adoption's raw input. */
export interface HwOrphan {
  index: number;
  address: string;
  bytes?: number;
  on?: string;
}

export interface CoreVerdicts {
  /** store row id -> verdict, for rows the store considers armed */
  rows: Map<string, RowVerdict>;
  /** enabled hardware slots no armed local row claims */
  orphans: HwOrphan[];
  /** every slot unreadable (OS lock / reset hold): treat as no-information */
  unreadable: boolean;
}

interface LocalRow {
  id: string;
  slot: number | null;
  address: number | null;
  state: string;
}

/** Compare what the store believes against what the comparators hold.
 *
 * Pure and family-agnostic. The one hard rule: when EVERY slot read as an
 * error (OS lock or reset hold makes the whole bank unreadable), the answer
 * is "unknown", never "mismatch" -- an unreadable comparator says nothing
 * about the breakpoint, and demoting local state on it would discard the
 * operator's setup over a transient.
 */
export function reconcileCore(locals: LocalRow[], hw: HwSlot[]): CoreVerdicts {
  const out: CoreVerdicts = { rows: new Map(), orphans: [], unreadable: false };
  const readable = hw.filter((slot) => !slot.error);
  if (hw.length > 0 && readable.length === 0) {
    out.unreadable = true;
    for (const row of locals) {
      if (row.state === "armed") {
        out.rows.set(row.id, {
          verdict: "unknown",
          reason: "comparators unreadable (OS lock or reset hold)",
        });
      }
    }
    return out;
  }
  const bySlot = new Map(readable.map((slot) => [slot.index, slot]));
  const claimed = new Set<number>();
  for (const row of locals) {
    if (row.state !== "armed" || row.slot === null) {
      continue;
    }
    claimed.add(row.slot);
    const slot = bySlot.get(row.slot);
    if (slot === undefined) {
      out.rows.set(row.id, {
        verdict: "unknown",
        reason: `slot ${row.slot} did not read`,
      });
      continue;
    }
    if (!slot.enabled) {
      out.rows.set(row.id, {
        verdict: "mismatch",
        reason: `slot ${row.slot} is DISABLED in hardware`,
      });
      continue;
    }
    const hwAddr = hexToInt(slot.address ?? null);
    if (
      hwAddr !== null &&
      row.address !== null &&
      (hwAddr & ~1) !== (row.address & ~1)
    ) {
      out.rows.set(row.id, {
        verdict: "mismatch",
        reason: `slot ${row.slot} holds ${slot.address}, not this address`,
      });
      continue;
    }
    out.rows.set(row.id, {
      verdict: "verified",
      reason: `slot ${row.slot} armed at ${slot.address}`,
    });
  }
  for (const slot of readable) {
    if (slot.enabled && !claimed.has(slot.index)) {
      out.orphans.push({
        index: slot.index,
        address: slot.address ?? "?",
        ...(slot.bytes !== undefined ? { bytes: slot.bytes } : {}),
        ...(slot.on !== undefined ? { on: slot.on } : {}),
      });
    }
  }
  return out;
}

/** Periodically reads the hardware comparators and reconciles them against
 * the breakpoint/watchpoint stores.
 *
 * Same guard discipline as poll/poller.ts and VariablesLive, with one
 * deliberate difference: it runs whether the focus is halted or running --
 * target code can cycle a core's reset and wipe its comparators at any time
 * (guide §3.6), and catching exactly that drift is this loop's purpose.
 * Cost awareness: one `bp list` is ~48 USB round trips, so each pass visits
 * only the cores that have local rows (plus the focus), and each list is
 * gated by its own view's Auto-refresh checkbox.
 */
export class ComparatorReconciler implements vscode.Disposable {
  private timer: ReturnType<typeof setInterval> | null = null;
  private inFlight = false;
  private failures = 0;
  /** "<core>#bp" / "<core>#wp" -> latest verdicts */
  readonly results = new Map<string, CoreVerdicts>();
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changeEmitter.event;

  constructor(
    private readonly state: DebugState,
    private readonly bpStore: BreakpointStore,
    private readonly wpStore: WatchpointStore,
    private readonly polling: ViewPolling,
    private readonly output: Output,
  ) {}

  start(): void {
    this.stop();
    const interval = setting<number>("comparators.verifyMs", 5000);
    if (interval <= 0) {
      return;
    }
    this.timer = setInterval(() => void this.tick(), Math.max(interval, 1000));
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  clear(): void {
    this.results.clear();
    this.changeEmitter.fire();
  }

  /** One verify pass. `full` widens the swept set to every discovered core
   * -- the connect / checkbox-recheck discovery pass that finds leftovers on
   * cores with no session rows; per-core failures are skipped there (an
   * unpowered M33 or a reset-held R52 must not kill the sweep). */
  async tick(full = false): Promise<void> {
    const wantBp = this.polling.on("breakpoints");
    const wantWp = this.polling.on("watchpoints");
    if (
      (!wantBp && !wantWp) ||
      !this.state.connected ||
      this.state.busyOp ||
      this.inFlight ||
      (this.state.client?.busy ?? true)
    ) {
      return;
    }
    this.inFlight = true;
    try {
      const client = this.state.client!;
      const cores = new Set<string>();
      if (full) {
        for (const name of this.state.cores.keys()) {
          cores.add(name);
        }
      }
      if (wantBp) {
        for (const b of this.bpStore.all) {
          cores.add(b.core);
        }
      }
      if (wantWp) {
        for (const w of this.wpStore.all) {
          cores.add(w.core);
        }
      }
      if (this.state.focus) {
        cores.add(this.state.focus);
      }
      let changed = false;
      for (const core of cores) {
        const m33 = familyOf(core) === "m33";
        try {
          if (wantBp) {
            const result = (await client.run(
              m33 ? `m33 bp list --core ${core}` : `bp list --core ${core}`,
            )) as { breakpoints?: HwSlot[] };
            this.results.set(
              `${core}#bp`,
              reconcileCore(
                this.bpStore.all.filter((b) => b.core === core),
                result.breakpoints ?? [],
              ),
            );
            changed = true;
          }
          if (wantWp) {
            const result = (await client.run(
              m33 ? `m33 wp list --core ${core}` : `wp list --core ${core}`,
            )) as { watchpoints?: HwSlot[] };
            this.results.set(
              `${core}#wp`,
              reconcileCore(
                this.wpStore.all.filter((w) => w.core === core),
                result.watchpoints ?? [],
              ),
            );
            changed = true;
          }
        } catch (err) {
          if (!full) {
            throw err;
          }
          this.output.line(
            `!! comparator sweep skipped ${core}: ` +
              (err instanceof Error ? err.message : String(err)),
          );
        }
      }
      if (changed) {
        this.changeEmitter.fire();
      }
      this.failures = 0;
    } catch (err) {
      this.failures += 1;
      const message = err instanceof Error ? err.message : String(err);
      if (this.failures === 3) {
        this.output.line(`!! comparator verify failing repeatedly: ${message}`);
      }
      if (this.failures >= 10) {
        this.stop();
        void vscode.window.showWarningMessage(
          "S32E: hardware comparator verification stopped after repeated " +
            "failures -- see the output log. Toggle a checkbox to retry.",
        );
      }
    } finally {
      this.inFlight = false;
    }
  }

  dispose(): void {
    this.stop();
    this.changeEmitter.dispose();
  }
}

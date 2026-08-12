import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { toHex } from "../backend/types";
import { Output } from "../ui/output";
import { BreakpointStore } from "./breakpoints";
import { ComparatorReconciler, CoreVerdicts } from "./comparators";
import { DebugState } from "./session";
import { WatchpointStore } from "./watchpoints";

export interface AdoptionAction {
  unit: "bp" | "wp";
  core: string;
  index: number;
  address: number;
  bytes?: number;
  on?: string;
}

interface ClaimedSlot {
  core: string;
  slot: number | null;
}

/** Which orphans need adopting -- pure, so the dedup rule is testable.
 *
 * An orphan is already "not claimed by an armed row" at reconcile time; the
 * re-check here guards the window between a sweep and this pass (a stale
 * onDidChange after adoption must not mint a duplicate row). */
export function planAdoptions(
  results: ReadonlyMap<string, CoreVerdicts>,
  bpSlots: ClaimedSlot[],
  wpSlots: ClaimedSlot[],
): AdoptionAction[] {
  const actions: AdoptionAction[] = [];
  for (const [key, verdicts] of results) {
    const hash = key.lastIndexOf("#");
    if (hash < 0) {
      continue;
    }
    const core = key.slice(0, hash);
    const unit = key.slice(hash + 1) as "bp" | "wp";
    const claimed = unit === "bp" ? bpSlots : wpSlots;
    for (const orphan of verdicts.orphans) {
      const address = Number.parseInt(orphan.address, 16);
      if (!Number.isFinite(address)) {
        continue;
      }
      if (claimed.some((r) => r.core === core && r.slot === orphan.index)) {
        continue;
      }
      actions.push({
        unit,
        core,
        index: orphan.index,
        address,
        ...(orphan.bytes !== undefined ? { bytes: orphan.bytes } : {}),
        ...(orphan.on !== undefined ? { on: orphan.on } : {}),
      });
    }
  }
  return actions;
}

/** Turns sweep orphans into session rows: foreign breakpoints become gutter
 * dots when their address resolves to a local source line (address-only
 * rows otherwise), foreign watchpoints become ordinary watchpoint rows with
 * the hardware-decoded size/access. No `bp/wp set` is ever issued -- the
 * comparators are already armed; deletion rides the ordinary disarm paths. */
export class ComparatorAdoption implements vscode.Disposable {
  private running = false;
  private readonly sub: vscode.Disposable;

  constructor(
    private readonly state: DebugState,
    private readonly comparators: ComparatorReconciler,
    private readonly bpStore: BreakpointStore,
    private readonly wpStore: WatchpointStore,
    private readonly output: Output,
  ) {
    this.sub = comparators.onDidChange(() => void this.run());
  }

  private async run(): Promise<void> {
    if (this.running || !this.state.connected) {
      return;
    }
    this.running = true;
    try {
      const actions = planAdoptions(
        this.comparators.results,
        this.bpStore.all.map((b) => ({
          core: b.core,
          slot: b.state === "armed" ? b.slot : null,
        })),
        this.wpStore.all.map((w) => ({
          core: w.core,
          slot: w.state === "armed" ? w.slot : null,
        })),
      );
      for (const action of actions) {
        if (action.unit === "wp") {
          this.wpStore.adoptForeign(
            action.core,
            action.address,
            action.index,
            action.bytes ?? 4,
            action.on ?? "both",
          );
          this.output.line(
            `adopted hardware watchpoint: slot ${action.index} on ` +
              `${action.core} at ${toHex(action.address)} ` +
              `(${action.bytes ?? 4} B, ${action.on ?? "both"})`,
          );
          continue;
        }
        const source = await this.resolveSource(action.address);
        this.bpStore.adoptForeign(
          action.core, action.address, action.index, source,
        );
        this.output.line(
          `adopted hardware breakpoint: slot ${action.index} on ` +
            `${action.core} at ${toHex(action.address)}` +
            (source
              ? ` -> ${path.basename(source.fsPath)}:${source.line}`
              : " (no source line -- address row)"),
        );
      }
    } catch (err) {
      this.output.line(
        `!! comparator adoption: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      this.running = false;
    }
  }

  private async resolveSource(
    address: number,
  ): Promise<{ fsPath: string; line: number } | null> {
    if (!this.state.symbolsLoaded || !this.state.client) {
      return null;
    }
    try {
      const lookup = (await this.state.client.run(
        `sym addr2line ${toHex(address)}`,
      )) as { lookups: { file: string | null; line: number | null }[] };
      const hit = lookup.lookups[0];
      if (hit?.file && hit.line && fs.existsSync(hit.file)) {
        return { fsPath: hit.file, line: hit.line };
      }
    } catch {
      /* no line info for the address -- adopt by address instead */
    }
    return null;
  }

  dispose(): void {
    this.sub.dispose();
  }
}

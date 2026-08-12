import * as path from "path";
import * as vscode from "vscode";
import { StackScanRow } from "../backend/types";

const PROJECT_KEY = "s32e.stacks.project";
const ARXML_KEY = "s32e.stacks.arxml";

/** The stack monitor's per-workspace project selection + the last scan.
 *
 * Deliberately NOT part of the PolledView union: the view has no
 * Auto-refresh checkbox and generates zero probe traffic on its own -- a
 * scan happens only when the refresh button asks for one. Scan results are
 * in-memory (a utilization snapshot is only meaningful against the session
 * it was read from); the project/ARXML choice persists per workspace.
 */
export class StacksState implements vscode.Disposable {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changeEmitter.event;

  /** Last scan, in ARXML order; empty until the first refresh. */
  rows: StackScanRow[] = [];
  scannedAt: Date | null = null;
  scanning = false;

  constructor(private readonly memento: vscode.Memento) {}

  get project(): string | null {
    return this.memento.get<string | null>(PROJECT_KEY, null);
  }

  get arxml(): string | null {
    return this.memento.get<string | null>(ARXML_KEY, null);
  }

  get arxmlBasename(): string | null {
    const a = this.arxml;
    return a ? path.basename(a) : null;
  }

  async set(project: string | null, arxml: string): Promise<void> {
    await this.memento.update(PROJECT_KEY, project);
    await this.memento.update(ARXML_KEY, arxml);
    this.rows = [];
    this.scannedAt = null;
    this.fire();
  }

  noteScan(rows: StackScanRow[]): void {
    this.rows = rows;
    this.scannedAt = new Date();
    this.fire();
  }

  fire(): void {
    this.changeEmitter.fire();
  }

  dispose(): void {
    this.changeEmitter.dispose();
  }
}

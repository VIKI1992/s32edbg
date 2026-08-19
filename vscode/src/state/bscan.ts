import * as path from "path";
import * as vscode from "vscode";
import { BscanPinRow } from "../backend/types";
import { setting } from "../config";

/** The Boundary Scan view's in-memory sample + the per-session consent.
 *
 * Deliberately NOT part of the PolledView union, and inverted from every
 * other view: it works only while DISCONNECTED, because a sample runs
 * one-shot `bscan` invocations (JTAGLoad needs the probe to itself) and the
 * RM Table 984 preamble takes the DAP away until a power-cycle. Sample rows
 * are in-memory only -- a pin snapshot is meaningful against the board state
 * it was read from. The BSDL choice lives in the `s32e.bscan.bsdlPath`
 * setting (empty = the bundled file, auto-selected by probed IDCODE).
 */
export class BscanState implements vscode.Disposable {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changeEmitter.event;

  /** Last sample, in cell order; empty until the first refresh. */
  rows: BscanPinRow[] = [];
  /** Where the pin names came from, as the backend reported it
   * (e.g. "bundled:SIP_...bsdl" or an absolute --bsdl path). */
  source: string | null = null;
  rawHex: string | null = null;
  sampledAt: Date | null = null;
  sampling = false;
  /** One modal warning per board session; re-armed when a debug session
   * connects (a working connect means the chain is cold again). */
  confirmed = false;
  /** Separate consent for the EXTEST drive path (it resets the MCU), same
   * once-per-board-session lifetime. */
  driveConfirmed = false;
  /** The requested EXTEST drives, pin -> "0"|"1"|"Z". Every apply sends the
   * WHOLE set: a drive run rebuilds the full vector from safe values, so a
   * partial send would silently release the pins it omits. In-memory only --
   * a latched drive cannot survive the power-cycle that ends it. */
  readonly driveSet = new Map<string, "0" | "1" | "Z">();
  /** True while an EXTEST latch is believed to be holding the pins. */
  driveLatched = false;

  get bsdlPath(): string | null {
    const p = setting<string>("bscan.bsdlPath", "").trim();
    return p.length > 0 ? p : null;
  }

  get bsdlLabel(): string {
    const p = this.bsdlPath;
    return p ? path.basename(p) : "bundled BSDL";
  }

  noteSample(source: string | null, rows: BscanPinRow[], rawHex: string | null): void {
    this.source = source;
    this.rows = rows;
    this.rawHex = rawHex;
    this.sampledAt = new Date();
    this.fire();
  }

  reset(): void {
    this.rows = [];
    this.source = null;
    this.rawHex = null;
    this.sampledAt = null;
    this.fire();
  }

  setDrive(name: string, value: "0" | "1" | "Z"): void {
    this.driveSet.set(name, value);
    this.fire();
  }

  clearDrive(name: string): void {
    this.driveSet.delete(name);
    this.fire();
  }

  clearDrives(): void {
    this.driveSet.clear();
    this.driveLatched = false;
    this.fire();
  }

  fire(): void {
    this.changeEmitter.fire();
  }

  dispose(): void {
    this.changeEmitter.dispose();
  }
}

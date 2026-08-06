import * as path from "path";
import * as vscode from "vscode";
import { BackendError } from "../backend/client";
import { toHex } from "../backend/types";
import { Output } from "../ui/output";
import { DebugState, familyOf } from "./session";

export interface S32Breakpoint {
  id: string;
  core: string;
  /** Comparator target once resolved. */
  address: number | null;
  /** Where the user clicked, for line breakpoints. */
  source?: { fsPath: string; line: number };
  /** Hardware comparator slot while armed. */
  slot: number | null;
  state: "pending" | "armed" | "failed";
  error?: string;
  /** vscode native breakpoint id, when this came from the gutter. */
  nativeId?: string;
  origin: "gutter" | "address" | "disasm";
}

let counter = 0;
const nextId = () => `s32bp-${++counter}`;

/** Our model of every breakpoint, synced with VS Code's native gutter dots.
 *
 * The native dots are presence only -- without a DAP session VS Code cannot
 * render verified/failed states -- so this store (and the Breakpoints view)
 * is the truth about what is actually armed in a hardware comparator, on
 * which core, in which slot.
 */
export class BreakpointStore implements vscode.Disposable {
  readonly all: S32Breakpoint[] = [];
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changeEmitter.event;
  private readonly subs: vscode.Disposable[] = [];
  /** line->address map provider for s32e-disasm documents (set by the
   * disassembly provider once it exists). */
  disasmLineToAddr: ((uri: vscode.Uri, line: number) => number | null) | null =
    null;

  constructor(
    private readonly state: DebugState,
    private readonly output: Output,
  ) {
    this.subs.push(
      vscode.debug.onDidChangeBreakpoints((ev) => {
        void this.onNativeChange(ev);
      }),
    );
    // Adopt dots that already existed when the window opened.
    for (const bp of vscode.debug.breakpoints) {
      this.adoptNative(bp);
    }
  }

  private fire(): void {
    this.changeEmitter.fire();
  }

  // -- native gutter sync ---------------------------------------------------

  private async onNativeChange(ev: vscode.BreakpointsChangeEvent): Promise<void> {
    for (const bp of ev.added) {
      const model = this.adoptNative(bp);
      if (model && this.state.connected) {
        await this.arm(model);
      }
    }
    for (const bp of ev.removed) {
      const model = this.all.find((m) => m.nativeId === bp.id);
      if (model) {
        await this.remove(model, { keepNative: true });
      }
    }
    for (const bp of ev.changed) {
      const model = this.all.find((m) => m.nativeId === bp.id);
      if (
        model &&
        bp instanceof vscode.SourceBreakpoint &&
        model.source &&
        model.source.line !== bp.location.range.start.line + 1
      ) {
        model.source.line = bp.location.range.start.line + 1;
        model.address = null;
        if (this.state.connected) {
          await this.disarm(model);
          await this.arm(model);
        }
      }
    }
    this.fire();
  }

  private adoptNative(bp: vscode.Breakpoint): S32Breakpoint | null {
    if (!(bp instanceof vscode.SourceBreakpoint)) {
      return null;
    }
    if (this.all.some((m) => m.nativeId === bp.id)) {
      return null;
    }
    const uri = bp.location.uri;
    const line = bp.location.range.start.line + 1;
    const core = this.defaultCore();
    let model: S32Breakpoint | null = null;
    if (uri.scheme === "file") {
      model = {
        id: nextId(),
        core,
        address: null,
        source: { fsPath: uri.fsPath, line },
        slot: null,
        state: "pending",
        nativeId: bp.id,
        origin: "gutter",
      };
    } else if (uri.scheme === "s32e-disasm") {
      const addr = this.disasmLineToAddr?.(uri, line) ?? null;
      if (addr === null) {
        return null;
      }
      model = {
        id: nextId(),
        core,
        address: addr,
        slot: null,
        state: "pending",
        nativeId: bp.id,
        origin: "disasm",
      };
    }
    if (model) {
      this.all.push(model);
      this.fire();
    }
    return model;
  }

  private defaultCore(): string {
    return this.state.focus ?? "rtu0.0";
  }

  // -- arming ---------------------------------------------------------------

  private async resolveAddress(model: S32Breakpoint): Promise<number | null> {
    if (model.address !== null) {
      return model.address;
    }
    if (!model.source) {
      return null;
    }
    const client = this.state.requireClient();
    const base = path.basename(model.source.fsPath);
    const result = (await client.run(
      `sym line2addr "${base}" ${model.source.line}`,
    )) as { line: number | null; addrs: string[] };
    if (!result.addrs?.length) {
      throw new BackendError(
        "usage",
        `no code for ${base}:${model.source.line}`,
        "sym line2addr",
      );
    }
    return Number.parseInt(result.addrs[0], 16);
  }

  async arm(model: S32Breakpoint): Promise<void> {
    if (!this.state.connected) {
      return;
    }
    try {
      const addr = await this.resolveAddress(model);
      if (addr === null) {
        throw new BackendError("usage", "no address", "bp set");
      }
      model.address = addr;
      const client = this.state.requireClient();
      const family = familyOf(model.core);
      const cmd =
        family === "m33"
          ? `m33 bp set ${toHex(addr)} --core ${model.core}`
          : `bp set ${toHex(addr)} --core ${model.core}`;
      const result = (await client.run(cmd)) as {
        breakpoint: { index: number };
      };
      model.slot = result.breakpoint.index;
      model.state = "armed";
      model.error = undefined;
    } catch (err) {
      model.state = "failed";
      model.error = err instanceof Error ? err.message : String(err);
      this.output.line(`!! breakpoint ${model.id}: ${model.error}`);
    }
    this.fire();
  }

  async disarm(model: S32Breakpoint): Promise<void> {
    if (model.state === "armed" && model.slot !== null && this.state.connected) {
      const family = familyOf(model.core);
      const cmd =
        family === "m33"
          ? `m33 bp clear ${model.slot} --core ${model.core}`
          : `bp clear ${model.slot} --core ${model.core}`;
      try {
        await this.state.requireClient().run(cmd);
      } catch (err) {
        this.output.line(
          `!! clearing breakpoint slot ${model.slot} on ${model.core}: ${err}`,
        );
      }
    }
    model.slot = null;
    if (model.state === "armed") {
      model.state = "pending";
    }
    this.fire();
  }

  // -- public operations ----------------------------------------------------

  addByAddress(core: string, address: number): S32Breakpoint {
    const model: S32Breakpoint = {
      id: nextId(),
      core,
      address,
      slot: null,
      state: "pending",
      origin: "address",
    };
    this.all.push(model);
    this.fire();
    if (this.state.connected) {
      void this.arm(model);
    }
    return model;
  }

  async remove(
    model: S32Breakpoint,
    { keepNative = false }: { keepNative?: boolean } = {},
  ): Promise<void> {
    await this.disarm(model);
    const i = this.all.indexOf(model);
    if (i >= 0) {
      this.all.splice(i, 1);
    }
    if (!keepNative && model.nativeId) {
      const native = vscode.debug.breakpoints.find((b) => b.id === model.nativeId);
      if (native) {
        vscode.debug.removeBreakpoints([native]);
      }
    }
    this.fire();
  }

  async retarget(model: S32Breakpoint, newCore: string): Promise<void> {
    await this.disarm(model);
    model.core = newCore;
    model.state = "pending";
    if (familyOf(newCore) !== "r52" || familyOf(model.core) !== "r52") {
      // address may still be fine; comparators differ but addresses carry over
    }
    if (this.state.connected) {
      await this.arm(model);
    }
    this.fire();
  }

  duplicate(model: S32Breakpoint, newCore: string): S32Breakpoint {
    const copy: S32Breakpoint = {
      ...model,
      id: nextId(),
      core: newCore,
      slot: null,
      state: "pending",
      nativeId: undefined,
      source: model.source ? { ...model.source } : undefined,
    };
    this.all.push(copy);
    this.fire();
    if (this.state.connected) {
      void this.arm(copy);
    }
    return copy;
  }

  /** Arm everything pending -- after connect, sym load, load, reset-halt. */
  async applyAll(): Promise<void> {
    for (const model of this.all) {
      if (model.state !== "armed") {
        await this.arm(model);
      }
    }
  }

  /** The target lost its comparators (SoC reset): re-arm what we had. */
  async reapplyAll(): Promise<void> {
    for (const model of this.all) {
      if (model.state === "armed") {
        model.state = "pending";
        model.slot = null;
      }
    }
    this.fire();
    await this.applyAll();
  }

  /** Session gone: slots are meaningless, everything is pending again. */
  markAllPending(): void {
    for (const model of this.all) {
      if (model.state === "armed") {
        model.state = "pending";
        model.slot = null;
      }
    }
    this.fire();
  }

  async clearAll(): Promise<void> {
    for (const model of [...this.all]) {
      await this.remove(model);
    }
  }

  dispose(): void {
    for (const s of this.subs) {
      s.dispose();
    }
    this.changeEmitter.dispose();
  }
}

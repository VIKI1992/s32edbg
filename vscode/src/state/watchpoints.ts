import * as vscode from "vscode";
import { toHex } from "../backend/types";
import { Output } from "../ui/output";
import { DebugState, familyOf } from "./session";

export interface S32Watchpoint {
  id: string;
  core: string;
  address: number;
  size: number;
  on: "load" | "store" | "both";
  slot: number | null;
  state: "pending" | "armed" | "failed";
  error?: string;
  /** Adopted from a comparator this session did not arm (a leftover kept
   * alive by --keep-breakpoints). Dropped, not re-armed, on disconnect. */
  adopted?: boolean;
}

let counter = 0;
const nextId = () => `s32wp-${++counter}`;

/** Panel-managed data watchpoints (no native VS Code concept to mirror). */
export class WatchpointStore implements vscode.Disposable {
  readonly all: S32Watchpoint[] = [];
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changeEmitter.event;

  constructor(
    private readonly state: DebugState,
    private readonly output: Output,
  ) {}

  private fire(): void {
    this.changeEmitter.fire();
  }

  add(core: string, address: number, size: number, on: S32Watchpoint["on"]): S32Watchpoint {
    const model: S32Watchpoint = {
      id: nextId(),
      core,
      address,
      size,
      on,
      slot: null,
      state: "pending",
    };
    this.all.push(model);
    this.fire();
    if (this.state.connected) {
      void this.arm(model);
    }
    return model;
  }

  async arm(model: S32Watchpoint): Promise<void> {
    try {
      const client = this.state.requireClient();
      const family = familyOf(model.core);
      const cmd =
        family === "m33"
          ? `m33 wp set ${toHex(model.address)} --size ${model.size} --on ${model.on} --core ${model.core}`
          : `wp set ${toHex(model.address)} --size ${model.size} --on ${model.on} --core ${model.core}`;
      const result = (await client.run(cmd)) as { watchpoint: { index: number } };
      model.slot = result.watchpoint.index;
      model.state = "armed";
      model.error = undefined;
    } catch (err) {
      model.state = "failed";
      model.error = err instanceof Error ? err.message : String(err);
      this.output.line(`!! watchpoint ${model.id}: ${model.error}`);
    }
    this.fire();
  }

  /** Adopt a watchpoint comparator this session did NOT arm: the slot is
   * already live, so no `wp set` is issued -- the row is born armed with
   * the size/access the hardware decodes. Deleting the row goes through
   * the ordinary path, which clears the slot by index. */
  adoptForeign(
    core: string,
    address: number,
    slot: number,
    size: number,
    on: string,
  ): S32Watchpoint {
    const access: S32Watchpoint["on"] =
      on === "load" || on === "store" ? on : "both";
    const model: S32Watchpoint = {
      id: nextId(),
      core,
      address,
      size,
      on: access,
      slot,
      state: "armed",
      adopted: true,
    };
    this.all.push(model);
    this.fire();
    return model;
  }

  async remove(model: S32Watchpoint): Promise<void> {
    if (model.state === "armed" && model.slot !== null && this.state.connected) {
      const family = familyOf(model.core);
      const cmd =
        family === "m33"
          ? `m33 wp clear ${model.slot} --core ${model.core}`
          : `wp clear ${model.slot} --core ${model.core}`;
      try {
        await this.state.requireClient().run(cmd);
      } catch (err) {
        this.output.line(`!! clearing watchpoint: ${err}`);
      }
    }
    const i = this.all.indexOf(model);
    if (i >= 0) {
      this.all.splice(i, 1);
    }
    this.fire();
  }

  async applyAll(): Promise<void> {
    for (const model of this.all) {
      if (model.state !== "armed") {
        await this.arm(model);
      }
    }
  }

  async reapplyAll(): Promise<void> {
    for (const model of this.all) {
      if (model.state === "armed") {
        model.state = "pending";
        model.slot = null;
      }
    }
    await this.applyAll();
  }

  /** Session gone -- adopted rows are dropped (the next session's sweep
   * re-adopts what is really armed); everything else goes back to pending. */
  markAllPending(): void {
    for (const model of [...this.all]) {
      if (model.adopted) {
        const i = this.all.indexOf(model);
        if (i >= 0) {
          this.all.splice(i, 1);
        }
        continue;
      }
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
    this.changeEmitter.dispose();
  }
}

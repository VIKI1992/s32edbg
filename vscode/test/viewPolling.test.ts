import { describe, expect, it } from "vitest";
import * as vscode from "vscode";
import {
  autoRefreshItem,
  isAutoRefreshNode,
  PolledView,
  ViewPolling,
} from "../src/state/viewPolling";

class FakeMemento implements vscode.Memento {
  private store = new Map<string, unknown>();
  keys(): readonly string[] {
    return [...this.store.keys()];
  }
  get<T>(key: string, defaultValue?: T): T {
    return (this.store.get(key) as T) ?? (defaultValue as T);
  }
  async update(key: string, value: unknown): Promise<void> {
    this.store.set(key, value);
  }
}

describe("ViewPolling defaults", () => {
  it("everything on except memory (a dump per region per stop)", () => {
    const p = new ViewPolling(new FakeMemento());
    const onByDefault = [
      "cores", "variables", "callStack", "registers",
      "breakpoints", "watchpoints",
    ] as const;
    for (const view of onByDefault) {
      expect(p.on(view)).toBe(true);
    }
    expect(p.on("memory")).toBe(false);
  });
});

describe("ViewPolling set/persist", () => {
  it("persists through the memento and fires per change", async () => {
    const memento = new FakeMemento();
    const p = new ViewPolling(memento);
    const fired: PolledView[] = [];
    p.onDidChange((view) => fired.push(view));

    await p.set("cores", false);
    await p.set("memory", true);
    expect(p.on("cores")).toBe(false);
    expect(p.on("memory")).toBe(true);
    expect(fired).toEqual(["cores", "memory"]);

    // A no-op set is silent -- checkbox events echo the current state.
    await p.set("cores", false);
    expect(fired).toEqual(["cores", "memory"]);

    // A fresh instance over the same memento reads the same answers.
    const again = new ViewPolling(memento);
    expect(again.on("cores")).toBe(false);
    expect(again.on("memory")).toBe(true);
    expect(again.on("variables")).toBe(true);
  });
});

describe("autoRefreshItem", () => {
  it("renders the checkbox with a stable id and a paused marker", () => {
    const on = autoRefreshItem("cores", true);
    expect(on.id).toBe("s32e.autorefresh.cores");
    expect(on.checkboxState).toBe(vscode.TreeItemCheckboxState.Checked);
    expect(on.description).not.toContain("paused");

    const off = autoRefreshItem("cores", false);
    expect(off.checkboxState).toBe(vscode.TreeItemCheckboxState.Unchecked);
    expect(off.description).toContain("paused");
  });
});

describe("isAutoRefreshNode", () => {
  it("recognises only the marker nodes", () => {
    expect(isAutoRefreshNode({ autoRefresh: "cores" })).toBe(true);
    expect(isAutoRefreshNode({ kind: "frame" })).toBe(false);
    expect(isAutoRefreshNode(null)).toBe(false);
    expect(isAutoRefreshNode("x")).toBe(false);
  });
});

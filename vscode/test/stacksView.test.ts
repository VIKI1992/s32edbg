import { describe, expect, it } from "vitest";
import * as vscode from "vscode";
import { StackScanRow } from "../src/backend/types";
import { DebugState } from "../src/state/session";
import { StacksState } from "../src/state/stacks";
import {
  stackBar,
  stackDescription,
  stackSeverity,
  stackTooltip,
  StacksView,
} from "../src/views/stacksView";

function row(over: Partial<StackScanRow> = {}): StackScanRow {
  return {
    name: "S",
    core: "OsCore0",
    type: "TASK_EXTENDED_STACK",
    users: [],
    address: "0x30000000",
    size: 1024,
    size_elf: 1024,
    used: 100,
    free: 924,
    pct: 9.8,
    overflow: false,
    watermark: "0x300000E4",
    note: null,
    error: null,
    ...over,
  };
}

function makeState(): StacksState {
  const store = new Map<string, unknown>();
  const memento = {
    get: <T>(key: string, fallback: T) =>
      store.has(key) ? (store.get(key) as T) : fallback,
    update: async (key: string, value: unknown) => {
      store.set(key, value);
    },
  };
  return new StacksState(memento as unknown as vscode.Memento);
}

const fakeDebugState = {
  onDidChange: () => ({ dispose() {} }),
} as unknown as DebugState;

describe("stackBar", () => {
  it("renders ten cells proportional to pct", () => {
    expect(stackBar(0)).toBe("▱▱▱▱▱▱▱▱▱▱");
    expect(stackBar(50)).toBe("▰▰▰▰▰▱▱▱▱▱");
    expect(stackBar(100)).toBe("▰▰▰▰▰▰▰▰▰▰");
    expect(stackBar(105)).toBe("▰▰▰▰▰▰▰▰▰▰");
    expect(stackBar(null)).toBe("");
  });
});

describe("stackSeverity", () => {
  it("applies the 80/95 thresholds and the overflow override", () => {
    expect(stackSeverity(row({ pct: 12 }))).toBe("ok");
    expect(stackSeverity(row({ pct: 79.9 }))).toBe("ok");
    expect(stackSeverity(row({ pct: 80 }))).toBe("warning");
    expect(stackSeverity(row({ pct: 94.9 }))).toBe("warning");
    expect(stackSeverity(row({ pct: 95 }))).toBe("error");
    expect(stackSeverity(row({ pct: 5, overflow: true }))).toBe("error");
    expect(stackSeverity(row({ error: "boom", pct: null }))).toBe("unknown");
  });
});

describe("stackDescription / stackTooltip", () => {
  it("shows bar, percentage, bytes and the overflow marker", () => {
    const d = stackDescription(row({ pct: 50, used: 512, size: 1024 }));
    expect(d).toContain("▰▰▰▰▰▱▱▱▱▱");
    expect(d).toContain("50.0%");
    expect(d).toContain("512/1024 B");
    expect(d).not.toContain("OVERFLOW");
    expect(
      stackDescription(row({ overflow: true })),
    ).toContain("OVERFLOW");
  });
  it("passes errors through verbatim", () => {
    expect(stackDescription(row({ error: "not in the loaded ELF" }))).toBe(
      "not in the loaded ELF",
    );
  });
  it("tooltip carries users, range, watermark and notes", () => {
    const t = stackTooltip(
      row({
        users: ["InitTask", "TimerIsr"],
        note: "size mismatch",
      }),
    );
    expect(t).toContain("users: InitTask, TimerIsr");
    expect(t).toContain("range: 0x30000000 + 1024 B");
    expect(t).toContain("high-water mark: 0x300000E4");
    expect(t).toContain("note: size mismatch");
  });
});

describe("StacksView", () => {
  it("hints at project selection until an ARXML is chosen", () => {
    const stacks = makeState();
    const view = new StacksView(fakeDebugState, stacks);
    const children = view.getChildren();
    expect(children).toEqual([{ hint: true }]);
    const item = view.getTreeItem(children[0]);
    expect(item.label).toContain("Select project");
    expect(item.command?.command).toBe("s32e.stacks.selectProject");
  });

  it("lists a header plus rows sorted core-then-name", async () => {
    const stacks = makeState();
    await stacks.set("C:\\proj", "C:\\proj\\cfg\\app_Os_ecuc.arxml");
    stacks.noteScan([
      row({ name: "Zeta", core: "OsCore1" }),
      row({ name: "Beta", core: "OsCore0" }),
      row({ name: "Alpha", core: "OsCore0" }),
    ]);
    const view = new StacksView(fakeDebugState, stacks);
    const children = view.getChildren();
    expect(children[0]).toEqual({ header: true });
    expect(
      children.slice(1).map((n) => (n as StackScanRow).name),
    ).toEqual(["Alpha", "Beta", "Zeta"]);
    const header = view.getTreeItem(children[0]);
    expect(header.label).toBe("app_Os_ecuc.arxml");
    expect(header.description).toContain("scanned");
  });

  it("marks error rows as unknown with the message in view", () => {
    const stacks = makeState();
    const view = new StacksView(fakeDebugState, stacks);
    const item = view.getTreeItem(
      row({ error: "OsCfg_Stack_Ghost_Dyn not in the loaded ELF", pct: null }),
    );
    expect((item.iconPath as vscode.ThemeIcon).id).toBe("question");
    expect(item.description).toContain("Ghost");
  });

  it("header says not-scanned-yet before the first refresh", async () => {
    const stacks = makeState();
    await stacks.set(null, "C:\\cfg\\os.arxml");
    const view = new StacksView(fakeDebugState, stacks);
    const header = view.getTreeItem(view.getChildren()[0]);
    expect(header.description).toContain("not scanned yet");
  });
});

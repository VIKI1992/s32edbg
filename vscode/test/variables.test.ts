import { describe, expect, it } from "vitest";
import { VarRow } from "../src/backend/types";
import {
  isEditable,
  isStaleHandleError,
  rowDescription,
  rowKey,
  validateWatchPath,
} from "../src/views/variablesFormat";

function row(partial: Partial<VarRow>): VarRow {
  return {
    handle: "0:1", name: "x", path: "x", type: "int", kind: "int",
    value: { display: "42", hex: "0x0000002A" },
    address: "0x31780F14", register: null, size: 4,
    expandable: false, count: null, static: false, error: null,
    ...partial,
  };
}

describe("rowDescription hex toggle", () => {
  it("flips ints and uints only", () => {
    expect(rowDescription(row({}), false)).toBe("42");
    expect(rowDescription(row({}), true)).toBe("0x0000002A");
    const en = row({ kind: "enum", value: { display: "MODE_RUN (1)", hex: "0x01" } });
    expect(rowDescription(en, true)).toBe("MODE_RUN (1)");
    const ptr = row({ kind: "pointer", value: { display: "0x32100094", hex: "0x32100094" } });
    expect(rowDescription(ptr, false)).toBe("0x32100094");
  });
  it("errors win", () => {
    expect(rowDescription(row({ error: "no frame base" }), false)).toContain("no frame base");
  });
});

describe("rowKey", () => {
  it("is stable and section-scoped", () => {
    expect(rowKey("locals", "cfg.limits[1].max")).toBe("locals/cfg.limits[1].max");
    expect(rowKey("watch", "x")).not.toBe(rowKey("globals", "x"));
  });
});

describe("validateWatchPath", () => {
  it.each(["counter", "cfg.limits[2].max", "*ptr", "p->x", "**pp", "a.b[0x10].c"])(
    "accepts %s", (p) => expect(validateWatchPath(p)).toBeNull(),
  );
  it.each(["", "cfg.", "[0]", "cfg..x", "1abc", "a-b"])(
    "rejects %s", (p) => expect(validateWatchPath(p)).not.toBeNull(),
  );
});

describe("stale handle contract", () => {
  it("matches the backend prefix", () => {
    expect(isStaleHandleError("stale-handle: the target ran...")).toBe(true);
    expect(isStaleHandleError("no variable named 'x'")).toBe(false);
  });
});

describe("isEditable", () => {
  it("scalars with an address and sane size", () => {
    expect(isEditable(row({}))).toBe(true);
    expect(isEditable(row({ kind: "struct", expandable: true }))).toBe(false);
    expect(isEditable(row({ size: 8 }))).toBe(false);
    expect(isEditable(row({ error: "x" }))).toBe(false);
    expect(isEditable(row({ handle: null }))).toBe(false);
    expect(isEditable(row({ address: null, register: "r3" }))).toBe(true);
    expect(isEditable(row({ address: null }))).toBe(false);
  });
});

import { describe, expect, it } from "vitest";
import { VarRow } from "../src/backend/types";
import { decodeSample } from "../src/plot/plotValue";

function row(partial: Partial<VarRow>): VarRow {
  return {
    handle: "0:1", name: "x", path: "x", type: "int", kind: "int",
    value: { display: "42", hex: "0x0000002A" },
    address: "0x31780F14", register: null, size: 4,
    expandable: false, count: null, static: false, error: null,
    ...partial,
  };
}

describe("decodeSample", () => {
  it("unsigned and enum come straight from hex", () => {
    expect(decodeSample(row({ kind: "uint", value: { display: "42", hex: "0x0000002A" } }))).toBe(42);
    expect(decodeSample(row({ kind: "enum", value: { display: "MODE_RUN (1)", hex: "0x01" } }))).toBe(1);
  });

  it("signed ints are two's-complement corrected by size", () => {
    expect(decodeSample(row({ kind: "int", size: 4, value: { display: "-1", hex: "0xFFFFFFFF" } }))).toBe(-1);
    expect(decodeSample(row({ kind: "int", size: 2, value: { display: "-2", hex: "0xFFFE" } }))).toBe(-2);
    expect(decodeSample(row({ kind: "int", size: 1, value: { display: "-128", hex: "0x80" } }))).toBe(-128);
    expect(decodeSample(row({ kind: "int", size: 4, value: { display: "42", hex: "0x0000002A" } }))).toBe(42);
  });

  it("bools become a 0/1 square wave", () => {
    expect(decodeSample(row({ kind: "bool", value: { display: "true", hex: "0x01" } }))).toBe(1);
    expect(decodeSample(row({ kind: "bool", value: { display: "false", hex: "0x00" } }))).toBe(0);
  });

  it("floats prefer the backend's display text, else bit-cast", () => {
    expect(decodeSample(row({ kind: "float", value: { display: "1.5", hex: "0x3FC00000" } }))).toBe(1.5);
    expect(
      decodeSample(row({ kind: "float", size: 4, value: { display: "?", hex: "0x3FC00000" } })),
    ).toBe(1.5);
  });

  it("error rows, null values and rubbish yield no sample", () => {
    expect(decodeSample(undefined)).toBeNull();
    expect(decodeSample(row({ value: null }))).toBeNull();
    expect(decodeSample(row({ error: "faulted" }))).toBeNull();
    expect(decodeSample(row({ value: { display: "?", hex: "0xZZ" } }))).toBeNull();
  });
});

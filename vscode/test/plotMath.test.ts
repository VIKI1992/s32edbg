import { describe, expect, it } from "vitest";
import {
  cursorDelta,
  decimate,
  nearestIndex,
  nearestSample,
} from "../src/plot/plotMath";
import { decodeRaw } from "../src/plot/plotValue";

const series = [
  { t: 0, v: 10 },
  { t: 100, v: 20 },
  { t: 200, v: 30 },
  { t: 300, v: 5 },
];

describe("nearestIndex/nearestSample", () => {
  it("finds the closest sample in time", () => {
    expect(nearestIndex(series, -50)).toBe(0);
    expect(nearestIndex(series, 149)).toBe(1);
    expect(nearestIndex(series, 151)).toBe(2);
    expect(nearestIndex(series, 1000)).toBe(3);
    expect(nearestSample(series, 100)?.v).toBe(20);
    expect(nearestSample([], 0)).toBeNull();
  });
});

describe("cursorDelta", () => {
  it("reports values at A and B plus the difference", () => {
    const d = cursorDelta(series, 90, 310);
    expect(d.a?.v).toBe(20);
    expect(d.b?.v).toBe(5);
    expect(d.dv).toBe(-15);
  });
  it("is null-safe on empty series", () => {
    expect(cursorDelta([], 0, 1).dv).toBeNull();
  });
});

describe("decimate", () => {
  it("bins by column with min/max envelope", () => {
    const dense = [];
    for (let i = 0; i < 1000; i++) {
      dense.push({ t: i, v: i % 2 === 0 ? 0 : 100 });
    }
    const cols = decimate(dense, 0, 1000, 10);
    expect(cols.length).toBe(10);
    for (const c of cols) {
      expect(c.min).toBe(0);
      expect(c.max).toBe(100);
    }
  });
  it("skips samples outside the window and empty input", () => {
    expect(decimate(series, 1000, 2000, 10)).toEqual([]);
    expect(decimate([], 0, 100, 10)).toEqual([]);
  });
  it("keeps per-column last for the trend line", () => {
    const cols = decimate(
      [{ t: 0, v: 1 }, { t: 1, v: 9 }, { t: 99, v: 4 }], 0, 100, 1,
    );
    expect(cols).toEqual([{ x: 0, min: 1, max: 9, last: 4 }]);
  });
});

describe("decodeRaw", () => {
  it("handles signed, bool, float32 and default unsigned", () => {
    expect(decodeRaw("uint", 4, 42)).toBe(42);
    expect(decodeRaw("int", 4, 0xffffffff)).toBe(-1);
    expect(decodeRaw("int", 2, 0xfffe)).toBe(-2);
    expect(decodeRaw("bool", 1, 7)).toBe(1);
    expect(decodeRaw("float", 4, 0x3fc00000)).toBe(1.5);
    expect(decodeRaw("enum", 1, 3)).toBe(3);
    expect(decodeRaw("uint", 4, Number.NaN)).toBeNull();
  });
});

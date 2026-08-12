import { describe, expect, it } from "vitest";
import { planAdoptions } from "../src/state/adoption";
import { CoreVerdicts, HwOrphan } from "../src/state/comparators";

function verdicts(orphans: HwOrphan[]): CoreVerdicts {
  return { rows: new Map(), orphans, unreadable: false };
}

describe("planAdoptions", () => {
  it("adopts a fresh breakpoint orphan with core and unit from the key", () => {
    const results = new Map([
      ["rtu0.0#bp", verdicts([{ index: 3, address: "0x32345678" }])],
    ]);
    expect(planAdoptions(results, [], [])).toEqual([
      { unit: "bp", core: "rtu0.0", index: 3, address: 0x32345678 },
    ]);
  });

  it("carries watchpoint size and access through", () => {
    const results = new Map([
      ["ce0#wp", verdicts([
        { index: 1, address: "0x317BA000", bytes: 2, on: "store" },
      ])],
    ]);
    expect(planAdoptions(results, [], [])).toEqual([
      {
        unit: "wp", core: "ce0", index: 1, address: 0x317ba000,
        bytes: 2, on: "store",
      },
    ]);
  });

  it("never re-adopts a slot an armed row already claims", () => {
    const results = new Map([
      ["rtu0.0#bp", verdicts([{ index: 3, address: "0x32345678" }])],
    ]);
    const claimed = [{ core: "rtu0.0", slot: 3 }];
    expect(planAdoptions(results, claimed, [])).toEqual([]);
  });

  it("a pending row (slot null) does not block adoption", () => {
    const results = new Map([
      ["rtu0.0#bp", verdicts([{ index: 3, address: "0x32345678" }])],
    ]);
    const pending = [{ core: "rtu0.0", slot: null }];
    expect(planAdoptions(results, pending, [])).toHaveLength(1);
  });

  it("claims are per unit and per core", () => {
    const results = new Map([
      ["rtu0.0#bp", verdicts([{ index: 2, address: "0x32100000" }])],
      ["rtu0.1#bp", verdicts([{ index: 2, address: "0x32100000" }])],
    ]);
    // The same slot number claimed on ANOTHER core must not suppress this one,
    // and a watchpoint claim must not suppress a breakpoint orphan.
    const bpClaims = [{ core: "rtu0.1", slot: 2 }];
    const wpClaims = [{ core: "rtu0.0", slot: 2 }];
    const actions = planAdoptions(results, bpClaims, wpClaims);
    expect(actions).toEqual([
      { unit: "bp", core: "rtu0.0", index: 2, address: 0x32100000 },
    ]);
  });

  it("skips orphans whose address did not decode", () => {
    const results = new Map([
      ["rtu0.0#bp", verdicts([{ index: 5, address: "?" }])],
    ]);
    expect(planAdoptions(results, [], [])).toEqual([]);
  });
});

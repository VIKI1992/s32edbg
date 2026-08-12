import { describe, expect, it } from "vitest";
import { HwSlot, reconcileCore } from "../src/state/comparators";

const armed = (id: string, slot: number, address: number) => ({
  id, slot, address, state: "armed",
});

const hwOn = (index: number, address: string): HwSlot => ({
  index, enabled: true, address,
});
const hwOff = (index: number): HwSlot => ({ index, enabled: false, address: "0x00000000" });
const hwErr = (index: number): HwSlot => ({
  index, error: "comparator registers did not read",
});

describe("reconcileCore verdicts", () => {
  it("verifies an armed row whose slot holds its address", () => {
    const out = reconcileCore(
      [armed("a", 0, 0x321d2ee6)],
      [hwOn(0, "0x321D2EE6"), hwOff(1)],
    );
    expect(out.rows.get("a")?.verdict).toBe("verified");
    expect(out.orphans).toEqual([]);
    expect(out.unreadable).toBe(false);
  });

  it("thumb bit does not count as a mismatch", () => {
    const out = reconcileCore([armed("a", 0, 0x32100001)], [hwOn(0, "0x32100000")]);
    expect(out.rows.get("a")?.verdict).toBe("verified");
  });

  it("flags a disabled slot and a wrong address as mismatches", () => {
    const out = reconcileCore(
      [armed("a", 0, 0x32100000), armed("b", 1, 0x32200000)],
      [hwOff(0), hwOn(1, "0x33300000")],
    );
    expect(out.rows.get("a")?.verdict).toBe("mismatch");
    expect(out.rows.get("a")?.reason).toContain("DISABLED");
    expect(out.rows.get("b")?.verdict).toBe("mismatch");
    expect(out.rows.get("b")?.reason).toContain("0x33300000");
  });

  it("reports enabled slots nobody claims as orphans", () => {
    const out = reconcileCore(
      [armed("a", 0, 0x32100000)],
      [hwOn(0, "0x32100000"), hwOn(3, "0x32345678")],
    );
    expect(out.orphans).toEqual([{ index: 3, address: "0x32345678" }]);
  });

  it("orphans carry watchpoint size/access through for adoption", () => {
    const out = reconcileCore(
      [],
      [{ index: 1, enabled: true, address: "0x317BA000", bytes: 2, on: "store" }],
    );
    expect(out.orphans).toEqual([
      { index: 1, address: "0x317BA000", bytes: 2, on: "store" },
    ]);
  });

  it("all-error slots mean unknown, never a demotion", () => {
    // OS lock / reset hold: the whole bank reads as errors. That is an
    // absence of information -- the local rows must survive untouched.
    const out = reconcileCore(
      [armed("a", 0, 0x32100000)],
      [hwErr(0), hwErr(1), hwErr(2)],
    );
    expect(out.unreadable).toBe(true);
    expect(out.rows.get("a")?.verdict).toBe("unknown");
    expect(out.orphans).toEqual([]);
  });

  it("a single unreadable slot is unknown for its row only", () => {
    const out = reconcileCore(
      [armed("a", 0, 0x32100000), armed("b", 1, 0x32200000)],
      [hwErr(0), hwOn(1, "0x32200000")],
    );
    expect(out.rows.get("a")?.verdict).toBe("unknown");
    expect(out.rows.get("b")?.verdict).toBe("verified");
  });

  it("pending and failed rows get no verdict", () => {
    const out = reconcileCore(
      [
        { id: "p", slot: null, address: 0x32100000, state: "pending" },
        { id: "f", slot: null, address: 0x32200000, state: "failed" },
      ],
      [hwOff(0), hwOff(1)],
    );
    expect(out.rows.size).toBe(0);
  });
});

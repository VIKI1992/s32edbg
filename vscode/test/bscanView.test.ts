import { beforeEach, describe, expect, it } from "vitest";
import * as vscode from "vscode";
import { configValues } from "./mocks/vscode";
import { BscanPinRow } from "../src/backend/types";
import { BscanState } from "../src/state/bscan";
import { DebugState } from "../src/state/session";
import {
  BscanView,
  driverBadge,
  pinDescription,
  pinLevelIcon,
  pinTooltip,
} from "../src/views/bscanView";

function row(over: Partial<BscanPinRow> = {}): BscanPinRow {
  return {
    name: "RESET_B",
    ball: "AC29",
    cell: 167,
    direction: "inout",
    function: "bidir",
    level: 1,
    driver_enabled: false,
    sample_input_blocked: false,
    control_cell: 166,
    ...over,
  };
}

function fakeState(connected: boolean): DebugState {
  return {
    connected,
    onDidChange: () => ({ dispose() {} }),
  } as unknown as DebugState;
}

beforeEach(() => configValues.clear());

describe("pinLevelIcon", () => {
  it("maps levels to filled/outline and unknowns to a question mark", () => {
    expect(pinLevelIcon(row({ level: 1 }))).toBe("circle-large-filled");
    expect(pinLevelIcon(row({ level: 0 }))).toBe("circle-large-outline");
    expect(pinLevelIcon(row({ level: undefined }))).toBe("question");
  });
});

describe("driverBadge", () => {
  it("is always present: on, off, or always-on for control-less pins", () => {
    expect(driverBadge(row({ driver_enabled: true }))).toBe("drv:on");
    expect(driverBadge(row({ driver_enabled: false }))).toBe("drv:off");
    expect(driverBadge(row({ driver_enabled: null, control_cell: null }))).toBe(
      "always-on",
    );
  });
});

describe("pinDescription", () => {
  it("always shows level, ball and driver state", () => {
    expect(pinDescription(row())).toBe("1  AC29  drv:off");
    expect(pinDescription(row({ driver_enabled: true }))).toContain("drv:on");
    expect(
      pinDescription(row({ name: "DDR0_DQ0", sample_input_blocked: true })),
    ).toContain("SAMPLE-blocked");
  });
  it("leads with the BS-drive marker when boundary scan holds the pin", () => {
    const d = pinDescription(row(), "0");
    expect(d.startsWith("⚡ BS-driving 0")).toBe(true);
    expect(d).toContain("drv:off");
  });
  it("shows the SIUL2 pad label between ball and driver state when known", () => {
    expect(pinDescription(row({ pad: "PAD_086" }))).toBe(
      "1  AC29  PAD_086  drv:off",
    );
    expect(pinDescription(row({ pad: null }))).toBe("1  AC29  drv:off");
  });
});

describe("pinTooltip pad detail", () => {
  it("carries the register-level identity when the pad is mapped", () => {
    const t = pinTooltip(
      row({ pad: "PAD_086", pad_detail: "SIUL2_1 MSCR 86" }),
    );
    expect(t).toContain("SIUL2 pad: PAD_086  (SIUL2_1 MSCR 86)");
    expect(pinTooltip(row())).not.toContain("SIUL2 pad:");
  });
});

describe("pinTooltip", () => {
  it("explains the driver state in words", () => {
    expect(pinTooltip(row({ driver_enabled: true }))).toContain("driving this pin");
    expect(pinTooltip(row({ driver_enabled: false }))).toContain("external/input");
    expect(
      pinTooltip(row({ name: "DDR0_RESET_N", driver_enabled: null, control_cell: null })),
    ).toContain("always driven");
    expect(
      pinTooltip(row({ sample_input_blocked: true })),
    ).toContain("DESIGN_WARNING");
  });
});

describe("BscanView", () => {
  it("shows nothing while connected so the welcome content explains", () => {
    const view = new BscanView(fakeState(true), new BscanState());
    expect(view.getChildren()).toEqual([]);
  });

  it("lists a header plus rows sorted by cell while disconnected", () => {
    const bscan = new BscanState();
    bscan.noteSample(
      "bundled:SIP.bsdl",
      [row({ cell: 300, name: "Z_PIN" }), row({ cell: 5, name: "A_PIN" })],
      "0xAB",
    );
    const view = new BscanView(fakeState(false), bscan);
    const children = view.getChildren();
    expect(children[0]).toEqual({ header: true });
    expect(children.slice(1).map((n) => (n as BscanPinRow).name)).toEqual([
      "A_PIN",
      "Z_PIN",
    ]);
    const header = view.getTreeItem(children[0]);
    expect(header.label).toBe("bundled BSDL");
    expect(header.description).toContain("sampled");
  });

  it("header says not-sampled-yet before the first refresh", () => {
    const view = new BscanView(fakeState(false), new BscanState());
    const header = view.getTreeItem(view.getChildren()[0]);
    expect(header.description).toContain("not sampled yet");
  });

  it("header label follows the s32e.bscan.bsdlPath setting", () => {
    configValues.set("s32e.bscan.bsdlPath", "C:\\vendor\\custom_chip.bsdl");
    const view = new BscanView(fakeState(false), new BscanState());
    const header = view.getTreeItem(view.getChildren()[0]);
    expect(header.label).toBe("custom_chip.bsdl");
  });

  it("pin rows carry the level icon and tooltip", () => {
    const bscan = new BscanState();
    bscan.noteSample(null, [row({ level: 0 })], "0x0");
    const view = new BscanView(fakeState(false), bscan);
    const item = view.getTreeItem(view.getChildren()[1]);
    expect((item.iconPath as vscode.ThemeIcon).id).toBe("circle-large-outline");
    expect(item.description).toContain("AC29");
    expect(item.tooltip).toContain("cell 167");
    expect(item.contextValue).toBe("bscanPin");
  });

  it("SoC-driving rows get a colored icon", () => {
    const bscan = new BscanState();
    bscan.noteSample(null, [row({ driver_enabled: true })], "0x0");
    const view = new BscanView(fakeState(false), bscan);
    const item = view.getTreeItem(view.getChildren()[1]);
    const icon = item.iconPath as vscode.ThemeIcon;
    expect(icon.id).toBe("circle-large-filled");
    expect((icon.color as vscode.ThemeColor | undefined)).toBeDefined();
  });

  it("BS-driven rows get the zap icon, the marker and the Driven context", () => {
    const bscan = new BscanState();
    bscan.noteSample(null, [row()], "0x0");
    bscan.setDrive("RESET_B", "0");
    bscan.driveLatched = true;
    const view = new BscanView(fakeState(false), bscan);
    const item = view.getTreeItem(view.getChildren()[1]);
    expect((item.iconPath as vscode.ThemeIcon).id).toBe("zap");
    expect(item.description).toContain("BS-driving 0");
    expect(item.contextValue).toBe("bscanPinDriven");
    expect(item.tooltip).toContain("EXTEST");
    const header = view.getTreeItem(view.getChildren()[0]);
    expect(header.description).toContain("EXTEST driving 1 pin(s)");
  });

  it("a staged-but-unlatched drive set does not mark rows as driven", () => {
    const bscan = new BscanState();
    bscan.noteSample(null, [row()], "0x0");
    bscan.setDrive("RESET_B", "1");
    const view = new BscanView(fakeState(false), bscan);
    const item = view.getTreeItem(view.getChildren()[1]);
    expect(item.contextValue).toBe("bscanPin");
    expect(item.description).not.toContain("BS-driving");
  });

  it("clearDrives drops the latch and the set together", () => {
    const bscan = new BscanState();
    bscan.setDrive("A", "1");
    bscan.driveLatched = true;
    bscan.clearDrives();
    expect(bscan.driveSet.size).toBe(0);
    expect(bscan.driveLatched).toBe(false);
  });
});

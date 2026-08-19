import * as vscode from "vscode";
import { BscanPinRow } from "../backend/types";
import { BscanState } from "../state/bscan";
import { DebugState } from "../state/session";

type Node = BscanPinRow | { header: true };

/** Icon id for a pin's sampled level. */
export function pinLevelIcon(row: BscanPinRow): string {
  if (row.level === 1) {
    return "circle-large-filled";
  }
  if (row.level === 0) {
    return "circle-large-outline";
  }
  return "question";
}

/** Driver-enable state as always-visible text: the whole point of the column
 * is that "off" is information too (the level is the external/input state). */
export function driverBadge(row: BscanPinRow): string {
  if (row.driver_enabled === true) {
    return "drv:on";
  }
  if (row.driver_enabled === false) {
    return "drv:off";
  }
  return "always-on"; // no control cell (output2): the driver never turns off
}

export function pinDescription(
  row: BscanPinRow,
  bsDrive?: "0" | "1" | "Z",
): string {
  const parts: string[] = [];
  if (bsDrive !== undefined) {
    parts.push(`⚡ BS-driving ${bsDrive}`);
  }
  parts.push(`${row.level ?? "?"}`, row.ball);
  if (row.pad) {
    parts.push(row.pad);
  }
  parts.push(driverBadge(row));
  if (row.sample_input_blocked) {
    parts.push("SAMPLE-blocked");
  }
  return parts.join("  ");
}

export function pinTooltip(row: BscanPinRow, bsDrive?: "0" | "1" | "Z"): string {
  const lines = [`${row.name}  (${row.direction}, ball ${row.ball})`];
  lines.push(`level: ${row.level ?? "?"}  cell ${row.cell}`);
  if (row.pad_detail) {
    lines.push(`SIUL2 pad: ${row.pad ?? "?"}  (${row.pad_detail})`);
  }
  if (bsDrive !== undefined) {
    lines.push(
      `EXTEST: boundary scan is driving this pin to ${bsDrive} (MCU held in ` +
        "reset; right-click → Stop Driving, or Release in the view title)",
    );
  }
  if (row.driver_enabled === true) {
    lines.push("output driver: ON (the SoC is driving this pin)");
  } else if (row.driver_enabled === false) {
    lines.push("output driver: off (pin level is the external/input state)");
  } else {
    lines.push("always driven (no control cell, no Z state)");
  }
  if (row.sample_input_blocked) {
    lines.push(
      "DDR pin: cannot SAMPLE its input in mission mode (BSDL DESIGN_WARNING)" +
        " -- the level shows the output latch",
    );
  }
  return lines.join("\n");
}

/** Refresh-button-only, and inverted from every other view: usable only
 * while DISCONNECTED. A sample is ~62 one-shot JTAGLoad runs (~10 s) and
 * the preamble takes the DAP away until power-cycle, so this runs last. */
export class BscanView implements vscode.TreeDataProvider<Node> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(
    private readonly state: DebugState,
    private readonly bscan: BscanState,
  ) {
    bscan.onDidChange(() => this.changeEmitter.fire());
    state.onDidChange(() => this.changeEmitter.fire());
  }

  refresh(): void {
    this.changeEmitter.fire();
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if ("header" in node) {
      const item = new vscode.TreeItem(this.bscan.bsdlLabel);
      const driving = this.bscan.driveLatched
        ? ` — EXTEST driving ${this.bscan.driveSet.size} pin(s)`
        : "";
      item.description =
        (this.bscan.sampling
          ? "sampling…"
          : this.bscan.sampledAt
            ? `sampled ${this.bscan.sampledAt.toLocaleTimeString()}`
            : "not sampled yet — press ⟳") + driving;
      item.iconPath = new vscode.ThemeIcon("circuit-board");
      item.tooltip =
        (this.bscan.source ?? "bundled BSDL, auto-selected by probed IDCODE") +
        "\nSampling ends DAP access until the board is power-cycled — run it last." +
        "\nSet s32e.bscan.bsdlPath to override the BSDL." +
        (this.bscan.driveLatched
          ? "\nEXTEST is latched: driven pins hold and the MCU is in reset " +
            "until Release or power-cycle."
          : "");
      item.contextValue = "bscanHeader";
      return item;
    }
    const item = new vscode.TreeItem(node.name);
    const bsDrive = this.bscan.driveLatched
      ? this.bscan.driveSet.get(node.name)
      : undefined;
    item.description = pinDescription(node, bsDrive);
    item.iconPath =
      bsDrive !== undefined
        ? new vscode.ThemeIcon("zap", new vscode.ThemeColor("charts.yellow"))
        : node.driver_enabled === true
          ? new vscode.ThemeIcon(
              pinLevelIcon(node), new vscode.ThemeColor("charts.orange"),
            )
          : new vscode.ThemeIcon(pinLevelIcon(node));
    item.tooltip = pinTooltip(node, bsDrive);
    item.contextValue = bsDrive !== undefined ? "bscanPinDriven" : "bscanPin";
    return item;
  }

  getChildren(node?: Node): Node[] {
    if (node) {
      return [];
    }
    if (this.state.connected) {
      // Empty -> the viewsWelcome content explains why (disconnect first).
      return [];
    }
    const rows = [...this.bscan.rows].sort((a, b) => a.cell - b.cell);
    return [{ header: true }, ...rows];
  }
}

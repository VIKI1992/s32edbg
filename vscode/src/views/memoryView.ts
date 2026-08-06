import * as vscode from "vscode";
import { toHex } from "../backend/types";
import { MemProvider, MemView, memUri } from "../editor/memProvider";

type Node = MemView | { hint: true };

export class MemoryView implements vscode.TreeDataProvider<Node> {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(private readonly provider: MemProvider) {
    provider.onDidChange(() => this.changeEmitter.fire());
  }

  refresh(): void {
    this.changeEmitter.fire();
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if ("hint" in node) {
      const item = new vscode.TreeItem("Read memory...");
      item.iconPath = new vscode.ThemeIcon("add");
      item.command = { command: "s32e.readMemory", title: "Read memory" };
      return item;
    }
    const item = new vscode.TreeItem(toHex(node.address));
    item.description = `${node.length} B via ${node.ap}`;
    item.iconPath = new vscode.ThemeIcon("file-binary");
    item.command = {
      command: "vscode.open",
      title: "Open",
      arguments: [memUri(node)],
    };
    item.contextValue = "memregion";
    return item;
  }

  getChildren(node?: Node): Node[] {
    if (node) {
      return [];
    }
    return [...this.provider.open, { hint: true }];
  }
}

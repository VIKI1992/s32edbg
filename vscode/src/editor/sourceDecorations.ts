import * as vscode from "vscode";
import { DISASM_SCHEME } from "./disasmProvider";

/** The current-PC marker: a gutter arrow plus a line highlight, in source
 * editors and the disasm doc -- both at once when both are open. */
export class PcDecorations implements vscode.Disposable {
  private readonly type: vscode.TextEditorDecorationType;
  /** uri string -> zero-based line */
  private current = new Map<string, number>();
  private readonly subs: vscode.Disposable[] = [];

  constructor(extensionUri: vscode.Uri) {
    this.type = vscode.window.createTextEditorDecorationType({
      isWholeLine: true,
      backgroundColor: new vscode.ThemeColor("s32e.pcLineBackground"),
      overviewRulerColor: new vscode.ThemeColor("s32e.pcLineBackground"),
      overviewRulerLane: vscode.OverviewRulerLane.Full,
      gutterIconSize: "contain",
      dark: {
        gutterIconPath: vscode.Uri.joinPath(
          extensionUri, "media", "pc-arrow-dark.svg",
        ),
      },
      light: {
        gutterIconPath: vscode.Uri.joinPath(
          extensionUri, "media", "pc-arrow-light.svg",
        ),
      },
    });
    this.subs.push(
      vscode.window.onDidChangeVisibleTextEditors(() => this.paint()),
      // The disasm provider rewrites its document asynchronously on every
      // render; repaint when the new content lands or the arrow would sit
      // on the previous text (or vanish on a longer rewrite).
      vscode.workspace.onDidChangeTextDocument((ev) => {
        if (ev.document.uri.scheme === DISASM_SCHEME) {
          this.paint();
        }
      }),
    );
  }

  /** A new stop: forget every marker. The afterHalt reveal then set()s the
   * source line and, when a disasm tab is open, the disasm line too. */
  beginStop(): void {
    this.current.clear();
    this.paint();
  }

  /** Mark uri:line (1-based) as the PC location. Other URIs keep theirs --
   * source and disassembly legitimately show the same PC simultaneously. */
  set(uri: vscode.Uri, line1: number): void {
    this.current.set(uri.toString(), line1 - 1);
    this.paint();
  }

  clear(): void {
    this.current.clear();
    this.paint();
  }

  private paint(): void {
    for (const editor of vscode.window.visibleTextEditors) {
      const line = this.current.get(editor.document.uri.toString());
      if (line === undefined || line >= editor.document.lineCount) {
        editor.setDecorations(this.type, []);
      } else {
        editor.setDecorations(this.type, [
          new vscode.Range(line, 0, line, 0),
        ]);
      }
    }
  }

  dispose(): void {
    this.type.dispose();
    for (const s of this.subs) {
      s.dispose();
    }
  }
}

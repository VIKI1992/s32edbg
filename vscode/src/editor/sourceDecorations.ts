import * as vscode from "vscode";

/** The current-PC line highlight in source editors and the disasm doc. */
export class PcDecorations implements vscode.Disposable {
  private readonly type: vscode.TextEditorDecorationType;
  /** uri string -> zero-based line */
  private current = new Map<string, number>();
  private readonly subs: vscode.Disposable[] = [];

  constructor() {
    this.type = vscode.window.createTextEditorDecorationType({
      isWholeLine: true,
      backgroundColor: new vscode.ThemeColor("s32e.pcLineBackground"),
      overviewRulerColor: new vscode.ThemeColor("s32e.pcLineBackground"),
      overviewRulerLane: vscode.OverviewRulerLane.Full,
    });
    this.subs.push(
      vscode.window.onDidChangeVisibleTextEditors(() => this.paint()),
    );
  }

  /** Mark uri:line (1-based) as the PC location; clears other files. */
  set(uri: vscode.Uri, line1: number): void {
    this.current.clear();
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

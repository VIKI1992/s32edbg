import * as fs from "fs";
import * as vscode from "vscode";
import { DisasmRow, hexToInt, toHex } from "../backend/types";
import { setting } from "../config";
import { DebugState } from "../state/session";

export const DISASM_SCHEME = "s32e-disasm";

export function disasmUri(core: string): vscode.Uri {
  // One stable document per core: steps re-render it in place.
  return vscode.Uri.from({
    scheme: DISASM_SCHEME,
    path: `/${core}/disassembly.s32e`,
  });
}

interface Rendered {
  text: string;
  /** zero-based doc line -> instruction address */
  lineToAddr: Map<number, number>;
  addrToLine: Map<number, number>;
  anchor: number;
}

/** Read-only per-core disassembly documents with a line<->address map.
 *
 * Content comes from the backend `disasm` command (ELF bytes first, target
 * memory fallback), with source lines interleaved where the DWARF maps them.
 * The provider re-renders on every halt of the core, keeping the PC visible.
 */
export class DisasmProvider
  implements vscode.TextDocumentContentProvider, vscode.Disposable
{
  private readonly changeEmitter = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this.changeEmitter.event;
  private rendered = new Map<string, Rendered>();
  private sourceCache = new Map<string, string[]>();

  constructor(private readonly state: DebugState) {}

  coreOf(uri: vscode.Uri): string {
    return uri.path.split("/")[1] ?? "rtu0.0";
  }

  lineToAddr(uri: vscode.Uri, line1: number): number | null {
    const r = this.rendered.get(this.coreOf(uri));
    return r?.lineToAddr.get(line1 - 1) ?? null;
  }

  addrToLine(core: string, addr: number): number | null {
    return this.rendered.get(core)?.addrToLine.get(addr & ~1) ?? null;
  }

  hasWindowAround(core: string, addr: number): boolean {
    return this.addrToLine(core, addr) !== null;
  }

  /** Fetch a fresh window and re-render the core's document. */
  async render(core: string, anchor?: number): Promise<void> {
    const client = this.state.requireClient();
    const before = setting<number>("disasm.before", 32);
    const count = before + setting<number>("disasm.after", 96);
    const anchorArg = anchor !== undefined ? ` --addr ${toHex(anchor)}` : "";
    const result = (await client.run(
      `disasm --core ${core}${anchorArg} --before ${before} --count ${count}`,
    )) as { anchor: string; mode: string; source: string; instructions: DisasmRow[] };

    const lines: string[] = [];
    const lineToAddr = new Map<number, number>();
    const addrToLine = new Map<number, number>();
    lines.push(
      `; ${core} -- ${result.mode} -- bytes from ${result.source} -- anchor ${result.anchor}`,
    );
    for (const row of result.instructions) {
      if (row.func) {
        lines.push("");
        lines.push(`${row.func}:`);
      }
      if (row.file && row.line !== undefined) {
        const text = this.sourceLine(row.file, row.line);
        lines.push(`; ${shortPath(row.file)}:${row.line}${text ? `    ${text}` : ""}`);
      }
      const addr = hexToInt(row.addr);
      const docLine = lines.length;
      lines.push(
        `  ${row.addr}:  ${row.bytes.padEnd(10)}  ${row.mnemonic.padEnd(8)} ${row.op_str}`,
      );
      if (addr !== null) {
        lineToAddr.set(docLine, addr);
        addrToLine.set(addr & ~1, docLine);
      }
    }
    this.rendered.set(core, {
      text: lines.join("\n") + "\n",
      lineToAddr,
      addrToLine,
      anchor: hexToInt(result.anchor) ?? 0,
    });
    this.changeEmitter.fire(disasmUri(core));
  }

  private sourceLine(file: string, line: number): string | null {
    let cached = this.sourceCache.get(file);
    if (cached === undefined) {
      try {
        cached = fs.readFileSync(file, "utf-8").split(/\r?\n/);
      } catch {
        cached = [];
      }
      this.sourceCache.set(file, cached);
    }
    return cached[line - 1]?.trim() || null;
  }

  clearSourceCache(): void {
    this.sourceCache.clear();
  }

  provideTextDocumentContent(uri: vscode.Uri): string {
    const r = this.rendered.get(this.coreOf(uri));
    return (
      r?.text ??
      "; no disassembly yet -- connect and halt the core, or run S32E: Open Disassembly\n"
    );
  }

  /** Open (or reveal) the core's disasm doc, rendering around `anchor`. */
  async open(core: string, anchor?: number, reveal = true): Promise<void> {
    await this.render(core, anchor);
    const doc = await vscode.workspace.openTextDocument(disasmUri(core));
    if (reveal) {
      const editor = await vscode.window.showTextDocument(doc, {
        preview: false,
        preserveFocus: true,
        viewColumn: vscode.ViewColumn.Beside,
      });
      const line = anchor !== undefined ? this.addrToLine(core, anchor) : null;
      if (line !== null) {
        editor.revealRange(
          new vscode.Range(line, 0, line, 0),
          vscode.TextEditorRevealType.InCenter,
        );
      }
    }
  }

  /** True when a document for this core is open in some tab. */
  isOpen(core: string): boolean {
    const target = disasmUri(core).toString();
    return vscode.workspace.textDocuments.some(
      (d) => d.uri.toString() === target,
    );
  }

  revealAddr(core: string, addr: number): vscode.TextEditor | null {
    const line = this.addrToLine(core, addr);
    if (line === null) {
      return null;
    }
    const target = disasmUri(core).toString();
    for (const editor of vscode.window.visibleTextEditors) {
      if (editor.document.uri.toString() === target) {
        editor.revealRange(
          new vscode.Range(line, 0, line, 0),
          vscode.TextEditorRevealType.InCenter,
        );
        return editor;
      }
    }
    return null;
  }

  dispose(): void {
    this.changeEmitter.dispose();
  }
}

function shortPath(p: string): string {
  const parts = p.split(/[\\/]/);
  return parts.slice(-2).join("/");
}

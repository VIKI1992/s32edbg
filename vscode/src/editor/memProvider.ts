import * as vscode from "vscode";
import { hexToInt, toHex } from "../backend/types";
import { DebugState } from "../state/session";

export const MEM_SCHEME = "s32e-mem";

export interface MemView {
  ap: string;
  address: number;
  length: number;
}

export function memUri(view: MemView): vscode.Uri {
  return vscode.Uri.from({
    scheme: MEM_SCHEME,
    path: `/${view.ap}/${toHex(view.address)}+${view.length}.hex`,
  });
}

export function parseMemUri(uri: vscode.Uri): MemView | null {
  const m = uri.path.match(/^\/([^/]+)\/(0x[0-9A-Fa-f]+)\+(\d+)\.hex$/);
  if (!m) {
    return null;
  }
  return {
    ap: m[1],
    address: hexToInt(m[2]) ?? 0,
    length: Number.parseInt(m[3], 10),
  };
}

/** Read-only hex+ASCII dumps of target memory, one document per region. */
export class MemProvider
  implements vscode.TextDocumentContentProvider, vscode.Disposable
{
  private readonly changeEmitter = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this.changeEmitter.event;
  private content = new Map<string, string>();
  readonly open: MemView[] = [];

  constructor(private readonly state: DebugState) {}

  async fetch(view: MemView): Promise<void> {
    const client = this.state.requireClient();
    const bytes = Math.ceil(view.length / 4) * 4;
    const result = (await client.run(
      `mem dump ${toHex(view.address)} ${bytes} --ap ${view.ap}`,
    )) as { words: string[] };
    const lines: string[] = [
      `; ${view.ap}  ${toHex(view.address)}  ${bytes} bytes`,
    ];
    const words = result.words.map((w) => hexToInt(w) ?? 0);
    for (let i = 0; i < words.length; i += 4) {
      const addr = view.address + i * 4;
      const row = words.slice(i, i + 4);
      const hex = row.map((w) => w.toString(16).toUpperCase().padStart(8, "0")).join(" ");
      const ascii = row
        .flatMap((w) => [w & 0xff, (w >> 8) & 0xff, (w >> 16) & 0xff, (w >>> 24) & 0xff])
        .map((b) => (b >= 0x20 && b < 0x7f ? String.fromCharCode(b) : "."))
        .join("");
      lines.push(`${toHex(addr)}:  ${hex.padEnd(36)}  ${ascii}`);
    }
    const uri = memUri(view);
    this.content.set(uri.toString(), lines.join("\n") + "\n");
    if (!this.open.some((v) => memUri(v).toString() === uri.toString())) {
      this.open.push(view);
    }
    this.changeEmitter.fire(uri);
  }

  async refreshAll(): Promise<void> {
    for (const view of this.open) {
      try {
        await this.fetch(view);
      } catch {
        /* a closed session mid-iteration; the views stay stale */
      }
    }
  }

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.content.get(uri.toString()) ?? "; no data yet -- refresh\n";
  }

  dispose(): void {
    this.changeEmitter.dispose();
  }
}

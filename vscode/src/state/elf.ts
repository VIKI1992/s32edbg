import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

const RECENTS_KEY = "s32e.elf.recents";
const ACTIVE_KEY = "s32e.elf.active";

/** The one active ELF per workspace: symbols + gutter mapping + Load target. */
export class ElfState implements vscode.Disposable {
  private watcher: fs.FSWatcher | null = null;
  private lastMtime = 0;
  private readonly changeEmitter = new vscode.EventEmitter<string | null>();
  /** Fires with the path on set/replace, null on clear. */
  readonly onDidSetElf = this.changeEmitter.event;
  private readonly rebuiltEmitter = new vscode.EventEmitter<string>();
  /** Fires when the active ELF's file changes on disk (a rebuild). */
  readonly onRebuilt = this.rebuiltEmitter.event;

  constructor(private readonly memento: vscode.Memento) {
    const active = this.active;
    if (active && fs.existsSync(active)) {
      this.watch(active);
    }
  }

  get active(): string | null {
    return this.memento.get<string | null>(ACTIVE_KEY, null);
  }

  get recents(): string[] {
    return this.memento.get<string[]>(RECENTS_KEY, []);
  }

  get basename(): string | null {
    const a = this.active;
    return a ? path.basename(a) : null;
  }

  async set(elfPath: string): Promise<void> {
    const recents = [elfPath, ...this.recents.filter((p) => p !== elfPath)].slice(0, 8);
    await this.memento.update(ACTIVE_KEY, elfPath);
    await this.memento.update(RECENTS_KEY, recents);
    this.watch(elfPath);
    this.changeEmitter.fire(elfPath);
    void vscode.commands.executeCommand("setContext", "s32e.hasElf", true);
  }

  private watch(elfPath: string): void {
    this.watcher?.close();
    this.watcher = null;
    try {
      this.lastMtime = fs.statSync(elfPath).mtimeMs;
      // fs.watch over a workspace-external absolute path (a build tree) --
      // vscode's FileSystemWatcher only covers workspace folders by default.
      this.watcher = fs.watch(path.dirname(elfPath), (_ev, filename) => {
        if (filename !== path.basename(elfPath)) {
          return;
        }
        try {
          const mtime = fs.statSync(elfPath).mtimeMs;
          if (mtime !== this.lastMtime) {
            this.lastMtime = mtime;
            this.rebuiltEmitter.fire(elfPath);
          }
        } catch {
          /* mid-rebuild: the file vanishes briefly */
        }
      });
    } catch {
      this.watcher = null;
    }
  }

  async pick(): Promise<string | null> {
    const items: vscode.QuickPickItem[] = this.recents
      .filter((p) => fs.existsSync(p))
      .map((p) => ({ label: path.basename(p), description: p }));
    items.push({ label: "$(folder-opened) Browse...", description: "" });
    const chosen = await vscode.window.showQuickPick(items, {
      title: "Active ELF (symbols + Load to RAM)",
      placeHolder: "Pick a recent ELF or browse",
    });
    if (!chosen) {
      return null;
    }
    let target = chosen.description || null;
    if (!target) {
      const uris = await vscode.window.showOpenDialog({
        title: "Choose the ELF to debug",
        filters: { ELF: ["elf", "out", "axf"], "All files": ["*"] },
        canSelectMany: false,
      });
      target = uris?.[0]?.fsPath ?? null;
    }
    if (target) {
      await this.set(target);
    }
    return target;
  }

  updateContext(): void {
    void vscode.commands.executeCommand(
      "setContext",
      "s32e.hasElf",
      this.active !== null,
    );
  }

  dispose(): void {
    this.watcher?.close();
    this.changeEmitter.dispose();
    this.rebuiltEmitter.dispose();
  }
}

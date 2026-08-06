import { ChildProcess, spawn } from "child_process";
import * as vscode from "vscode";
import { BackendConfig, globalArgs } from "../config";
import { Output } from "../ui/output";
import { ByeEvent, Envelope, ProgressEvent, ReadyEvent } from "./types";

interface Pending {
  line: string;
  resolve: (env: Envelope) => void;
  reject: (err: Error) => void;
  onProgress?: (message: string) => void;
}

export class BackendError extends Error {
  constructor(
    readonly kind: string,
    message: string,
    readonly cmd: string,
  ) {
    super(message);
  }
}

/** One `s32edbg debug --machine` child process: the held probe connection.
 *
 * Strictly sequential request/response over stdio. NEVER kill this process
 * casually -- a hard kill skips the session's restore ledger and leaves
 * comparators armed and cores halted on the board (and a stale catch latch
 * breaks the application's own core release). Always `quit()` first.
 */
export class SessionClient implements vscode.Disposable {
  private proc: ChildProcess | null = null;
  private pending: Pending[] = [];
  private stdoutBuf = "";
  private stderrTail: string[] = [];
  private bye: ByeEvent | null = null;
  private byeWaiter: ((bye: ByeEvent | null) => void) | null = null;
  private exited = false;

  private readonly exitEmitter = new vscode.EventEmitter<{
    code: number | null;
    expected: boolean;
    stderrTail: string;
  }>();
  readonly onExit = this.exitEmitter.event;
  private expectingExit = false;

  private constructor(private readonly output: Output) {}

  get busy(): boolean {
    return this.pending.length > 0;
  }

  get alive(): boolean {
    return this.proc !== null && !this.exited;
  }

  static start(
    cfg: BackendConfig,
    output: Output,
    timeoutMs = 45000,
  ): Promise<{ client: SessionClient; ready: ReadyEvent }> {
    const client = new SessionClient(output);
    const args = [...globalArgs(cfg), "debug", "--machine"];
    output.section(`connect: ${cfg.pythonPath} ${args.join(" ")}`);
    const proc = spawn(cfg.pythonPath, args, {
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    client.proc = proc;

    return new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (!settled) {
          settled = true;
          client.kill();
          reject(
            new Error(
              `the backend did not become ready within ${timeoutMs / 1000}s.\n` +
                client.tail(),
            ),
          );
        }
      }, timeoutMs);

      proc.on("error", (err) => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(
            new Error(
              `could not start '${cfg.pythonPath}': ${err.message}. ` +
                `Check the s32e.pythonPath setting.`,
            ),
          );
        }
      });
      proc.stderr!.setEncoding("utf-8");
      proc.stderr!.on("data", (chunk: string) => {
        client.pushStderr(chunk);
        output.stderr(chunk);
      });
      proc.stdout!.setEncoding("utf-8");
      proc.stdout!.on("data", (chunk: string) => {
        client.stdoutBuf += chunk;
        let idx: number;
        while ((idx = client.stdoutBuf.indexOf("\n")) >= 0) {
          const line = client.stdoutBuf.slice(0, idx).trim();
          client.stdoutBuf = client.stdoutBuf.slice(idx + 1);
          if (!line) {
            continue;
          }
          let obj: Record<string, unknown>;
          try {
            obj = JSON.parse(line);
          } catch {
            output.line(`?? unparseable stdout line: ${line}`);
            continue;
          }
          if (obj.event === "ready") {
            if (!settled) {
              settled = true;
              clearTimeout(timer);
              resolve({ client, ready: obj as unknown as ReadyEvent });
            }
            continue;
          }
          client.route(obj);
        }
      });
      proc.on("exit", (code) => {
        client.exited = true;
        const expected = client.expectingExit;
        const err = new Error(
          `the backend session exited (code ${code}).\n${client.tail()}`,
        );
        for (const p of client.pending.splice(0)) {
          p.reject(err);
        }
        if (client.byeWaiter) {
          client.byeWaiter(client.bye);
          client.byeWaiter = null;
        }
        client.exitEmitter.fire({
          code,
          expected,
          stderrTail: client.tail(),
        });
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(err);
        }
      });
    });
  }

  private route(obj: Record<string, unknown>): void {
    if (obj.event === "progress") {
      const ev = obj as unknown as ProgressEvent;
      this.pending[0]?.onProgress?.(ev.message);
      return;
    }
    if (obj.event === "bye") {
      this.bye = obj as unknown as ByeEvent;
      return;
    }
    const env = obj as unknown as Envelope;
    const p = this.pending.shift();
    if (p === undefined) {
      this.output.line(`?? unmatched envelope seq ${env.seq}`);
      return;
    }
    this.output.envelope(
      `${env.seq} ${env.ok ? "ok" : `${env.error?.kind}: ${env.error?.message}`} (${env.cmd})`,
    );
    p.resolve(env);
  }

  private pushStderr(chunk: string): void {
    for (const line of chunk.split(/\r?\n/)) {
      if (line.trim()) {
        this.stderrTail.push(line);
      }
    }
    if (this.stderrTail.length > 40) {
      this.stderrTail = this.stderrTail.slice(-40);
    }
  }

  tail(): string {
    return this.stderrTail.slice(-12).join("\n");
  }

  /** Send one session line; resolves with the envelope (ok or not). */
  send(line: string, onProgress?: (message: string) => void): Promise<Envelope> {
    if (!this.alive) {
      return Promise.reject(new Error("not connected"));
    }
    this.output.sent(line);
    return new Promise<Envelope>((resolve, reject) => {
      this.pending.push({ line, resolve, reject, onProgress });
      this.proc!.stdin!.write(line + "\n");
    });
  }

  /** Send and throw a BackendError when the backend says not-ok. */
  async run(line: string, onProgress?: (message: string) => void): Promise<unknown> {
    const env = await this.send(line, onProgress);
    if (!env.ok) {
      throw new BackendError(
        env.error?.kind ?? "tool",
        env.error?.message ?? "unknown backend error",
        env.cmd,
      );
    }
    return env.result;
  }

  /** Graceful shutdown: `quit` -> restore ledger runs -> bye -> exit. */
  async quit(timeoutMs = 8000): Promise<ByeEvent | null> {
    if (!this.alive) {
      return this.bye;
    }
    this.expectingExit = true;
    const byePromise = new Promise<ByeEvent | null>((resolve) => {
      this.byeWaiter = resolve;
      setTimeout(() => {
        if (this.byeWaiter) {
          this.byeWaiter = null;
          resolve(null);
        }
      }, timeoutMs);
    });
    try {
      await this.send("quit");
    } catch {
      // the exit handler settles everything
    }
    try {
      this.proc?.stdin?.end();
    } catch {
      /* ignore */
    }
    const bye = await byePromise;
    if (!this.exited) {
      this.kill();
    }
    return bye;
  }

  /** Last resort only -- skips the restore ledger (see class docstring). */
  kill(): void {
    this.expectingExit = true;
    try {
      this.proc?.kill();
    } catch {
      /* ignore */
    }
  }

  dispose(): void {
    void this.quit();
  }
}

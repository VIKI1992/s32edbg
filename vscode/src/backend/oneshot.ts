import { spawn } from "child_process";
import { randomBytes } from "crypto";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { BackendConfig, globalArgs } from "../config";
import { Output } from "../ui/output";

/** Run one `s32edbg <cmd> --json <tmpfile>` invocation and parse the JSON.
 *
 * Diagnostics only (doctor/probe/report/--version), and only while there is
 * no live session: every one-shot invocation opens its own probe connection,
 * which pulses reset into the board's SBC/PMIC (defect D-2). The JSON goes
 * through a temp file because on stdout it would be interleaved with Rich
 * human output.
 */
export async function oneshot(
  cfg: BackendConfig,
  output: Output,
  cmdArgs: string[],
  { wantJson = true, timeoutMs = 120000 }: { wantJson?: boolean; timeoutMs?: number } = {},
): Promise<{ code: number | null; json: unknown | null; text: string }> {
  const jsonPath = path.join(
    os.tmpdir(),
    `s32e-${randomBytes(6).toString("hex")}.json`,
  );
  const args = [...globalArgs(cfg)];
  if (wantJson) {
    args.push("--json", jsonPath);
  }
  args.push(...cmdArgs);
  output.section(`one-shot: s32edbg ${cmdArgs.join(" ")}`);

  return new Promise((resolve, reject) => {
    const proc = spawn(cfg.pythonPath, args, {
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    let text = "";
    const timer = setTimeout(() => {
      proc.kill();
      reject(new Error(`s32edbg ${cmdArgs[0]} timed out after ${timeoutMs / 1000}s`));
    }, timeoutMs);
    proc.on("error", (err) => {
      clearTimeout(timer);
      reject(
        new Error(`could not start '${cfg.pythonPath}': ${err.message}`),
      );
    });
    for (const stream of [proc.stdout, proc.stderr]) {
      stream!.setEncoding("utf-8");
      stream!.on("data", (chunk: string) => {
        text += chunk;
        output.stderr(chunk);
      });
    }
    proc.on("exit", (code) => {
      clearTimeout(timer);
      let json: unknown | null = null;
      try {
        json = JSON.parse(fs.readFileSync(jsonPath, "utf-8"));
      } catch {
        json = null;
      }
      try {
        fs.unlinkSync(jsonPath);
      } catch {
        /* never existed */
      }
      resolve({ code, json, text });
    });
  });
}

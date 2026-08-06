import * as vscode from "vscode";

export interface BackendConfig {
  pythonPath: string;
  jlinkDir: string;
  serial: string;
  speedKhz: number;
  device: string;
}

export function backendConfig(): BackendConfig {
  const cfg = vscode.workspace.getConfiguration("s32e");
  return {
    pythonPath: cfg.get<string>("pythonPath", "python"),
    jlinkDir: cfg.get<string>("jlinkDir", ""),
    serial: cfg.get<string>("serial", ""),
    speedKhz: cfg.get<number>("speedKhz", 16000),
    device: cfg.get<string>("device", "CORTEX-R52"),
  };
}

export function setting<T>(key: string, fallback: T): T {
  return vscode.workspace.getConfiguration("s32e").get<T>(key, fallback);
}

/** Global args for every s32edbg invocation (session and one-shot alike). */
export function globalArgs(cfg: BackendConfig): string[] {
  const args = ["-u", "-m", "s32edbg", "--speed", String(cfg.speedKhz)];
  if (cfg.serial) {
    args.push("--serial", cfg.serial);
  }
  if (cfg.jlinkDir) {
    args.push("--jlink-dir", cfg.jlinkDir);
  }
  if (cfg.device) {
    args.push("--device", cfg.device);
  }
  return args;
}

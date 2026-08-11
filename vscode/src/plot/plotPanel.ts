import * as vscode from "vscode";
import { VarRefreshResult, VarRow, VarWatchResult } from "../backend/types";
import { setting } from "../config";
import { DebugState } from "../state/session";
import { Output } from "../ui/output";
import { isStaleHandleError } from "../views/variablesFormat";
import { decodeSample } from "./plotValue";

interface Sample {
  t: number;
  v: number;
}

/** Full-history cap for CSV export; the webview keeps its own drawing ring. */
const CSV_CAP = 500_000;

/** One plot panel per (core, variable path); reveal instead of duplicating. */
export class PlotPanels implements vscode.Disposable {
  private readonly panels = new Map<string, PlotPanel>();

  constructor(
    private readonly state: DebugState,
    private readonly output: Output,
  ) {}

  open(core: string, row: VarRow): void {
    const key = `${core}:${row.path}`;
    const existing = this.panels.get(key);
    if (existing) {
      existing.reveal();
      return;
    }
    this.panels.set(
      key,
      new PlotPanel(this.state, this.output, core, row.path, () =>
        this.panels.delete(key),
      ),
    );
  }

  dispose(): void {
    for (const panel of [...this.panels.values()]) {
      panel.dispose();
    }
  }
}

/** A webview scope for one variable: a sampler at a configurable rate on the
 * extension side, a rolling canvas chart on the webview side.
 *
 * Sampler guard discipline matches the other loops (drop the tick while the
 * queue is busy -- the ACHIEVED rate is displayed, honesty over promise).
 * Handles die whenever the target runs; a stale handle re-resolves by path
 * via a frameless `var watch`, so the plot survives step/continue cycles.
 */
class PlotPanel {
  private readonly panel: vscode.WebviewPanel;
  private timer: ReturnType<typeof setInterval> | null = null;
  private inFlight = false;
  private failures = 0;
  private paused = false;
  private handle: string | null = null;
  private hz: number;
  private readonly csv: Sample[] = [];

  constructor(
    private readonly state: DebugState,
    private readonly output: Output,
    private readonly core: string,
    private readonly path: string,
    private readonly onGone: () => void,
  ) {
    this.hz = clampHz(setting<number>("plot.sampleHz", 10));
    this.panel = vscode.window.createWebviewPanel(
      "s32ePlot",
      `Plot: ${path}`,
      vscode.ViewColumn.Beside,
      { enableScripts: true, retainContextWhenHidden: true },
    );
    this.panel.webview.html = plotHtml(path, core, this.hz);
    this.panel.onDidDispose(() => {
      this.stop();
      this.onGone();
    });
    this.panel.webview.onDidReceiveMessage((msg: Record<string, unknown>) => {
      if (msg.type === "setHz") {
        this.hz = clampHz(Number(msg.hz));
        this.startTimer();
      } else if (msg.type === "pause") {
        this.paused = Boolean(msg.value);
      } else if (msg.type === "clear") {
        this.csv.length = 0;
      } else if (msg.type === "exportCsv") {
        void this.exportCsv();
      }
    });
    this.startTimer();
  }

  reveal(): void {
    this.panel.reveal(undefined, true);
  }

  private startTimer(): void {
    this.stop();
    this.timer = setInterval(() => void this.tick(), 1000 / this.hz);
  }

  private stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  private post(msg: Record<string, unknown>): void {
    void this.panel.webview.postMessage(msg);
  }

  private note(text: string): void {
    this.post({ type: "note", text });
  }

  private async tick(): Promise<void> {
    if (
      this.paused ||
      this.inFlight ||
      !this.state.connected ||
      this.state.busyOp ||
      (this.state.client?.busy ?? true)
    ) {
      return;
    }
    this.inFlight = true;
    try {
      const client = this.state.client!;
      if (this.handle === null && !(await this.resolve())) {
        return;
      }
      let row: VarRow | undefined;
      try {
        const result = (await client.run(
          `var refresh ${this.handle}`,
        )) as VarRefreshResult;
        row = result.rows[0];
      } catch (err) {
        const text = err instanceof Error ? err.message : String(err);
        if (!isStaleHandleError(text)) {
          throw err;
        }
        // The target ran since the handle was minted: re-resolve by path.
        this.handle = null;
        if (!(await this.resolve())) {
          return;
        }
        const retry = (await client.run(
          `var refresh ${this.handle}`,
        )) as VarRefreshResult;
        row = retry.rows[0];
      }
      const value = decodeSample(row);
      if (value !== null) {
        const sample = { t: Date.now(), v: value };
        this.csv.push(sample);
        if (this.csv.length > CSV_CAP) {
          this.csv.splice(0, this.csv.length - CSV_CAP);
        }
        this.post({ type: "sample", t: sample.t, v: value });
      }
      this.failures = 0;
    } catch (err) {
      this.failures += 1;
      if (this.failures >= 5) {
        this.paused = true;
        this.post({ type: "paused", value: true });
        this.note(
          `sampling paused after repeated failures: ${
            err instanceof Error ? err.message : String(err)
          }`,
        );
        this.output.line(`!! plot ${this.path}: ${err}`);
      }
    } finally {
      this.inFlight = false;
    }
  }

  /** Frameless resolve by path -- statics/globals only, which is exactly
   * the set that is sampleable while the target runs. */
  private async resolve(): Promise<boolean> {
    const result = (await this.state.client!.run(
      `var watch "${this.path}" --core ${this.core}`,
    )) as VarWatchResult;
    const row = result.row;
    if (row.handle !== null && row.address !== null && !row.error) {
      this.handle = row.handle;
      return true;
    }
    this.paused = true;
    this.post({ type: "paused", value: true });
    this.note(
      `not sampleable: ${
        row.error ?? "no memory address (a register or frame-local value)"
      }`,
    );
    return false;
  }

  private async exportCsv(): Promise<void> {
    const name = this.path.replace(/[^A-Za-z0-9_.-]+/g, "_");
    const uri = await vscode.window.showSaveDialog({
      filters: { CSV: ["csv"] },
      defaultUri: vscode.Uri.file(`${name}.csv`),
    });
    if (!uri) {
      return;
    }
    const lines = [
      "timestamp_ms,value",
      ...this.csv.map((s) => `${s.t},${s.v}`),
    ];
    await vscode.workspace.fs.writeFile(
      uri,
      Buffer.from(lines.join("\n") + "\n", "utf-8"),
    );
    void vscode.window.setStatusBarMessage(
      `S32E: exported ${this.csv.length} samples`,
      4000,
    );
  }

  dispose(): void {
    this.stop();
    this.panel.dispose();
  }
}

function clampHz(hz: number): number {
  if (!Number.isFinite(hz)) {
    return 10;
  }
  return Math.min(50, Math.max(1, Math.round(hz)));
}

function nonceText(): string {
  let out = "";
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  for (let i = 0; i < 32; i++) {
    out += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return out;
}

/** The whole webview: toolbar, canvas, hover layer. Self-contained (strict
 * CSP, no external resources); series color comes from the theme's chart
 * palette with validated fallbacks per scheme; text wears text tokens. */
function plotHtml(path: string, core: string, hz: number): string {
  const nonce = nonceText();
  const title = `${escapeHtml(path)} — ${escapeHtml(core)}`;
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
<style nonce="${nonce}">
  body {
    margin: 0; padding: 0 0 4px 0;
    color: var(--vscode-foreground);
    background: var(--vscode-editor-background);
    font-family: var(--vscode-font-family); font-size: 12px;
    display: flex; flex-direction: column; height: 100vh; box-sizing: border-box;
  }
  #bar {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 6px 10px;
  }
  #bar .path { font-weight: 600; }
  #bar label { color: var(--vscode-descriptionForeground); }
  input, select, button {
    color: var(--vscode-input-foreground);
    background: var(--vscode-input-background);
    border: 1px solid var(--vscode-input-border, transparent);
    border-radius: 2px; padding: 2px 6px; font-size: 12px;
  }
  button { cursor: pointer; }
  input#hz { width: 3.5em; }
  #readout { margin-left: auto; color: var(--vscode-descriptionForeground); }
  #readout b { color: var(--vscode-foreground); font-size: 14px; }
  #wrap { position: relative; flex: 1; min-height: 120px; }
  canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
  #tip {
    position: absolute; display: none; pointer-events: none;
    background: var(--vscode-editorHoverWidget-background);
    color: var(--vscode-editorHoverWidget-foreground);
    border: 1px solid var(--vscode-editorHoverWidget-border);
    border-radius: 3px; padding: 2px 8px; white-space: nowrap; z-index: 2;
  }
  #note {
    position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%);
    color: var(--vscode-descriptionForeground); display: none;
    background: var(--vscode-editor-background); padding: 4px 10px;
  }
</style>
<title>Plot</title>
</head>
<body>
<div id="bar">
  <span class="path">${title}</span>
  <label>Hz <input id="hz" type="number" min="1" max="50" step="1" value="${hz}"></label>
  <label>window <select id="win">
    <option value="5">5 s</option>
    <option value="10" selected>10 s</option>
    <option value="30">30 s</option>
    <option value="60">60 s</option>
  </select></label>
  <button id="pause">Pause</button>
  <button id="clear">Clear</button>
  <button id="csv">Export CSV</button>
  <span id="readout"></span>
</div>
<div id="wrap">
  <canvas id="c"></canvas>
  <div id="tip"></div>
  <div id="note"></div>
</div>
<script nonce="${nonce}">
(function () {
  const vscode = acquireVsCodeApi();
  const canvas = document.getElementById("c");
  const ctx = canvas.getContext("2d");
  const tip = document.getElementById("tip");
  const noteEl = document.getElementById("note");
  const readout = document.getElementById("readout");
  const wrap = document.getElementById("wrap");

  const RING = 100000;
  let samples = [];          // {t, v} ascending
  let windowMs = 10000;
  let paused = false;
  let hover = null;          // client x within the canvas, or null

  function css(name, fallback) {
    const v = getComputedStyle(document.body).getPropertyValue(name).trim();
    return v || fallback;
  }
  function seriesColor() {
    // Theme chart palette first; validated fallbacks per scheme (the
    // high-contrast themes get the plain foreground).
    const cls = document.body.className;
    if (cls.indexOf("high-contrast") >= 0) return css("--vscode-foreground", "#ffffff");
    const themed = css("--vscode-charts-blue", "");
    if (themed) return themed;
    return cls.indexOf("vscode-light") >= 0 ? "#1a6fd4" : "#3794ff";
  }

  window.addEventListener("message", (ev) => {
    const msg = ev.data;
    if (msg.type === "sample") {
      samples.push({ t: msg.t, v: msg.v });
      if (samples.length > RING) samples.splice(0, samples.length - RING);
      noteEl.style.display = "none";
    } else if (msg.type === "note") {
      noteEl.textContent = msg.text;
      noteEl.style.display = "block";
    } else if (msg.type === "paused") {
      paused = Boolean(msg.value);
      pauseBtn.textContent = paused ? "Resume" : "Pause";
    }
  });

  const hzInput = document.getElementById("hz");
  hzInput.addEventListener("change", () => {
    const hz = Math.min(50, Math.max(1, Math.round(Number(hzInput.value) || 10)));
    hzInput.value = String(hz);
    vscode.postMessage({ type: "setHz", hz });
  });
  document.getElementById("win").addEventListener("change", (ev) => {
    windowMs = Number(ev.target.value) * 1000;
  });
  const pauseBtn = document.getElementById("pause");
  pauseBtn.addEventListener("click", () => {
    paused = !paused;
    pauseBtn.textContent = paused ? "Resume" : "Pause";
    vscode.postMessage({ type: "pause", value: paused });
  });
  document.getElementById("clear").addEventListener("click", () => {
    samples = [];
    vscode.postMessage({ type: "clear" });
  });
  document.getElementById("csv").addEventListener("click", () => {
    vscode.postMessage({ type: "exportCsv" });
  });
  canvas.addEventListener("mousemove", (ev) => {
    hover = ev.offsetX;
  });
  canvas.addEventListener("mouseleave", () => {
    hover = null;
    tip.style.display = "none";
  });

  function niceStep(span, target) {
    const raw = span / target;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    for (const m of [1, 2, 5, 10]) {
      if (raw <= m * mag) return m * mag;
    }
    return 10 * mag;
  }
  function fmt(v) {
    if (!isFinite(v)) return String(v);
    const a = Math.abs(v);
    if (a !== 0 && (a >= 1e6 || a < 1e-3)) return v.toExponential(2);
    return String(Math.round(v * 1000) / 1000);
  }

  function draw() {
    const dpr = window.devicePixelRatio || 1;
    const w = wrap.clientWidth, h = wrap.clientHeight;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr; canvas.height = h * dpr;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const padL = 56, padR = 10, padT = 8, padB = 22;
    const plotW = w - padL - padR, plotH = h - padT - padB;
    if (plotW < 20 || plotH < 20) return;

    const now = Date.now();
    const t0 = now - windowMs;
    let lo = samples.length, hi = samples.length;
    for (let i = samples.length - 1; i >= 0; i--) {
      if (samples[i].t < t0) { lo = i; break; }
      lo = i;
    }
    const visible = samples.slice(Math.max(0, lo - 1));
    let vMin = Infinity, vMax = -Infinity;
    for (const s of visible) {
      if (s.t < t0 - 1000) continue;
      if (s.v < vMin) vMin = s.v;
      if (s.v > vMax) vMax = s.v;
    }
    if (!isFinite(vMin)) { vMin = 0; vMax = 1; }
    if (vMin === vMax) { const pad = Math.max(1, Math.abs(vMin) * 0.1); vMin -= pad; vMax += pad; }
    const span = vMax - vMin;
    vMin -= span * 0.08; vMax += span * 0.08;

    const x = (t) => padL + ((t - t0) / windowMs) * plotW;
    const y = (v) => padT + (1 - (v - vMin) / (vMax - vMin)) * plotH;

    const gridColor = css("--vscode-editorWidget-border", "#808080");
    const textColor = css("--vscode-descriptionForeground", "#999999");
    ctx.font = "11px " + css("--vscode-font-family", "sans-serif");

    // Recessive grid + axis labels (text tokens, never the series color).
    ctx.globalAlpha = 0.35;
    ctx.strokeStyle = gridColor;
    ctx.lineWidth = 1;
    const step = niceStep(vMax - vMin, 4);
    ctx.beginPath();
    const yLabels = [];
    for (let v = Math.ceil(vMin / step) * step; v <= vMax; v += step) {
      const py = Math.round(y(v)) + 0.5;
      ctx.moveTo(padL, py); ctx.lineTo(w - padR, py);
      yLabels.push([v, py]);
    }
    for (let sec = 0; sec <= windowMs / 1000; sec += windowMs / 5000) {
      const px = Math.round(x(now - sec * 1000)) + 0.5;
      ctx.moveTo(px, padT); ctx.lineTo(px, h - padB);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = textColor;
    for (const [v, py] of yLabels) {
      ctx.fillText(fmt(v), 4, py + 3.5);
    }
    ctx.textAlign = "center";
    for (let sec = 0; sec <= windowMs / 1000; sec += windowMs / 5000) {
      ctx.fillText("-" + sec + "s", x(now - sec * 1000), h - 8);
    }
    ctx.textAlign = "left";

    // The series: a 2px line; the newest point gets a dot.
    const color = seriesColor();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.beginPath();
    let started = false;
    for (const s of visible) {
      const px = x(s.t), py = y(s.v);
      if (!started) { ctx.moveTo(px, py); started = true; }
      else ctx.lineTo(px, py);
    }
    ctx.stroke();
    if (visible.length > 0) {
      const last = visible[visible.length - 1];
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x(last.t), y(last.v), 3, 0, Math.PI * 2);
      ctx.fill();
    }

    // Crosshair + tooltip on the nearest sample.
    if (hover !== null && visible.length > 0) {
      const tAt = t0 + ((hover - padL) / plotW) * windowMs;
      let best = null, bestD = Infinity;
      for (const s of visible) {
        const d = Math.abs(s.t - tAt);
        if (d < bestD) { bestD = d; best = s; }
      }
      if (best && hover >= padL && hover <= w - padR) {
        const px = x(best.t), py = y(best.v);
        ctx.globalAlpha = 0.5;
        ctx.strokeStyle = textColor;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(Math.round(px) + 0.5, padT);
        ctx.lineTo(Math.round(px) + 0.5, h - padB);
        ctx.stroke();
        ctx.globalAlpha = 1;
        ctx.fillStyle = color;
        ctx.beginPath(); ctx.arc(px, py, 4, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = css("--vscode-editor-background", "#1e1e1e");
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(px, py, 4, 0, Math.PI * 2); ctx.stroke();
        tip.textContent = fmt(best.v) + "  (" + ((best.t - now) / 1000).toFixed(2) + "s)";
        tip.style.display = "block";
        tip.style.left = Math.min(px + 10, w - tip.offsetWidth - 4) + "px";
        tip.style.top = Math.max(4, py - 26) + "px";
      }
    } else {
      tip.style.display = "none";
    }

    // Readout: current + window stats + achieved rate.
    if (samples.length > 0) {
      const last = samples[samples.length - 1];
      let rate = 0;
      for (let i = samples.length - 1; i >= 0; i--) {
        if (samples[i].t < now - 2000) break;
        rate++;
      }
      readout.innerHTML =
        "<b>" + fmt(last.v) + "</b>&nbsp;&nbsp;min " + fmt(vMin + span * 0.08) +
        " · max " + fmt(vMax - span * 0.08) +
        " · " + (rate / 2).toFixed(1) + " Hz achieved";
    }
    requestAnimationFrame(draw);
  }
  requestAnimationFrame(draw);
})();
</script>
</body>
</html>`;
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

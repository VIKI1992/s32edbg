import * as vscode from "vscode";
import { SamplesEvent, VarRow, VarWatchResult } from "../backend/types";
import { setting } from "../config";
import { DebugState } from "../state/session";
import { Output } from "../ui/output";
import { decodeRaw } from "./plotValue";

/** Fixed-order series palette (red, blue, orange, green, purple, yellow) --
 * theme chart colors at runtime, with fallbacks validated by the dataviz
 * six-checks script for both surfaces. Color follows the signal, never its
 * position; a removed signal's color is not inherited by its neighbours. */
const SLOT_VARS = [
  "--vscode-charts-red",
  "--vscode-charts-blue",
  "--vscode-charts-orange",
  "--vscode-charts-green",
  "--vscode-charts-purple",
  "--vscode-charts-yellow",
];
const SLOT_DARK = ["#f14c4c", "#3794ff", "#a86400", "#47a94d", "#9d6ec9", "#a88a00"];
const SLOT_LIGHT = ["#b51d1d", "#1a6fd4", "#8f4700", "#2f9c49", "#7137a1", "#9a7d00"];

const MAX_SIGNALS = 6;
const CSV_CAP = 200_000;

interface SignalMeta {
  id: string;
  path: string;
  core: string;
  addressHex: string;
  bits: 8 | 16 | 32;
  kind: string;
  size: number | null;
  slot: number;
  hidden: boolean;
  panelKey: string;
  csv: { t: number; v: number }[];
}

/** All scope panels + the single shared backend stream.
 *
 * Every panel's visible signals feed ONE `var sample` stream (the wire
 * budget is global); each batch is decoded here and routed to its panel.
 */
export class PlotPanels implements vscode.Disposable {
  private readonly panels = new Map<string, ScopePanel>();
  private readonly signals = new Map<string, SignalMeta>();
  private counter = 0;
  private focusedKey: string | null = null;
  private rate: number;
  private readonly subs: vscode.Disposable[] = [];
  private wired = false;

  constructor(
    private readonly state: DebugState,
    private readonly output: Output,
  ) {
    this.rate = clampRate(setting<number>("plot.sampleHz", 100));
  }

  /** Subscribe to the client's sample events (client exists post-connect). */
  resync(): void {
    const client = this.state.client;
    if (client && !this.wired) {
      this.wired = true;
      this.subs.push(
        client.onSamples((ev) => this.route(ev)),
        client.onSampleStopped((ev) => {
          for (const panel of this.panels.values()) {
            panel.note(`stream stopped: ${ev.reason}`);
          }
        }),
        client.onExit(() => {
          this.wired = false;
          for (const s of this.subs.splice(0)) {
            s.dispose();
          }
        }),
      );
    }
    void this.sync();
  }

  addFromRow(core: string, row: VarRow): void {
    if (row.address === null || row.error) {
      throw new Error(
        "this row cannot be sampled: it has no memory address (a register " +
          "value or an error row). Plot a global, a static, or a watch path.",
      );
    }
    const bits = bitsOf(row.size);
    if (bits === null) {
      throw new Error(
        `${row.type} is ${row.size} bytes; streamed sampling reads 8/16/32-bit scalars`,
      );
    }
    const id = `${core}:${row.path}`;
    const existing = this.signals.get(id);
    if (existing) {
      this.panels.get(existing.panelKey)?.reveal();
      return;
    }
    if (this.signals.size >= MAX_SIGNALS) {
      throw new Error(
        `at most ${MAX_SIGNALS} signals total: the ~1000 samples/s wire ` +
          "budget is shared, and thinner slices stop being waveforms. " +
          "Remove one first.",
      );
    }
    const panel = this.focusedPanel() ?? this.newPanel();
    const meta: SignalMeta = {
      id,
      path: row.path,
      core,
      addressHex: row.address,
      bits,
      kind: row.kind,
      size: row.size,
      slot: panel.freeSlot(),
      hidden: false,
      panelKey: panel.key,
      csv: [],
    };
    this.signals.set(id, meta);
    panel.attach(meta);
    void this.sync();
  }

  async addByPath(panelKey: string, path: string): Promise<void> {
    const core = this.state.focus;
    if (!this.state.connected || !core) {
      throw new Error("not connected");
    }
    const result = (await this.state
      .requireClient()
      .run(`var watch "${path}" --core ${core}`)) as VarWatchResult;
    this.addFromRow(core, result.row);
  }

  newPanel(): ScopePanel {
    this.counter += 1;
    const key = `scope${this.counter}`;
    const panel = new ScopePanel(this, key, this.rate);
    this.panels.set(key, panel);
    this.focusedKey = key;
    return panel;
  }

  focusedPanel(): ScopePanel | null {
    const panel = this.focusedKey ? this.panels.get(this.focusedKey) : null;
    return panel ?? [...this.panels.values()][0] ?? null;
  }

  noteFocus(key: string): void {
    this.focusedKey = key;
  }

  remove(id: string): void {
    const meta = this.signals.get(id);
    if (!meta) {
      return;
    }
    this.signals.delete(id);
    this.panels.get(meta.panelKey)?.detach(id);
    void this.sync();
  }

  setHidden(id: string, hidden: boolean): void {
    const meta = this.signals.get(id);
    if (meta) {
      meta.hidden = hidden;
      this.panels.get(meta.panelKey)?.refreshList();
      void this.sync();
    }
  }

  panelGone(key: string): void {
    for (const meta of [...this.signals.values()]) {
      if (meta.panelKey === key) {
        this.signals.delete(meta.id);
      }
    }
    this.panels.delete(key);
    if (this.focusedKey === key) {
      this.focusedKey = null;
    }
    void this.sync();
  }

  async setRate(hz: number): Promise<void> {
    this.rate = clampRate(hz);
    for (const panel of this.panels.values()) {
      panel.rateApplied(this.rate);
    }
    await this.sync();
  }

  signalsOf(key: string): SignalMeta[] {
    return [...this.signals.values()].filter((s) => s.panelKey === key);
  }

  /** One shared stream: every visible signal of every unpaused panel. */
  private async sync(): Promise<void> {
    if (!this.state.connected || !this.state.client) {
      return;
    }
    const active = [...this.signals.values()].filter(
      (s) => !s.hidden && !(this.panels.get(s.panelKey)?.paused ?? true),
    );
    try {
      if (active.length === 0) {
        await this.state.client.run("var sample stop");
        return;
      }
      const specs = active
        .map((s) => ` --signal "${s.id}@${s.addressHex}:${s.bits}"`)
        .join("");
      await this.state.client.run(
        `var sample start${specs} --rate ${this.rate}`,
      );
    } catch (err) {
      this.output.line(`!! plot stream: ${err}`);
    }
  }

  private route(ev: SamplesEvent): void {
    for (const sig of ev.signals) {
      const meta = this.signals.get(sig.name);
      if (!meta) {
        continue;
      }
      const t: number[] = [];
      const v: number[] = [];
      for (let i = 0; i < sig.v.length; i++) {
        const decoded = decodeRaw(meta.kind, meta.size, sig.v[i]);
        if (decoded !== null) {
          const at = ev.t0_ms + sig.dt[i];
          t.push(at);
          v.push(decoded);
          meta.csv.push({ t: at, v: decoded });
        }
      }
      if (meta.csv.length > CSV_CAP) {
        meta.csv.splice(0, meta.csv.length - CSV_CAP);
      }
      if (t.length > 0) {
        this.panels.get(meta.panelKey)?.pushBatch(meta.id, t, v);
      }
    }
  }

  async exportCsv(panelKey: string): Promise<void> {
    const signals = this.signalsOf(panelKey);
    const rows: { t: number; name: string; v: number }[] = [];
    for (const meta of signals) {
      for (const s of meta.csv) {
        rows.push({ t: s.t, name: meta.path, v: s.v });
      }
    }
    rows.sort((a, b) => a.t - b.t);
    const uri = await vscode.window.showSaveDialog({
      filters: { CSV: ["csv"] },
      defaultUri: vscode.Uri.file("scope.csv"),
    });
    if (!uri) {
      return;
    }
    const lines = [
      "timestamp_ms,signal,value",
      ...rows.map((r) => `${Math.round(r.t)},${r.name},${r.v}`),
    ];
    await vscode.workspace.fs.writeFile(
      uri, Buffer.from(lines.join("\n") + "\n", "utf-8"),
    );
    void vscode.window.setStatusBarMessage(
      `S32E: exported ${rows.length} samples`, 4000,
    );
  }

  clearData(panelKey: string): void {
    for (const meta of this.signalsOf(panelKey)) {
      meta.csv = [];
    }
  }

  dispose(): void {
    for (const panel of [...this.panels.values()]) {
      panel.dispose();
    }
    for (const s of this.subs) {
      s.dispose();
    }
  }
}

/** One webview scope: the chart, its signals sidebar, cursors, controls. */
class ScopePanel {
  readonly panel: vscode.WebviewPanel;
  paused = false;

  constructor(
    private readonly owner: PlotPanels,
    readonly key: string,
    rate: number,
  ) {
    this.panel = vscode.window.createWebviewPanel(
      "s32ePlot",
      "S32E Scope",
      vscode.ViewColumn.Beside,
      { enableScripts: true, retainContextWhenHidden: true },
    );
    this.panel.webview.html = scopeHtml(rate);
    this.panel.onDidDispose(() => this.owner.panelGone(this.key));
    this.panel.onDidChangeViewState((ev) => {
      if (ev.webviewPanel.active) {
        this.owner.noteFocus(this.key);
      }
    });
    this.panel.webview.onDidReceiveMessage((msg: Record<string, unknown>) => {
      void (async () => {
        try {
          if (msg.type === "addPath") {
            await this.owner.addByPath(this.key, String(msg.path));
          } else if (msg.type === "remove") {
            this.owner.remove(String(msg.id));
          } else if (msg.type === "hide") {
            this.owner.setHidden(String(msg.id), Boolean(msg.value));
          } else if (msg.type === "pause") {
            this.paused = Boolean(msg.value);
            this.owner.resync();
          } else if (msg.type === "setRate") {
            await this.owner.setRate(Number(msg.hz));
          } else if (msg.type === "clear") {
            this.owner.clearData(this.key);
          } else if (msg.type === "exportCsv") {
            await this.owner.exportCsv(this.key);
          }
        } catch (err) {
          this.note(err instanceof Error ? err.message : String(err));
        }
      })();
    });
  }

  reveal(): void {
    this.panel.reveal(undefined, true);
  }

  freeSlot(): number {
    const used = new Set(this.owner.signalsOf(this.key).map((s) => s.slot));
    for (let slot = 0; slot < MAX_SIGNALS; slot++) {
      if (!used.has(slot)) {
        return slot;
      }
    }
    return 0;
  }

  attach(meta: SignalMeta): void {
    this.refreshList();
    this.reveal();
    void meta;
  }

  detach(id: string): void {
    this.post({ type: "drop", id });
    this.refreshList();
  }

  refreshList(): void {
    this.post({
      type: "signals",
      list: this.owner.signalsOf(this.key).map((s) => ({
        id: s.id,
        name: s.path,
        slot: s.slot,
        kind: s.kind,
        hidden: s.hidden,
      })),
    });
  }

  pushBatch(id: string, t: number[], v: number[]): void {
    this.post({ type: "batch", id, t, v });
  }

  rateApplied(rate: number): void {
    this.post({ type: "rate", rate });
  }

  note(text: string): void {
    this.post({ type: "note", text });
  }

  private post(msg: Record<string, unknown>): void {
    void this.panel.webview.postMessage(msg);
  }

  dispose(): void {
    this.panel.dispose();
  }
}

function bitsOf(size: number | null): 8 | 16 | 32 | null {
  if (size === 1) {
    return 8;
  }
  if (size === 2) {
    return 16;
  }
  if (size === 4 || size === null) {
    return 32;
  }
  return null;
}

function clampRate(hz: number): number {
  if (!Number.isFinite(hz)) {
    return 100;
  }
  return Math.min(1000, Math.max(1, Math.round(hz)));
}

function nonceText(): string {
  let out = "";
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  for (let i = 0; i < 32; i++) {
    out += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return out;
}

/** The scope webview. Self-contained (strict CSP); series colors come from
 * the theme's chart palette in the validated fixed order; text wears text
 * tokens; the sidebar doubles as the legend (identity never color-alone). */
function scopeHtml(rate: number): string {
  const nonce = nonceText();
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
<style nonce="${nonce}">
  body {
    margin: 0; height: 100vh; display: flex; flex-direction: column;
    color: var(--vscode-foreground); background: var(--vscode-editor-background);
    font-family: var(--vscode-font-family); font-size: 12px; box-sizing: border-box;
  }
  #bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 6px 10px; }
  #bar label { color: var(--vscode-descriptionForeground); }
  input, select, button {
    color: var(--vscode-input-foreground); background: var(--vscode-input-background);
    border: 1px solid var(--vscode-input-border, transparent);
    border-radius: 2px; padding: 2px 6px; font-size: 12px;
  }
  button { cursor: pointer; }
  #rate { width: 4.5em; }
  #achieved { margin-left: auto; color: var(--vscode-descriptionForeground); }
  #main { flex: 1; display: flex; min-height: 100px; }
  #side {
    width: 200px; flex: none; overflow-y: auto; padding: 4px 8px;
    border-right: 1px solid var(--vscode-editorWidget-border);
  }
  .sig { display: flex; align-items: center; gap: 6px; padding: 2px 0; }
  .chip { width: 10px; height: 10px; border-radius: 2px; flex: none; }
  .sig .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sig .val { color: var(--vscode-descriptionForeground); }
  .sig button { padding: 0 4px; }
  .sig.hidden .name, .sig.hidden .val { opacity: 0.45; }
  #addrow { display: flex; gap: 4px; margin-top: 6px; }
  #addrow input { flex: 1; min-width: 40px; }
  #wrap { position: relative; flex: 1; }
  canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
  #note {
    position: absolute; left: 50%; top: 8px; transform: translateX(-50%);
    color: var(--vscode-descriptionForeground); display: none;
    background: var(--vscode-editor-background); padding: 2px 10px; z-index: 3;
  }
  #readout {
    padding: 4px 10px; border-top: 1px solid var(--vscode-editorWidget-border);
    color: var(--vscode-descriptionForeground); min-height: 18px;
    font-variant-numeric: tabular-nums; overflow-x: auto; white-space: nowrap;
  }
  #readout b { color: var(--vscode-foreground); }
</style>
<title>S32E Scope</title>
</head>
<body>
<div id="bar">
  <label>rate <input id="rate" type="number" min="1" max="1000" step="1" value="${rate}"> /s total</label>
  <label>window <select id="win">
    <option value="2">2 s</option>
    <option value="5">5 s</option>
    <option value="10" selected>10 s</option>
    <option value="30">30 s</option>
    <option value="60">60 s</option>
  </select></label>
  <button id="layout">Overlay</button>
  <button id="pause">Pause</button>
  <button id="clearCursors">Clear cursors</button>
  <button id="clear">Clear</button>
  <button id="csv">Export CSV</button>
  <span id="achieved"></span>
</div>
<div id="main">
  <div id="side">
    <div id="signals"></div>
    <div id="addrow"><input id="addpath" placeholder="add path…"><button id="addbtn">+</button></div>
  </div>
  <div id="wrap"><canvas id="c"></canvas><div id="note"></div></div>
</div>
<div id="readout"></div>
<script nonce="${nonce}">
(function () {
  const vscode = acquireVsCodeApi();
  const canvas = document.getElementById("c");
  const ctx = canvas.getContext("2d");
  const wrap = document.getElementById("wrap");
  const noteEl = document.getElementById("note");
  const readout = document.getElementById("readout");
  const achieved = document.getElementById("achieved");
  const signalsEl = document.getElementById("signals");

  const DARK = ${JSON.stringify(SLOT_DARK)};
  const LIGHT = ${JSON.stringify(SLOT_LIGHT)};
  const VARS = ${JSON.stringify(SLOT_VARS)};
  const RING = 200000;

  let list = [];                 // [{id, name, slot, kind, hidden}]
  const data = new Map();        // id -> {t: [], v: []}
  let windowMs = 10000;
  let lanes = true;
  let paused = false;
  let cursorA = null;            // absolute ms
  let cursorB = null;
  let dragging = null;           // "A" | "B"

  function css(name, fallback) {
    const v = getComputedStyle(document.body).getPropertyValue(name).trim();
    return v || fallback;
  }
  function slotColor(slot) {
    const cls = document.body.className;
    if (cls.indexOf("high-contrast") >= 0) return css("--vscode-foreground", "#fff");
    const themed = css(VARS[slot % VARS.length], "");
    if (themed) return themed;
    const table = cls.indexOf("vscode-light") >= 0 ? LIGHT : DARK;
    return table[slot % table.length];
  }
  function fmt(v) {
    if (v === null || v === undefined || !isFinite(v)) return "—";
    const a = Math.abs(v);
    if (a !== 0 && (a >= 1e6 || a < 1e-3)) return v.toExponential(3);
    return String(Math.round(v * 1000) / 1000);
  }
  function series(id) {
    let s = data.get(id);
    if (!s) { s = { t: [], v: [] }; data.set(id, s); }
    return s;
  }
  function latestT() {
    let out = 0;
    for (const s of data.values()) {
      if (s.t.length && s.t[s.t.length - 1] > out) out = s.t[s.t.length - 1];
    }
    return out;
  }
  // -- plotMath transcriptions (unit-tested in src/plot/plotMath.ts) --------
  function nearestIndex(ts, t) {
    if (ts.length === 0) return -1;
    let lo = 0, hi = ts.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (ts[mid] < t) lo = mid + 1; else hi = mid; }
    if (lo > 0 && Math.abs(ts[lo - 1] - t) <= Math.abs(ts[lo] - t)) return lo - 1;
    return lo;
  }
  function decimate(s, t0, t1, columns) {
    const out = [];
    if (columns <= 0 || t1 <= t0) return out;
    const width = t1 - t0;
    let cur = null;
    const i0 = Math.max(0, nearestIndex(s.t, t0) - 1);
    for (let i = i0; i < s.t.length; i++) {
      const t = s.t[i];
      if (t < t0 || t > t1) continue;
      const x = Math.min(columns - 1, Math.floor(((t - t0) / width) * columns));
      const v = s.v[i];
      if (cur === null || cur.x !== x) {
        if (cur !== null) out.push(cur);
        cur = { x, min: v, max: v, last: v };
      } else {
        if (v < cur.min) cur.min = v;
        if (v > cur.max) cur.max = v;
        cur.last = v;
      }
    }
    if (cur !== null) out.push(cur);
    return out;
  }

  window.addEventListener("message", (ev) => {
    const msg = ev.data;
    if (msg.type === "batch") {
      const s = series(msg.id);
      for (let i = 0; i < msg.t.length; i++) { s.t.push(msg.t[i]); s.v.push(msg.v[i]); }
      if (s.t.length > RING) { const cut = s.t.length - RING; s.t.splice(0, cut); s.v.splice(0, cut); }
      noteEl.style.display = "none";
    } else if (msg.type === "signals") {
      list = msg.list;
      renderList();
    } else if (msg.type === "drop") {
      data.delete(msg.id);
    } else if (msg.type === "rate") {
      document.getElementById("rate").value = String(msg.rate);
    } else if (msg.type === "note") {
      noteEl.textContent = msg.text;
      noteEl.style.display = "block";
    }
  });

  function renderList() {
    signalsEl.textContent = "";
    for (const sig of list) {
      const row = document.createElement("div");
      row.className = "sig" + (sig.hidden ? " hidden" : "");
      const hide = document.createElement("input");
      hide.type = "checkbox"; hide.checked = !sig.hidden;
      hide.title = "show/hide (hidden signals stop streaming and free budget)";
      hide.addEventListener("change", () =>
        vscode.postMessage({ type: "hide", id: sig.id, value: !hide.checked }));
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.style.background = slotColor(sig.slot);
      const name = document.createElement("span");
      name.className = "name"; name.textContent = sig.name; name.title = sig.id;
      const val = document.createElement("span");
      val.className = "val"; val.id = "val-" + sig.id;
      const rm = document.createElement("button");
      rm.textContent = "✕"; rm.title = "remove from plot";
      rm.addEventListener("click", () => vscode.postMessage({ type: "remove", id: sig.id }));
      row.append(hide, chip, name, val, rm);
      signalsEl.append(row);
    }
  }

  document.getElementById("rate").addEventListener("change", (ev) => {
    const hz = Math.min(1000, Math.max(1, Math.round(Number(ev.target.value) || 100)));
    ev.target.value = String(hz);
    vscode.postMessage({ type: "setRate", hz });
  });
  document.getElementById("win").addEventListener("change", (ev) => {
    windowMs = Number(ev.target.value) * 1000;
  });
  const layoutBtn = document.getElementById("layout");
  layoutBtn.addEventListener("click", () => {
    lanes = !lanes;
    layoutBtn.textContent = lanes ? "Overlay" : "Lanes";
  });
  const pauseBtn = document.getElementById("pause");
  pauseBtn.addEventListener("click", () => {
    paused = !paused;
    pauseBtn.textContent = paused ? "Resume" : "Pause";
    vscode.postMessage({ type: "pause", value: paused });
  });
  document.getElementById("clearCursors").addEventListener("click", () => {
    cursorA = cursorB = null;
  });
  document.getElementById("clear").addEventListener("click", () => {
    for (const s of data.values()) { s.t.length = 0; s.v.length = 0; }
    cursorA = cursorB = null;
    vscode.postMessage({ type: "clear" });
  });
  document.getElementById("csv").addEventListener("click", () =>
    vscode.postMessage({ type: "exportCsv" }));
  const addInput = document.getElementById("addpath");
  const doAdd = () => {
    const path = addInput.value.trim();
    if (path) { vscode.postMessage({ type: "addPath", path }); addInput.value = ""; }
  };
  document.getElementById("addbtn").addEventListener("click", doAdd);
  addInput.addEventListener("keydown", (ev) => { if (ev.key === "Enter") doAdd(); });
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") cursorA = cursorB = null;
  });

  const PAD_L = 56, PAD_R = 10, PAD_T = 6, PAD_B = 20;
  let geom = { t0: 0, t1: 1, plotW: 1 };

  function xToT(px) {
    return geom.t0 + ((px - PAD_L) / geom.plotW) * (geom.t1 - geom.t0);
  }
  function tToX(t) {
    return PAD_L + ((t - geom.t0) / (geom.t1 - geom.t0)) * geom.plotW;
  }

  canvas.addEventListener("mousedown", (ev) => {
    const t = xToT(ev.offsetX);
    const near = (c) => c !== null && Math.abs(tToX(c) - ev.offsetX) < 6;
    if (near(cursorA)) { dragging = "A"; return; }
    if (near(cursorB)) { dragging = "B"; return; }
    if (cursorA === null) { cursorA = t; dragging = "A"; }
    else if (cursorB === null) { cursorB = t; dragging = "B"; }
    else { cursorB = t; dragging = "B"; }
  });
  canvas.addEventListener("mousemove", (ev) => {
    if (dragging === "A") cursorA = xToT(ev.offsetX);
    else if (dragging === "B") cursorB = xToT(ev.offsetX);
  });
  window.addEventListener("mouseup", () => { dragging = null; });

  function draw() {
    const dpr = window.devicePixelRatio || 1;
    const w = wrap.clientWidth, h = wrap.clientHeight;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr; canvas.height = h * dpr;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const end = latestT() || Date.now();
    geom = { t0: end - windowMs, t1: end, plotW: w - PAD_L - PAD_R };
    const plotH = h - PAD_T - PAD_B;
    if (geom.plotW < 30 || plotH < 30) { requestAnimationFrame(draw); return; }

    const gridColor = css("--vscode-editorWidget-border", "#808080");
    const textColor = css("--vscode-descriptionForeground", "#999");
    ctx.font = "11px " + css("--vscode-font-family", "sans-serif");

    const visible = list.filter((s) => !s.hidden);
    const laneH = lanes && visible.length > 0 ? plotH / visible.length : plotH;
    const columns = Math.max(1, Math.floor(geom.plotW));

    // time grid
    ctx.globalAlpha = 0.3; ctx.strokeStyle = gridColor; ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 0; i <= 5; i++) {
      const px = Math.round(PAD_L + (geom.plotW * i) / 5) + 0.5;
      ctx.moveTo(px, PAD_T); ctx.lineTo(px, h - PAD_B);
    }
    ctx.stroke(); ctx.globalAlpha = 1;
    ctx.fillStyle = textColor; ctx.textAlign = "center";
    for (let i = 0; i <= 5; i++) {
      const sec = (windowMs / 1000) * (1 - i / 5);
      ctx.fillText("-" + sec.toFixed(sec < 10 ? 1 : 0) + "s",
        PAD_L + (geom.plotW * i) / 5, h - 6);
    }
    ctx.textAlign = "left";

    const scales = new Map();  // id -> {min, max, y0, hgt}
    visible.forEach((sig, idx) => {
      const s = series(sig.id);
      const cols = decimate(s, geom.t0, geom.t1, columns);
      let vmin = Infinity, vmax = -Infinity;
      for (const c of cols) { if (c.min < vmin) vmin = c.min; if (c.max > vmax) vmax = c.max; }
      if (!isFinite(vmin)) { vmin = 0; vmax = 1; }
      if (vmin === vmax) { const p = Math.max(1, Math.abs(vmin) * 0.1); vmin -= p; vmax += p; }
      const span = vmax - vmin; vmin -= span * 0.07; vmax += span * 0.07;
      const y0 = lanes ? PAD_T + idx * laneH : PAD_T;
      const hgt = lanes ? laneH - 6 : plotH;
      scales.set(sig.id, { vmin, vmax, y0, hgt, cols });
    });

    // lane separators + min/max labels + series
    visible.forEach((sig) => {
      const sc = scales.get(sig.id);
      const color = slotColor(sig.slot);
      if (lanes) {
        ctx.globalAlpha = 0.3; ctx.strokeStyle = gridColor;
        ctx.beginPath();
        ctx.moveTo(PAD_L, Math.round(sc.y0 + sc.hgt) + 0.5);
        ctx.lineTo(w - PAD_R, Math.round(sc.y0 + sc.hgt) + 0.5);
        ctx.stroke(); ctx.globalAlpha = 1;
      }
      ctx.fillStyle = textColor;
      ctx.fillText(fmt(sc.vmax), 4, sc.y0 + 10);
      ctx.fillText(fmt(sc.vmin), 4, sc.y0 + sc.hgt - 2);
      const y = (v) => sc.y0 + (1 - (v - sc.vmin) / (sc.vmax - sc.vmin)) * sc.hgt;
      ctx.strokeStyle = color; ctx.lineWidth = 1;
      ctx.beginPath();
      for (const c of sc.cols) {
        const px = PAD_L + c.x + 0.5;
        ctx.moveTo(px, y(c.min)); ctx.lineTo(px, y(c.max));
      }
      ctx.stroke();
      ctx.lineWidth = 2; ctx.lineJoin = "round";
      ctx.beginPath();
      let started = false;
      for (const c of sc.cols) {
        const px = PAD_L + c.x, py = y(c.last);
        if (!started) { ctx.moveTo(px, py); started = true; } else ctx.lineTo(px, py);
      }
      ctx.stroke();
      const label = document.getElementById("val-" + sig.id);
      if (label) {
        const s = series(sig.id);
        label.textContent = s.v.length ? fmt(s.v[s.v.length - 1]) : "";
      }
    });

    // cursors
    for (const [name, c] of [["A", cursorA], ["B", cursorB]]) {
      if (c === null || c < geom.t0 || c > geom.t1) continue;
      const px = Math.round(tToX(c)) + 0.5;
      ctx.strokeStyle = textColor; ctx.globalAlpha = 0.8; ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(px, PAD_T); ctx.lineTo(px, h - PAD_B); ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1; ctx.fillStyle = textColor;
      ctx.fillText(name, px + 3, PAD_T + 10);
    }

    // readout: cursors table or live hint
    if (cursorA !== null || cursorB !== null) {
      const bits = [];
      const rel = (t) => t === null ? "—" : ((t - geom.t1) / 1000).toFixed(3) + "s";
      bits.push("A: " + rel(cursorA), "B: " + rel(cursorB));
      if (cursorA !== null && cursorB !== null) {
        bits.push("<b>Δt: " + ((cursorB - cursorA) / 1000).toFixed(3) + "s</b>");
      }
      for (const sig of visible) {
        const s = series(sig.id);
        const at = (c) => {
          if (c === null) return null;
          const i = nearestIndex(s.t, c);
          return i < 0 ? null : s.v[i];
        };
        const va = at(cursorA), vb = at(cursorB);
        let part = sig.name + ": " + fmt(va) + " → " + fmt(vb);
        if (va !== null && vb !== null) part += " (Δ " + fmt(vb - va) + ")";
        bits.push(part);
      }
      readout.innerHTML = bits.join("&nbsp;&nbsp;·&nbsp;&nbsp;");
    } else {
      // achieved rate over the last 2 s, total across signals
      let n = 0;
      const cut = end - 2000;
      for (const s of data.values()) {
        for (let i = s.t.length - 1; i >= 0 && s.t[i] >= cut; i--) n++;
      }
      achieved.textContent = n ? (n / 2).toFixed(0) + " samples/s achieved" : "";
      readout.textContent =
        "click the chart to place cursor A, again for B · drag to adjust · Esc clears";
    }
    requestAnimationFrame(draw);
  }
  requestAnimationFrame(draw);
})();
</script>
</body>
</html>`;
}

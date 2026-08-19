import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { SessionClient } from "./backend/client";
import { oneshot } from "./backend/oneshot";
import {
  BscanDriveResult,
  BscanSampleResult,
  hexToInt,
  PollResult,
  StackFrame,
  StacksConfigResult,
  StacksScanResult,
  toHex,
  VarRow,
  VarWriteResult,
} from "./backend/types";
import { backendConfig, setting } from "./config";
import { DISASM_SCHEME, DisasmProvider, disasmUri } from "./editor/disasmProvider";
import { MemProvider, memUri } from "./editor/memProvider";
import { PcDecorations } from "./editor/sourceDecorations";
import { PlotPanels } from "./plot/plotPanel";
import { Poller } from "./poll/poller";
import { BreakpointStore, S32Breakpoint } from "./state/breakpoints";
import { BscanState } from "./state/bscan";
import { ComparatorReconciler } from "./state/comparators";
import { ElfState } from "./state/elf";
import { CoreInfo, DebugState, familyOf, M33_NAMES } from "./state/session";
import { StacksState } from "./state/stacks";
import { VariablesLive, VariablesState } from "./state/variables";
import { PolledView, ViewPolling } from "./state/viewPolling";
import { S32Watchpoint, WatchpointStore } from "./state/watchpoints";
import { confirmDestructive } from "./ui/confirm";
import { Output } from "./ui/output";
import { StatusBar } from "./ui/statusBar";
import { coreArg } from "./views/coresView";
import { StackView } from "./views/stackView";
import {
  isStaleHandleError,
  validateWatchPath,
} from "./views/variablesFormat";

export interface Deps {
  state: DebugState;
  elf: ElfState;
  output: Output;
  poller: Poller;
  bpStore: BreakpointStore;
  wpStore: WatchpointStore;
  disasm: DisasmProvider;
  mem: MemProvider;
  pcDecor: PcDecorations;
  statusBar: StatusBar;
  stackView: StackView;
  variables: VariablesState;
  varLive: VariablesLive;
  viewPolling: ViewPolling;
  comparators: ComparatorReconciler;
  plots: PlotPanels;
  stacks: StacksState;
  bscan: BscanState;
  variablesTreeVisible: () => boolean;
  refreshTitles: () => void;
}

const APS = ["axi", "apb", "mdm", "sda", "ahb-smu", "ahb-ce0", "ahb-ce1", "ahb-r0", "ahb-r1"];

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

async function guarded(deps: Deps, fn: () => Promise<void>): Promise<void> {
  try {
    await fn();
  } catch (err) {
    void vscode.window
      .showErrorMessage(`S32E: ${message(err)}`, "Show Log")
      .then((pick) => pick === "Show Log" && deps.output.show());
  }
}

// ---------------------------------------------------------------------------
// connect / disconnect
// ---------------------------------------------------------------------------

async function connect(deps: Deps): Promise<void> {
  const { state, elf, output, poller, bpStore, wpStore, statusBar } = deps;
  if (state.connected || state.connecting) {
    return;
  }
  state.connecting = true;
  state.fireChange();
  statusBar.refresh();
  try {
    const { client, ready } = await SessionClient.start(backendConfig(), output);
    state.client = client;
    state.connected = true;
    state.connecting = false;
    state.backendVersion = ready.version;
    // Pushed halts: the backend's monitor notices a breakpoint hit within
    // ~50 ms and this fires the same reaction chain the poller's transition
    // detection would -- the poller remains only as a slow fallback.
    client.onStopped((ev) => state.noteStopped(ev));
    client.onExit(({ expected }) => {
      if (expected) {
        return;
      }
      poller.stop();
      deps.varLive.stop();
      deps.comparators.stop();
      deps.comparators.clear();
      deps.variables.reset();
      bpStore.markAllPending();
      wpStore.markAllPending();
      deps.pcDecor.clear();
      state.resetForDisconnect();
      statusBar.refresh();
      void vscode.window
        .showErrorMessage(
          "S32E: the backend session ended unexpectedly. The board may be " +
            "left with comparators armed or cores halted (the restore ledger " +
            "did not run) -- reconnect and disconnect cleanly, or power-cycle.",
          "Reconnect",
          "Show Log",
        )
        .then((pick) => {
          if (pick === "Reconnect") {
            void connect(deps);
          } else if (pick === "Show Log") {
            output.show();
          }
        });
    });

    if (ready.discovery_error) {
      output.line("!! no cores at session open -- running `gates apply`");
      try {
        await client.run("gates apply");
      } catch (err) {
        void vscode.window.showWarningMessage(
          `S32E: gates apply failed (${message(err)}). ` +
            "Is the board powered? Run Diagnostics: Doctor after disconnecting.",
        );
      }
    }
    const focusTarget = ready.focus ?? ready.m33_focus ?? "rtu0.0";
    try {
      await client.run(`use ${focusTarget}`);
      state.focus = focusTarget;
    } catch {
      state.focus = null;
    }
    try {
      state.applyPoll((await client.run("poll")) as PollResult);
    } catch (err) {
      output.line(`!! first poll failed: ${message(err)}`);
    }
    if (elf.active) {
      await symLoad(deps);
      await bpStore.applyAll();
      await wpStore.applyAll();
    }
    poller.start();
    deps.varLive.start();
    deps.comparators.start();
    // One all-cores discovery sweep: leftovers from a previous session (kept
    // alive by --keep-breakpoints) surface for adoption even on cores this
    // session has no rows on. The periodic sweep stays narrow.
    void deps.comparators.tick(true);
    deps.plots.resync();
    state.fireChange();
    statusBar.refresh();
    deps.refreshTitles();
  } catch (err) {
    state.resetForDisconnect();
    statusBar.refresh();
    void vscode.window
      .showErrorMessage(`S32E: could not connect -- ${message(err)}`, "Show Log")
      .then((pick) => pick === "Show Log" && deps.output.show());
  }
}

async function disconnect(deps: Deps): Promise<void> {
  const { state, output, poller, bpStore, wpStore, statusBar } = deps;
  const client = state.client;
  if (!client) {
    return;
  }
  poller.stop();
  deps.varLive.stop();
  deps.comparators.stop();
  deps.comparators.clear();
  deps.variables.reset();
  bpStore.markAllPending();
  wpStore.markAllPending();
  deps.pcDecor.clear();
  const bye = await client.quit();
  if (bye && bye.restore_notes.length > 0) {
    output.section("restore on disconnect");
    for (const note of bye.restore_notes) {
      output.line(`   ${note}`);
    }
  }
  state.resetForDisconnect();
  statusBar.refresh();
  deps.refreshTitles();
}

async function symLoad(deps: Deps): Promise<boolean> {
  const { state, elf, output } = deps;
  if (!elf.active || !state.connected) {
    return false;
  }
  try {
    await state.requireClient().run(`sym load "${elf.active}"`);
    state.symbolsLoaded = true;
    deps.disasm.clearSourceCache();
    return true;
  } catch (err) {
    state.symbolsLoaded = false;
    output.line(`!! sym load: ${message(err)}`);
    void vscode.window.showWarningMessage(
      `S32E: could not load symbols from ${elf.basename}: ${message(err)}. ` +
        "Gutter breakpoints and source stepping stay unavailable.",
    );
    return false;
  }
}

// ---------------------------------------------------------------------------
// stack monitor
// ---------------------------------------------------------------------------

/** One button press = one scan. Re-issues `stacks config` first: parsing the
 * ARXML is host-side work (<100 ms) and re-doing it keeps the refresh honest
 * across backend reconnects (the session forgets its config with the
 * process). Needs symbols -- addresses come from the ELF. */
async function refreshStacks(deps: Deps): Promise<void> {
  const { state, stacks } = deps;
  if (!state.connected) {
    throw new Error("not connected");
  }
  if (!stacks.arxml) {
    await vscode.commands.executeCommand("s32e.stacks.selectProject");
    return;
  }
  if (!state.symbolsLoaded && !(await symLoad(deps))) {
    throw new Error(
      "stack addresses come from the ELF's symbols — pick an ELF first",
    );
  }
  const client = state.requireClient();
  const pattern =
    setting<string>("stacks.fillPattern", "0xAAAAAAAA").trim() || "0xAAAAAAAA";
  stacks.scanning = true;
  stacks.fire();
  try {
    await vscode.window.withProgress(
      { location: { viewId: "s32e.stacks" } },
      async () => {
        await client.run(`stacks config --arxml "${stacks.arxml}"`);
        const result = (await client.run(
          `stacks scan --pattern ${pattern}`,
        )) as StacksScanResult;
        stacks.noteScan(result.rows);
      },
    );
  } finally {
    stacks.scanning = false;
    stacks.fire();
  }
}

// ---------------------------------------------------------------------------
// boundary scan
// ---------------------------------------------------------------------------

/** One button press = one `bscan sample` one-shot (~62 JTAGLoad runs).
 *
 * Inverted from every other refresh: it REFUSES while connected. JTAGLoad
 * needs the probe to itself (`bscan` is SESSION_BLOCKED in the backend for
 * the same reason), and the RM Table 984 preamble the sample plays takes the
 * DAP away until the board is power-cycled -- so one modal confirm per board
 * session before the first run. */
async function refreshBscan(deps: Deps): Promise<void> {
  const { state, bscan, output } = deps;
  if (state.connected) {
    throw new Error(
      "boundary scan changes the JTAG chain and takes the DAP away until " +
        "power-cycle — disconnect the session first (run it last)",
    );
  }
  if (!bscan.confirmed) {
    const go = await confirmDestructive(
      "Run boundary scan? The preamble changes the JTAG chain.",
      "The RM Table 984 preamble links JTAGC_AE into the scan chain (IR 8 → 16) " +
        "and the change persists. DP/AP access — connect, memory, run control — " +
        "will stop working until the board is power-cycled. Sampling itself is " +
        "non-invasive (SAMPLE/PRELOAD; the application keeps running). Run " +
        "boundary scan last.",
      "Run boundary scan",
    );
    if (!go) {
      return;
    }
    bscan.confirmed = true;
  }
  const args = ["bscan", "sample"];
  const bsdlPath = bscan.bsdlPath;
  if (bsdlPath) {
    args.push("--bsdl", bsdlPath);
  }
  if (setting<boolean>("bscan.reverseCells", false)) {
    args.push("--reverse-cells");
  }
  // The sample SVF ends with SAMPLE/PRELOAD latched, so an active EXTEST
  // drive is released during the read; re-latch it right after (user chose
  // auto re-apply -- the glitch is stated in the drive consent modal).
  const relatch = bscan.driveLatched && bscan.driveSet.size > 0;
  bscan.sampling = true;
  bscan.fire();
  try {
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "S32E: boundary-scan sample (one JTAGLoad run per byte, ~10 s)...",
      },
      async () => {
        const r = await oneshot(backendConfig(), output, args, {
          timeoutMs: 600000,
        });
        const json = r.json as BscanSampleResult | null;
        if (!json?.first?.pins) {
          throw new Error(
            `bscan sample failed (exit ${r.code}) — see the S32E log`,
          );
        }
        bscan.noteSample(json.bsdl ?? null, json.first.pins, json.first.raw_hex);
      },
    );
  } finally {
    bscan.sampling = false;
    bscan.fire();
  }
  if (relatch) {
    await applyDrives(deps);
  }
}

/** Mirror of the backend's `jtag/bscan.py:DRIVE_OPT_IN`. The modal below is
 * the human consent this flag attests to; passing it without the modal is
 * exactly what the flag exists to prevent, so keep them together. */
const BSCAN_DRIVE_OPT_IN = "--i-understand-boundary-scan-drives-pins";

/** Send the WHOLE drive set as one EXTEST latch (empty set = release).
 *
 * Whole-set is not a convenience: a `bscan drive` run rebuilds the full
 * boundary vector from safe values, so omitting a previously-driven pin
 * silently releases it. The view's per-pin actions all funnel through here.
 */
async function applyDrives(deps: Deps): Promise<void> {
  const { state, bscan, output } = deps;
  if (state.connected) {
    throw new Error(
      "boundary scan changes the JTAG chain and takes the DAP away until " +
        "power-cycle — disconnect the session first (run it last)",
    );
  }
  if (bscan.driveSet.size === 0) {
    await releaseDrives(deps);
    return;
  }
  if (!bscan.driveConfirmed) {
    const go = await confirmDestructive(
      "Drive pins via EXTEST? This stops the MCU.",
      "KNOWN LIMITATION: EXTEST drive is UNCONFIRMED on S32E silicon — on an " +
        "S32E277 the pads did not follow the preloaded data (bench 2026-08-19, " +
        "pending NXP). Verify with a scope; do not rely on it. SAMPLE/read works.\n\n" +
        "EXTEST asserts the internal MCU system reset (RM 136.4.4.3): the " +
        "application stops and the listed pins are driven by boundary scan. " +
        "The drive PERSISTS after each command until Release or a " +
        "power-cycle. Every change re-sends the whole drive set, and " +
        "sampling (⟳) briefly returns the pins to the (booting) system " +
        "before the drive is automatically re-applied.",
      "Drive pins",
    );
    if (!go) {
      return;
    }
    bscan.driveConfirmed = true;
  }
  const args = ["bscan", "drive"];
  for (const [pin, value] of bscan.driveSet) {
    args.push(`${pin}=${value}`);
  }
  args.push(BSCAN_DRIVE_OPT_IN);
  const bsdlPath = bscan.bsdlPath;
  if (bsdlPath) {
    args.push("--bsdl", bsdlPath);
  }
  await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: `S32E: EXTEST drive (${bscan.driveSet.size} pin(s))...`,
    },
    async () => {
      const r = await oneshot(backendConfig(), output, args, {
        timeoutMs: 120000,
      });
      const json = r.json as BscanDriveResult | null;
      if (!json?.ok) {
        // Latch state is unknown after a failed run; assume released and say
        // how to be sure.
        bscan.driveLatched = false;
        bscan.fire();
        const detail = json?.failures?.[0] ?? `exit ${r.code}`;
        throw new Error(
          `EXTEST drive failed (${detail}) — see the S32E log; press Release ` +
            "or power-cycle if unsure of the pin state",
        );
      }
      bscan.driveLatched = true;
      bscan.fire();
    },
  );
}

async function releaseDrives(deps: Deps): Promise<void> {
  const { state, bscan, output } = deps;
  if (state.connected) {
    throw new Error("disconnect first — boundary scan runs without a session");
  }
  const r = await oneshot(backendConfig(), output, ["bscan", "release"], {
    timeoutMs: 120000,
  });
  const json = r.json as BscanDriveResult | null;
  if (json && !json.ok) {
    throw new Error("release SVF failed — see the S32E log; power-cycle to be sure");
  }
  bscan.clearDrives();
  void vscode.window.showInformationMessage(
    "S32E: boundary-scan drive released — pins returned to the system, the " +
      "MCU is booting. Power-cycle before further boundary scan or debugging.",
  );
}

// ---------------------------------------------------------------------------
// focused-core data refresh + on-halt behavior
// ---------------------------------------------------------------------------

export async function refreshFocusData(
  deps: Deps, force?: Set<PolledView>,
): Promise<void> {
  const { state, viewPolling } = deps;
  const core = state.focus;
  if (!core || !state.connected || state.cores.get(core)?.halted !== true) {
    return;
  }
  // Each view's Auto-refresh checkbox gates its block; a view's own refresh
  // button passes itself in `force` so a one-shot always works.
  const wants = (view: PolledView) =>
    viewPolling.on(view) || (force?.has(view) ?? false);
  const client = state.requireClient();
  const family = familyOf(core);
  if (wants("registers")) {
    try {
      if (family === "r52") {
        const regs = (await client.run(`core regs --core ${core}`)) as {
          registers: Record<string, string>;
          pc: string | null;
        };
        const merged = { ...regs.registers, pc: regs.pc ?? "?" };
        state.prevRegs.set(core, state.lastRegs.get(core) ?? {});
        state.lastRegs.set(core, merged);
        state.notePc(core, regs.pc);
      } else {
        const regs = (await client.run(`m33 regs --core ${core}`)) as {
          registers: Record<string, string>;
        };
        state.prevRegs.set(core, state.lastRegs.get(core) ?? {});
        state.lastRegs.set(core, regs.registers);
        state.notePc(core, regs.registers.pc);
      }
    } catch (err) {
      deps.output.line(`!! regs: ${message(err)}`);
    }
  } else {
    // The register read doubled as the PC source for the source reveal and
    // the gutter arrow; with it unchecked, fetch just the PC (one register).
    try {
      const result = (await client.run(
        family === "r52"
          ? `core reg pc --core ${core}`
          : `m33 reg pc --core ${core}`,
      )) as { value: string };
      state.notePc(core, result.value);
    } catch (err) {
      deps.output.line(`!! pc: ${message(err)}`);
    }
  }
  if (wants("callStack")) {
    try {
      const stack = (await client.run(
        `stack --core ${core} --max-frames ${setting<number>("maxStackFrames", 32)}`,
      )) as { frames: StackFrame[]; stopped: string };
      state.lastStack = stack.frames;
      state.lastStackReason = stack.stopped;
    } catch (err) {
      state.lastStack = [];
      state.lastStackReason = message(err);
    }
  }
  deps.variables.selectedFrame = 0;
  deps.variables.noteHalt();
  if (deps.variablesTreeVisible() && wants("variables")) {
    // One render for the whole variables refresh instead of one per fetch.
    // The backend serves the repeats from its per-stop caches, so the
    // watches after locals cost tens of transactions, not a re-unwind each.
    await deps.variables.batch(async () => {
      await deps.variables.fetchLocals(client, core, 0);
      await deps.variables.fetchWatches(client, core, 0);
      if (deps.variables.globalsOpened) {
        await deps.variables.fetchGlobals(client, core);
      }
    });
  }
  if (wants("memory")) {
    // Opt-in: re-read every open memory region at the stop.
    try {
      await deps.mem.refreshAll();
    } catch (err) {
      deps.output.line(`!! memory refresh: ${message(err)}`);
    }
  }
  deps.stackView.refresh();
  state.fireChange();
  // One comparator verification per stop, on top of the slow interval; it
  // gates itself on the Breakpoints/Watchpoints checkboxes.
  void deps.comparators.tick();
}

/** Standard debugger feel after any halt: refresh + reveal source or disasm. */
async function afterHalt(deps: Deps, core: string, pcHint?: string | null): Promise<void> {
  const { state, disasm, pcDecor } = deps;
  state.markHalted(core, true);
  if (core !== state.focus) {
    return;
  }
  await refreshFocusData(deps);
  // A null hint means the command's payload had no PC (the R52 `core halt`
  // result, unlike the M33's) -- fall back to lastPc, which the refresh
  // above just renewed. Taking null literally used to skip the reveal and
  // leave manual R52 halts arrow-less.
  const pc = pcHint != null ? state.notePc(core, pcHint) : (state.lastPc.get(core) ?? null);
  // A new stop: drop every stale PC marker, even if this one's PC is unknown
  // -- an arrow at the previous stop's line would be a lie.
  pcDecor.beginStop();
  if (pc === null) {
    return;
  }
  let revealed = false;
  if (state.symbolsLoaded) {
    try {
      const lookup = (await state.requireClient().run(
        `sym addr2line ${toHex(pc)}`,
      )) as { lookups: { file: string | null; line: number | null }[] };
      const hit = lookup.lookups[0];
      if (hit?.file && hit.line && fs.existsSync(hit.file)) {
        const doc = await vscode.workspace.openTextDocument(hit.file);
        const editor = await vscode.window.showTextDocument(doc, {
          preview: true,
          preserveFocus: false,
        });
        const line0 = hit.line - 1;
        editor.revealRange(
          new vscode.Range(line0, 0, line0, 0),
          vscode.TextEditorRevealType.InCenter,
        );
        pcDecor.set(doc.uri, hit.line);
        revealed = true;
      }
    } catch {
      /* fall through to disasm */
    }
  }
  if (!revealed) {
    try {
      await disasm.open(core, pc, true);
      const line = disasm.addrToLine(core, pc);
      if (line !== null) {
        pcDecor.set(disasmUri(core), line + 1);
      }
    } catch (err) {
      deps.output.line(`!! disasm reveal: ${message(err)}`);
    }
  } else if (disasm.isOpen(core)) {
    // Keep an already-open disasm tab in step with the source view -- arrow
    // included: source and disassembly both mark the same PC.
    try {
      await disasm.render(core, pc);
      disasm.revealAddr(core, pc);
      const line = disasm.addrToLine(core, pc);
      if (line !== null) {
        pcDecor.set(disasmUri(core), line + 1);
      }
    } catch {
      /* non-fatal */
    }
  }
}

function requireFocusCore(state: DebugState): string {
  const core = state.focus;
  if (!core) {
    throw new Error("no focused core -- pick one in the Cores view");
  }
  return core;
}

function requireHaltedFocus(state: DebugState): string {
  const core = requireFocusCore(state);
  if (state.cores.get(core)?.halted !== true) {
    throw new Error(`${core} is not halted`);
  }
  return core;
}

// ---------------------------------------------------------------------------
// registration
// ---------------------------------------------------------------------------

export function registerCommands(
  context: vscode.ExtensionContext,
  deps: Deps,
): void {
  const { state, elf, output, bpStore, wpStore, disasm, mem } = deps;
  const reg = (id: string, fn: (...args: unknown[]) => unknown) =>
    context.subscriptions.push(
      vscode.commands.registerCommand(id, (...args: unknown[]) =>
        guarded(deps, async () => {
          await fn(...args);
        }),
      ),
    );

  // -- session --
  reg("s32e.connect", () => connect(deps));
  reg("s32e.disconnect", () => disconnect(deps));
  reg("s32e.connectOrDisconnect", () =>
    state.connected ? disconnect(deps) : connect(deps),
  );
  reg("s32e.showOutput", () => output.show());
  reg("s32e.healthCheck", async () => {
    const r = await oneshot(backendConfig(), output, ["--version"], {
      wantJson: false,
      timeoutMs: 20000,
    });
    void vscode.window.showInformationMessage(
      r.code === 0
        ? `S32E backend OK: ${r.text.trim().split("\n").pop()}`
        : `S32E backend NOT working (exit ${r.code}) -- check s32e.pythonPath ` +
            "and `pip install -e <repo>[ide]`. See the output log.",
    );
  });

  // -- focus --
  reg("s32e.pickFocusCore", async () => {
    if (!state.connected) {
      throw new Error("not connected");
    }
    const items = [...state.cores.values()].map((c) => ({
      label: (c.name === state.focus ? "$(record) " : "") + c.name,
      description: c.detail,
      name: c.name,
    }));
    const pick = await vscode.window.showQuickPick(items, {
      title: "Focus core (breakpoints, stepping, registers, stack act here)",
    });
    if (pick) {
      await focusCore(deps, pick.name);
    }
  });
  reg("s32e.focusCore", async (item) => {
    const core = coreArg(state, item as CoreInfo);
    if (core) {
      await focusCore(deps, core);
    }
  });

  // -- run control --
  reg("s32e.haltCore", (item) => haltCore(deps, coreArg(state, item as CoreInfo)));
  reg("s32e.halt", () => haltCore(deps, requireFocusCore(state)));
  reg("s32e.resumeCore", (item) => resumeCore(deps, coreArg(state, item as CoreInfo)));
  reg("s32e.continue", () => resumeCore(deps, requireFocusCore(state)));
  reg("s32e.haltAll", async () => {
    await state.requireClient().run("core halt --all");
    await deps.poller.tick();
    await afterHalt(deps, requireFocusCore(state));
  });
  reg("s32e.resumeAll", async () => {
    await state
      .requireClient()
      .run(`core resume --all${state.sync ? " --sync" : ""}`);
    deps.pcDecor.clear();
    await deps.poller.tick();
  });
  reg("s32e.toggleSync", async () => {
    const client = state.requireClient();
    const target = state.sync ? "off" : "on";
    if (target === "on") {
      const go = await vscode.window.showInformationMessage(
        "Turn sync (stop-the-world) ON? Any halt -- manual or breakpoint -- " +
          "then stops ALL cores together, and Continue restarts them together. " +
          "This is the safe mode for the software-coupled core pairs.",
        { modal: true },
        "Turn On",
      );
      if (go !== "Turn On") {
        return;
      }
    }
    await client.run(`sync ${target}`);
    state.sync = target === "on";
    state.fireChange();
  });
  reg("s32e.refreshCores", () => deps.poller.tick(true));
  reg("s32e.unlockCore", async (item) => {
    const core = coreArg(state, item as CoreInfo);
    await state.requireClient().run(`core unlock --core ${core}`);
    await deps.poller.tick();
  });

  // -- stepping --
  // The Cores-view row buttons pass the clicked item. Stepping acts on the
  // focus, so a click on a DIFFERENT halted core focuses it first -- the
  // same thing clicking the row does -- and then steps it; the arrow and
  // every focus-bound view follow. Keyboard/palette pass no item: no-op.
  const ensureFocusedOn = async (item: unknown): Promise<void> => {
    const target = coreArg(state, item as CoreInfo | undefined);
    if (target && target !== state.focus) {
      await focusCore(deps, target);
    }
  };
  reg("s32e.stepLineIn", async (item) => {
    await ensureFocusedOn(item);
    await stepLine(deps, false);
  });
  reg("s32e.stepLineOver", async (item) => {
    await ensureFocusedOn(item);
    await stepLine(deps, true);
  });
  reg("s32e.stepOut", async () => {
    const core = requireHaltedFocus(state);
    const result = (await state
      .requireClient()
      .run(`stepout --core ${core}`)) as { stopped: string; pc: string | null };
    if (result.stopped === "reached") {
      await afterHalt(deps, core, result.pc);
    } else {
      state.markHalted(core, false);
      deps.pcDecor.clear();
      deps.variables.invalidate();
      void vscode.window.showWarningMessage(
        `S32E: ${core} did not return within the timeout -- it is RUNNING ` +
          "(the temporary breakpoint was removed). Halt it manually when ready.",
      );
    }
  });
  reg("s32e.stepInstrIn", async (item) => {
    await ensureFocusedOn(item);
    await stepInstr(deps, false);
  });
  reg("s32e.stepInstrOver", async (item) => {
    await ensureFocusedOn(item);
    await stepInstr(deps, true);
  });
  reg("s32e.runToCursor", async () => {
    const core = requireHaltedFocus(state);
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      throw new Error("no active editor");
    }
    const line = editor.selection.active.line + 1;
    let addr: number | null = null;
    if (editor.document.uri.scheme === DISASM_SCHEME) {
      addr = disasm.lineToAddr(editor.document.uri, line);
    } else {
      if (!state.symbolsLoaded) {
        throw new Error("no symbols loaded -- choose an ELF first");
      }
      const base = path.basename(editor.document.uri.fsPath);
      const result = (await state
        .requireClient()
        .run(`sym line2addr "${base}" ${line}`)) as { addrs: string[] };
      addr = result.addrs?.length ? hexToInt(result.addrs[0]) : null;
    }
    if (addr === null) {
      throw new Error("no code at this line");
    }
    const result = (await state
      .requireClient()
      .run(`runto ${toHex(addr)} --core ${core}`)) as {
      stopped: string;
      pc: string | null;
    };
    if (result.stopped === "reached") {
      await afterHalt(deps, core, result.pc);
    } else {
      state.markHalted(core, false);
      deps.pcDecor.clear();
      deps.variables.invalidate();
      void vscode.window.showWarningMessage(
        `S32E: ${core} did not reach ${toHex(addr)} within the timeout -- ` +
          "it is RUNNING (the temporary breakpoint was removed).",
      );
    }
  });

  // -- catch / reset-halt --
  reg("s32e.catchCore", async (item) => {
    const core = coreArg(state, item as CoreInfo);
    if (!core || familyOf(core) !== "r52") {
      throw new Error("catch works on the R52 cores");
    }
    await state.requireClient().run(`catch --core ${core}`);
    const info = state.cores.get(core);
    if (info) {
      info.catchArmed = true;
    }
    state.fireChange();
    void vscode.window.showInformationMessage(
      `S32E: catch armed on ${core} -- it will halt at its first instruction ` +
        "when the application (or a reset) releases it. The latch is dropped " +
        "automatically on disconnect.",
    );
  });
  reg("s32e.disarmCatch", async (item) => {
    const core = coreArg(state, item as CoreInfo);
    await state.requireClient().run(`core catch --core ${core} --disarm`);
    const info = core ? state.cores.get(core) : null;
    if (info) {
      info.catchArmed = false;
    }
    state.fireChange();
  });
  reg("s32e.resetHaltCore", async (item) => {
    const core = coreArg(state, item as CoreInfo);
    if (!core || familyOf(core) !== "r52") {
      throw new Error("reset-halt works on the R52 cores (the M33 variant is blocked on this silicon)");
    }
    const go = await confirmDestructive(
      `Reset and halt ${core}?`,
      "This resets the WHOLE SoC -- every core reboots and everything running " +
        "stops. The core halts at its reset vector; breakpoints are re-armed after.",
      "Reset SoC",
    );
    if (!go) {
      return;
    }
    deps.state.busyOp = true;
    state.fireChange();
    try {
      const result = (await state
        .requireClient()
        .run(`reset-halt --core ${core}`)) as { pc?: string };
      await bpStore.reapplyAll();
      await wpStore.reapplyAll();
      await deps.poller.tick();
      await afterHalt(deps, core, result.pc ?? null);
    } finally {
      deps.state.busyOp = false;
      state.fireChange();
    }
  });

  // -- ELF / load --
  reg("s32e.chooseElf", async () => {
    const picked = await elf.pick();
    if (picked && state.connected) {
      if (await symLoad(deps)) {
        await reresolveLineBreakpoints(deps);
      }
    }
    deps.statusBar.refresh();
  });
  reg("s32e.resymbolicate", async () => {
    if (await symLoad(deps)) {
      await reresolveLineBreakpoints(deps);
      void vscode.window.showInformationMessage("S32E: symbols reloaded.");
    }
  });
  reg("s32e.loadElf", () => loadElf(deps));

  // -- breakpoints / watchpoints --
  reg("s32e.addBreakpointByAddress", async () => {
    const addrText = await vscode.window.showInputBox({
      title: "Breakpoint address (0x-prefixed hex)",
      placeHolder: "0x321D2EE6",
      validateInput: (v) => (/^0x[0-9a-fA-F]+$/.test(v.trim()) ? null : "0x-prefixed hex"),
    });
    if (!addrText) {
      return;
    }
    const core = await pickCore(state, "Arm the breakpoint on which core?");
    if (!core) {
      return;
    }
    bpStore.addByAddress(core, Number.parseInt(addrText.trim(), 16));
  });
  reg("s32e.removeBreakpoint", (node) => {
    const bp = (node as { bp?: S32Breakpoint })?.bp;
    if (bp) {
      return bpStore.remove(bp);
    }
  });
  reg("s32e.retargetBreakpoint", async (node) => {
    const bp = (node as { bp?: S32Breakpoint })?.bp;
    if (!bp) {
      return;
    }
    const core = await pickCore(state, `Move breakpoint to which core?`, bp.core);
    if (core) {
      await bpStore.retarget(bp, core);
    }
  });
  reg("s32e.duplicateBreakpoint", async (node) => {
    const bp = (node as { bp?: S32Breakpoint })?.bp;
    if (!bp) {
      return;
    }
    const core = await pickCore(state, `Duplicate breakpoint to which core?`, bp.core);
    if (core) {
      bpStore.duplicate(bp, core);
    }
  });
  reg("s32e.clearAllBreakpoints", () => bpStore.clearAll());
  reg("s32e.toggleDisasmBreakpoint", async () => {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.document.uri.scheme !== DISASM_SCHEME) {
      throw new Error("open the disassembly view first");
    }
    const line1 = editor.selection.active.line + 1;
    const addr = disasm.lineToAddr(editor.document.uri, line1);
    if (addr === null) {
      throw new Error("no instruction on this line");
    }
    const core = disasm.coreOf(editor.document.uri);
    const existing = bpStore.all.find(
      (b) => b.core === core && b.address === addr,
    );
    if (existing) {
      await bpStore.remove(existing);
    } else {
      bpStore.addByAddress(core, addr);
    }
  });
  reg("s32e.addWatchpoint", async () => {
    const addrText = await vscode.window.showInputBox({
      title: "Watchpoint: data address (0x-prefixed hex)",
      validateInput: (v) => (/^0x[0-9a-fA-F]+$/.test(v.trim()) ? null : "0x-prefixed hex"),
    });
    if (!addrText) {
      return;
    }
    const size = await vscode.window.showQuickPick(["1", "2", "4", "8"], {
      title: "Watched size in bytes (8 only on R52 cores)",
    });
    if (!size) {
      return;
    }
    const on = (await vscode.window.showQuickPick(["both", "load", "store"], {
      title: "Trigger on which access?",
    })) as S32Watchpoint["on"] | undefined;
    if (!on) {
      return;
    }
    const core = await pickCore(state, "Arm the watchpoint on which core?");
    if (!core) {
      return;
    }
    wpStore.add(core, Number.parseInt(addrText.trim(), 16), Number.parseInt(size, 10), on);
  });
  reg("s32e.removeWatchpoint", (node) => {
    // The view wraps rows as { kind: "wp", wp } since the hardware-verify
    // work; accept the bare model too for safety.
    const wp = (node as { wp?: S32Watchpoint })?.wp ?? (node as S32Watchpoint);
    if (wp?.id) {
      return wpStore.remove(wp);
    }
  });
  reg("s32e.clearAllWatchpoints", () => wpStore.clearAll());
  reg("s32e.clearOrphanComparator", async (node) => {
    const orphan = node as {
      unit?: "bp" | "wp"; core?: string; index?: number; address?: string;
    };
    if (!orphan?.core || orphan.index === undefined || !orphan.unit) {
      return;
    }
    const what = orphan.unit === "bp" ? "breakpoint" : "watchpoint";
    const go = await confirmDestructive(
      `Clear hardware ${what} comparator ${orphan.index} on ${orphan.core}?`,
      `The comparator is armed at ${orphan.address} but no ${what} in this ` +
        "session claims it -- clearing removes whatever set it (another " +
        "tool, a previous session, or target-side code).",
      "Clear comparator",
    );
    if (!go) {
      return;
    }
    const m33 = familyOf(orphan.core) === "m33";
    const base = orphan.unit;
    await state.requireClient().run(
      m33
        ? `m33 ${base} clear ${orphan.index} --core ${orphan.core}`
        : `${base} clear ${orphan.index} --core ${orphan.core}`,
    );
    void deps.comparators.tick();
  });

  // -- views / data --
  reg("s32e.refreshStack", () => refreshFocusData(deps, new Set(["callStack"])));
  reg("s32e.refreshRegisters", () => refreshFocusData(deps, new Set(["registers"])));

  // -- variables --
  const { variables } = deps;
  reg("s32e.selectFrame", async (frameIndex, file, line) => {
    const core = requireHaltedFocus(state);
    await variables.fetchLocals(state.requireClient(), core, frameIndex as number);
    if (typeof file === "string" && typeof line === "number") {
      await vscode.commands.executeCommand("s32e.openFrameSource", file, line);
    }
  });
  reg("s32e.refreshGlobalsSection", async () => {
    if (state.connected && state.focus) {
      await variables.fetchGlobals(state.requireClient(), state.focus);
    }
  });
  reg("s32e.expandMoreChildren", async (node) => {
    const more = node as { parentHandle: string; from: number };
    await variables.expand(state.requireClient(), more.parentHandle, more.from);
  });
  reg("s32e.refreshVariables", async () => {
    if (!state.connected || !state.focus) {
      return;
    }
    const client = state.requireClient();
    const halted = state.focusInfo?.halted === true;
    variables.noteHalt();
    if (halted) {
      await variables.fetchLocals(client, state.focus, variables.selectedFrame);
      await variables.fetchWatches(client, state.focus, variables.selectedFrame);
    } else {
      await variables.fetchWatches(client, state.focus, null);
    }
    if (variables.globalsOpened) {
      await variables.fetchGlobals(client, state.focus);
    }
  });
  reg("s32e.toggleHexVariables", () => {
    variables.hexMode = !variables.hexMode;
    variables.fire();
  });
  reg("s32e.toggleVariablesLive", () => {
    variables.livePaused = !variables.livePaused;
    void vscode.commands.executeCommand(
      "setContext", "s32e.varsLivePaused", variables.livePaused,
    );
    variables.fire();
  });
  reg("s32e.filterGlobals", async () => {
    const text = await vscode.window.showInputBox({
      title: "Globals filter (substring; empty shows the first page unfiltered)",
      value: variables.globalsFilter,
    });
    if (text === undefined) {
      return;
    }
    variables.globalsFilter = text.trim();
    variables.globalsOpened = true;
    if (state.connected && state.focus) {
      await variables.fetchGlobals(state.requireClient(), state.focus);
    }
  });
  reg("s32e.addWatch", async () => {
    const text = await vscode.window.showInputBox({
      title: "Add watch: a variable path",
      placeHolder: "counter · cfg.limits[2].max · *ptr · p->x",
      validateInput: (v) => validateWatchPath(v),
    });
    if (!text) {
      return;
    }
    await variables.addWatch(text.trim());
    if (state.connected && state.focus) {
      const halted = state.focusInfo?.halted === true;
      await variables.fetchWatches(
        state.requireClient(), state.focus,
        halted ? variables.selectedFrame : null,
      );
    }
  });
  reg("s32e.addToWatchFromSelection", async () => {
    const editor = vscode.window.activeTextEditor;
    const text = editor?.document.getText(editor.selection).trim();
    if (!text) {
      throw new Error("select a variable path first");
    }
    const invalid = validateWatchPath(text);
    if (invalid) {
      throw new Error(`not a watchable path: ${invalid}`);
    }
    await variables.addWatch(text);
    if (state.connected && state.focus) {
      const halted = state.focusInfo?.halted === true;
      await variables.fetchWatches(
        state.requireClient(), state.focus,
        halted ? variables.selectedFrame : null,
      );
    }
  });
  reg("s32e.removeWatch", async (node) => {
    const watchPath = (node as { watchPath?: string })?.watchPath;
    if (watchPath) {
      await variables.removeWatch(watchPath);
    }
  });
  reg("s32e.plotVariable", (node) => {
    const row = (node as { row?: VarRow })?.row;
    if (!row) {
      throw new Error("pick a variable row to plot");
    }
    const core = requireFocusCore(state);
    deps.plots.resync();
    deps.plots.addFromRow(core, row);
  });
  reg("s32e.newPlotPanel", () => {
    deps.plots.resync();
    deps.plots.newPanel();
  });
  reg("s32e.editVariable", async (node) => {
    const row = (node as { row?: VarRow })?.row;
    const section = (node as { section?: string })?.section ?? "";
    if (!row || row.handle === null) {
      return;
    }
    const focus = state.focusInfo;
    if (section === "locals" && focus?.halted !== true) {
      throw new Error("locals are editable only while the core is halted");
    }
    if (section !== "locals" && focus?.halted === false) {
      const go = await confirmDestructive(
        `Write ${row.path} while ${state.focus} is RUNNING?`,
        "The write lands mid-execution -- the application sees the new " +
          "value immediately.",
        "Write",
      );
      if (!go) {
        return;
      }
    }
    const text = await vscode.window.showInputBox({
      title: `${row.path}  (${row.type})`,
      value: row.value?.display ?? "",
      prompt: "decimal, 0x hex, 'c' for chars, true/false, or an enumerator name",
    });
    if (!text) {
      return;
    }
    try {
      const result = (await state
        .requireClient()
        .run(`var write ${row.handle} "${text.trim()}"`)) as VarWriteResult;
      if (!result.match) {
        void vscode.window.showInformationMessage(
          `S32E: wrote ${result.wrote}, reads back ${result.readback} ` +
            "(differs -- volatile or hardware-written?)",
        );
      }
      variables.applyRefresh({ generation: variables.generation, rows: [result.row] });
    } catch (err) {
      if (isStaleHandleError(message(err))) {
        void vscode.window.showWarningMessage(
          "S32E: the target ran since this row was fetched -- the view was " +
            "refreshed, try again.",
        );
        await vscode.commands.executeCommand("s32e.refreshVariables");
        return;
      }
      throw err;
    }
  });
  reg("s32e.editRegister", async (item) => {
    const name = (item as { regName?: string })?.regName ?? (item as { name?: string })?.name;
    if (!name) {
      return;
    }
    const core = requireHaltedFocus(state);
    const current = state.lastRegs.get(core)?.[name] ?? "";
    const text = await vscode.window.showInputBox({
      title: `${core} ${name}`,
      value: current,
      validateInput: (v) =>
        /^(0x[0-9a-fA-F]+|\d+)$/.test(v.trim()) ? null : "decimal or 0x hex",
    });
    if (!text || name === "dspsr") {
      if (name === "dspsr" && text) {
        throw new Error("writing DSPSR from the view is not supported");
      }
      return;
    }
    const family = familyOf(core);
    const cmd =
      family === "m33"
        ? `m33 reg ${name} ${text.trim()} --core ${core}`
        : `core reg ${name} ${text.trim()} --core ${core}`;
    await state.requireClient().run(cmd);
    await refreshFocusData(deps);
  });
  reg("s32e.openFrameSource", async (file, line) => {
    const doc = await vscode.workspace.openTextDocument(file as string);
    const editor = await vscode.window.showTextDocument(doc, { preview: true });
    const line0 = (line as number) - 1;
    editor.revealRange(
      new vscode.Range(line0, 0, line0, 0),
      vscode.TextEditorRevealType.InCenter,
    );
  });
  reg("s32e.openDisasm", async (item) => {
    const core = coreArg(state, item as CoreInfo);
    if (!core) {
      throw new Error("no core selected");
    }
    const anchor = state.lastPc.get(core);
    await disasm.open(core, anchor);
  });

  // -- memory --
  reg("s32e.readMemory", async () => {
    if (!state.connected) {
      throw new Error("not connected");
    }
    const addrText = await vscode.window.showInputBox({
      title: "Read memory: address (0x-prefixed hex)",
      validateInput: (v) => (/^0x[0-9a-fA-F]+$/.test(v.trim()) ? null : "0x-prefixed hex"),
    });
    if (!addrText) {
      return;
    }
    const lenText = await vscode.window.showInputBox({
      title: "Length in bytes",
      value: "256",
      validateInput: (v) => (/^\d+$/.test(v.trim()) ? null : "decimal bytes"),
    });
    if (!lenText) {
      return;
    }
    const ap = await vscode.window.showQuickPick(APS, {
      title: "Access port (axi sees RAM; unreachable during functional reset)",
      placeHolder: setting<string>("memory.defaultAp", "axi"),
    });
    if (!ap) {
      return;
    }
    const view = {
      ap,
      address: Number.parseInt(addrText.trim(), 16),
      length: Number.parseInt(lenText.trim(), 10),
    };
    await mem.fetch(view);
    await vscode.window.showTextDocument(memUri(view), { preview: false });
    deps.refreshTitles();
  });
  reg("s32e.refreshMemory", () => mem.refreshAll());

  // -- stack monitor --
  reg("s32e.stacks.selectProject", async () => {
    if (!state.connected) {
      throw new Error(
        "connect first — the backend searches and parses the project's ARXML",
      );
    }
    const uris = await vscode.window.showOpenDialog({
      title: "Project folder (searched for the resolved Os ECUC .arxml)",
      canSelectFiles: false,
      canSelectFolders: true,
      canSelectMany: false,
      defaultUri: deps.stacks.project
        ? vscode.Uri.file(deps.stacks.project)
        : vscode.workspace.workspaceFolders?.[0]?.uri,
    });
    const dir = uris?.[0]?.fsPath;
    if (!dir) {
      return;
    }
    const client = state.requireClient();
    let result = (await vscode.window.withProgress(
      {
        location: { viewId: "s32e.stacks" },
        title: "searching for stack ARXML…",
      },
      () => client.run(`stacks config --project "${dir}"`),
    )) as StacksConfigResult;
    if (result.candidates.length > 1) {
      const pick = await vscode.window.showQuickPick(
        result.candidates.map((c) => ({
          label: path.basename(c),
          description: c,
        })),
        { title: "Several ARXML files define stacks — which one?" },
      );
      if (!pick) {
        return;
      }
      result = (await client.run(
        `stacks config --arxml "${pick.description}"`,
      )) as StacksConfigResult;
    }
    if (!result.arxml) {
      throw new Error("no ARXML selected");
    }
    await deps.stacks.set(dir, result.arxml);
    void vscode.window.setStatusBarMessage(
      `S32E: ${result.total} stacks from ${path.basename(result.arxml)}`,
      4000,
    );
    await refreshStacks(deps);
  });
  reg("s32e.refreshStacks", () => refreshStacks(deps));

  reg("s32e.writeMemory", async () => {
    if (!state.connected) {
      throw new Error("not connected");
    }
    const addrText = await vscode.window.showInputBox({
      title: "Write memory: address (0x-prefixed hex)",
      validateInput: (v) => (/^0x[0-9a-fA-F]+$/.test(v.trim()) ? null : "0x-prefixed hex"),
    });
    if (!addrText) {
      return;
    }
    const valueText = await vscode.window.showInputBox({
      title: "Value (decimal or 0x hex)",
      validateInput: (v) =>
        /^(0x[0-9a-fA-F]+|\d+)$/.test(v.trim()) ? null : "decimal or 0x hex",
    });
    if (!valueText) {
      return;
    }
    const ap = await vscode.window.showQuickPick(APS, { title: "Access port" });
    if (!ap) {
      return;
    }
    const go = await confirmDestructive(
      `Write ${valueText} to ${addrText}?`,
      `One 32-bit write via the ${ap} AP. The read-back is reported after.`,
      "Write",
    );
    if (!go) {
      return;
    }
    const result = (await state
      .requireClient()
      .run(`mem write ${addrText.trim()} ${valueText.trim()} --ap ${ap} --yes`)) as {
      wrote: string;
      readback: string;
      match: boolean;
    };
    void vscode.window.showInformationMessage(
      `S32E: wrote ${result.wrote}, reads back ${result.readback}` +
        (result.match ? "" : " (differs -- write-1-to-clear or read-only field?)"),
    );
    await mem.refreshAll();
  });

  // -- profiling / diagnostics --
  reg("s32e.sampleCore", async (item) => {
    const core = coreArg(state, item as CoreInfo);
    if (!core) {
      throw new Error("no core selected");
    }
    const countText = await vscode.window.showInputBox({
      title: `Sample ${core}'s PC while it runs -- how many samples?`,
      value: "200",
      validateInput: (v) => (/^\d+$/.test(v.trim()) ? null : "decimal"),
    });
    if (!countText) {
      return;
    }
    const family = familyOf(core);
    const cmd =
      family === "m33"
        ? `m33 sample --core ${core} --count ${countText.trim()}`
        : `core sample --core ${core} --count ${countText.trim()}`;
    const result = (await state.requireClient().run(cmd)) as {
      samples: number;
      distinct: number;
      top: { address: string; hits: number; percent: number }[];
      note?: string;
    };
    let names = new Map<string, string>();
    if (state.symbolsLoaded && result.top.length > 0) {
      try {
        const lookup = (await state
          .requireClient()
          .run(`sym addr2line ${result.top.map((t) => t.address).join(" ")}`)) as {
          lookups: { addr: string; func: string | null; file: string | null; line: number | null }[];
        };
        names = new Map(
          lookup.lookups.map((l) => [
            l.addr.toLowerCase(),
            `${l.func ?? "?"}${l.file ? `  ${path.basename(l.file)}:${l.line}` : ""}`,
          ]),
        );
      } catch {
        /* names stay empty */
      }
    }
    output.section(`PC samples: ${core} (${result.samples} samples, ${result.distinct} distinct)`);
    for (const row of result.top) {
      const name = names.get(row.address.toLowerCase()) ?? "";
      output.line(
        `  ${row.address}  ${String(row.hits).padStart(5)}  ${row.percent.toFixed(1).padStart(5)}%  ${name}`,
      );
    }
    if (result.note) {
      output.line(`  note: ${result.note}`);
    }
    output.show();
  });

  const diag = (name: string, args: string[]) =>
    reg(`s32e.${name}`, async () => {
      if (state.connected) {
        throw new Error(
          `${name} opens its own probe connection -- disconnect the session first`,
        );
      }
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `S32E: ${name}...` },
        async () => {
          const r = await oneshot(backendConfig(), output, args, {
            timeoutMs: 300000,
          });
          output.section(`${name} result (exit ${r.code})`);
          if (r.json !== null) {
            output.line(JSON.stringify(r.json, null, 2));
          }
          output.show();
          if (name === "doctor" && r.json) {
            const findings =
              (r.json as { findings?: { severity: string; title: string }[] })
                .findings ?? [];
            const blockers = findings.filter((f) => f.severity === "blocker");
            void vscode.window.showInformationMessage(
              blockers.length > 0
                ? `Doctor: ${blockers.length} blocker(s) -- ${blockers
                    .map((b) => b.title)
                    .join("; ")}`
                : `Doctor: no blockers (${findings.length} finding(s), see log).`,
            );
          }
        },
      );
    });
  diag("doctor", ["doctor"]);
  diag("probe", ["probe"]);
  diag("report", ["report"]);

  // -- boundary scan (disconnect-only, like the diagnostics) --
  reg("s32e.refreshBscan", () => refreshBscan(deps));
  // Context-menu drive actions: the clicked tree node arrives as the first
  // command argument (the row element itself). Immediate model: every click
  // updates the set and re-latches the whole set at once.
  const drivePin = (value: "0" | "1" | "Z") => async (node: unknown) => {
    const row = node as { name?: string } | undefined;
    if (!row?.name) {
      throw new Error("right-click a pin row in the Boundary Scan view");
    }
    deps.bscan.setDrive(row.name, value);
    await applyDrives(deps);
  };
  reg("s32e.bscan.driveHigh", drivePin("1"));
  reg("s32e.bscan.driveLow", drivePin("0"));
  reg("s32e.bscan.driveZ", drivePin("Z"));
  reg("s32e.bscan.stopDriving", async (node: unknown) => {
    const row = node as { name?: string } | undefined;
    if (!row?.name) {
      throw new Error("right-click a driven pin row in the Boundary Scan view");
    }
    deps.bscan.clearDrive(row.name);
    await applyDrives(deps); // empty set falls through to release
  });
  reg("s32e.bscan.release", () => releaseDrives(deps));
  reg("s32e.bscan.selectBsdl", async () => {
    const uris = await vscode.window.showOpenDialog({
      title: "BSDL file (cancel keeps the current choice)",
      filters: { BSDL: ["bsdl"] },
      canSelectMany: false,
      defaultUri: deps.bscan.bsdlPath
        ? vscode.Uri.file(path.dirname(deps.bscan.bsdlPath))
        : undefined,
    });
    const p = uris?.[0]?.fsPath;
    if (!p) {
      return;
    }
    await vscode.workspace
      .getConfiguration("s32e")
      .update("bscan.bsdlPath", p, vscode.ConfigurationTarget.Workspace);
    deps.bscan.reset();
    await refreshBscan(deps);
  });
}

// ---------------------------------------------------------------------------
// shared flows
// ---------------------------------------------------------------------------

async function focusCore(deps: Deps, core: string): Promise<void> {
  const { state } = deps;
  await state.requireClient().run(`use ${core}`);
  state.focus = core;
  state.fireChange();
  deps.statusBar.refresh();
  deps.refreshTitles();
  if (state.cores.get(core)?.halted === true) {
    await refreshFocusData(deps);
  } else {
    state.lastStack = [];
    deps.stackView.refresh();
  }
}

async function haltCore(deps: Deps, core: string | null): Promise<void> {
  if (!core) {
    throw new Error("no core selected");
  }
  const { state } = deps;
  const family = familyOf(core);
  const result = (await state
    .requireClient()
    .run(family === "m33" ? `m33 halt --core ${core}` : `core halt --core ${core}`)) as
    | { halt?: { core: string }[] }
    | { pc?: string };
  const pc = "pc" in result ? (result.pc ?? null) : null;
  await afterHalt(deps, core, pc);
}

async function resumeCore(deps: Deps, core: string | null): Promise<void> {
  if (!core) {
    throw new Error("no core selected");
  }
  const { state } = deps;
  const family = familyOf(core);
  const result = (await state
    .requireClient()
    .run(
      family === "m33" ? `m33 resume --core ${core}` : `core resume --core ${core}`,
    )) as { resume?: { core?: string; rehalted?: boolean }[] };
  const rehalted = result.resume?.[0]?.rehalted === true;
  if (rehalted) {
    void vscode.window.showInformationMessage(
      `S32E: ${core} ran and immediately re-halted -- that is a working ` +
        "breakpoint at the current PC, not a failed resume. Step past it or " +
        "remove the breakpoint first.",
    );
    await afterHalt(deps, core);
    return;
  }
  state.markHalted(core, false, "running");
  deps.variables.invalidate();
  if (core === state.focus) {
    deps.pcDecor.clear();
  }
}

async function stepLine(deps: Deps, over: boolean): Promise<void> {
  const { state } = deps;
  const core = requireHaltedFocus(state);
  if (!state.symbolsLoaded) {
    throw new Error("no symbols loaded -- choose an ELF first (or use instruction stepping)");
  }
  const result = (await state
    .requireClient()
    .run(`stepline --core ${core}${over ? " --over" : ""}`)) as {
    pc_after: string;
    stopped: string;
    steps: number;
    loop_at?: string | null;
  };
  if (result.stopped === "no-line-info") {
    void vscode.window.setStatusBarMessage(
      "S32E: no line info at the PC -- took one instruction step",
      4000,
    );
  } else if (result.stopped === "loop") {
    // Worth a real notification rather than a status-bar flash: the step did
    // NOT advance a line and no amount of further stepping will, so the user
    // has to pick a different verb.
    void vscode.window.showWarningMessage(
      `S32E: this source line loops back to ${result.loop_at ?? "itself"} — a source step ` +
        "can never leave it. Use Continue, Step Out, or run to a line past the loop.",
    );
  } else if (result.stopped === "max-steps") {
    void vscode.window.setStatusBarMessage(
      `S32E: still on the same line after ${result.steps} steps`,
      4000,
    );
  } else if (result.stopped === "breakpoint") {
    void vscode.window.setStatusBarMessage(
      "S32E: a breakpoint inside the stepped line fired — stopped there",
      4000,
    );
  } else if (result.stopped === "timeout") {
    void vscode.window.showWarningMessage(
      "S32E: the line step stalled mid-run; the core was halted where it " +
        "stood. The line may wait on an interrupt or a peripheral.",
    );
  }
  await afterHalt(deps, core, result.pc_after);
}

async function stepInstr(deps: Deps, over: boolean): Promise<void> {
  const { state } = deps;
  const core = requireHaltedFocus(state);
  const family = familyOf(core);
  const cmd =
    family === "m33"
      ? `m33 step --core ${core}${over ? " --over" : ""}`
      : `core step --core ${core} ${over ? "--over" : "--into"}`;
  const result = (await state.requireClient().run(cmd)) as { pc?: string };
  await afterHalt(deps, core, result.pc ?? null);
}

async function loadElf(deps: Deps): Promise<void> {
  const { state, elf, bpStore, wpStore } = deps;
  if (!state.connected) {
    throw new Error("not connected");
  }
  if (!elf.active) {
    const picked = await elf.pick();
    if (!picked) {
      return;
    }
  }
  const go = await confirmDestructive(
    `Load ${elf.basename} to RAM?`,
    "This resets the WHOLE SoC: every core reboots and everything running " +
      "stops. The image is written into RTU0 RAM (~35 s at 16 MHz) and " +
      "rtu0.0 is left halted at the entry. Breakpoints are re-armed after.",
    "Load & Reset SoC",
  );
  if (!go) {
    return;
  }
  state.busyOp = true;
  state.fireChange();
  try {
    const report = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: `S32E: loading ${elf.basename} to RAM`,
      },
      (progress) =>
        state.requireClient().run(`load "${elf.active}"`, (msg) =>
          progress.report({ message: msg }),
        ) as Promise<{ pc: string; kb_per_s: number; seconds: number; total_bytes: number }>,
    );
    state.focus = "rtu0.0";
    try {
      await state.requireClient().run("use rtu0.0");
    } catch {
      /* focus is cosmetic here */
    }
    if (!state.symbolsLoaded) {
      await symLoad(deps);
    }
    await bpStore.reapplyAll();
    await wpStore.reapplyAll();
    await deps.poller.tick();
    void vscode.window.showInformationMessage(
      `S32E: loaded ${(report.total_bytes / 1024).toFixed(0)} KiB in ` +
        `${report.seconds.toFixed(1)} s (${report.kb_per_s.toFixed(0)} KB/s). ` +
        "rtu0.0 is parked at the entry -- set breakpoints, then Continue.",
    );
    await afterHalt(deps, "rtu0.0", report.pc);
  } finally {
    state.busyOp = false;
    state.fireChange();
  }
}

async function reresolveLineBreakpoints(deps: Deps): Promise<void> {
  for (const bp of deps.bpStore.all) {
    if (bp.source) {
      await deps.bpStore.disarm(bp);
      bp.address = null;
      await deps.bpStore.arm(bp);
    }
  }
}

async function pickCore(
  state: DebugState,
  title: string,
  except?: string,
): Promise<string | null> {
  const known = state.connected
    ? [...state.cores.keys()]
    : ["rtu0.0", "rtu0.1", "rtu1.0", "rtu1.1", ...M33_NAMES];
  const items = known.filter((c) => c !== except);
  const pick = await vscode.window.showQuickPick(items, { title });
  return pick ?? null;
}

export { afterHalt, connect, disconnect, symLoad };

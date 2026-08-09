import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { SessionClient } from "./backend/client";
import { oneshot } from "./backend/oneshot";
import {
  hexToInt,
  PollResult,
  StackFrame,
  toHex,
} from "./backend/types";
import { backendConfig, setting } from "./config";
import { DISASM_SCHEME, DisasmProvider, disasmUri } from "./editor/disasmProvider";
import { MemProvider, memUri } from "./editor/memProvider";
import { PcDecorations } from "./editor/sourceDecorations";
import { Poller } from "./poll/poller";
import { BreakpointStore, S32Breakpoint } from "./state/breakpoints";
import { ElfState } from "./state/elf";
import { CoreInfo, DebugState, familyOf, M33_NAMES } from "./state/session";
import { S32Watchpoint, WatchpointStore } from "./state/watchpoints";
import { confirmDestructive } from "./ui/confirm";
import { Output } from "./ui/output";
import { StatusBar } from "./ui/statusBar";
import { coreArg } from "./views/coresView";
import { StackView } from "./views/stackView";

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
    client.onExit(({ expected }) => {
      if (expected) {
        return;
      }
      poller.stop();
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
// focused-core data refresh + on-halt behavior
// ---------------------------------------------------------------------------

async function refreshFocusData(deps: Deps): Promise<void> {
  const { state } = deps;
  const core = state.focus;
  if (!core || !state.connected || state.cores.get(core)?.halted !== true) {
    return;
  }
  const client = state.requireClient();
  const family = familyOf(core);
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
  deps.stackView.refresh();
  state.fireChange();
}

/** Standard debugger feel after any halt: refresh + reveal source or disasm. */
async function afterHalt(deps: Deps, core: string, pcHint?: string | null): Promise<void> {
  const { state, disasm, pcDecor } = deps;
  state.markHalted(core, true);
  if (core !== state.focus) {
    return;
  }
  await refreshFocusData(deps);
  const pc = pcHint !== undefined ? state.notePc(core, pcHint) : (state.lastPc.get(core) ?? null);
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
    // Keep an already-open disasm tab in step with the source view.
    try {
      await disasm.render(core, pc);
      disasm.revealAddr(core, pc);
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
  reg("s32e.refreshCores", () => deps.poller.tick());
  reg("s32e.unlockCore", async (item) => {
    const core = coreArg(state, item as CoreInfo);
    await state.requireClient().run(`core unlock --core ${core}`);
    await deps.poller.tick();
  });

  // -- stepping --
  reg("s32e.stepLineIn", () => stepLine(deps, false));
  reg("s32e.stepLineOver", () => stepLine(deps, true));
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
      void vscode.window.showWarningMessage(
        `S32E: ${core} did not return within the timeout -- it is RUNNING ` +
          "(the temporary breakpoint was removed). Halt it manually when ready.",
      );
    }
  });
  reg("s32e.stepInstrIn", () => stepInstr(deps, false));
  reg("s32e.stepInstrOver", () => stepInstr(deps, true));
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
    const wp = node as S32Watchpoint;
    if (wp?.id) {
      return wpStore.remove(wp);
    }
  });
  reg("s32e.clearAllWatchpoints", () => wpStore.clearAll());

  // -- views / data --
  reg("s32e.refreshStack", () => refreshFocusData(deps));
  reg("s32e.refreshRegisters", () => refreshFocusData(deps));
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

export { afterHalt, connect, disconnect, refreshFocusData, symLoad };

import * as vscode from "vscode";
import { toHex } from "./backend/types";
import { afterHalt, Deps, disconnect, registerCommands } from "./commands";
import { DISASM_SCHEME, DisasmProvider } from "./editor/disasmProvider";
import { MEM_SCHEME, MemProvider } from "./editor/memProvider";
import { PcDecorations } from "./editor/sourceDecorations";
import { Poller } from "./poll/poller";
import { BreakpointStore } from "./state/breakpoints";
import { ElfState } from "./state/elf";
import { DebugState } from "./state/session";
import { WatchpointStore } from "./state/watchpoints";
import { Output } from "./ui/output";
import { StatusBar } from "./ui/statusBar";
import { BreakpointsView } from "./views/breakpointsView";
import { CoresView } from "./views/coresView";
import { MemoryView } from "./views/memoryView";
import { RegistersView } from "./views/registersView";
import { SessionView } from "./views/sessionView";
import { StackView } from "./views/stackView";
import { WatchpointsView } from "./views/watchpointsView";

let depsRef: Deps | null = null;

export function activate(context: vscode.ExtensionContext): void {
  const output = new Output();
  const state = new DebugState();
  const elf = new ElfState(context.workspaceState);
  const bpStore = new BreakpointStore(state, output);
  const wpStore = new WatchpointStore(state, output);
  const disasm = new DisasmProvider(state);
  const mem = new MemProvider(state);
  const pcDecor = new PcDecorations();
  const poller = new Poller(state, output);
  const statusBar = new StatusBar(state, elf);
  bpStore.disasmLineToAddr = (uri, line) => disasm.lineToAddr(uri, line);

  // Views. Stack + registers get real TreeViews so their titles can carry the
  // focused core's name (the always-visible focused-core requirement).
  const coresView = new CoresView(state);
  const stackView = new StackView(state);
  const registersView = new RegistersView(state);
  const stackTree = vscode.window.createTreeView("s32e.callStack", {
    treeDataProvider: stackView,
  });
  const regsTree = vscode.window.createTreeView("s32e.registers", {
    treeDataProvider: registersView,
  });
  const refreshTitles = () => {
    const suffix = state.focus ? ` — ${state.focus}` : "";
    stackTree.title = `Call Stack${suffix}`;
    regsTree.title = `Registers${suffix}`;
  };
  refreshTitles();

  context.subscriptions.push(
    output, state, elf, bpStore, wpStore, disasm, mem, pcDecor, poller,
    statusBar, stackTree, regsTree,
    vscode.window.registerTreeDataProvider("s32e.cores", coresView),
    vscode.window.registerTreeDataProvider("s32e.session", new SessionView(state, elf)),
    vscode.window.registerTreeDataProvider("s32e.breakpoints", new BreakpointsView(bpStore)),
    vscode.window.registerTreeDataProvider("s32e.watchpoints", new WatchpointsView(wpStore)),
    vscode.window.registerTreeDataProvider("s32e.memory", new MemoryView(mem)),
    vscode.workspace.registerTextDocumentContentProvider(DISASM_SCHEME, disasm),
    vscode.workspace.registerTextDocumentContentProvider(MEM_SCHEME, mem),
  );

  const deps: Deps = {
    state, elf, output, poller, bpStore, wpStore, disasm, mem, pcDecor,
    statusBar, stackView, refreshTitles,
  };
  depsRef = deps;
  registerCommands(context, deps);

  // Raw SP window for the stack view, fetched lazily on expand.
  stackView.fetchRaw = async () => {
    const sp = state.lastStack[0]?.sp;
    const core = state.focus;
    if (!sp || !core) {
      return [];
    }
    const spInt = Number.parseInt(sp, 16);
    const family = state.focusFamily;
    const ap = family === "m33" ? `ahb-${core}` : "axi";
    const result = (await state.requireClient().run(
      `mem dump ${toHex(spInt & ~3)} 64 --ap ${ap}`,
    )) as { words: string[] };
    return result.words.map(
      (w, i) => `${toHex((spInt & ~3) + i * 4)}:  ${w}`,
    );
  };

  // On-halt reactions: fresh halts seen by the poller (breakpoint hits,
  // watchpoint hits, fired catches).
  context.subscriptions.push(
    state.onCoreHalted((info) => {
      void (async () => {
        try {
          if (info.catchArmed && info.family === "r52") {
            // A fired catch is an EDBGREQ hold, not Debug state -- convert it
            // exactly as `catch --wait` would, then treat it as a halt.
            state.busyOp = true;
            try {
              await state
                .requireClient()
                .run(`catch --core ${info.name} --wait --timeout 5`);
            } finally {
              state.busyOp = false;
            }
            void vscode.window.showInformationMessage(
              `S32E: ${info.name} caught at its first instruction.`,
            );
          } else if (info.name !== state.focus) {
            void vscode.window.setStatusBarMessage(
              `S32E: ${info.name} halted (${info.detail})`,
              5000,
            );
          }
          await afterHalt(deps, info.name);
        } catch (err) {
          output.line(`!! on-halt reaction: ${err}`);
        }
      })();
    }),
  );

  // Rebuild detection on the active ELF.
  context.subscriptions.push(
    elf.onRebuilt((elfPath) => {
      void vscode.window
        .showInformationMessage(
          `S32E: ${elfPath.split(/[\\/]/).pop()} changed on disk.`,
          "Re-symbolicate",
          "Load to RAM",
          "Ignore",
        )
        .then((pick) => {
          if (pick === "Re-symbolicate") {
            void vscode.commands.executeCommand("s32e.resymbolicate");
          } else if (pick === "Load to RAM") {
            void vscode.commands.executeCommand("s32e.loadElf");
          }
        });
    }),
  );

  // Config changes that matter mid-session.
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((ev) => {
      if (ev.affectsConfiguration("s32e.pollIntervalMs") && state.connected) {
        poller.start();
      }
    }),
  );

  state.onDidChange(() => statusBar.refresh());
  state.updateContexts();
  elf.updateContext();
}

export async function deactivate(): Promise<void> {
  // Best effort: quit the session so the restore ledger runs (clear
  // comparators, disarm sync, drop catch latches, resume cores).
  if (depsRef) {
    await disconnect(depsRef);
  }
}

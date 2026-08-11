import * as vscode from "vscode";
import { toHex } from "./backend/types";
import {
  afterHalt,
  Deps,
  disconnect,
  refreshFocusData,
  registerCommands,
} from "./commands";
import { DISASM_SCHEME, DisasmProvider } from "./editor/disasmProvider";
import { MEM_SCHEME, MemProvider } from "./editor/memProvider";
import { PcDecorations } from "./editor/sourceDecorations";
import { PlotPanels } from "./plot/plotPanel";
import { Poller } from "./poll/poller";
import { BreakpointStore } from "./state/breakpoints";
import { ComparatorReconciler } from "./state/comparators";
import { ElfState } from "./state/elf";
import { DebugState } from "./state/session";
import { VariablesLive, VariablesState } from "./state/variables";
import {
  isAutoRefreshNode,
  PolledView,
  ViewPolling,
} from "./state/viewPolling";
import { WatchpointStore } from "./state/watchpoints";
import { Output } from "./ui/output";
import { StatusBar } from "./ui/statusBar";
import { BreakpointsView } from "./views/breakpointsView";
import { CoresView } from "./views/coresView";
import { MemoryView } from "./views/memoryView";
import { RegistersView } from "./views/registersView";
import { SessionView } from "./views/sessionView";
import { StackView } from "./views/stackView";
import { VariablesView } from "./views/variablesView";
import { WatchpointsView } from "./views/watchpointsView";

let depsRef: Deps | null = null;

export function activate(context: vscode.ExtensionContext): void {
  const output = new Output();
  const state = new DebugState();
  const elf = new ElfState(context.workspaceState);
  const viewPolling = new ViewPolling(context.workspaceState);
  const bpStore = new BreakpointStore(state, output);
  const wpStore = new WatchpointStore(state, output);
  const disasm = new DisasmProvider(state);
  const mem = new MemProvider(state);
  const pcDecor = new PcDecorations(context.extensionUri);
  const poller = new Poller(state, output, viewPolling);
  const comparators = new ComparatorReconciler(
    state, bpStore, wpStore, viewPolling, output,
  );
  const plots = new PlotPanels(state, output);
  const statusBar = new StatusBar(state, elf);
  bpStore.disasmLineToAddr = (uri, line) => disasm.lineToAddr(uri, line);

  // Views. Stack, registers and variables get real TreeViews so their titles
  // can carry the focused core's name (the always-visible focused-core
  // requirement) and so visibility can gate the live-refresh loop; cores and
  // memory get them for their Auto-refresh checkbox events.
  const variables = new VariablesState(context.workspaceState);
  const coresView = new CoresView(state, viewPolling);
  const stackView = new StackView(state, variables, viewPolling);
  const registersView = new RegistersView(state, viewPolling);
  const variablesView = new VariablesView(state, variables, viewPolling);
  const memoryView = new MemoryView(mem, state, viewPolling);
  const stackTree = vscode.window.createTreeView("s32e.callStack", {
    treeDataProvider: stackView,
  });
  const regsTree = vscode.window.createTreeView("s32e.registers", {
    treeDataProvider: registersView,
  });
  const varsTree = vscode.window.createTreeView("s32e.variables", {
    treeDataProvider: variablesView,
  });
  const coresTree = vscode.window.createTreeView("s32e.cores", {
    treeDataProvider: coresView,
  });
  const memTree = vscode.window.createTreeView("s32e.memory", {
    treeDataProvider: memoryView,
  });
  const bpTree = vscode.window.createTreeView("s32e.breakpoints", {
    treeDataProvider: new BreakpointsView(bpStore, viewPolling, comparators),
  });
  const wpTree = vscode.window.createTreeView("s32e.watchpoints", {
    treeDataProvider: new WatchpointsView(wpStore, viewPolling, comparators),
  });
  const varLive = new VariablesLive(
    state, variables, output,
    () => varsTree.visible,
    () => viewPolling.on("variables"),
  );
  const refreshTitles = () => {
    const suffix = state.focus ? ` — ${state.focus}` : "";
    stackTree.title = `Call Stack${suffix}`;
    regsTree.title = `Registers${suffix}`;
    varsTree.title = `Variables${suffix}`;
  };
  refreshTitles();

  // The Auto-refresh checkboxes: each polled view's first row. The checkbox
  // event hands back the provider's node; the paused hint lives in the view
  // description so an unchecked tab says so even when scrolled.
  const polledTrees: [PolledView, vscode.TreeView<unknown>][] = [
    ["cores", coresTree],
    ["variables", varsTree],
    ["callStack", stackTree],
    ["registers", regsTree],
    ["memory", memTree],
    ["breakpoints", bpTree],
    ["watchpoints", wpTree],
  ];
  const applyPauseHints = () => {
    for (const [key, tree] of polledTrees) {
      tree.description = viewPolling.on(key) ? undefined : "paused";
    }
  };
  applyPauseHints();
  for (const [key, tree] of polledTrees) {
    context.subscriptions.push(
      tree.onDidChangeCheckboxState((ev) => {
        for (const [node, box] of ev.items) {
          if (isAutoRefreshNode(node)) {
            void viewPolling.set(
              key, box === vscode.TreeItemCheckboxState.Checked,
            );
          }
        }
      }),
    );
  }

  context.subscriptions.push(
    output, state, elf, bpStore, wpStore, disasm, mem, pcDecor, poller,
    comparators, plots, statusBar, stackTree, regsTree, varsTree, coresTree,
    memTree, bpTree, wpTree, variables, varLive, viewPolling,
    vscode.window.registerTreeDataProvider("s32e.session", new SessionView(state, elf)),
    vscode.workspace.registerTextDocumentContentProvider(DISASM_SCHEME, disasm),
    vscode.workspace.registerTextDocumentContentProvider(MEM_SCHEME, mem),
  );

  const deps: Deps = {
    state, elf, output, poller, bpStore, wpStore, disasm, mem, pcDecor,
    statusBar, stackView, variables, varLive, viewPolling, comparators,
    plots,
    variablesTreeVisible: () => varsTree.visible,
    refreshTitles,
  };
  depsRef = deps;
  registerCommands(context, deps);

  // Re-checking a box while stopped fills the tab immediately; while running
  // the next tick/stop picks it up on its own.
  context.subscriptions.push(
    viewPolling.onDidChange((view) => {
      applyPauseHints();
      if (!state.connected || !viewPolling.on(view)) {
        return;
      }
      if (view === "cores") {
        void poller.tick(true);
      } else if (view === "memory") {
        void mem.refreshAll();
      } else if (view === "breakpoints" || view === "watchpoints") {
        // Revive the interval (a 10-failure stop may have killed it) and
        // verify immediately.
        comparators.start();
        void comparators.tick();
      } else if (state.focusInfo?.halted === true) {
        void refreshFocusData(deps, new Set([view]));
      }
    }),
  );

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
          const looksLikeCatchFire =
            info.catchArmed &&
            info.family === "r52" &&
            info.detail.includes("external debug request");
          if (looksLikeCatchFire) {
            // A fired catch is an EDBGREQ hold, not Debug state -- convert it
            // exactly as `catch --wait` would, then treat it as a halt. Only
            // when the status actually says debug-request: a breakpoint hit
            // on a catch-armed core is an ordinary stop and must not pay the
            // conversion (or its up-to-5 s wait).
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
      if (
        ev.affectsConfiguration("s32e.variables.liveRefreshMs") &&
        state.connected
      ) {
        varLive.start();
      }
      if (
        ev.affectsConfiguration("s32e.comparators.verifyMs") &&
        state.connected
      ) {
        comparators.start();
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

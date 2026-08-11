# S32E Debug

Debug NXP S32E27x boards over a SEGGER J-Link from its own Activity Bar container — no Run-and-Debug tab involved. Backed by the `s32edbg` CLI in the parent repository.

Features: persistent probe session, per-core run control for 4× Cortex-R52 + 3× Cortex-M33, source- and instruction-level stepping, gutter breakpoints on a focused core, watchpoints, call stack + raw stack, a Variables view (frame-linked locals, filterable globals, persisted watch paths, scalar editing, live refresh while running), disassembly view, RAM ELF loading, catch-at-first-instruction, memory viewer, PC profiling, and pre-connect board diagnostics.

Interactive latency (2026-08-11): source-line steps use comparator range-stepping (straight-line stretches run at silicon speed; only real control flow is single-stepped), breakpoint hits are **pushed** by the backend within ~50 ms instead of being discovered by the status poll, and each stop's registers/stack/variables refresh shares one hardware read through per-stop caches. The S32E output channel prints every request's backend cost as `<ms>ms/<ops>op` — one `op` is one USB round trip — so a slow operation names its own cause.

Halted rows in the Cores view carry inline step buttons (step-into and step-over by source line, step by instruction) beside continue — on a non-focused core they focus it first, then step. Per-tab refresh control: Cores, Variables, Call Stack, Registers, Memory, Breakpoints and Watchpoints each start with an **Auto-refresh checkbox** row.

The Breakpoints/Watchpoints checkboxes gate a **hardware-comparator verification** sweep (`s32e.comparators.verifyMs`, default 5 s, plus one pass per stop): each row gains `✓ hw` when its comparator really is armed at its address, a yellow `⚠ hw:` marker when the hardware disagrees (e.g. a target-side reset wiped the slot), and enabled comparators nobody claims appear as orphan rows with a right-click **Clear Hardware Comparator** action. An unreadable comparator bank (OS lock / reset hold) reads as `hw: unreadable` and never demotes your list.

**Plot Variable** (the chart icon on any Variables row with a memory address, or right-click): an oscilloscope-style panel graphs the value over time — a free-running counter draws a saw-tooth — with a configurable sampling rate (`s32e.plot.sampleHz`, default 10 Hz, 1–50, editable live in the panel), a 5–60 s rolling window, crosshair readout, pause/clear, and CSV export. The panel shows the *achieved* rate; samples ride the shared session queue, so stepping stays responsive and the plot survives run/stop cycles by re-resolving its handle. Checked, the tab refreshes on its usual trigger (the status poll for Cores, the on-stop fetch for the rest; Memory — off by default — re-reads its open regions at every stop). Unchecked, the tab causes no probe traffic: it keeps its last values with a `paused` hint, and its refresh button still does a one-shot. The choice persists per workspace. A yellow **gutter arrow** marks the current PC line in the source editor and the disassembly view — both at once when both are open — moving with every step and clearing on resume.

## Requirements

- Windows, 64-bit Python ≥ 3.11 with the backend installed: `pip install -e <repo-root>[ide]`
- A SEGGER J-Link install (auto-discovered, or set `s32e.jlinkDir`)

## Getting started

1. Set `s32e.pythonPath` if `python` on PATH is not the interpreter with s32edbg.
2. Open the S32E Debug container in the Activity Bar → **Connect**.
3. Pick your ELF (status bar) → set breakpoints → debug.

Full documentation: see the repository README ("IDE integration" section).

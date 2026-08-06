# S32E Debug

Debug NXP S32E27x boards over a SEGGER J-Link from its own Activity Bar container — no Run-and-Debug tab involved. Backed by the `s32edbg` CLI in the parent repository.

Features (v1): persistent probe session, per-core run control for 4× Cortex-R52 + 3× Cortex-M33, source- and instruction-level stepping, gutter breakpoints on a focused core, watchpoints, call stack + raw stack, disassembly view, RAM ELF loading, catch-at-first-instruction, memory viewer, PC profiling, and pre-connect board diagnostics.

## Requirements

- Windows, 64-bit Python ≥ 3.11 with the backend installed: `pip install -e <repo-root>[ide]`
- A SEGGER J-Link install (auto-discovered, or set `s32e.jlinkDir`)

## Getting started

1. Set `s32e.pythonPath` if `python` on PATH is not the interpreter with s32edbg.
2. Open the S32E Debug container in the Activity Bar → **Connect**.
3. Pick your ELF (status bar) → set breakpoints → debug.

Full documentation: see the repository README ("IDE integration" section).

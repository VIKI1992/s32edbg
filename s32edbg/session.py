"""A long-lived debug session: one probe connection, many commands.

Why this exists: every ordinary ``s32edbg`` invocation opens and closes its own
J-Link session, and on this board each connect pulses reset through R439 into
the SBC/PMIC -- defect **D-2** in the test plan. The same document's operating
advice is "keep halts short -- halt, read, resume in one session rather than
across invocations", and three separate run-control bugs were traced to state
straddling invocations. A ``DebugSession`` is the object the ``debug`` REPL
holds: one connection, cached discovery, and a record of everything the session
changed on the target so closing it can put the target back.

This module deliberately knows nothing about click or the console: it tracks
and restores, and returns human-readable notes for the CLI to print.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .dap import m33 as m33_mod
from .dap import runcontrol
from .errors import S32EError


@dataclass
class DebugSession:
    """One held connection, plus the bookkeeping to undo the session's changes.

    The restore-on-close contract: comparators the session set are cleared
    first, then cores the session stopped are resumed -- in that order, so a
    resumed core does not immediately re-halt on a session breakpoint. Only
    what *this* session did is undone; pre-existing state the operator asked to
    keep (``--keep-breakpoints``) is not touched.
    """

    transport: Any
    dap: Any
    keep_breakpoints: bool = False

    #: Set once the ADIv6 power-up and AP-read calibration have run.
    prepared: bool = False
    #: Cached discovery. Identity is stable for the life of a session; state
    #: (halted, locked) is not, and is re-read on every use of the cache.
    cores: list[runcontrol.Core] | None = None
    m33_cores: list[m33_mod.M33Core] | None = None

    #: Default --core for R52 and M33 commands issued without one.
    focus: str | None = None
    m33_focus: str | None = None

    # -- what the session changed on the target ------------------------------
    halted_r52: set[str] = field(default_factory=set)
    halted_m33: set[str] = field(default_factory=set)
    bp_set: set[tuple[str, int]] = field(default_factory=set)
    wp_set: set[tuple[str, int]] = field(default_factory=set)
    m33_bp_set: set[tuple[str, int]] = field(default_factory=set)
    m33_wp_set: set[tuple[str, int]] = field(default_factory=set)
    #: Cores whose comparators were sanitised already (guide §5.7) -- once per
    #: core per session. M33 cores are prefixed "m33:".
    sanitized: set[str] = field(default_factory=set)

    # -- recording helpers, called from the CLI command bodies ---------------

    def note_halted(self, name: str) -> None:
        self.halted_r52.add(name)

    def note_resumed(self, name: str) -> None:
        self.halted_r52.discard(name)

    def note_m33_halted(self, name: str) -> None:
        self.halted_m33.add(name)

    def note_m33_resumed(self, name: str) -> None:
        self.halted_m33.discard(name)

    def note_bp(self, core: str, index: int) -> None:
        self.bp_set.add((core, index))

    def note_bp_cleared(self, core: str, index: int | None) -> None:
        if index is None:  # --all
            self.bp_set = {(c, n) for c, n in self.bp_set if c != core}
        else:
            self.bp_set.discard((core, index))

    def note_wp(self, core: str, index: int) -> None:
        self.wp_set.add((core, index))

    def note_wp_cleared(self, core: str, index: int | None) -> None:
        if index is None:
            self.wp_set = {(c, n) for c, n in self.wp_set if c != core}
        else:
            self.wp_set.discard((core, index))

    def note_m33_bp(self, core: str, index: int) -> None:
        self.m33_bp_set.add((core, index))

    def note_m33_bp_cleared(self, core: str, index: int) -> None:
        self.m33_bp_set.discard((core, index))

    def note_m33_wp(self, core: str, index: int) -> None:
        self.m33_wp_set.add((core, index))

    def note_m33_wp_cleared(self, core: str, index: int) -> None:
        self.m33_wp_set.discard((core, index))

    # -- restore-on-close ----------------------------------------------------

    def restore(self) -> list[str]:
        """Undo the session's changes. Never raises: every step is attempted,
        and a step that fails becomes a note rather than aborting the rest --
        a close that gives up halfway leaves the target in the worst state of
        all, half-restored with no report of which half.
        """
        notes: list[str] = []
        core_by_name = {c.name: c for c in (self.cores or [])}
        m33_by_name = {c.name: c for c in (self.m33_cores or [])}

        # Comparators first: a core resumed below must not trip over a
        # breakpoint this session planted.
        for name, index in sorted(self.bp_set):
            core = core_by_name.get(name)
            if core is None:
                continue
            try:
                runcontrol.clear_breakpoint(self.dap, core, index)
                notes.append(f"cleared breakpoint {index} on {name}")
            except S32EError as exc:
                notes.append(f"could not clear breakpoint {index} on {name}: {exc}")
        for name, index in sorted(self.wp_set):
            core = core_by_name.get(name)
            if core is None:
                continue
            try:
                runcontrol.clear_watchpoint(self.dap, core, index)
                notes.append(f"cleared watchpoint {index} on {name}")
            except S32EError as exc:
                notes.append(f"could not clear watchpoint {index} on {name}: {exc}")
        for name, index in sorted(self.m33_bp_set):
            m33core = m33_by_name.get(name)
            if m33core is None:
                continue
            try:
                m33_mod.clear_breakpoint(self.dap, m33core, index)
                notes.append(f"cleared breakpoint {index} on {name}")
            except S32EError as exc:
                notes.append(f"could not clear breakpoint {index} on {name}: {exc}")
        for name, index in sorted(self.m33_wp_set):
            m33core = m33_by_name.get(name)
            if m33core is None:
                continue
            try:
                m33_mod.clear_watchpoint(self.dap, m33core, index)
                notes.append(f"cleared watchpoint {index} on {name}")
            except S32EError as exc:
                notes.append(f"could not clear watchpoint {index} on {name}: {exc}")

        for name in sorted(self.halted_r52):
            core = core_by_name.get(name)
            if core is None:
                notes.append(f"could not resume {name}: it was never discovered")
                continue
            try:
                if runcontrol.read_state(self.dap, core.debug_base).halted is True:
                    runcontrol.resume(self.dap, core)
                    notes.append(f"resumed {name}")
            except S32EError as exc:
                notes.append(f"could not resume {name}: {exc}")

        for name in sorted(self.halted_m33):
            core = m33_by_name.get(name)
            if core is None:
                notes.append(f"could not resume {name}: it was never discovered")
                continue
            try:
                m33_mod.resume(self.dap, core)
                notes.append(f"resumed {name}")
            except S32EError as exc:
                notes.append(f"could not resume {name}: {exc}")

        return notes

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

from .dap import gates
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
    #: The symbol index (`sym load <elf>`) an IDE frontend attached, if any.
    #: Survives `note_system_reset` deliberately: a `load` does not change
    #: which ELF the source-level commands should read.
    symbols: Any = None
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
    #: R52 cores with a `core catch` MDM EDBGREQ latch armed and not yet
    #: converted. A latch that outlives the session breaks the application's
    #: own core release (Finding 7), so restore() drops these first.
    edbgreq_armed: set[str] = field(default_factory=set)
    #: Stop-the-world mode (`sync on`): the guide-§5.9 cross-halt group is
    #: armed across the usable cores, bare halt/continue act on the whole
    #: group, and restore() disarms it -- a session must not leave the chip
    #: in a mode where any halt stops the application everywhere.
    sync: bool = False
    #: Variables-view handle table. A handle is "<generation>:<id>"; any
    #: run-state change bumps the generation, so a stale handle is refused
    #: instead of read -- the addresses it described may no longer exist.
    var_generation: int = 0
    var_rows: dict[int, Any] = field(default_factory=dict)
    #: Bumped when target memory or registers change while the run state does
    #: not -- a mem/var/reg write under a halt. The read caches below key on
    #: (var_generation, data_generation) so either kind of change drops them;
    #: variables-view handles deliberately survive a write (the IDE re-reads
    #: values through the same handles right after `var write`).
    data_generation: int = 0
    #: Cores the session resumed and nothing has re-halted yet -- the set the
    #: machine-mode halt monitor sweeps for the running->halted transition.
    #: M33 names carry an "m33:" prefix. Owned by note_resumed/note_halted.
    monitor_watch: set[str] = field(default_factory=set)

    #: Read caches, valid only for _cache_key == the current generations.
    #: An IDE stop refreshes registers three to five times (regs, stack,
    #: locals, every watch) and re-walks the same stack words; nothing on a
    #: halted core changes between those commands, so one hardware read
    #: serves them all. reg cache: core name (M33 prefixed "m33:") -> the
    #: read_registers result. word cache: (ap_base, word address) -> value,
    #: None for a faulted read.
    _cache_key: tuple[int, int] = (-1, -1)
    _reg_cache: dict[str, Any] = field(default_factory=dict)
    _word_cache: dict[tuple[int, int], Any] = field(default_factory=dict)

    # -- recording helpers, called from the CLI command bodies ---------------

    def bump_var_generation(self) -> None:
        """Run state changed: every variables-view handle is now a lie."""
        self.var_generation += 1
        self.var_rows.clear()

    def note_target_written(self) -> None:
        """Memory or a register changed under a halt: drop the read caches."""
        self.data_generation += 1

    def read_caches(self) -> tuple[dict[str, Any], dict[tuple[int, int], Any]]:
        """(registers, words) caches for the current generations.

        Callers fill and consult these freely; the pair is cleared here the
        first time it is asked for after either generation moved.
        """
        key = (self.var_generation, self.data_generation)
        if key != self._cache_key:
            self._cache_key = key
            self._reg_cache.clear()
            self._word_cache.clear()
        return self._reg_cache, self._word_cache

    def note_halted(self, name: str) -> None:
        self.bump_var_generation()
        self.halted_r52.add(name)
        self.monitor_watch.discard(name)

    def note_resumed(self, name: str) -> None:
        self.bump_var_generation()
        self.halted_r52.discard(name)
        # The machine-mode halt monitor watches exactly the cores the session
        # resumed: the first spontaneous halt after this (a breakpoint or
        # watchpoint firing) is the event an IDE is waiting on.
        self.monitor_watch.add(name)

    def note_m33_halted(self, name: str) -> None:
        self.bump_var_generation()
        self.halted_m33.add(name)
        self.monitor_watch.discard(f"m33:{name}")

    def note_m33_resumed(self, name: str) -> None:
        self.bump_var_generation()
        self.halted_m33.discard(name)
        self.monitor_watch.add(f"m33:{name}")

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

    def note_core_reset(self, name: str) -> None:
        """One R52 core was reset under this session -- the application cycling
        a core's RGM reset (core0's firmware releasing core1, or a `catch`
        converting). A core-local cold reset wipes that core's comparators and
        re-sets its OS lock (guide §3.6), so every session record about it now
        describes state the reset already destroyed: forget them, and let the
        next touch re-unlock and re-sanitise (guide §5.7)."""
        self.bump_var_generation()
        self.halted_r52.discard(name)
        self.monitor_watch.discard(name)
        self.bp_set = {(c, n) for c, n in self.bp_set if c != name}
        self.wp_set = {(c, n) for c, n in self.wp_set if c != name}
        self.sanitized.discard(name)
        gates.invalidate_status_cache(self.dap)

    def note_system_reset(self) -> None:
        """A flow (`load`) reset the whole SoC under this session.

        Guide §3.6: a functional reset wipes RTU debug state -- comparators,
        OS-lock clears, halts -- so the session's per-target bookkeeping now
        describes a machine that no longer exists. Forget it, or restore()
        would try to undo changes the reset already undid. What survives:
        ``prepared`` (CDBGPWRUPREQ and the AP-read calibration hold -- the
        bench-validated `reset-halt` flow never re-prepares either), the core
        identity caches (frame addresses are stable; state is re-read on every
        use), and the focus.
        """
        self.bump_var_generation()
        self.halted_r52.clear()
        self.halted_m33.clear()
        self.monitor_watch.clear()
        self.bp_set.clear()
        self.wp_set.clear()
        self.m33_bp_set.clear()
        self.m33_wp_set.clear()
        self.sanitized.clear()
        # `load` arms and clears every R52 latch itself (loader P1/P5), so any
        # catch bookkeeping now describes latches the flow already dropped.
        self.edbgreq_armed.clear()
        gates.invalidate_status_cache(self.dap)

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

        # Sync before the resumes: with the cross-halt group armed, a halted
        # core's DBGTRIGGER level would swallow every single-core restart
        # attempted below.
        if self.sync:
            usable = [
                c for c in (self.cores or []) if not runcontrol.identity_problems(c)
            ]
            try:
                runcontrol.set_cross_trigger(self.dap, usable, False)
                notes.append("disarmed the cross-trigger sync group")
            except S32EError as exc:
                notes.append(f"could not disarm the sync group: {exc}")
            self.sync = False

        # Armed catch latches next, still before any resume: a latch that
        # outlives the session halts the core the moment the application (or
        # this restore) releases or resumes it -- the Finding 7 failure.
        for name in sorted(self.edbgreq_armed):
            core = core_by_name.get(name)
            if core is None:
                notes.append(f"could not drop the catch latch for {name}: never discovered")
                continue
            try:
                rtu, index = runcontrol.core_rtu_index(core)
                gates.clear_r52_edbgreq(self.dap, rtu, index)
                notes.append(
                    f"dropped the armed catch latch for {name} (a stale latch "
                    "breaks the application's own core release -- Finding 7)"
                )
            except S32EError as exc:
                notes.append(f"could not drop the catch latch for {name}: {exc}")

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

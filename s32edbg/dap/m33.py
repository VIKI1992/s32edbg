"""Cortex-M33 run-control. A sibling of ``runcontrol``, not an extension of it.

The two share a philosophy and almost no mechanism. An R52 is driven through a
CoreSight external-debug frame on the APB -- EDSCR, an instruction-transfer
register, a cross-trigger interface. An M33 is driven through its own AHB-AP as
plain memory: the debug registers are in the processor's own address map, so
halting is one write and reading a register is two.

That makes this module much smaller, and it keeps the parts that matter:

* **Identity before writing.** ``CPUID`` must say Cortex-M33 before anything is
  written, because a wrong AP does not fault -- it reads some other bus.
* **Checked accesses.** A faulting AHB access returns zero and latches
  CTRL/STAT.STICKYERR, after which everything reads zero. Same hazard, same
  remedy as the R52 path.
* **Counts read, never assumed.** ``FP_CTRL`` says how many breakpoint
  comparators exist; the TRM allows four or eight.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..data import m33debug as m33
from ..errors import UnsafeOperation
from .adiv6 import STICKYERR, Dap
from .runcontrol import RunControlError

#: RM Table 957. Each M33 subsystem has its own AHB-AP.
M33_APS: dict[str, int] = {
    "smu": 0x01C8_0000,
    "ce0": 0x01C9_0000,
    "ce1": 0x01C9_2000,
}


@dataclass
class M33Identity:
    cpuid: int | None = None
    dhcsr: int | None = None
    demcr: int | None = None
    fp_ctrl: int | None = None
    dwt_ctrl: int | None = None
    error: str | None = None

    @property
    def is_cortex_m33(self) -> bool:
        return self.cpuid is not None and bool(m33.decode_cpuid(self.cpuid)["is_cortex_m33"])

    @property
    def read_as_all_zeros(self) -> bool:
        """Not an identity -- the signature of an AP that answers with nothing."""
        values = (self.cpuid, self.fp_ctrl, self.dwt_ctrl)
        return all(v == 0 for v in values if v is not None) and any(
            v is not None for v in values
        )

    @property
    def breakpoints(self) -> int | None:
        return m33.fp_num_code(self.fp_ctrl) if self.fp_ctrl is not None else None

    @property
    def watchpoints(self) -> int | None:
        return m33.dwt_numcomp(self.dwt_ctrl) if self.dwt_ctrl is not None else None


@dataclass
class M33Core:
    name: str
    ap_base: int
    identity: M33Identity = field(default_factory=M33Identity)

    @property
    def halted(self) -> bool | None:
        if self.identity.dhcsr is None:
            return None
        return bool(self.identity.dhcsr & m33.DHCSR_S_HALT)

    def as_dict(self) -> dict[str, object]:
        ident = self.identity
        return {
            "name": self.name,
            "ap": f"0x{self.ap_base:08X}",
            "cpuid": m33.decode_cpuid(ident.cpuid) if ident.cpuid is not None else None,
            "dhcsr": m33.decode_dhcsr(ident.dhcsr) if ident.dhcsr is not None else None,
            "breakpoints": ident.breakpoints,
            "watchpoints": ident.watchpoints,
            "halted": self.halted,
            "error": ident.error,
        }


# ---------------------------------------------------------------------------
# Checked access -- same hazard as the R52 path
# ---------------------------------------------------------------------------


def _read(dap: Dap, ap_base: int, addr: int) -> int | None:
    dap.clear_sticky()
    value = dap.mem_read(ap_base, addr, size=32)
    if dap.read_ctrl_stat() & STICKYERR:
        dap.clear_sticky()
        return None
    return value


def _write(dap: Dap, ap_base: int, addr: int, value: int) -> bool:
    dap.clear_sticky()
    dap.mem_write(ap_base, addr, value, size=32)
    if dap.read_ctrl_stat() & STICKYERR:
        dap.clear_sticky()
        return False
    return True


# ---------------------------------------------------------------------------
# Discovery and identity
# ---------------------------------------------------------------------------


def identify(dap: Dap, ap_base: int) -> M33Identity:
    """Read a subsystem's identity. Never writes."""
    ident = M33Identity()
    ident.cpuid = _read(dap, ap_base, m33.CPUID)
    ident.dhcsr = _read(dap, ap_base, m33.DHCSR)
    ident.demcr = _read(dap, ap_base, m33.DEMCR)
    ident.fp_ctrl = _read(dap, ap_base, m33.FP_CTRL)
    ident.dwt_ctrl = _read(dap, ap_base, m33.DWT_CTRL)
    if ident.cpuid is None:
        ident.error = (
            f"the AP at 0x{ap_base:08X} did not answer. Run `gates apply` first; if it "
            "still does not, this subsystem is probably held in reset or powered down."
        )
    return ident


def discover(dap: Dap) -> list[M33Core]:
    """Identify every M33 subsystem RM Table 957 documents.

    Unlike the R52 cores there is nothing to enumerate from a ROM table -- the
    APs themselves are the enumeration, and `apmap` has already checked they are
    the ones the RM documents.
    """
    cores: list[M33Core] = []
    for name, ap_base in M33_APS.items():
        core = M33Core(name=name, ap_base=ap_base)
        core.identity = identify(dap, ap_base)
        cores.append(core)
    return cores


def resolve(spec: str, cores: list[M33Core]) -> M33Core:
    key = spec.strip().lower()
    for core in cores:
        if core.name == key or f"0x{core.ap_base:08X}".lower() == key:
            return core
    try:
        value = int(spec, 0)
    except ValueError:
        raise ValueError(
            f"unknown M33 core {spec!r}. Use one of {', '.join(M33_APS)}, or an AP base."
        ) from None
    for core in cores:
        if core.ap_base == value:
            return core
    raise ValueError(f"no M33 subsystem with AP base {spec}")


def preflight(core: M33Core, *, write: bool) -> list[str]:
    """Refuse what would otherwise give a confidently wrong result."""
    ident = core.identity
    if ident.error:
        raise UnsafeOperation(f"{core.name}: {ident.error}")
    if ident.read_as_all_zeros:
        raise UnsafeOperation(
            f"{core.name}: every identity register read as zero. That is a poisoned "
            "debug port or an AP that answers with nothing, not a processor."
        )
    if not ident.is_cortex_m33:
        raise UnsafeOperation(
            f"{core.name}: CPUID 0x{ident.cpuid or 0:08X} does not decode as a "
            "Cortex-M33. Refusing run-control against it -- a wrong AP does not fault, "
            "it reads some other bus."
        )

    notes: list[str] = []
    if ident.dhcsr is not None and not ident.dhcsr & m33.DHCSR_S_SDE:
        notes.append(
            "secure debug is not enabled (DHCSR.S_SDE clear), so secure code and the "
            "secure alias of the debug block stay invisible."
        )
    if write and ident.dhcsr is not None and not ident.dhcsr & m33.DHCSR_C_DEBUGEN:
        notes.append("halting debug was disabled; enabling it (DHCSR.C_DEBUGEN).")
    return notes


# ---------------------------------------------------------------------------
# Halt, resume, step
# ---------------------------------------------------------------------------


def _set_dhcsr(dap: Dap, core: M33Core, bits: int) -> None:
    if not _write(dap, core.ap_base, m33.DHCSR, m33.dhcsr_write(bits)):
        raise RunControlError(f"{core.name}: the DHCSR write faulted.")


def _wait_halted(
    dap: Dap, core: M33Core, want: bool, *, timeout_s: float
) -> int:
    deadline = time.monotonic() + timeout_s
    while True:
        dhcsr = _read(dap, core.ap_base, m33.DHCSR)
        if dhcsr is None:
            raise RunControlError(f"{core.name}: DHCSR became unreadable.")
        if bool(dhcsr & m33.DHCSR_S_HALT) == want:
            return dhcsr
        if time.monotonic() > deadline:
            raise RunControlError(
                f"{core.name} did not {'halt' if want else 'resume'} within "
                f"{timeout_s:.1f}s (DHCSR 0x{dhcsr:08X})."
            )
        time.sleep(0.005)


def halt(dap: Dap, core: M33Core, *, timeout_s: float = 2.0) -> int:
    """Stop the core. Two DHCSR writes, per the guide §6.2.

    ``C_DEBUGEN`` is enabled first, on its own -- the architecture requires the
    write that raises C_DEBUGEN to carry ``C_MASKINTS = 0``, and the guide's
    §6.2 sequence enables before it halts. ``C_HALT`` follows in a second
    write. (Both writes carry the DBGKEY, or they are silently ignored.)
    """
    _set_dhcsr(dap, core, m33.DHCSR_C_DEBUGEN)
    _set_dhcsr(dap, core, m33.DHCSR_C_DEBUGEN | m33.DHCSR_C_HALT)
    return _wait_halted(dap, core, True, timeout_s=timeout_s)


def halt_cause(dap: Dap, core: M33Core) -> dict[str, object]:
    """Read and clear DFSR -- why the core is halted (guide §6.2).

    DFSR bits are write-1-to-clear and accumulate across halts, so what is
    reported is also cleared: without that, the *next* halt's cause would be
    polluted by this one's.
    """
    raw = _read(dap, core.ap_base, m33.DFSR)
    if raw is None:
        return {"error": "DFSR did not read"}
    if raw:
        _write(dap, core.ap_base, m33.DFSR, raw)  # W1C what we report
    return m33.decode_dfsr(raw)


def resume(dap: Dap, core: M33Core, *, timeout_s: float = 2.0) -> tuple[int, bool]:
    """Let the core run. Returns (DHCSR, re-halted).

    ``C_DEBUGEN`` is deliberately kept set. Clearing it would also disable every
    breakpoint that is currently armed, so a resume would silently disarm the
    debug session.

    **Success is "the core executed", not "the core is still running."** A
    breakpoint on a hot address re-halts faster than this can observe it
    running: measured on the SMU, resuming onto a breakpoint in its idle loop
    came back halted every time. ``DHCSR.S_RETIRE_ST`` is the discriminator --
    it says an instruction retired since the last DHCSR read, so it separates
    "ran and stopped again" from "never started". Reading DHCSR clears it, which
    is why it is tested in the same read that checks S_HALT.

    The R52 path had the identical bug and reported a working breakpoint as a
    failed resume.

    ``S_RESTART_ST`` is checked alongside it: the guide §6.5 names it as the
    architected restart confirmation -- "the core left Debug state since the
    last read". Both bits clear on read, which is why both are tested in the
    same read that checks S_HALT.
    """
    # Flush the sticky clear-on-read bits so what the loop below sees was
    # produced by *this* resume, not left over from an earlier halt cycle.
    _read(dap, core.ap_base, m33.DHCSR)
    _set_dhcsr(dap, core, m33.DHCSR_C_DEBUGEN)

    deadline = time.monotonic() + timeout_s
    while True:
        dhcsr = _read(dap, core.ap_base, m33.DHCSR)
        if dhcsr is None:
            raise RunControlError(f"{core.name}: DHCSR became unreadable.")
        if not dhcsr & m33.DHCSR_S_HALT:
            return dhcsr, False
        if dhcsr & (m33.DHCSR_S_RETIRE_ST | m33.DHCSR_S_RESTART_ST):
            return dhcsr, True
        if time.monotonic() > deadline:
            raise RunControlError(
                f"{core.name} did not resume within {timeout_s:.1f}s (DHCSR "
                f"0x{dhcsr:08X}). Neither S_RESTART_ST nor S_RETIRE_ST set either, "
                "so it never left Debug state."
            )
        time.sleep(0.005)


def step(
    dap: Dap, core: M33Core, *, mask_interrupts: bool = True, timeout_s: float = 2.0
) -> int:
    """Execute one instruction.

    Interrupts are masked by default. Without that, a step at almost any point
    in a running system lands in an interrupt handler rather than the next
    instruction, which looks like the step going somewhere random.

    **Masking takes two DHCSR writes, not one.** The guide §6.4: C_MASKINTS may
    only be *changed* while halted, and "changing C_MASKINTS in the same write
    that clears C_HALT is UNPREDICTABLE -- so 'set MASKINTS' and 'start the
    step' must be two writes: first 0xA05F000F (halted, set step+mask), then
    0xA05F000D (release)."

    **Armed FPB comparators are suspended for the duration.** A comparator at
    the current PC matches the fetch the moment the core leaves Debug state,
    before the instruction executes -- the same re-trigger the R52 path fixed;
    "this is the second time a fix existed on one path and not the other" is
    the test plan's own warning. They are restored whatever happens.
    """
    dhcsr = _read(dap, core.ap_base, m33.DHCSR)
    if dhcsr is None or not dhcsr & m33.DHCSR_S_HALT:
        raise RunControlError(f"{core.name} is not halted, so it cannot be stepped.")

    suspended: list[tuple[int, int]] = []
    try:
        for entry in list_breakpoints(dap, core):
            if entry.get("enabled") and not entry.get("error"):
                index = int(entry["index"])
                raw = int(str(entry["raw"]), 16)
                suspended.append((index, raw))
                _write(dap, core.ap_base, m33.fp_comp(index), raw & ~m33.FP_COMP_BE)
    except RunControlError:
        pass  # no believable comparator count; nothing to suspend

    step_bits = m33.DHCSR_C_DEBUGEN | m33.DHCSR_C_STEP
    if mask_interrupts:
        step_bits |= m33.DHCSR_C_MASKINTS
        # Write 1: still halted -- raise C_STEP and C_MASKINTS with C_HALT kept.
        _set_dhcsr(dap, core, step_bits | m33.DHCSR_C_HALT)
    # Write 2 (or the only write, unmasked): clear C_HALT to release the step.
    _set_dhcsr(dap, core, step_bits)

    try:
        deadline = time.monotonic() + timeout_s
        while True:
            dhcsr = _read(dap, core.ap_base, m33.DHCSR)
            if dhcsr is not None and dhcsr & m33.DHCSR_S_HALT:
                # Leave interrupts unmasked again, or the next resume runs with
                # them off and the system quietly stops servicing anything.
                # Changing C_MASKINTS here is legal: C_HALT stays set.
                _set_dhcsr(dap, core, m33.DHCSR_C_DEBUGEN | m33.DHCSR_C_HALT)
                return dhcsr
            if time.monotonic() > deadline:
                raise RunControlError(
                    f"{core.name}: the step did not complete within {timeout_s:.1f}s."
                )
            time.sleep(0.002)
    finally:
        for index, raw in suspended:
            # Put them back even if the step failed, or the session silently
            # loses the breakpoints the operator armed.
            _write(dap, core.ap_base, m33.fp_comp(index), raw)


# ---------------------------------------------------------------------------
# Registers
# ---------------------------------------------------------------------------


def _wait_regrdy(dap: Dap, core: M33Core, *, timeout_s: float = 0.5) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        dhcsr = _read(dap, core.ap_base, m33.DHCSR)
        if dhcsr is not None and dhcsr & m33.DHCSR_S_REGRDY:
            return
        if time.monotonic() > deadline:
            raise RunControlError(
                f"{core.name}: the register transfer did not complete (S_REGRDY stayed "
                "clear). The core must be halted for DCRSR to work."
            )
        time.sleep(0.002)


def read_register(dap: Dap, core: M33Core, name: str) -> int:
    """Read one core register by name.

    Two accesses: select, then read the data register. No instruction execution
    and no scratch register, so unlike the R52 path this cannot disturb the
    halted context.
    """
    selector = m33.REGISTER_SELECTORS.get(name.lower())
    if selector is None:
        raise ValueError(
            f"unknown register {name!r}. Known: {', '.join(m33.REGISTER_SELECTORS)}"
        )
    if not _write(dap, core.ap_base, m33.DCRSR, selector):
        raise RunControlError(f"{core.name}: the DCRSR write faulted.")
    _wait_regrdy(dap, core)
    value = _read(dap, core.ap_base, m33.DCRDR)
    if value is None:
        raise RunControlError(f"{core.name}: DCRDR did not read.")
    return value


def write_register(dap: Dap, core: M33Core, name: str, value: int) -> None:
    selector = m33.REGISTER_SELECTORS.get(name.lower())
    if selector is None:
        raise ValueError(f"unknown register {name!r}")
    if not _write(dap, core.ap_base, m33.DCRDR, value):
        raise RunControlError(f"{core.name}: the DCRDR write faulted.")
    if not _write(dap, core.ap_base, m33.DCRSR, selector | m33.DCRSR_REGWNR):
        raise RunControlError(f"{core.name}: the DCRSR write faulted.")
    _wait_regrdy(dap, core)


def read_registers(dap: Dap, core: M33Core) -> dict[str, int]:
    dhcsr = _read(dap, core.ap_base, m33.DHCSR)
    if dhcsr is None or not dhcsr & m33.DHCSR_S_HALT:
        raise RunControlError(
            f"{core.name} is not halted. DCRSR only transfers registers in debug state."
        )
    return {name: read_register(dap, core, name) for name in m33.REGISTER_ORDER}


# ---------------------------------------------------------------------------
# Breakpoints, via the BPU
# ---------------------------------------------------------------------------


def breakpoint_count(core: M33Core) -> int:
    count = core.identity.breakpoints
    if count is None or not 1 <= count <= 8:
        raise RunControlError(
            f"{core.name}: FP_CTRL 0x{core.identity.fp_ctrl or 0:08X} does not give a "
            "believable comparator count (the TRM allows four or eight)."
        )
    return count


def list_breakpoints(dap: Dap, core: M33Core) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for n in range(breakpoint_count(core)):
        raw = _read(dap, core.ap_base, m33.fp_comp(n))
        if raw is None:
            out.append({"index": n, "error": "comparator did not read"})
        else:
            out.append({"index": n, **m33.decode_breakpoint(raw)})
    return out


def set_breakpoint(
    dap: Dap, core: M33Core, address: int, *, index: int | None = None
) -> dict[str, object]:
    """Arm a BPU comparator, enabling the unit and verifying the write landed.

    ``FP_CTRL`` needs its KEY bit set in the same write as ENABLE, or the write
    is discarded -- the same trap as DHCSR's debug key, and just as silent.
    """
    total = breakpoint_count(core)
    if index is None:
        free = [e for e in list_breakpoints(dap, core) if not e.get("enabled")]
        if not free:
            raise RunControlError(
                f"all {total} BPU comparators on {core.name} are in use. Clear one first."
            )
        index = int(free[0]["index"])
    if not 0 <= index < total:
        raise RunControlError(f"{core.name} implements {total} comparators, not {index + 1}.")

    if not _write(dap, core.ap_base, m33.FP_CTRL, m33.FP_CTRL_KEY | m33.FP_CTRL_ENABLE):
        raise RunControlError(f"{core.name}: the FP_CTRL write faulted.")
    value = m33.encode_breakpoint(address)
    if not _write(dap, core.ap_base, m33.fp_comp(index), value):
        raise RunControlError(f"{core.name}: the FP_COMP write faulted.")

    back = _read(dap, core.ap_base, m33.fp_comp(index))
    if back != value:
        raise RunControlError(
            f"{core.name}: breakpoint {index} did not take. Wrote 0x{value:08X}, read "
            f"back 0x{back or 0:08X}."
        )
    return {"index": index, **m33.decode_breakpoint(value)}


def clear_breakpoint(dap: Dap, core: M33Core, index: int) -> None:
    _write(dap, core.ap_base, m33.fp_comp(index), 0)


# ---------------------------------------------------------------------------
# Watchpoints, via the DWT (guide §6.7)
# ---------------------------------------------------------------------------


def watchpoint_count(core: M33Core) -> int:
    count = core.identity.watchpoints
    if count is None or not 1 <= count <= 15:
        raise RunControlError(
            f"{core.name}: DWT_CTRL 0x{core.identity.dwt_ctrl or 0:08X} does not give "
            "a believable comparator count."
        )
    return count


def _enable_dwt(dap: Dap, core: M33Core) -> None:
    """DEMCR.TRCENA gates the whole DWT -- guide §6.7: it "must be 1 first"."""
    demcr = _read(dap, core.ap_base, m33.DEMCR)
    if demcr is not None and not demcr & m33.DEMCR_TRCENA:
        if not _write(dap, core.ap_base, m33.DEMCR, demcr | m33.DEMCR_TRCENA):
            raise RunControlError(f"{core.name}: the DEMCR.TRCENA write faulted.")


def list_watchpoints(dap: Dap, core: M33Core) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for n in range(watchpoint_count(core)):
        comp = _read(dap, core.ap_base, m33.dwt_comp(n))
        function = _read(dap, core.ap_base, m33.dwt_function(n))
        if comp is None or function is None:
            out.append({"index": n, "error": "comparator registers did not read"})
        else:
            out.append({"index": n, **m33.decode_watchpoint(comp, function)})
    return out


def set_watchpoint(
    dap: Dap,
    core: M33Core,
    address: int,
    *,
    size: int = 4,
    on: str = "both",
    index: int | None = None,
) -> dict[str, object]:
    """Arm a DWT comparator as a halting data watchpoint (guide §6.7).

    ``DEMCR.TRCENA`` is enabled first (and left enabled -- the PC sampler wants
    it too). The comparator address goes in before the function word arms it,
    and both are read back: comparator capabilities differ per slot
    (``DWT_FUNCTIONn.ID``), so a slot that cannot do data-address matching
    shows up here as a read-back mismatch rather than as a watchpoint that
    silently never fires.
    """
    total = watchpoint_count(core)
    if index is None:
        free = [
            e for e in list_watchpoints(dap, core) if not e.get("enabled") and not e.get("error")
        ]
        if not free:
            raise RunControlError(
                f"all {total} DWT comparators on {core.name} are in use. Clear one first."
            )
        index = int(free[0]["index"])
    if not 0 <= index < total:
        raise RunControlError(f"{core.name} implements {total} comparators, not {index + 1}.")

    comp, function = m33.encode_watchpoint(address, size=size, on=on)
    _enable_dwt(dap, core)
    ok = _write(dap, core.ap_base, m33.dwt_function(index), m33.DWT_MATCH_DISABLED)
    ok &= _write(dap, core.ap_base, m33.dwt_comp(index), comp)
    ok &= _write(dap, core.ap_base, m33.dwt_function(index), function)
    if not ok:
        raise RunControlError(f"{core.name}: a DWT comparator write faulted.")

    back_comp = _read(dap, core.ap_base, m33.dwt_comp(index))
    back_function = _read(dap, core.ap_base, m33.dwt_function(index))
    # Ignore the read-only bits when comparing what was programmed: MATCHED
    # (bit 24, clears on read) and the ID capability field [31:27] -- measured
    # on the SMU, 0x815 reads back as 0x58000815.
    if back_comp != comp or (
        back_function is None or (back_function & ~m33.DWT_FUNCTION_RO_MASK) != function
    ):
        raise RunControlError(
            f"{core.name}: watchpoint {index} did not take. Wrote COMP 0x{comp:08X} / "
            f"FUNCTION 0x{function:08X}, read back 0x{back_comp or 0:08X} / "
            f"0x{back_function or 0:08X}. This DWT slot may not support data-address "
            "matching (DWT_FUNCTION.ID) -- try another --index."
        )
    return {"index": index, **m33.decode_watchpoint(comp, function)}


def clear_watchpoint(dap: Dap, core: M33Core, index: int) -> None:
    _write(dap, core.ap_base, m33.dwt_function(index), 0)
    _write(dap, core.ap_base, m33.dwt_comp(index), 0)


def sanitize_comparators(dap: Dap, core: M33Core) -> int:
    """Blank every FPB and DWT comparator on this core.

    The M33 counterpart of the R52 sanitation: FP_COMP contents are UNKNOWN out
    of reset and the first ``bp set`` writes ``FP_CTRL = ENABLE``, which arms
    every comparator including garbage ones. Returns how many slots read as
    armed before sanitising.
    """
    armed = 0
    for entry in list_breakpoints(dap, core):
        if entry.get("enabled"):
            armed += 1
        if not entry.get("error"):
            _write(dap, core.ap_base, m33.fp_comp(int(entry["index"])), 0)
    for entry in list_watchpoints(dap, core):
        if entry.get("enabled"):
            armed += 1
        if not entry.get("error"):
            index = int(entry["index"])
            _write(dap, core.ap_base, m33.dwt_function(index), 0)
            _write(dap, core.ap_base, m33.dwt_comp(index), 0)
    return armed


# ---------------------------------------------------------------------------
# Reset-and-halt (guide §6.8)
# ---------------------------------------------------------------------------


def reset_halt(dap: Dap, core: M33Core, *, timeout_s: float = 5.0) -> tuple[int, int]:
    """Warm-reset the core and halt it at the reset handler's first instruction.

    The architected flow, guide §6.8: ``C_DEBUGEN`` (survives warm resets) +
    ``DEMCR.VC_CORERESET`` (reset vector catch), flush the sticky
    ``S_RESET_ST`` with one DHCSR read, request the reset through
    ``AIRCR.SYSRESETREQ``, then poll for ``S_RESET_ST && S_HALT``. On success
    the core is halted at the reset handler entry with MSP loaded.

    Returns ``(dhcsr, pc)``. ``VC_CORERESET`` is cleared afterwards whatever
    happens, so a later reset does not halt unexpectedly.

    **Scope caveat**: how far SYSRESETREQ reaches on this SoC (this core, its
    subsystem, or wider) is not stated in the guide's sources -- treat the
    system state after this as rebooted until proven narrower.
    """
    _set_dhcsr(dap, core, m33.DHCSR_C_DEBUGEN)
    demcr = _read(dap, core.ap_base, m33.DEMCR)
    if demcr is None:
        raise RunControlError(f"{core.name}: DEMCR did not read; cannot arm the vector catch.")
    if not _write(dap, core.ap_base, m33.DEMCR, demcr | m33.DEMCR_VC_CORERESET):
        raise RunControlError(f"{core.name}: the DEMCR.VC_CORERESET write faulted.")

    try:
        _read(dap, core.ap_base, m33.DHCSR)  # flush sticky S_RESET_ST
        if not _write(dap, core.ap_base, m33.AIRCR, m33.AIRCR_SYSRESETREQ):
            raise RunControlError(f"{core.name}: the AIRCR.SYSRESETREQ write faulted.")

        reset_seen = False
        deadline = time.monotonic() + timeout_s
        while True:
            dhcsr = _read(dap, core.ap_base, m33.DHCSR)
            if dhcsr is not None:
                if dhcsr & m33.DHCSR_S_RESET_ST:
                    reset_seen = True  # clear-on-read: latch it across the poll
                if reset_seen and dhcsr & m33.DHCSR_S_HALT:
                    break
            if time.monotonic() > deadline:
                raise RunControlError(
                    f"{core.name}: no reset-and-halt within {timeout_s:.1f}s "
                    f"(DHCSR 0x{dhcsr or 0:08X}, reset seen: {reset_seen}). The guide "
                    "notes SYSRESETREQ is not guaranteed to take effect immediately."
                )
            time.sleep(0.005)
    finally:
        now = _read(dap, core.ap_base, m33.DEMCR)
        if now is not None:
            _write(dap, core.ap_base, m33.DEMCR, now & ~m33.DEMCR_VC_CORERESET)

    pc = read_register(dap, core, "pc")
    return dhcsr, pc


# ---------------------------------------------------------------------------
# Non-invasive PC sampling, via DWT_PCSR
# ---------------------------------------------------------------------------


def sample_pc(dap: Dap, core: M33Core, count: int = 200) -> dict[str, object]:
    """Sample a running core's PC without stopping it.

    The M33 equivalent of the R52's EDPCSR, and used for the same reason:
    finding an address worth breakpointing without repeatedly halting a system
    that may not survive it. Needs ``DEMCR.TRCENA`` set for the DWT to be
    clocked, which this enables and leaves enabled.
    """
    from collections import Counter

    demcr = _read(dap, core.ap_base, m33.DEMCR)
    if demcr is not None and not demcr & m33.DEMCR_TRCENA:
        _write(dap, core.ap_base, m33.DEMCR, demcr | m33.DEMCR_TRCENA)

    hist: Counter = Counter()
    invalid = 0
    for _ in range(count):
        value = _read(dap, core.ap_base, m33.DWT_PCSR)
        if value is None or not m33.pcsr_is_valid(value):
            invalid += 1
        else:
            hist[value] += 1

    dhcsr = _read(dap, core.ap_base, m33.DHCSR) or 0
    note = None
    if invalid == count:
        why = (
            "the core is asleep (DHCSR.S_SLEEP)"
            if dhcsr & m33.DHCSR_S_SLEEP
            else "the core is halted"
            if dhcsr & m33.DHCSR_S_HALT
            else "the core is not executing -- held in reset, or its clock is gated"
        )
        note = (
            f"every sample was invalid, so no PC was captured: {why}. DWT_PCSR only "
            "yields a PC for a core that is running."
        )

    return {
        "core": core.name,
        "samples": count,
        "invalid": invalid,
        "distinct": len(hist),
        "sleeping": bool(dhcsr & m33.DHCSR_S_SLEEP),
        "note": note,
        "top": [
            {"address": f"0x{a:08X}", "hits": n, "percent": round(100 * n / count, 1)}
            for a, n in hist.most_common(16)
        ],
    }

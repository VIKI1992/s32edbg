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
    """Stop the core. One write, unlike the R52's cross-trigger dance.

    ``C_DEBUGEN`` goes in the same write as ``C_HALT``: halting without it is
    accepted and does nothing, because halting debug has to be enabled for the
    halt request to mean anything.
    """
    _set_dhcsr(dap, core, m33.DHCSR_C_DEBUGEN | m33.DHCSR_C_HALT)
    return _wait_halted(dap, core, True, timeout_s=timeout_s)


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
    """
    _set_dhcsr(dap, core, m33.DHCSR_C_DEBUGEN)

    deadline = time.monotonic() + timeout_s
    while True:
        dhcsr = _read(dap, core.ap_base, m33.DHCSR)
        if dhcsr is None:
            raise RunControlError(f"{core.name}: DHCSR became unreadable.")
        if not dhcsr & m33.DHCSR_S_HALT:
            return dhcsr, False
        if dhcsr & m33.DHCSR_S_RETIRE_ST:
            return dhcsr, True
        if time.monotonic() > deadline:
            raise RunControlError(
                f"{core.name} did not resume within {timeout_s:.1f}s (DHCSR "
                f"0x{dhcsr:08X}). No instruction retired either, so it never started."
            )
        time.sleep(0.005)


def step(
    dap: Dap, core: M33Core, *, mask_interrupts: bool = True, timeout_s: float = 2.0
) -> int:
    """Execute one instruction.

    Interrupts are masked by default. Without that, a step at almost any point
    in a running system lands in an interrupt handler rather than the next
    instruction, which looks like the step going somewhere random.
    """
    dhcsr = _read(dap, core.ap_base, m33.DHCSR)
    if dhcsr is None or not dhcsr & m33.DHCSR_S_HALT:
        raise RunControlError(f"{core.name} is not halted, so it cannot be stepped.")

    bits = m33.DHCSR_C_DEBUGEN | m33.DHCSR_C_STEP
    if mask_interrupts:
        bits |= m33.DHCSR_C_MASKINTS
    _set_dhcsr(dap, core, bits)

    deadline = time.monotonic() + timeout_s
    while True:
        dhcsr = _read(dap, core.ap_base, m33.DHCSR)
        if dhcsr is not None and dhcsr & m33.DHCSR_S_HALT:
            # Leave interrupts unmasked again, or the next resume runs with them
            # off and the system quietly stops servicing anything.
            _set_dhcsr(dap, core, m33.DHCSR_C_DEBUGEN | m33.DHCSR_C_HALT)
            return dhcsr
        if time.monotonic() > deadline:
            raise RunControlError(
                f"{core.name}: the step did not complete within {timeout_s:.1f}s."
            )
        time.sleep(0.002)


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

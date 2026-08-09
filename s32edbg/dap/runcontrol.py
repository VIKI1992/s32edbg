"""Core run-control: find the cores, say what state they are in.

Layering matches ``memaccess.py``: ``adiv6.py`` supplies the transport and knows
nothing about cores, this module owns the register semantics and the policy, and
the CLI talks only to this. Every access goes through ``Dap.mem_read``/
``mem_write`` on the APB-AP, which is the route proven on silicon.

Two things this module refuses to do, and why:

* **Trust the offset map before checking it.** ``identify()`` reads three
  independent anchors -- ``MIDR``, ``DEVTYPE`` and ``DEVARCH`` -- and reports
  whether they agree. Nothing downstream should write to a frame whose identity
  did not check out, because a wrong base address turns a comparator write into
  a write somewhere else entirely.
* **Assume the comparator count.** The breakpoints guide says eight per core.
  ``EDDFR`` is read instead, because a wrong offset would also produce a number,
  and programming comparator 7 on a core that implements four writes into a
  reserved region.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..data import a32
from ..data import coresight as cs
from ..data import r52debug as r52
from ..data.expected_apmap import AP_APB, AP_AXI
from ..errors import DapError, UnsafeOperation
from . import gates
from . import memaccess
from . import romtable
from .adiv6 import STICKYERR, Dap

#: Address stride between successive core debug frames within one RTU, and the
#: frame of core 0 in each RTU. Both proven by the ROM walk on this silicon.
CORE_STRIDE = 0x0010_0000
RTU_FIRST_CORE_FRAME: tuple[int, ...] = (0x0041_0000, 0x00C1_0000)

#: How far to probe past what the ROM table enumerates.
#:
#: The ROM table on this part lists two debug frames per RTU while the datasheet
#: gives four physical Cortex-R52 cores per RTU -- most likely lockstep pairing,
#: where each pair presents one frame. Rather than pick a side, discovery probes
#: the by-stride addresses too and reports anything that answers but was not
#: enumerated. Either answer is then a measurement rather than an assumption.
MAX_CORES_PER_RTU = 4


@dataclass
class CoreIdentity:
    """What a debug frame says it is."""

    midr: int | None = None
    devtype: int | None = None
    devarch: int | None = None
    devaff0: int | None = None
    devaff1: int | None = None
    eddfr: int | None = None
    authstatus: int | None = None
    lsr: int | None = None
    error: str | None = None

    @property
    def is_cortex_r52(self) -> bool:
        return self.midr is not None and bool(r52.decode_midr(self.midr)["is_cortex_r52"])

    @property
    def devtype_ok(self) -> bool:
        return self.devtype is not None and (self.devtype & 0xFF) == 0x15

    @property
    def devarch_ok(self) -> bool:
        return self.devarch is not None and (self.devarch & 0xFFFF) == 0x6A05

    @property
    def read_as_all_zeros(self) -> bool:
        """Every identity register read as zero, which is not an identity.

        This is the signature of a poisoned DP rather than of a strange
        component. Measured on silicon: once CTRL/STAT.STICKYERR latches, the DP
        discards AP transfers and returns zeros **without raising**, so a frame
        that reads 0x411FD133 when asked directly reports MIDR 0 here. Without
        this check the tool would say "not a Cortex-R52" about a perfectly
        healthy core, which is a confidently wrong answer of exactly the kind
        the rest of this tool works to avoid.
        """
        values = (self.midr, self.devtype, self.devarch, self.eddfr, self.authstatus)
        return all(v == 0 for v in values if v is not None) and any(
            v is not None for v in values
        )

    @property
    def anchors_agree(self) -> bool:
        """All three independent identity checks pass.

        Any one of them could pass by accident on a misaddressed frame; three
        agreeing is what makes the offset map trustworthy enough to write to.
        """
        return self.is_cortex_r52 and self.devtype_ok and self.devarch_ok

    @property
    def comparators(self) -> dict[str, object] | None:
        return r52.decode_eddfr(self.eddfr) if self.eddfr is not None else None

    @property
    def invasive_debug_granted(self) -> bool | None:
        if self.authstatus is None:
            return None
        return cs.invasive_debug_granted(self.authstatus)

    @property
    def affinity(self) -> tuple[int, int, int] | None:
        """(Aff2, Aff1, Aff0) from DEVAFF0 -- the associated core's MPIDR.

        This is the authoritative core identity. Address arithmetic says where a
        frame *is*; affinity says which core it *belongs to*, which is the thing
        that actually matters when the enumeration and the datasheet disagree.
        """
        if self.devaff0 is None:
            return None
        return (
            (self.devaff0 >> 16) & 0xFF,
            (self.devaff0 >> 8) & 0xFF,
            self.devaff0 & 0xFF,
        )


@dataclass
class CoreState:
    """Where a core is right now."""

    edscr: int | None = None
    edprsr: int | None = None
    error: str | None = None
    #: Why the core-domain registers could not be read, when EDPRSR explains it.
    unreachable_reason: str | None = None
    #: Set by resume() when the core ran and immediately re-entered Debug state
    #: for a *different* reason -- almost always a breakpoint on a hot address.
    rehalted: bool = False
    #: Whether EDPRSR.SDR confirmed the restart. The guide §5.5 is explicit:
    #: "Confirm restart with EDPRSR.SDR (clear-on-read), *not* EDSCR.STATUS".
    #: None means no restart was attempted (or SDR was never sampled).
    restart_confirmed: bool | None = None

    @property
    def halted(self) -> bool | None:
        """Halted, requiring both status registers to agree.

        EDSCR.STATUS and EDPRSR.HALTED are independent reports of the same fact.
        Disagreement means one of the two bit maps is wrong, so it is surfaced
        as unknown rather than resolved by preferring one.
        """
        if self.edscr is None or self.edprsr is None:
            return None
        by_edscr = r52.edscr_halted(self.edscr)
        by_edprsr = bool(self.edprsr & r52.EDPRSR_HALTED)
        return by_edscr if by_edscr == by_edprsr else None

    @property
    def status_registers_disagree(self) -> bool:
        """Only meaningful when both registers were actually read.

        An unread EDSCR is not a disagreement -- it is an absence, and reporting
        it as "the bit maps are wrong" would send someone to debug the wrong
        thing entirely.
        """
        if self.edscr is None or self.edprsr is None:
            return False
        return r52.edscr_halted(self.edscr) != bool(self.edprsr & r52.EDPRSR_HALTED)

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"error": self.error}
        if self.edscr is not None:
            out["edscr"] = r52.decode_edscr(self.edscr)
        if self.edprsr is not None:
            out["edprsr"] = r52.decode_edprsr(self.edprsr)
        out["halted"] = self.halted
        out["unreachable_reason"] = self.unreachable_reason
        if self.restart_confirmed is not None:
            out["restart_confirmed"] = self.restart_confirmed
        if self.status_registers_disagree:
            out["warning"] = (
                "EDSCR.STATUS and EDPRSR.HALTED disagree about whether this core is "
                "halted. One of the two bit maps in data/r52debug.py is wrong; do not "
                "trust run-control on this core until that is resolved."
            )
        return out


@dataclass
class Core:
    """A discovered core: where its frames are, what it is, what it is doing."""

    index: int
    debug_base: int
    cti_base: int
    found_in_rom: bool
    identity: CoreIdentity = field(default_factory=CoreIdentity)
    state: CoreState = field(default_factory=CoreState)
    #: Set by discover() when the affinity-derived name would be ambiguous.
    name_override: str | None = None

    @property
    def name(self) -> str:
        """Prefer the affinity-derived name; fall back to the address.

        Affinity 0.0 is a legitimate identity -- it is RTU0 core 0 -- so an
        all-zero DEVAFF0 must not be read as "unknown". Only a DEVAFF0 that did
        not *read* falls back to the address. The remaining hazard, a core whose
        DEVAFF0 is not implemented and therefore reads zero, shows up as two
        cores claiming the same name; discover() detects that and overrides
        both.
        """
        if self.name_override is not None:
            return self.name_override
        aff = self.identity.affinity
        if aff is not None:
            return f"rtu{aff[1]}.{aff[2]}"
        return f"core@0x{self.debug_base:08X}"

    def as_dict(self) -> dict[str, object]:
        ident = self.identity
        return {
            "index": self.index,
            "name": self.name,
            "debug_base": f"0x{self.debug_base:08X}",
            "cti_base": f"0x{self.cti_base:08X}",
            "found_in_rom": self.found_in_rom,
            "midr": r52.decode_midr(ident.midr) if ident.midr is not None else None,
            "affinity": ident.affinity,
            "devtype": f"0x{ident.devtype:08X}" if ident.devtype is not None else None,
            "devarch": f"0x{ident.devarch:08X}" if ident.devarch is not None else None,
            "anchors_agree": ident.anchors_agree,
            "comparators": ident.comparators,
            "authstatus": cs.decode_authstatus(ident.authstatus)
            if ident.authstatus is not None
            else None,
            "invasive_debug_granted": ident.invasive_debug_granted,
            "software_lock": _lock_note(ident.lsr),
            "identity_error": ident.error,
            "state": self.state.as_dict(),
        }


def _lock_note(lsr: int | None) -> str | None:
    if lsr is None:
        return None
    if not lsr & cs.LSR_SLI:
        return "not implemented"
    return "locked" if lsr & cs.LSR_SLK else "unlocked"


# ---------------------------------------------------------------------------
# Reading a frame
# ---------------------------------------------------------------------------


def _read(dap: Dap, ap_base: int, base: int, offset: int) -> int:
    return dap.mem_read(ap_base, base + offset, size=32)


def _read_checked(dap: Dap, ap_base: int, base: int, offset: int) -> int | None:
    """Read one register, returning None if the access actually faulted.

    **A faulting APB access does not raise.** Measured on this silicon: the read
    returns 0x00000000, latches CTRL/STAT.STICKYERR, and from then on *every*
    subsequent AP transfer also returns zero until the flag is cleared. So a
    plain read cannot distinguish "this register is zero" from "this register
    faulted" from "something faulted earlier and I am now reading rubbish".

    Checking the sticky flag after each read is the only way to tell, and
    clearing it is the only way for the next read to mean anything. This costs
    two extra DP accesses per register, which is affordable for the dozen reads
    identity and status need, and is what keeps one unreachable register from
    silently invalidating every value that follows it.
    """
    dap.clear_sticky()
    value = dap.mem_read(ap_base, base + offset, size=32)
    if dap.read_ctrl_stat() & STICKYERR:
        dap.clear_sticky()
        return None
    return value


def _write_checked(dap: Dap, ap_base: int, base: int, offset: int, value: int) -> bool:
    """Write one register, returning whether the access actually landed.

    Same hazard as ``_read_checked`` and the same remedy. A write to a locked or
    unpowered debug register is accepted and discarded, so without checking the
    sticky flag afterwards a failed unlock looks exactly like a successful one.
    """
    dap.clear_sticky()
    dap.mem_write(ap_base, base + offset, value, size=32)
    if dap.read_ctrl_stat() & STICKYERR:
        dap.clear_sticky()
        return False
    return True


def identify(dap: Dap, debug_base: int, *, ap_base: int = AP_APB) -> CoreIdentity:
    """Read a frame's identity. Never writes.

    Reads three independent anchors so a misaddressed frame is caught before
    anything is written to it: MIDR at 0xD00 (implementer and part number),
    DEVTYPE at 0xFCC, DEVARCH at 0xFBC. MIDR is the strongest of the three
    because it sits far from the ID block the ROM walk already validated -- a
    correct decode there means the middle of the map is addressed correctly too.
    """
    ident = CoreIdentity()
    try:
        ident.midr = _read_checked(dap, ap_base, debug_base, r52.MIDR)
        ident.devtype = _read_checked(dap, ap_base, debug_base, cs.DEVTYPE)
        ident.devarch = _read_checked(dap, ap_base, debug_base, cs.DEVARCH)
        ident.devaff0 = _read_checked(dap, ap_base, debug_base, cs.DEVAFF0)
        ident.devaff1 = _read_checked(dap, ap_base, debug_base, cs.DEVAFF1)
        ident.eddfr = _read_checked(dap, ap_base, debug_base, r52.EDDFR_LO)
        ident.authstatus = _read_checked(dap, ap_base, debug_base, cs.AUTHSTATUS)
        ident.lsr = _read_checked(dap, ap_base, debug_base, cs.LSR)
    except DapError as exc:
        ident.error = str(exc)
    return ident


def read_state(dap: Dap, debug_base: int, *, ap_base: int = AP_APB) -> CoreState:
    """Read EDPRSR and EDSCR. Never writes.

    EDPRSR is read **first**, deliberately. It lives in the debug power domain
    and stays readable when the core-domain registers do not, so it is what
    turns an unexplained EDSCR fault into a stated reason -- OS lock set, core
    in reset, external debug access disabled. Measured on this silicon: three of
    the four cores fault on EDSCR while EDPRSR, MIDR and EDDFR all read fine.
    """
    state = CoreState()
    try:
        state.edprsr = _read_checked(dap, ap_base, debug_base, r52.EDPRSR)
        # Attempt EDSCR regardless of what EDPRSR implies, and use EDPRSR only
        # to *explain* a failure. Predicting instead of trying refused a read
        # that worked: EDPRSR.EDAD stays set on cores that read EDSCR perfectly
        # well once the OS lock is clear, and whether it clears at all is
        # implementation-defined. A faulted read costs one recovered sticky
        # error, which is cheaper than a wrong refusal.
        state.edscr = _read_checked(dap, ap_base, debug_base, r52.EDSCR)
        if state.edscr is None:
            _, reason = (
                r52.debug_registers_reachable(state.edprsr)
                if state.edprsr is not None
                else (False, None)
            )
            state.unreachable_reason = reason or "the EDSCR read faulted, with no reason given by EDPRSR"
    except DapError as exc:
        state.error = str(exc)
    return state


def looks_like_a_debug_frame(dap: Dap, debug_base: int, *, ap_base: int = AP_APB) -> bool:
    """Cheap probe: valid CoreSight preamble and DEVTYPE 0x15.

    Used to check by-stride addresses the ROM table does not enumerate, so
    reading an address that is not mapped is the *expected* case rather than an
    error.

    **The sticky-error clear is not optional.** Measured on silicon: a read of
    unmapped APBIC2 space latches CTRL/STAT.STICKYERR (0xF0000000 -> 0xF0000020),
    and from that moment the DP discards every subsequent AP transfer and returns
    zeros **without raising**. The first run of this probe left MIDR reading
    0x00000000 on a core that reads 0x411FD133 when asked directly, and every
    identity check downstream then failed against a healthy core. Clearing after
    each probe confines the damage to the probe that caused it.
    """
    try:
        cidr = tuple(
            _read(dap, ap_base, debug_base, off) & 0xFF
            for off in (cs.CIDR0, cs.CIDR1, cs.CIDR2, cs.CIDR3)
        )
        found = cs.preamble_ok(cidr) and (  # type: ignore[arg-type]
            _read(dap, ap_base, debug_base, cs.DEVTYPE) & 0xFF
        ) == 0x15
    except DapError:
        found = False

    if not found:
        # Only on the miss: a hit did not fault, and clearing unconditionally
        # would discard a sticky error raised by something else entirely.
        try:
            dap.clear_sticky()
        except DapError:
            pass
    return found


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def frames_from_rom(root: romtable.Node) -> list[int]:
    """Debug-frame base addresses a walked ROM tree enumerates.

    Matches on DEVTYPE 0x15 or DEVARCH archid 0x6A05 -- either is enough, since
    a component that claims to be processor external debug by one route and not
    the other is worth surfacing rather than dropping.
    """
    found: list[int] = []
    for node in root.flatten():
        ident = node.ident
        if ident is None or not ident.preamble_ok:
            continue
        by_devtype = ident.devtype_raw is not None and (ident.devtype_raw & 0xFF) == 0x15
        by_devarch = (
            ident.devarch is not None
            and ident.devarch.present
            and ident.devarch.archid == 0x6A05
        )
        if by_devtype or by_devarch:
            found.append(node.base)
    return sorted(found)


def candidate_frames() -> list[int]:
    """By-stride addresses worth probing, whether or not the ROM lists them."""
    return [
        first + n * CORE_STRIDE
        for first in RTU_FIRST_CORE_FRAME
        for n in range(MAX_CORES_PER_RTU)
    ]


def discover(
    dap: Dap, *, ap_base: int = AP_APB, probe_unlisted: bool = True
) -> list[Core]:
    """Find every core debug frame, from the ROM table and by probing.

    The two routes exist because they fail differently. The ROM table is
    authoritative about what the design intends to expose, but on this part it
    lists two frames per RTU while the datasheet gives four cores per RTU. The
    by-stride probe finds anything present but unlisted. Frames found only by
    probing are flagged ``found_in_rom=False`` rather than quietly merged, so
    the discrepancy stays visible.
    """
    rom, present = dap.read_ap_base(ap_base)
    listed: list[int] = []
    if present and rom is not None:
        try:
            listed = frames_from_rom(romtable.walk(dap.mem_reader(ap_base), rom))
        except DapError:
            listed = []

    bases = list(listed)
    if probe_unlisted:
        for candidate in candidate_frames():
            if candidate not in bases and looks_like_a_debug_frame(
                dap, candidate, ap_base=ap_base
            ):
                bases.append(candidate)

    cores: list[Core] = []
    for index, base in enumerate(sorted(bases)):
        core = Core(
            index=index,
            debug_base=base,
            cti_base=base + r52.CTI_OFFSET_FROM_DEBUG,
            found_in_rom=base in listed,
        )
        core.identity = identify(dap, base, ap_base=ap_base)
        core.state = read_state(dap, base, ap_base=ap_base)
        cores.append(core)

    # If DEVAFF0 is not implemented it reads zero, and every core then claims
    # affinity 0.0. Two cores with one name would make --core ambiguous, so
    # detect the collision and fall back to addresses for the ones involved.
    seen: dict[str, list[Core]] = {}
    for core in cores:
        seen.setdefault(core.name, []).append(core)
    for name, group in seen.items():
        if len(group) > 1:
            for core in group:
                core.name_override = f"core@0x{core.debug_base:08X}"

    return cores


def resolve(spec: str, cores: list[Core]) -> Core:
    """Turn a --core value into a Core: index, name, or debug-frame address."""
    key = spec.strip().lower()
    for core in cores:
        if core.name.lower() == key:
            return core
    try:
        value = int(spec, 0)
    except ValueError:
        raise ValueError(
            f"unknown core {spec!r}. Use an index (0-{len(cores) - 1}), a name "
            f"({', '.join(c.name for c in cores)}), or a debug-frame base address."
        ) from None
    for core in cores:
        if core.index == value or core.debug_base == value:
            return core
    raise ValueError(
        f"no core with index or debug-frame base {spec}. Found: "
        + ", ".join(f"{c.index}={c.name}@0x{c.debug_base:08X}" for c in cores)
    )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


#: What to say when a frame reads as all zeros. Kept as a constant because both
#: the advisory path and the refusal path must give the same diagnosis.
ALL_ZEROS_DIAGNOSIS = (
    "every identity register read as 0x00000000. That is not a component, it is a "
    "poisoned debug port: once CTRL/STAT.STICKYERR latches, the DP discards AP "
    "transfers and returns zeros without raising an error. Clear it and retry -- "
    "reading an unmapped address is the usual cause"
)


def identity_problems(core: Core) -> list[str]:
    """Which of the three identity anchors disagree, in words.

    An all-zero read is reported as its own diagnosis rather than as three
    separate anchor failures, because "MIDR is not a Cortex-R52" is a misleading
    thing to say about a core whose MIDR simply was not read.
    """
    ident = core.identity
    if ident.read_as_all_zeros:
        return [ALL_ZEROS_DIAGNOSIS]
    return [
        text
        for text, wrong in (
            ("MIDR is not a Cortex-R52", not ident.is_cortex_r52),
            ("DEVTYPE is not 0x15", not ident.devtype_ok),
            ("DEVARCH archid is not 0x6A05", not ident.devarch_ok),
        )
        if wrong
    ]


def preflight_notes(core: Core) -> list[str]:
    """Everything worth telling the operator, without refusing anything.

    Used by the read-only commands. A `status` command that refused to report on
    a core because something looked wrong would withhold exactly the information
    needed to diagnose it, so the problems are reported as notes here and only
    become refusals in ``preflight()``.
    """
    notes: list[str] = []
    ident = core.identity

    if ident.error:
        notes.append(
            f"the debug frame at 0x{core.debug_base:08X} did not read: {ident.error}"
        )

    problems = identity_problems(core)
    if problems and not ident.error:
        notes.append(
            "this component does not identify as a Cortex-R52 debug frame ("
            + ", ".join(problems)
            + "). Run-control against it is refused."
        )

    if ident.invasive_debug_granted is False:
        notes.append(
            f"invasive debug is not granted (DBGAUTHSTATUS 0x{ident.authstatus or 0:08X}). "
            "Halting and comparator writes would be accepted and do nothing."
        )

    if not core.found_in_rom:
        notes.append(
            "found by probing, not by the ROM table -- it answers, but the design did "
            "not enumerate it."
        )

    comparators = ident.comparators
    if comparators is not None and not comparators["plausible"]:
        notes.append(
            f"EDDFR reads {comparators['raw']}, decoding to {comparators['breakpoints']} "
            f"breakpoints and {comparators['watchpoints']} watchpoints -- not a plausible "
            "count, so the EDDFR offset or field layout in data/r52debug.py is probably "
            "wrong. Comparator indices are not trustworthy."
        )

    if core.state.status_registers_disagree:
        notes.append(
            "EDSCR.STATUS and EDPRSR.HALTED disagree about whether this core is halted."
        )

    if core.state.edprsr is not None and core.state.edprsr & r52.EDPRSR_R:
        notes.append(
            "the core is held in reset (EDPRSR.R) -- it has never been released, so "
            "run-control and comparators cannot reach it yet. `core catch` arms a "
            "halt at its first instruction for when something releases it."
        )

    if _lock_note(ident.lsr) == "locked":
        notes.append(
            "the debug frame's software lock is engaged; writes will be ignored until "
            "it is cleared."
        )

    return notes


# ---------------------------------------------------------------------------
# Unlocking, halting, resuming
# ---------------------------------------------------------------------------


class RunControlError(DapError):
    """A run-control operation did not take effect."""


def unlock(dap: Dap, core: Core, *, ap_base: int = AP_APB) -> list[str]:
    """Clear the locks that stop external debug reaching the core registers.

    Two independent locks, in the order the breakpoints guide §5.2 gives:

    * **The software lock** on the debug frame (``EDLAR``/``EDLSR``), which gates
      writes through this memory-mapped interface. Only touched when ``EDLSR``
      says it is implemented and engaged.
    * **The OS lock** (``OSLAR``), which the running software owns. On this
      board three of the four cores come up with it set, and while it is set a
      read of ``EDSCR`` *faults* -- so nothing else works until it is cleared.

    **This changes target state.** The OS lock is normally managed by the
    firmware, typically around power management; clearing it takes that claim
    away. That is what a debugger does, but it is not nothing.

    Returns notes; raises ``RunControlError`` if a lock would not clear, because
    proceeding would produce writes that are accepted and discarded.
    """
    notes: list[str] = []
    base = core.debug_base

    lsr = _read_checked(dap, ap_base, base, cs.LSR)
    if lsr is not None and lsr & cs.LSR_SLI and lsr & cs.LSR_SLK:
        if not _write_checked(dap, ap_base, base, cs.LAR, cs.LAR_KEY):
            raise RunControlError(
                f"{core.name}: the software-lock key write to EDLAR faulted."
            )
        after = _read_checked(dap, ap_base, base, cs.LSR)
        if after is not None and after & cs.LSR_SLK:
            raise RunControlError(
                f"{core.name}: EDLSR still reports the software lock engaged after "
                f"writing the unlock key (EDLSR 0x{after:08X})."
            )
        notes.append("cleared the debug frame's software lock")

    edprsr = _read_checked(dap, ap_base, base, r52.EDPRSR)
    if edprsr is None:
        raise RunControlError(
            f"{core.name}: EDPRSR did not read, so the debug frame is not reachable "
            "at all. `gates apply` first."
        )
    if edprsr & r52.EDPRSR_DLK:
        raise RunControlError(
            f"{core.name}: the OS double lock is set (EDPRSR 0x{edprsr:08X}). Only the "
            "target's own software can clear that one; an external debugger cannot."
        )

    if edprsr & r52.EDPRSR_R:
        raise RunControlError(
            f"{core.name}: the core is held in reset (EDPRSR 0x{edprsr:08X}) -- it has "
            "never been released, so unlocking and comparator writes cannot reach it. "
            "On this application core0's firmware performs the release; `core catch` "
            "arms a halt at its first instruction."
        )

    if edprsr & r52.EDPRSR_OSLK:
        # Writing 0 releases it. The write goes to OSLAR, which is writable even
        # while the lock it controls is set -- unlike everything the lock gates.
        if not _write_checked(dap, ap_base, base, r52.OSLAR, 0):
            raise RunControlError(f"{core.name}: the OSLAR write faulted.")
        edprsr = _read_checked(dap, ap_base, base, r52.EDPRSR)
        if edprsr is None or edprsr & r52.EDPRSR_OSLK:
            raise RunControlError(
                f"{core.name}: the OS lock is still set after writing OSLAR "
                f"(EDPRSR 0x{edprsr or 0:08X})."
            )
        notes.append("cleared the OS lock")

    # Verify by reading the register that matters, not by predicting from
    # EDPRSR. Whether EDPRSR.EDAD clears along with the OS lock is
    # implementation-defined, and on this silicon EDAD stays set on cores that
    # then read EDSCR perfectly well. Predicting from the proxy refused an
    # unlock that had in fact worked.
    if _read_checked(dap, ap_base, base, r52.EDSCR) is not None:
        return notes

    _, reason = r52.debug_registers_reachable(edprsr)
    raise RunControlError(
        f"{core.name}: EDSCR is still unreadable after unlocking"
        + (f" -- {reason}" if reason else "")
        + f" (EDPRSR 0x{edprsr:08X})."
    )


def enable_halting_debug(dap: Dap, core: Core, *, ap_base: int = AP_APB) -> bool:
    """Set ``EDSCR.HDE`` so a debug event enters Debug state.

    Guide §5.2 item 3: with halting debug disabled the event is routed to
    self-hosted debug handling instead, so the core never stops and the debugger
    sees a halt request that simply does nothing.

    Read-modify-write: EDSCR carries live status in the same word, so writing
    the bit alone would clobber it.
    """
    edscr = _read_checked(dap, ap_base, core.debug_base, r52.EDSCR)
    if edscr is None:
        raise RunControlError(f"{core.name}: EDSCR did not read; run unlock() first.")
    if edscr & r52.EDSCR_HDE:
        return False
    if not _write_checked(dap, ap_base, core.debug_base, r52.EDSCR, edscr | r52.EDSCR_HDE):
        raise RunControlError(f"{core.name}: the EDSCR.HDE write faulted.")
    return True


def _prepare_cti(dap: Dap, core: Core, *, ap_base: int = AP_APB) -> None:
    """Route CTI channels to the halt and restart trigger outputs.

    Armv8 has no "write 1 to halt" bit -- the external debug request is CTI
    trigger output 0 and the restart request is trigger output 1. Each gets its
    own channel so a resume cannot be mistaken for a halt request at the trigger
    matrix.

    ``CTIGATE`` is cleared for both channels deliberately: it controls
    propagation out to the cross-trigger matrix, and leaving it enabled would
    make a single-core halt disturb every other core on the CTM. Cross-core
    halting is opt-in, not a side effect.
    """
    cti = core.cti_base
    lsr = _read_checked(dap, ap_base, cti, cs.LSR)
    if lsr is not None and lsr & cs.LSR_SLI and lsr & cs.LSR_SLK:
        _write_checked(dap, ap_base, cti, cs.LAR, cs.LAR_KEY)

    _write_checked(dap, ap_base, cti, r52.CTICONTROL, r52.CTICONTROL_GLBEN)
    # Read-modify-write the trigger routing. An absolute write here silently
    # removed the cross-halt channel that `core xtrigger on` had routed onto
    # trigger output 0, so a later single-core halt disarmed the group.
    outen0 = _read_checked(dap, ap_base, cti, r52.ctiouten(r52.TRIGOUT_DEBUG_REQUEST)) or 0
    _write_checked(
        dap,
        ap_base,
        cti,
        r52.ctiouten(r52.TRIGOUT_DEBUG_REQUEST),
        outen0 | (1 << r52.CHANNEL_HALT),
    )
    outen1 = _read_checked(dap, ap_base, cti, r52.ctiouten(r52.TRIGOUT_RESTART)) or 0
    _write_checked(
        dap,
        ap_base,
        cti,
        r52.ctiouten(r52.TRIGOUT_RESTART),
        outen1 | (1 << r52.CHANNEL_RESTART),
    )
    gate = _read_checked(dap, ap_base, cti, r52.CTIGATE)
    if gate is not None:
        _write_checked(
            dap,
            ap_base,
            cti,
            r52.CTIGATE,
            gate & ~((1 << r52.CHANNEL_HALT) | (1 << r52.CHANNEL_RESTART)),
        )


def _wait_for(
    dap: Dap, core: Core, want_halted: bool, *, ap_base: int, timeout_s: float
) -> CoreState:
    """Poll until the core reaches the wanted state, or time out.

    Both status registers must agree before this returns success. They are
    independent reports of the same fact, so agreement is what makes the answer
    trustworthy -- and disagreement means one of the two bit maps is wrong,
    which is worth failing on rather than papering over.
    """
    deadline = time.monotonic() + timeout_s
    state = CoreState()
    while True:
        state = read_state(dap, core.debug_base, ap_base=ap_base)
        if state.halted == want_halted:
            return state
        if time.monotonic() > deadline:
            return state
        time.sleep(0.01)


def _acknowledge_halt(
    dap: Dap, core: Core, *, ap_base: int, timeout_s: float = 0.5
) -> None:
    """Drop the debug request and confirm the trigger actually deasserted.

    Guide §5.5, step 1: ``CTIINTACK``, then **poll ``CTITRIGOUTSTATUS`` until
    the debug-request trigger reads deasserted** -- "you must first clear any
    still-asserted Debug request, or the core swallows the restart".
    ``CTIAPPCLEAR`` additionally drops any latched application-set state on the
    halt channel.

    Skipping the acknowledge was a measured bug: the request stayed asserted at
    the trigger matrix, the core was released and re-halted repeatedly, the PC
    moved between CLI commands, and the register-transfer path failed until the
    request was dropped.

    One case legitimately never deasserts: with the cross-halt group armed
    (``xtrigger``/``sync``), this halted core's own DBGTRIGGER is a **level**
    driving channel 2 back into its debug request, straight through the
    acknowledge -- an interaction guide §5.9 never addresses. Polling would
    time out by construction, so the poll is skipped with the group-restart's
    CTIINEN teardown (`suspend_cross_halt`) as the documented owner of
    lowering it.
    """
    _write_checked(
        dap, ap_base, core.cti_base, r52.CTIINTACK, 1 << r52.TRIGOUT_DEBUG_REQUEST
    )
    _write_checked(dap, ap_base, core.cti_base, r52.CTIAPPCLEAR, 1 << r52.CHANNEL_HALT)
    ch2 = 1 << r52.CHANNEL_CROSS_HALT
    inen = _read_checked(
        dap, ap_base, core.cti_base, r52.ctiinen(r52.TRIGIN_CROSS_HALT)
    )
    outen = _read_checked(
        dap, ap_base, core.cti_base, r52.ctiouten(r52.TRIGOUT_DEBUG_REQUEST)
    )
    if inen is not None and outen is not None and inen & ch2 and outen & ch2:
        return  # level-held by the armed group; the restart teardown lowers it
    deadline = time.monotonic() + timeout_s
    while True:
        status = _read_checked(dap, ap_base, core.cti_base, r52.CTITRIGOUTSTATUS)
        if status is None or not status & (1 << r52.TRIGOUT_DEBUG_REQUEST):
            # Unreadable is treated as unconfirmed rather than fatal: the
            # acknowledge writes have been issued either way.
            return
        if time.monotonic() > deadline:
            raise RunControlError(
                f"{core.name}: the debug-request trigger did not deassert after "
                f"CTIINTACK (CTITRIGOUTSTATUS 0x{status:08X}). A restart now would "
                "be swallowed (guide §5.5)."
            )
        time.sleep(0.005)


def halt(
    dap: Dap, core: Core, *, ap_base: int = AP_APB, timeout_s: float = 2.0
) -> CoreState:
    """Stop a core, by pulsing the CTI channel wired to the debug request.

    **This stops the application running on that core**, and on this part the
    core's lockstep partner stops with it in hardware. Peripherals may or may not
    freeze with it -- the MDM-AP IP_DBG controls decide, and this tool does not
    touch them, so a watchdog may keep counting and reset the board (guide §3.5).
    """
    _prepare_cti(dap, core, ap_base=ap_base)
    enable_halting_debug(dap, core, ap_base=ap_base)

    if not _write_checked(
        dap, ap_base, core.cti_base, r52.CTIAPPPULSE, 1 << r52.CHANNEL_HALT
    ):
        raise RunControlError(f"{core.name}: the CTIAPPPULSE write faulted.")

    state = _wait_for(dap, core, True, ap_base=ap_base, timeout_s=timeout_s)
    if state.halted is not True:
        raise RunControlError(
            f"{core.name} did not halt within {timeout_s:.1f}s "
            f"(EDSCR 0x{state.edscr or 0:08X}, EDPRSR 0x{state.edprsr or 0:08X})."
            + (
                " The two status registers disagree, which means a bit map in "
                "data/r52debug.py is wrong rather than that the halt failed."
                if state.status_registers_disagree
                else ""
            )
        )

    # **Acknowledge the request, or the halt is not stable.**
    #
    # CTIAPPPULSE raises the debug request but nothing lowers it. Left
    # asserted, the core is released and re-halted repeatedly: measured, the
    # PC moved between one CLI command and the next, so a step appeared to
    # do nothing and a `bp clear` appeared to resume execution. The lingering
    # request also broke the register-transfer path -- reading the PC
    # straight after a halt failed, and worked immediately after this
    # acknowledgement.
    #
    # With this, a halt survives across sessions: halt in one invocation,
    # reopen in the next, and the PC is where it was left.
    _acknowledge_halt(dap, core, ap_base=ap_base)
    return read_state(dap, core.debug_base, ap_base=ap_base)


def _confirm_restart(
    dap: Dap,
    core: Core,
    *,
    ap_base: int,
    timeout_s: float,
    before_status: int | None,
) -> CoreState:
    """Confirm a requested restart actually happened.

    The architected confirmation is **EDPRSR.SDR** -- Sticky Debug Restart, set
    when the PE restarts and clear-on-read. Guide §5.5: "Confirm restart with
    EDPRSR.SDR (clear-on-read), *not* EDSCR.STATUS -- the architecture is
    explicit about this." (SDR is UNKNOWN across a warm reset, which is why
    EDPRSR.SR is decoded alongside it elsewhere.)

    EDPRSR is read exactly once per iteration, directly -- a ``read_state()``
    call in the same loop would also read EDPRSR and silently consume the
    clear-on-read SDR bit before this loop could see it.

    Success is "the core executed", not "the core is still running". A
    breakpoint on a hot address re-halts before this poll can even see it
    running -- measured: resuming onto a breakpoint in an idle loop came back
    halted with EDSCR.STATUS changed from external-debug-request to breakpoint.
    Treating that as a failed resume reported a working breakpoint as a broken
    resume -- the guide's §5.5 resume-past-a-breakpoint case. SDR settles it
    either way: set means the core left Debug state, whatever state it is in now.
    """
    sdr_seen = False
    deadline = time.monotonic() + timeout_s
    while True:
        edprsr = _read_checked(dap, ap_base, core.debug_base, r52.EDPRSR)
        if edprsr is not None and edprsr & r52.EDPRSR_SDR:
            sdr_seen = True
        edscr = _read_checked(dap, ap_base, core.debug_base, r52.EDSCR)
        state = CoreState(edscr=edscr, edprsr=edprsr)

        if state.halted is False:
            state.restart_confirmed = sdr_seen
            return state
        status = r52.edscr_status(edscr) if edscr is not None else None
        if state.halted is True and (
            sdr_seen or (before_status is not None and status != before_status)
        ):
            state.rehalted = True
            state.restart_confirmed = sdr_seen
            return state
        if time.monotonic() > deadline:
            raise RunControlError(
                f"{core.name} did not restart within {timeout_s:.1f}s "
                f"(EDSCR 0x{edscr or 0:08X}, EDPRSR 0x{edprsr or 0:08X}). EDPRSR.SDR "
                "never set, so the core never left Debug state (guide §5.5)."
            )
        time.sleep(0.01)


def resume(
    dap: Dap,
    core: Core,
    *,
    ap_base: int = AP_APB,
    timeout_s: float = 2.0,
    step_off_breakpoint: bool = True,
) -> CoreState:
    """Let a halted core run again.

    The debug request is acknowledged first -- and its deassertion confirmed
    through CTITRIGOUTSTATUS -- before the restart channel is pulsed. Without
    that the request is still asserted at the trigger matrix and the core
    swallows the restart (guide §5.5). The restart itself is then confirmed
    through EDPRSR.SDR.

    **Stepping off a breakpoint.** A breakpoint matches when the instruction at
    the matched address is committed for execution, and the PE halts *before*
    executing it -- so DLR still points at that instruction. Restarting from
    there re-matches the same comparator before anything retires, and the core
    halts again having made no progress. There is no skip-one bit on the R52:
    the comparator has to be taken out of the way for exactly one instruction.

    Without this, two breakpoints on consecutive lines behave as "the first one
    is hit, continue does nothing visible, the second is never reached" --
    which reads as a lost breakpoint rather than a working one. Reproduced on
    an S32E278 at cdd_btn9970.c:437/:443: continue from :437 re-halted at
    0x323DC472 every time and never reached :443.

    ``step`` already suspends every enabled comparator, single-steps and
    restores them, so one step is all that is needed and the comparators stay
    armed afterwards. Pass ``step_off_breakpoint=False`` for a literal restart
    (what the halt-group paths want, since they resume cores that were stopped
    by the cross-trigger rather than by their own comparator).
    """
    before = read_state(dap, core.debug_base, ap_base=ap_base)
    before_status = r52.edscr_status(before.edscr) if before.edscr is not None else None

    if step_off_breakpoint and before_status == r52.EDSCR_STATUS_BREAKPOINT:
        # Retire the instruction under the comparator, then fall through to the
        # ordinary restart. If the step itself fails, resume anyway rather than
        # leaving the core halted: the caller asked it to run.
        try:
            after_step = step(dap, core, ap_base=ap_base)
        except RunControlError:
            pass
        else:
            before_status = (
                r52.edscr_status(after_step.edscr)
                if after_step.edscr is not None
                else before_status
            )

    _prepare_cti(dap, core, ap_base=ap_base)
    _restart(dap, core, ap_base=ap_base)
    return _confirm_restart(
        dap, core, ap_base=ap_base, timeout_s=timeout_s, before_status=before_status
    )


def suspend_cross_halt(
    dap: Dap, cores: list[Core], *, ap_base: int = AP_APB
) -> list[tuple[int, int]]:
    """Mask the §5.9 halt group's input on these cores; returns what to restore.

    With the group armed, a halted core's DBGTRIGGER is a **level** driving
    channel 2 -- straight through any acknowledge -- so its own restart would
    be swallowed and the ack poll could never see the trigger deassert. The
    guide's §5.9 restart recipe never addresses this; the sequence here is
    beyond-guide, chosen as follows: masking ``CTIINEN0``'s channel-2 bit
    kills both the internal self-loop and the CTM drive whichever of the two
    the silicon implements, which is strictly stronger than closing the gate
    (a gate only stops matrix propagation). Each (cti_base, previous CTIINEN0)
    is returned for ``restore_cross_halt``.
    """
    saved: list[tuple[int, int]] = []
    mask = 1 << r52.CHANNEL_CROSS_HALT
    for c in cores:
        inen = _read_checked(
            dap, ap_base, c.cti_base, r52.ctiinen(r52.TRIGIN_CROSS_HALT)
        )
        if inen is None:
            continue
        saved.append((c.cti_base, inen))
        if inen & mask:
            _write_checked(
                dap,
                ap_base,
                c.cti_base,
                r52.ctiinen(r52.TRIGIN_CROSS_HALT),
                inen & ~mask,
            )
    return saved


def restore_cross_halt(
    dap: Dap, saved: list[tuple[int, int]], *, ap_base: int = AP_APB
) -> None:
    """Re-arm exactly what ``suspend_cross_halt`` masked."""
    for cti_base, inen in saved:
        _write_checked(
            dap, ap_base, cti_base, r52.ctiinen(r52.TRIGIN_CROSS_HALT), inen
        )


def resume_group(
    dap: Dap,
    cores: list[Core],
    *,
    ap_base: int = AP_APB,
    timeout_s: float = 2.0,
    unarm_cross_halt: bool = False,
) -> dict[str, CoreState]:
    """Restart every halted core in ``cores`` on a single pulse (guide §5.9).

    Per core: acknowledge its debug request (and confirm the trigger
    deasserted), route the restart channel, and open that channel's CTIGATE bit
    so it reaches the cross-trigger matrix. One CTIAPPPULSE on any member then
    restarts the whole group. The gate bits are closed again afterwards --
    whatever happens -- so a later single-core resume stays local, which is the
    tool's default posture.

    ``unarm_cross_halt`` is the armed-group (``sync``) teardown: the halted
    members' DBGTRIGGER levels are masked out of channel 2 first (see
    ``suspend_cross_halt`` -- without this the group would swallow its own
    restart) and re-armed in the ``finally``, after the restart gates close.
    A member that instantly re-halts on a hot breakpoint then re-drives the
    channel and stops the group again -- which is exactly what an armed group
    is for.
    """
    halted = [
        c
        for c in cores
        if read_state(dap, c.debug_base, ap_base=ap_base).halted is True
    ]
    if not halted:
        raise RunControlError("no halted core to restart as a group.")

    before: dict[int, int | None] = {}
    mask = 1 << r52.CHANNEL_RESTART
    results: dict[str, CoreState] = {}
    saved_inen: list[tuple[int, int]] = []
    try:
        if unarm_cross_halt:
            saved_inen = suspend_cross_halt(dap, halted, ap_base=ap_base)
        for c in halted:
            state = read_state(dap, c.debug_base, ap_base=ap_base)
            before[c.debug_base] = (
                r52.edscr_status(state.edscr) if state.edscr is not None else None
            )
            _prepare_cti(dap, c, ap_base=ap_base)
            _acknowledge_halt(dap, c, ap_base=ap_base)
            gate = _read_checked(dap, ap_base, c.cti_base, r52.CTIGATE) or 0
            _write_checked(dap, ap_base, c.cti_base, r52.CTIGATE, gate | mask)

        # One pulse on any member; the open gates broadcast it over the CTM.
        if not _write_checked(
            dap, ap_base, halted[0].cti_base, r52.CTIAPPPULSE, mask
        ):
            raise RunControlError(
                f"{halted[0].name}: the group CTIAPPPULSE write faulted."
            )

        for c in halted:
            results[c.name] = _confirm_restart(
                dap,
                c,
                ap_base=ap_base,
                timeout_s=timeout_s,
                before_status=before[c.debug_base],
            )
    finally:
        for c in halted:
            gate = _read_checked(dap, ap_base, c.cti_base, r52.CTIGATE) or 0
            _write_checked(dap, ap_base, c.cti_base, r52.CTIGATE, gate & ~mask)
        restore_cross_halt(dap, saved_inen, ap_base=ap_base)
    return results


def core_rtu_index(core: Core) -> tuple[int, int]:
    """(rtu, core index) from DEVAFF0 -- the identity the latch registers use."""
    aff = core.identity.affinity
    if aff is None:
        raise RunControlError(
            f"{core.name}: DEVAFF0 did not read, so its RTU/core index cannot "
            "be identified for the MDM latch registers."
        )
    return aff[1], aff[2]


def pin_down_edbgreq_hold(
    dap: Dap, core: Core, *, ap_base: int = AP_APB
) -> CoreState:
    """Convert an MDM-EDBGREQ hold into a stable, acknowledged CTI halt.

    Measured 2026-08-04 (the load flow's Finding 6/7 companion): a core held
    only by the MDM EDBGREQ reads EDPRSR.HALTED **without being architecturally
    in Debug state** -- EDSCR.HDE is still clear from cold reset, the EDITR
    register path refuses, and the moment the request is cleared the core
    resumes. The caller must have unlocked the core first (the halt path
    reads EDSCR, which faults under the OS lock -- guide §5.2); this then
    issues the bench-validated CTI ``halt()`` (HDE, Debug-state confirm,
    acknowledge), drops only *this core's* latch, and re-verifies the halt
    survived the drop.
    """
    halt(dap, core, ap_base=ap_base)
    rtu, index = core_rtu_index(core)
    gates.clear_r52_edbgreq(dap, rtu, index)
    state = read_state(dap, core.debug_base, ap_base=ap_base)
    if state.halted is not True:
        raise RunControlError(
            f"{core.name} did not stay halted after the MDM EDBGREQ was "
            f"cleared (EDSCR 0x{state.edscr or 0:08X}, EDPRSR "
            f"0x{state.edprsr or 0:08X})"
        )
    core.state = state
    return state


def reset_halt(
    dap: Dap, core: Core, *, ap_base: int = AP_APB, timeout_s: float = 5.0
) -> CoreState:
    """Functional-reset the system and catch ``core`` before its first instruction.

    The guide's §5.10.2 **hold flow**, chosen over the §5.10.3 SDARSTMASK
    variant because the bench settled it: the mask write lands but the RTU
    debug state is wiped anyway, and a functional reset taken with the mask set
    wedged the boot (measured 2026-08-02; see gates.py). This flow needs no
    mask:

    1. Clear the core's ``SDARSTCTRL.RSTREL`` bit -- hold it in reset.
    2. Assert and release functional reset through ``SDASYSRSTC``. The system
       boots normally; the held core stays in reset.
    3. Arm ``EDECR.RCE`` on the held core. EDECR is exempt from the OS lock and
       readable/writable in this state (guide §5.2 exempt list).
    4. Set ``RSTREL`` back: the core leaves reset, pends the Reset Catch, and
       halts before fetching an instruction. The cold reset re-set its OS lock,
       so the halt is detected through ``EDPRSR`` (exempt) and the lock is
       cleared before EDSCR is read for the catch status.

    **The functional reset is system-wide**: every core reboots, and only the
    chosen core is caught. Comparators on the *other* cores do not survive it
    (§3.6 -- and the masked variant that was meant to preserve them does not
    work here). The catch bit is cleared and the RSTREL bit restored whatever
    happens.
    """
    aff = core.identity.affinity
    if aff is None:
        raise RunControlError(
            f"{core.name}: DEVAFF0 did not read, so the SDARSTCTRL release bit "
            "cannot be identified."
        )
    hold_bit = 1 << gates.rstrel_bit(aff[1], aff[2])

    rstctrl = gates.read_reset_control(dap)
    gates.write_reset_control(dap, rstctrl & ~hold_bit)
    back = gates.read_reset_control(dap)
    if back & hold_bit:
        raise RunControlError(
            f"{core.name}: the SDARSTCTRL hold bit did not land "
            f"(wrote 0x{rstctrl & ~hold_bit:08X}, read back 0x{back:08X})."
        )

    released = False
    try:
        gates.hold_in_functional_reset(dap)
        time.sleep(0.05)
        gates.release_functional_reset(dap)

        # The system is rebooting; wait until the held core's frame answers
        # and reports it in reset.
        deadline = time.monotonic() + 2.0
        while True:
            edprsr = _read_checked(dap, ap_base, core.debug_base, r52.EDPRSR)
            if edprsr is not None and edprsr & r52.EDPRSR_R:
                break
            if time.monotonic() > deadline:
                raise RunControlError(
                    f"{core.name}: not held in reset after the functional reset "
                    f"(EDPRSR 0x{edprsr or 0:08X}) -- the RSTREL hold did not stick."
                )
            time.sleep(0.02)

        # Arm the catch while held. EDECR is in the §5.2 OS-lock-exempt list.
        edecr = _read_checked(dap, ap_base, core.debug_base, r52.EDECR)
        if edecr is None or not _write_checked(
            dap, ap_base, core.debug_base, r52.EDECR, (edecr or 0) | r52.EDECR_RCE
        ):
            raise RunControlError(
                f"{core.name}: the EDECR.RCE write faulted while the core was held "
                "in reset."
            )
        check = _read_checked(dap, ap_base, core.debug_base, r52.EDECR)
        if check is None or not check & r52.EDECR_RCE:
            raise RunControlError(
                f"{core.name}: EDECR.RCE did not stick while held in reset "
                f"(read back 0x{check or 0:08X})."
            )

        # Release the core; the catch fires before its first instruction.
        gates.write_reset_control(dap, gates.read_reset_control(dap) | hold_bit)
        released = True

        # The cold reset re-set the OS lock, so EDSCR is unreadable until it is
        # cleared -- detect the halt through EDPRSR (exempt), then unlock.
        deadline = time.monotonic() + timeout_s
        while True:
            edprsr = _read_checked(dap, ap_base, core.debug_base, r52.EDPRSR)
            if (
                edprsr is not None
                and not edprsr & r52.EDPRSR_R
                and edprsr & r52.EDPRSR_HALTED
            ):
                break
            if time.monotonic() > deadline:
                raise RunControlError(
                    f"{core.name}: no reset catch within {timeout_s:.1f}s of "
                    f"releasing the core (EDPRSR 0x{edprsr or 0:08X})."
                )
            time.sleep(0.01)

        unlock(dap, core, ap_base=ap_base)
        return read_state(dap, core.debug_base, ap_base=ap_base)
    finally:
        now = _read_checked(dap, ap_base, core.debug_base, r52.EDECR)
        if now is not None:
            _write_checked(
                dap,
                ap_base,
                core.debug_base,
                r52.EDECR,
                now & ~(r52.EDECR_RCE | r52.EDECR_OSUCE),
            )
        if not released:
            # Never leave the core held: restore the release bit even when the
            # flow failed partway.
            gates.write_reset_control(dap, gates.read_reset_control(dap) | hold_bit)


# ---------------------------------------------------------------------------
# Core registers, through the EDITR instruction-transfer path
# ---------------------------------------------------------------------------
#
# There is no register file to read. In Debug state the core executes whatever
# instruction is written to EDITR, so a debugger reads R5 by making the core
# execute "move R5 into the debug transfer register" and then reading that.
#
# Every value therefore passes through a scratch core register, which must be
# saved and put back or the halted context is corrupted by the act of
# inspecting it.


def _wait_ite(dap: Dap, core: Core, *, ap_base: int, timeout_s: float) -> int:
    """Wait for EDSCR.ITE -- the core is ready for the next instruction."""
    deadline = time.monotonic() + timeout_s
    while True:
        edscr = _read_checked(dap, ap_base, core.debug_base, r52.EDSCR)
        if edscr is None:
            raise RunControlError(f"{core.name}: EDSCR unreadable during instruction transfer.")
        if edscr & r52.EDSCR_ITE:
            return edscr
        if time.monotonic() > deadline:
            raise RunControlError(
                f"{core.name}: the core never became ready for an instruction "
                f"(EDSCR 0x{edscr:08X}, ITE clear after {timeout_s:.2f}s)."
            )


def execute(
    dap: Dap, core: Core, instruction: int, *, ap_base: int = AP_APB, timeout_s: float = 0.5
) -> None:
    """Make a halted core execute one instruction.

    Raises if the instruction faulted. EDSCR.ERR latches on a fault and stays
    latched, so without clearing it every later transfer would inherit the
    failure and look like it faulted too.
    """
    edscr = _wait_ite(dap, core, ap_base=ap_base, timeout_s=timeout_s)
    if edscr & r52.EDSCR_ERR:
        _write_checked(dap, ap_base, core.debug_base, r52.EDRCR, r52.EDRCR_CSE)

    if not _write_checked(dap, ap_base, core.debug_base, r52.EDITR, instruction):
        raise RunControlError(f"{core.name}: the EDITR write faulted.")

    edscr = _wait_ite(dap, core, ap_base=ap_base, timeout_s=timeout_s)
    if edscr & r52.EDSCR_ERR:
        _write_checked(dap, ap_base, core.debug_base, r52.EDRCR, r52.EDRCR_CSE)
        raise RunControlError(
            f"{core.name}: instruction 0x{instruction:08X} faulted in Debug state "
            f"(EDSCR 0x{edscr:08X}). The encoding is probably wrong -- these were "
            "derived from the MCR/MRC layout, not read from an Arm document."
        )


#: Which instruction encoding a core's EDITR accepts, once calibrated.
#: Measured T32 on this silicon; kept per-core rather than global because it is
#: a property of the execution state, not of the tool.
_INSTRUCTION_SET: dict[int, bool] = {}


def read_reg(
    dap: Dap, core: Core, n: int, *, ap_base: int = AP_APB, timeout_s: float = 0.5
) -> int:
    """Read core register ``n`` of a halted core."""
    t32 = _INSTRUCTION_SET.get(core.debug_base, True)
    execute(
        dap, core, r52.instr_reg_to_dtr(n, t32=t32), ap_base=ap_base, timeout_s=timeout_s
    )
    value = _read_checked(dap, ap_base, core.debug_base, r52.DBGDTRTX)
    if value is None:
        raise RunControlError(f"{core.name}: DBGDTRTX did not read after moving r{n} into it.")
    return value


def write_reg(
    dap: Dap, core: Core, n: int, value: int, *, ap_base: int = AP_APB, timeout_s: float = 0.5
) -> None:
    """Write core register ``n`` of a halted core."""
    t32 = _INSTRUCTION_SET.get(core.debug_base, True)
    if not _write_checked(dap, ap_base, core.debug_base, r52.DBGDTRRX, value):
        raise RunControlError(f"{core.name}: the DBGDTRRX write faulted.")
    execute(
        dap, core, r52.instr_dtr_to_reg(n, t32=t32), ap_base=ap_base, timeout_s=timeout_s
    )


def instruction_set(core: Core) -> str:
    return "T32" if _INSTRUCTION_SET.get(core.debug_base, True) else "A32"


def verify_register_path(dap: Dap, core: Core, *, ap_base: int = AP_APB) -> bool:
    """Round-trip a known pattern through the scratch register.

    **Nothing may report a register value until this passes.** The instruction
    encodings this path depends on were derived from the MCR/MRC layout rather
    than read from an Arm document, and a wrong encoding does not necessarily
    fault -- it can execute something else and leave a plausible-looking number
    in the transfer register. A distinctive asymmetric pattern surviving a
    write-then-read is what rules that out.

    It also *calibrates*: T32 and A32 are both tried, and whichever round-trips
    is remembered for this core. On the bench part the A32 form sets EDSCR.ERR
    and the T32 form works, but that is a property of the core's execution state
    rather than of the tool, so it is measured per core instead of hardcoded.
    Same discipline as ``calibrate_tdo_order()`` for raw JTAG: prove the channel
    against a known answer, or refuse to use it.

    Restores the scratch register whichever way it goes -- an oracle that
    corrupted the context it was checking would be worse than no oracle.
    """
    for t32 in (True, False):
        _INSTRUCTION_SET[core.debug_base] = t32
        try:
            saved = read_reg(dap, core, r52.SCRATCH_REG, ap_base=ap_base)
            try:
                write_reg(dap, core, r52.SCRATCH_REG, r52.DTR_PROBE_PATTERN, ap_base=ap_base)
                got = read_reg(dap, core, r52.SCRATCH_REG, ap_base=ap_base)
            finally:
                write_reg(dap, core, r52.SCRATCH_REG, saved, ap_base=ap_base)
        except RunControlError:
            continue
        if got == r52.DTR_PROBE_PATTERN:
            return True
    return False


@dataclass
class Registers:
    """A halted core's registers, and how much of them to believe."""

    values: dict[str, int] = field(default_factory=dict)
    #: The PC, from DLR. Cortex-R52 TRM §4.2.31: "the value of the PC in
    #: Halting Debug state". This is the real thing, not a sample.
    pc: int | None = None
    pc_sampled: int | None = None
    path_verified: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        dspsr = self.values.get("dspsr")
        return {
            "path_verified": self.path_verified,
            "registers": {k: f"0x{v:08X}" for k, v in self.values.items()},
            "pc": f"0x{self.pc:08X}" if self.pc is not None else None,
            "dspsr_decoded": r52.decode_psr(dspsr) if dspsr is not None else None,
            "pc_sampled": f"0x{self.pc_sampled:08X}" if self.pc_sampled is not None else None,
            "notes": self.notes,
        }


def read_registers(dap: Dap, core: Core, *, ap_base: int = AP_APB) -> Registers:
    """Read r0-r14 and CPSR of a halted core, plus a sampled PC.

    The scratch register is read first and written back last, so the value
    reported for it is the one the core had rather than the one this function
    left behind.
    """
    out = Registers()
    state = read_state(dap, core.debug_base, ap_base=ap_base)
    if state.halted is not True:
        raise RunControlError(
            f"{core.name} is not halted, so its registers cannot be read. In Debug "
            "state the core executes instructions written to EDITR; while running it "
            "does not."
        )

    out.path_verified = verify_register_path(dap, core, ap_base=ap_base)
    if not out.path_verified:
        out.notes.append(
            "the register-transfer round trip FAILED under both T32 and A32 encodings: "
            "a known pattern written to the scratch register did not read back. The "
            "instruction encodings in data/r52debug.py are wrong. No register value "
            "can be trusted."
        )
        return out
    out.notes.append(f"register transfers use {instruction_set(core)} encodings on this core")

    for n in range(15):
        out.values[f"r{n}"] = read_reg(dap, core, n, ap_base=ap_base)

    # DLR and DSPSR both need a scratch register to land in, so save once and
    # restore once around both.
    t32 = _INSTRUCTION_SET.get(core.debug_base, True)
    saved = read_reg(dap, core, r52.SCRATCH_REG, ap_base=ap_base)
    try:
        execute(
            dap, core, r52.instr_dlr_to_reg(r52.SCRATCH_REG, t32=t32), ap_base=ap_base
        )
        out.pc = read_reg(dap, core, r52.SCRATCH_REG, ap_base=ap_base)
    except RunControlError as exc:
        out.notes.append(f"PC (DLR) not read: {exc}")
    try:
        execute(
            dap, core, r52.instr_dspsr_to_reg(r52.SCRATCH_REG, t32=t32), ap_base=ap_base
        )
        out.values["dspsr"] = read_reg(dap, core, r52.SCRATCH_REG, ap_base=ap_base)
    except RunControlError as exc:
        out.notes.append(f"DSPSR not read: {exc}")
    finally:
        write_reg(dap, core, r52.SCRATCH_REG, saved, ap_base=ap_base)
        out.values[f"r{r52.SCRATCH_REG}"] = saved

    # PC comes from the sample register rather than the register file. In Debug
    # state r15 does not read as the PC, and the architecturally correct source
    # (DLR) needs an instruction encoding this tool has not verified. EDPCSR is
    # documented but implementation-defined while halted, so it is reported
    # separately and labelled, never mixed in with the values above.
    # EDPCSR is reported only as a cross-check, and only when it is valid. It is
    # a profiling aid: measured 0xFFFFFFFF -- the architectural "sample not
    # valid" -- on every halted core here. The PC above comes from DLR, which is
    # the architected debug-state source.
    sampled = _read_checked(dap, ap_base, core.debug_base, r52.EDPCSR_LO)
    if sampled is not None and sampled != r52.EDPCSR_INVALID:
        out.pc_sampled = sampled
        if out.pc is not None and sampled != out.pc:
            out.notes.append(
                f"EDPCSR samples 0x{sampled:08X} while DLR gives 0x{out.pc:08X}. DLR is "
                "the debug-state PC; EDPCSR is a profiling sample and may lag."
            )
    if out.pc is None:
        out.notes.append(
            "PC unavailable: the DLR read did not succeed and EDPCSR is not valid "
            "while halted. Where a core stopped is still knowable from the breakpoint "
            "that stopped it -- see `core status`."
        )
    return out


# ---------------------------------------------------------------------------
# Hardware breakpoints and watchpoints
# ---------------------------------------------------------------------------


def comparator_counts(core: Core) -> tuple[int, int]:
    """(breakpoints, watchpoints) this core implements, from EDDFR.

    Read, never assumed. The guide says eight; a wrong EDDFR offset would also
    produce a number, and programming comparator 7 on a core that implements
    four writes into a reserved region.
    """
    counts = core.identity.comparators
    if counts is None or not counts["plausible"]:
        raise RunControlError(
            f"{core.name}: EDDFR does not give a believable comparator count "
            f"({counts['raw'] if counts else 'unread'}), so comparator indices cannot "
            "be trusted."
        )
    return int(counts["breakpoints"]), int(counts["watchpoints"])


def list_breakpoints(dap: Dap, core: Core, *, ap_base: int = AP_APB) -> list[dict[str, object]]:
    """Every breakpoint comparator, programmed or not."""
    total, _ = comparator_counts(core)
    out: list[dict[str, object]] = []
    for n in range(total):
        value = _read_checked(dap, ap_base, core.debug_base, r52.dbgbvr(n))
        control = _read_checked(dap, ap_base, core.debug_base, r52.dbgbcr(n))
        if value is None or control is None:
            out.append({"index": n, "error": "comparator registers did not read"})
            continue
        out.append({"index": n, **r52.decode_breakpoint(value, control)})
    return out


def list_watchpoints(dap: Dap, core: Core, *, ap_base: int = AP_APB) -> list[dict[str, object]]:
    _, total = comparator_counts(core)
    out: list[dict[str, object]] = []
    for n in range(total):
        value = _read_checked(dap, ap_base, core.debug_base, r52.dbgwvr(n))
        control = _read_checked(dap, ap_base, core.debug_base, r52.dbgwcr(n))
        if value is None or control is None:
            out.append({"index": n, "error": "comparator registers did not read"})
            continue
        out.append({"index": n, **r52.decode_watchpoint(value, control)})
    return out


def _free_slot(entries: list[dict[str, object]], kind: str) -> int:
    for entry in entries:
        if not entry.get("error") and not entry.get("enabled"):
            return int(entry["index"])
    raise RunControlError(
        f"all {len(entries)} {kind} comparators on this core are in use. Clear one "
        f"first -- `{kind[:2]} list` shows what is programmed."
    )


def set_breakpoint(
    dap: Dap,
    core: Core,
    address: int,
    *,
    index: int | None = None,
    thumb: bool = True,
    ap_base: int = AP_APB,
) -> dict[str, object]:
    """Program an instruction breakpoint, and verify it landed.

    Written in the order the breakpoints guide §5.7 gives: disable, set the
    value, set the control with enable last. Changing a live comparator's value
    while it is enabled can produce a match against a half-updated address.
    """
    total, _ = comparator_counts(core)
    if index is None:
        index = _free_slot(list_breakpoints(dap, core, ap_base=ap_base), "breakpoint")
    if not 0 <= index < total:
        raise RunControlError(
            f"{core.name} implements {total} breakpoint comparators, so {index} is not "
            "one of them."
        )

    # **Arming a comparator does nothing unless halting debug is enabled.**
    # With EDSCR.HDE clear the debug event is routed to self-hosted handling
    # instead, so the breakpoint reads back perfectly and simply never fires
    # (guide §5.2 item 3: HDE is a prerequisite for breakpoint/watchpoint
    # halting). Measured: a comparator armed on a freshly unlocked core never
    # fired, because only `halt()` had been setting HDE.
    enable_halting_debug(dap, core, ap_base=ap_base)

    value, control = r52.encode_breakpoint(address, thumb=thumb)
    ok = _write_checked(dap, ap_base, core.debug_base, r52.dbgbcr(index), 0)
    ok &= _write_checked(dap, ap_base, core.debug_base, r52.dbgbvr(index), value)
    ok &= _write_checked(dap, ap_base, core.debug_base, r52.dbgbcr(index), control)
    if not ok:
        raise RunControlError(f"{core.name}: a breakpoint comparator write faulted.")

    back_value = _read_checked(dap, ap_base, core.debug_base, r52.dbgbvr(index))
    back_control = _read_checked(dap, ap_base, core.debug_base, r52.dbgbcr(index))
    if back_value != value or back_control != control:
        raise RunControlError(
            f"{core.name}: breakpoint {index} did not take. Wrote DBGBVR 0x{value:08X} / "
            f"DBGBCR 0x{control:08X}, read back 0x{back_value or 0:08X} / "
            f"0x{back_control or 0:08X}. Comparator writes reading back as zero is the "
            "classic sign that invasive debug is not actually authorised "
            "(guide §3.3, §9 pitfall 4)."
        )
    return {"index": index, **r52.decode_breakpoint(value, control)}


def clear_breakpoint(dap: Dap, core: Core, index: int, *, ap_base: int = AP_APB) -> None:
    """Disable a comparator and blank it.

    The guide §5.7 recommends clearing the value too, not just the enable bit,
    before handing the target back -- a disabled comparator still holding an
    address is confusing to whoever looks next.
    """
    _write_checked(dap, ap_base, core.debug_base, r52.dbgbcr(index), 0)
    _write_checked(dap, ap_base, core.debug_base, r52.dbgbvr(index), 0)


def set_watchpoint(
    dap: Dap,
    core: Core,
    address: int,
    *,
    size: int = 4,
    on: str = "both",
    index: int | None = None,
    ap_base: int = AP_APB,
) -> dict[str, object]:
    _, total = comparator_counts(core)
    if index is None:
        index = _free_slot(list_watchpoints(dap, core, ap_base=ap_base), "watchpoint")
    if not 0 <= index < total:
        raise RunControlError(
            f"{core.name} implements {total} watchpoint comparators, so {index} is not "
            "one of them."
        )

    # Same prerequisite as set_breakpoint (guide §5.2 item 3): with EDSCR.HDE
    # clear the watchpoint event routes to self-hosted handling -- the
    # comparator arms, reads back, and never halts anything. The breakpoint
    # path learned this the measured way; the watchpoint path gets it here
    # rather than at the bench.
    enable_halting_debug(dap, core, ap_base=ap_base)

    value, control = r52.encode_watchpoint(address, size=size, on=on)
    ok = _write_checked(dap, ap_base, core.debug_base, r52.dbgwcr(index), 0)
    ok &= _write_checked(dap, ap_base, core.debug_base, r52.dbgwvr(index), value)
    ok &= _write_checked(dap, ap_base, core.debug_base, r52.dbgwcr(index), control)
    if not ok:
        raise RunControlError(f"{core.name}: a watchpoint comparator write faulted.")

    back_value = _read_checked(dap, ap_base, core.debug_base, r52.dbgwvr(index))
    back_control = _read_checked(dap, ap_base, core.debug_base, r52.dbgwcr(index))
    if back_value != value or back_control != control:
        raise RunControlError(
            f"{core.name}: watchpoint {index} did not take (read back "
            f"0x{back_value or 0:08X} / 0x{back_control or 0:08X})."
        )
    return {"index": index, **r52.decode_watchpoint(value, control)}


def clear_watchpoint(dap: Dap, core: Core, index: int, *, ap_base: int = AP_APB) -> None:
    _write_checked(dap, ap_base, core.debug_base, r52.dbgwcr(index), 0)
    _write_checked(dap, ap_base, core.debug_base, r52.dbgwvr(index), 0)


def sanitize_comparators(dap: Dap, core: Core, *, ap_base: int = AP_APB) -> int:
    """Blank every breakpoint and watchpoint comparator on this core.

    Guide §5.7: "Every DBGBVR/DBGBCR/DBGWVR/DBGWCR field is UNKNOWN after cold
    reset -- including E! Initialize all comparators you use *and explicitly
    write E = 0 on every unused one*." A garbage comparator that happens to
    read as enabled can fire at an arbitrary address; nothing else ever clears
    it, because the free-slot search skips anything that reads as armed.

    Returns how many slots read as armed before sanitising, so the caller can
    say what was discarded rather than doing it silently.
    """
    bp_total, wp_total = comparator_counts(core)
    armed = 0
    for n in range(bp_total):
        control = _read_checked(dap, ap_base, core.debug_base, r52.dbgbcr(n))
        if control is not None and control & r52.DBGBCR_E:
            armed += 1
        _write_checked(dap, ap_base, core.debug_base, r52.dbgbcr(n), 0)
        _write_checked(dap, ap_base, core.debug_base, r52.dbgbvr(n), 0)
    for n in range(wp_total):
        control = _read_checked(dap, ap_base, core.debug_base, r52.dbgwcr(n))
        if control is not None and control & r52.DBGWCR_E:
            armed += 1
        _write_checked(dap, ap_base, core.debug_base, r52.dbgwcr(n), 0)
        _write_checked(dap, ap_base, core.debug_base, r52.dbgwvr(n), 0)
    return armed


def _restart(dap: Dap, core: Core, *, ap_base: int) -> None:
    """Acknowledge the halt request, then pulse the CTI restart channel.

    Shared by resume() and step(). The acknowledge polls CTITRIGOUTSTATUS until
    the debug-request trigger deasserts -- guide §5.5, "or the core swallows
    the restart".
    """
    _acknowledge_halt(dap, core, ap_base=ap_base)
    if not _write_checked(
        dap, ap_base, core.cti_base, r52.CTIAPPPULSE, 1 << r52.CHANNEL_RESTART
    ):
        raise RunControlError(f"{core.name}: the CTIAPPPULSE write faulted.")


def read_pc(dap: Dap, core: Core, *, ap_base: int = AP_APB) -> int:
    """The PC of a halted core, from DLR."""
    t32 = _INSTRUCTION_SET.get(core.debug_base, True)
    saved = read_reg(dap, core, r52.SCRATCH_REG, ap_base=ap_base)
    try:
        execute(dap, core, r52.instr_dlr_to_reg(r52.SCRATCH_REG, t32=t32), ap_base=ap_base)
        return read_reg(dap, core, r52.SCRATCH_REG, ap_base=ap_base)
    finally:
        write_reg(dap, core, r52.SCRATCH_REG, saved, ap_base=ap_base)


def read_dspsr(dap: Dap, core: Core, *, ap_base: int = AP_APB) -> int:
    """The saved PSR of a halted core, from DSPSR."""
    t32 = _INSTRUCTION_SET.get(core.debug_base, True)
    saved = read_reg(dap, core, r52.SCRATCH_REG, ap_base=ap_base)
    try:
        execute(
            dap, core, r52.instr_dspsr_to_reg(r52.SCRATCH_REG, t32=t32), ap_base=ap_base
        )
        return read_reg(dap, core, r52.SCRATCH_REG, ap_base=ap_base)
    finally:
        write_reg(dap, core, r52.SCRATCH_REG, saved, ap_base=ap_base)


def _write_debug_sysreg(
    dap: Dap, core: Core, builder, value: int, *, ap_base: int
) -> None:
    """Move ``value`` into a p15 debug register through the scratch register."""
    t32 = _INSTRUCTION_SET.get(core.debug_base, True)
    saved = read_reg(dap, core, r52.SCRATCH_REG, ap_base=ap_base)
    try:
        write_reg(dap, core, r52.SCRATCH_REG, value, ap_base=ap_base)
        execute(dap, core, builder(r52.SCRATCH_REG, t32=t32), ap_base=ap_base)
    finally:
        write_reg(dap, core, r52.SCRATCH_REG, saved, ap_base=ap_base)


def write_pc(dap: Dap, core: Core, value: int, *, ap_base: int = AP_APB) -> None:
    """Set the address the core will restart from, by writing DLR.

    Guide §5.5: "On exit the core restarts from DLR; if you changed the PC,
    write DLR first." The Thumb state bit is not part of the address -- which
    instruction set the core resumes in comes from DSPSR.T, not from DLR[0].
    """
    _write_debug_sysreg(dap, core, r52.instr_reg_to_dlr, value & ~1, ap_base=ap_base)


def write_dspsr(dap: Dap, core: Core, value: int, *, ap_base: int = AP_APB) -> None:
    """Set the PSR the core will restore on restart, by writing DSPSR."""
    _write_debug_sysreg(dap, core, r52.instr_reg_to_dspsr, value, ap_base=ap_base)


def step(
    dap: Dap,
    core: Core,
    *,
    mask_interrupts: bool = True,
    ap_base: int = AP_APB,
    timeout_s: float = 2.0,
) -> CoreState:
    """Execute exactly one instruction on a halted core.

    Armv8 halting step: set ``EDECR.SS``, restart, and the core executes one
    instruction and re-enters Debug state with a halting-step status. The bit is
    cleared afterwards whatever happens -- leaving it set would turn the next
    ordinary resume into another single step, which looks like a resume that
    does not work.

    **Interrupts are masked by default**, via ``EDSCR.INTdis``. Without that, a
    step on a system with a periodic tick executes one instruction, immediately
    takes the pending interrupt, and stops in the vector table -- so stepping
    appears to teleport into exception handlers and never advance through the
    code under test. Both the step bit and the interrupt mask are restored to
    what they were, so a later resume runs with interrupts on.
    """
    state = read_state(dap, core.debug_base, ap_base=ap_base)
    if state.halted is not True:
        raise RunControlError(f"{core.name} is not halted, so it cannot be stepped.")

    edecr = _read_checked(dap, ap_base, core.debug_base, r52.EDECR)
    if edecr is None:
        raise RunControlError(f"{core.name}: EDECR did not read.")
    edscr = _read_checked(dap, ap_base, core.debug_base, r52.EDSCR)
    if edscr is None:
        raise RunControlError(f"{core.name}: EDSCR did not read.")

    # **Suspend every armed breakpoint for the duration of the step.**
    #
    # A comparator armed at the current PC re-triggers the instant the core is
    # restarted, before any instruction executes: the core comes back halted
    # with a breakpoint status and the PC unmoved. Measured exactly that --
    # `step` reported 0x321D2EE6 -> 0x321D2EE6. The breakpoints guide describes
    # the remedy in §5.5: disable the comparator, step, re-enable it.
    #
    # All of them are suspended rather than just the one matching the PC,
    # because finding the match needs the PC, which needs the register path,
    # which may not be verified. A single step executes one instruction, so
    # nothing else can be missed in the meantime.
    suspended: list[tuple[int, int]] = []
    try:
        for n in range(comparator_counts(core)[0]):
            control = _read_checked(dap, ap_base, core.debug_base, r52.dbgbcr(n))
            if control is not None and control & r52.DBGBCR_E:
                suspended.append((n, control))
                _write_checked(
                    dap, ap_base, core.debug_base, r52.dbgbcr(n), control & ~r52.DBGBCR_E
                )
    except RunControlError:
        pass  # no believable comparator count; nothing to suspend

    if mask_interrupts:
        _write_checked(
            dap,
            ap_base,
            core.debug_base,
            r52.EDSCR,
            r52.edscr_with_intdis(edscr, True),
        )
    if not _write_checked(
        dap, ap_base, core.debug_base, r52.EDECR, edecr | r52.EDECR_SS
    ):
        raise RunControlError(f"{core.name}: the EDECR.SS write faulted.")
    try:
        _prepare_cti(dap, core, ap_base=ap_base)
        _restart(dap, core, ap_base=ap_base)

        deadline = time.monotonic() + timeout_s
        while True:
            state = read_state(dap, core.debug_base, ap_base=ap_base)
            status = r52.edscr_status(state.edscr) if state.edscr is not None else None
            if state.halted and status in r52.STEP_COMPLETE_STATUSES:
                return state
            if state.halted and status == r52.EDSCR_STATUS_BREAKPOINT:
                # A breakpoint on the instruction the step landed *on*. The step
                # happened; this is a legitimate stop. Distinguishable from the
                # re-trigger case above only because armed comparators are
                # suspended for the duration -- without that suspension this
                # branch accepted "nothing moved" as a successful step.
                return state
            if (
                state.halted
                and status is not None
                and status != r52.EDSCR_STATUS_EXTERNAL_DEBUG_REQUEST
            ):
                return state
            if time.monotonic() > deadline:
                raise RunControlError(
                    f"{core.name}: the step did not complete within {timeout_s:.1f}s "
                    f"(EDSCR 0x{state.edscr or 0:08X})."
                )
            time.sleep(0.005)
    finally:
        _write_checked(dap, ap_base, core.debug_base, r52.EDECR, edecr & ~r52.EDECR_SS)
        for n, control in suspended:
            # Put them back even if the step failed, or the session silently
            # loses the breakpoints the operator armed.
            _write_checked(dap, ap_base, core.debug_base, r52.dbgbcr(n), control)
        if mask_interrupts:
            # Put INTdis back. Leaving interrupts masked would make the next
            # ordinary resume run with them off, and the system would quietly
            # stop servicing anything.
            now = _read_checked(dap, ap_base, core.debug_base, r52.EDSCR)
            if now is not None:
                _write_checked(
                    dap,
                    ap_base,
                    core.debug_base,
                    r52.EDSCR,
                    r52.edscr_with_intdis(now, bool(edscr & r52.EDSCR_INTDIS_MASK)),
                )


def read_instruction(
    dap: Dap, core: Core, address: int, *, thumb: bool, ap_base: int = AP_AXI
) -> a32.Instruction:
    """Read and decode the instruction at ``address``.

    Fetched through the **AXI-AP**, not the APB-AP: instructions live in system
    memory, and the APB-AP only reaches debug components.
    """
    address &= ~1  # the Thumb state bit is not part of the address
    aligned = address & ~3

    # The tool's own bus policy applies on this path too. RM Table 957 forbids
    # AXI-AP traffic while functional reset is asserted (the guide's pitfall
    # #3: recoverable only by a destructive reset), and `mem --ap axi` refuses
    # in that state -- so the step-over instruction fetch must refuse the same
    # way rather than bypass the guard.
    memaccess.preflight(dap, ap_base, write=False)

    word = dap.mem_read_checked(ap_base, aligned, size=32)
    if word is None:
        raise RunControlError(
            f"{core.name}: could not read the instruction at 0x{address:08X} through the "
            "AXI-AP. Step-over needs to see the instruction to know whether it is a call "
            "and how long it is."
        )

    if not thumb:
        return a32.decode_a32(address, word)

    # T32 instructions are halfword-aligned, so one can start in the *high* half
    # of a word -- and then its second halfword is the low half of the *next*
    # word. Reading a 32-bit word at the instruction address and slicing it as
    # (low, high) is wrong for those, and wrong in a way that only shows up on
    # odd halfwords: measured on silicon, an instruction at 0x3241F522 was
    # decoded 2 bytes wide when stepping proved it 4.
    if address & 0b10:
        first = (word >> 16) & 0xFFFF
        following = dap.mem_read_checked(ap_base, aligned + 4, size=32)
        second = (following or 0) & 0xFFFF
    else:
        first = word & 0xFFFF
        second = (word >> 16) & 0xFFFF
    return a32.decode_t32(address, first, second)


def step_over(
    dap: Dap,
    core: Core,
    *,
    mask_interrupts: bool = True,
    ap_base: int = AP_APB,
    timeout_s: float = 5.0,
) -> tuple[CoreState, dict[str, object]]:
    """Step one instruction, but run a call to completion.

    Reads the instruction at the PC. If it is a branch-with-link, a temporary
    breakpoint goes at the return address and the core is resumed; otherwise
    this is an ordinary single step. The temporary breakpoint is always removed,
    including when the wait times out -- a leaked comparator would consume one of
    the eight and fire later at a confusing moment.
    """
    pc = read_pc(dap, core, ap_base=ap_base)
    state = read_state(dap, core.debug_base, ap_base=ap_base)
    thumb = True
    if state.edscr is not None:
        # DSPSR.T says which instruction set the core was in when it stopped.
        t32 = _INSTRUCTION_SET.get(core.debug_base, True)
        saved = read_reg(dap, core, r52.SCRATCH_REG, ap_base=ap_base)
        try:
            execute(
                dap, core, r52.instr_dspsr_to_reg(r52.SCRATCH_REG, t32=t32), ap_base=ap_base
            )
            thumb = bool(read_reg(dap, core, r52.SCRATCH_REG, ap_base=ap_base) & r52.PSR_T)
        except RunControlError:
            pass
        finally:
            write_reg(dap, core, r52.SCRATCH_REG, saved, ap_base=ap_base)

    instruction = read_instruction(dap, core, pc, thumb=thumb)
    info: dict[str, object] = {
        "pc": f"0x{pc:08X}",
        "width": instruction.width,
        "is_call": instruction.is_call,
        "mnemonic": instruction.mnemonic or "(not a call)",
    }

    if not instruction.is_call:
        info["action"] = "single step"
        return step(
            dap, core, mask_interrupts=mask_interrupts, ap_base=ap_base
        ), info

    ret = instruction.next_address
    info["action"] = f"ran the call, breakpoint at the return address 0x{ret:08X}"
    info["return_address"] = f"0x{ret:08X}"

    index = _free_slot(list_breakpoints(dap, core, ap_base=ap_base), "breakpoint")
    set_breakpoint(dap, core, ret, index=index, thumb=thumb, ap_base=ap_base)
    try:
        _prepare_cti(dap, core, ap_base=ap_base)
        _restart(dap, core, ap_base=ap_base)
        deadline = time.monotonic() + timeout_s
        while True:
            state = read_state(dap, core.debug_base, ap_base=ap_base)
            if (
                state.halted
                and r52.edscr_status(state.edscr or 0) == r52.EDSCR_STATUS_BREAKPOINT
            ):
                return state, info
            if time.monotonic() > deadline:
                raise RunControlError(
                    f"{core.name}: the call did not return within {timeout_s:.1f}s. The "
                    "core is left halted or running as it was; the temporary breakpoint "
                    "has been removed."
                )
            time.sleep(0.005)
    finally:
        clear_breakpoint(dap, core, index, ap_base=ap_base)


def set_cross_trigger(
    dap: Dap, cores: list[Core], enable: bool, *, ap_base: int = AP_APB
) -> list[str]:
    """Make a halt on any one core halt all the others, or stop doing so.

    Each core's CTI trigger input 0 asserts when that core enters Debug state.
    Routing it onto a channel shared across the cross-trigger matrix, and routing
    that channel to every core's trigger output 0 (the debug request), turns one
    core stopping into all of them stopping.

    ``CTIGATE`` is the part that matters: it gates propagation out to the matrix,
    and is deliberately *cleared* for the halt and restart channels during normal
    single-core operation so an ordinary halt does not disturb anything else.
    Enabling cross-trigger is opting into the opposite.

    Without this, the guide §5.9 case applies -- the other cores keep running and
    corrupt the state you stopped to inspect.
    """
    notes: list[str] = []
    mask = 1 << r52.CHANNEL_CROSS_HALT
    for core in cores:
        cti = core.cti_base
        _write_checked(dap, ap_base, cti, r52.CTICONTROL, r52.CTICONTROL_GLBEN)

        inen = _read_checked(dap, ap_base, cti, r52.ctiinen(r52.TRIGIN_CROSS_HALT)) or 0
        outen = _read_checked(dap, ap_base, cti, r52.ctiouten(r52.TRIGOUT_DEBUG_REQUEST)) or 0
        gate = _read_checked(dap, ap_base, cti, r52.CTIGATE) or 0

        if enable:
            inen |= mask
            outen |= mask
            gate |= mask
        else:
            inen &= ~mask
            outen &= ~mask
            gate &= ~mask

        _write_checked(dap, ap_base, cti, r52.ctiinen(r52.TRIGIN_CROSS_HALT), inen)
        _write_checked(dap, ap_base, cti, r52.ctiouten(r52.TRIGOUT_DEBUG_REQUEST), outen)
        _write_checked(dap, ap_base, cti, r52.CTIGATE, gate)
        notes.append(
            f"{core.name}: cross-trigger {'enabled' if enable else 'disabled'} "
            f"(CTIGATE 0x{gate:08X})"
        )
    return notes


def sample_pc(
    dap: Dap, core: Core, count: int = 200, *, ap_base: int = AP_APB
) -> dict[str, object]:
    """Sample where a **running** core is executing, without stopping it.

    EDPCSR is the PC Sample register -- a profiling aid, not a debug-state PC.
    Read while halted it returns 0xFFFFFFFF, which is what made it look useless;
    read while the core runs it works perfectly. Measured: 400 samples of each
    RTU0 core, zero invalid.

    This is the safe way to find an address worth breakpointing. Halting a core
    repeatedly to look for a hot spot risks the deadlock that stops the
    application; this touches nothing.
    """
    from collections import Counter

    hist: Counter = Counter()
    invalid = 0
    for _ in range(count):
        value = _read_checked(dap, ap_base, core.debug_base, r52.EDPCSR_LO)
        if value is None or value == r52.EDPCSR_INVALID:
            invalid += 1
        else:
            hist[value] += 1

    # Say *why* nothing was captured. An all-invalid result has several very
    # different causes and "0 distinct addresses" distinguishes none of them.
    note = (
        "EDPCSR is only valid while the core is running; a halted core samples "
        "0xFFFFFFFF. These are statistical samples, not a trace."
    )
    if invalid == count:
        state = read_state(dap, core.debug_base, ap_base=ap_base)
        if state.unreachable_reason:
            why = state.unreachable_reason
        elif state.halted:
            why = "the core is halted, and EDPCSR does not work in Debug state"
        else:
            why = "the core is not executing, or EDPCSR is not implemented here"
        note = f"every sample was invalid, so no PC was captured: {why}."

    return {
        "core": core.name,
        "samples": count,
        "invalid": invalid,
        "distinct": len(hist),
        "top": [
            {"address": f"0x{addr:08X}", "hits": n, "percent": round(100 * n / count, 1)}
            for addr, n in hist.most_common(16)
        ],
        "note": note,
    }


def halt_location(dap: Dap, core: Core, *, ap_base: int = AP_APB) -> dict[str, object]:
    """Where a halted core stopped, derived from *why* it stopped.

    The PC is not readable on this part -- see ``read_registers`` -- so this
    reconstructs the answer instead of reporting nothing. When ``EDSCR.STATUS``
    says the core stopped on a breakpoint, the address is whichever comparator
    is armed; with exactly one armed it is exact, and with several it is
    narrowed to that set rather than guessed at.

    This is weaker evidence than reading a PC register and is labelled as such.
    It is also the answer that matters in practice: you set the breakpoint, so
    you already know the address you care about.
    """
    state = read_state(dap, core.debug_base, ap_base=ap_base)
    if state.edscr is None:
        return {"known": False, "reason": state.unreachable_reason or "EDSCR unreadable"}
    if not r52.edscr_halted(state.edscr):
        return {"known": False, "reason": "the core is running"}

    status = r52.edscr_status(state.edscr)
    out: dict[str, object] = {
        "known": False,
        "halt_reason": r52.edscr_status_name(state.edscr),
    }

    if status == r52.EDSCR_STATUS_BREAKPOINT:
        armed = [
            entry
            for entry in list_breakpoints(dap, core, ap_base=ap_base)
            if entry.get("enabled")
        ]
        out["armed_breakpoints"] = armed
        if len(armed) == 1:
            out["known"] = True
            out["address"] = armed[0]["address"]
            out["source"] = "the single armed breakpoint comparator"
        elif armed:
            out["candidates"] = [e["address"] for e in armed]
            out["source"] = (
                f"{len(armed)} breakpoints are armed, so the stop is one of these. "
                "Arm one at a time to make it exact."
            )
        else:
            out["source"] = (
                "EDSCR says breakpoint but no comparator is armed -- the breakpoint "
                "may have been cleared since, or this was a software breakpoint."
            )
    elif status == r52.EDSCR_STATUS_WATCHPOINT:
        out["armed_watchpoints"] = [
            e for e in list_watchpoints(dap, core, ap_base=ap_base) if e.get("enabled")
        ]
        # Guide §5.3: "On a watchpoint halt, the data address is in EDWAR" --
        # the high word is UNKNOWN and ignored. §5.8 adds that the reported
        # address may be rounded within a naturally-aligned block (on the R52,
        # no lower than the watched address rounded down to 16 bytes).
        edwar = _read_checked(dap, ap_base, core.debug_base, r52.EDWAR_LO)
        if edwar is not None:
            out["known"] = True
            out["address"] = f"0x{edwar:08X}"
            out["data_address"] = f"0x{edwar:08X}"
            out["source"] = (
                "EDWAR: the data address that triggered the watchpoint (not the PC; "
                "may be rounded down within 16 bytes)"
            )
        else:
            out["source"] = (
                "stopped on a data watchpoint; EDWAR did not read, so the armed "
                "comparators above are the only address evidence"
            )

    return out


def preflight(core: Core, *, write: bool) -> list[str]:
    """Refuse the cases that would otherwise produce a confidently wrong result.

    Two hard refusals, both `UnsafeOperation` (exit 3):

    * **Identity anchors disagree.** A wrong base address does not fault -- it
      reads some other component. Writing a comparator into it would land
      somewhere unintended.
    * **Invasive debug not granted**, for writes. The breakpoints guide (§3.3,
      §9 pitfall 4) lists the symptom: "comparator writes fail or read back as
      zero". The writes are accepted and do nothing, which is worse than being
      refused.

    Everything else comes back as advisory notes.
    """
    ident = core.identity

    if ident.error:
        raise UnsafeOperation(
            f"cannot read the debug frame at 0x{core.debug_base:08X}: {ident.error}. "
            "Run `s32edbg gates apply` first -- RM 135.11 leaves the debug clocks off "
            "by default."
        )

    problems = identity_problems(core)
    if problems:
        raise UnsafeOperation(
            f"the component at 0x{core.debug_base:08X} does not identify as a Cortex-R52 "
            f"debug frame ({', '.join(problems)}). Refusing to drive run-control against "
            "it: a wrong base address does not fault, it reads and writes some other "
            "component."
        )

    if write and ident.invasive_debug_granted is False:
        raise UnsafeOperation(
            f"invasive debug is not granted on {core.name} (DBGAUTHSTATUS "
            f"0x{ident.authstatus or 0:08X}). Halting and comparator writes would be "
            "accepted and do nothing. This is the HSE/lifecycle authorisation the "
            "breakpoints guide describes in §3.3 and §9 pitfall 4, not something "
            "this tool can work around."
        )

    return preflight_notes(core)

"""The `load` (RAM-load) flow: chip bring-up, image write, verify, park at entry.

Replicates the reference plugin's debugger-does-everything gta flow (its
``cortexDebugPrep.gdb`` steps plus NXP's ``_prologue`` action lists, see
``data/rtuboot.py`` for the verbatim tables and citations), adapted in exactly
two user-decided ways: the RESET_B pin toggle becomes an SDA-AP functional
reset by default, and the flow ends with rtu0.0 **halted at the ELF entry**
instead of running to a startup catch.

Phase order, and why it is load-bearing:

P0b disarm MC_RGM.FRENTC if the running application armed it (Finding 18:
    the NXP RTD Mcu_Init arms the functional-reset entry timeout at zero, and
    a debugger reset taken with debug requests latched then escalates to a
    destructive one that drops the JTAG-DP). AXI-AP traffic, so it runs here
    while the system is still running, never during the reset.
P1  assert reset; latch MDM EDBGREQ for every R52 (MDM survives functional
    reset -- guide §3.6 -- so the requests are pending when cores wake).
    MDM-AP traffic only: the AXI-AP is forbidden during reset (guide §9#3),
    and `memaccess.preflight` enforces exactly that.
P2  release reset; wait for both reset flags to clear; ONE AXI preflight.
P2b Finding 19 guard. On the br1_cabin board the MDM-AP did NOT survive the
    reset: every enable and latch read zero, the APBIC2 frames went dead and
    the parked core was lost (docs/handover-cabin-ram-load.md). If what P1
    wrote is gone, re-apply LADDER-1 and re-latch while RGM still holds the
    R52s; then re-pin every answering M33 by its own DHCSR, so a bootloader
    on the SMU cannot copy the flashed image over the load. The guard runs
    again before P5 and before P7, and a park that fails under a vanished
    hold is repaired and retried once -- on that board the hold vanished
    late, during P5.
P3  MC_ME/RGM partition bring-up + NXP generic SRAM scrubs (verbatim).
P4  SRAMC v2 D-bank ladder -- MUST run after P3 (whose scrub leaves the
    edge-triggered RAMCR.INIT set at the wrong range) and MUST see its
    witness words read zero, or DataRAM ECC is uninitialized and the OS
    dies in Os_Hal_CoreFreeze ~1 s after boot.
P5  park rtu0.0: MC_ME boot address -> `b .` loop; the pending EDBGREQ halts
    it on leaving reset. Only THEN may the image overwrite CodeRAM, because
    the park word lives at 0x7990_0000 -- the same physical RAM as the ELF's
    first word at 0x3210_0000.
P6  write every PT_LOAD at its LMA (fast path via the DLL's WriteMem when it
    proves itself, pure ADIv6 word bursts otherwise).
P7  verify (sampled always, full on request).
P8  PC = entry, DSPSR.T to match; stay halted.
P9  optionally bring up rtu0.1 -- default is not to: the application's own
    BrsHwEarlyInitPowerOn() does the complete core-1 bring-up itself
    (verified in br1_rear_ecu BrsHw.c:159-212, and it asserts core-1 reset
    first so any debugger pre-state is tolerated).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .. import elf
from ..data import r52debug as r52
from ..data import rtuboot
from ..data.expected_apmap import AP_AXI
from ..data.ladder1 import (
    MDM_CONTROL2_EDBGREQ_RTU0_ALL,
    MDM_CONTROL3_EDBGREQ_RTU1_ALL,
    MDM_CONTROL_M33_EDBGREQ_MASK,
)
from ..errors import (
    DapError,
    LoadError,
    S32EError,
    TransportError,
    UnsafeOperation,
    UnsupportedOperation,
)
from . import gates, memaccess, runcontrol
from . import m33 as m33_mod
from .adiv6 import Dap

#: One sampled verify word per this many words in a fast-path block.
FAST_SAMPLE_STRIDE = 256
#: Sub-block size for progress + fast-path granularity.
WRITE_SLICE_WORDS = 16 * 1024 // 4
#: WriteMem attempts per block before that block falls back to ADIv6, and the
#: pause between attempts. Measured 2026-08-04: the -1 failures cluster in
#: the FETK region (0x326Bxxxx) and come in bursts -- consistent with bus
#: contention from the measurement hardware polling its page, not with a
#: broken writer -- so back-to-back retries fail together and a short pause
#: gives the window time to pass.
FAST_ATTEMPTS = 3
FAST_RETRY_PAUSE_S = 0.025
#: P2b: how long to wait for the SMU AHB-AP to answer after the reset release
#: before giving up on its DHCSR re-pin. Finding 10: it reads all-zeros for
#: ~0.5 s after a release on the rear board. A wiped (DEST0) board's stays
#: dead until a flash bring-up that `load` never runs, so there this is the
#: whole cost of the guard.
POST_RESET_SMU_SETTLE_TIMEOUT_S = 1.5


class ResetDriver(Protocol):
    name: str

    def assert_reset(self) -> None: ...

    def release_reset(self) -> None: ...


class SdaReset:
    """Chip-internal functional reset via SDA-AP SYSFUNCREQ (the default).

    The repo's bench-validated reset path (`reset-halt`, `attach`). Keeps the
    board's shared reset net -- which reaches the SBC/PMIC through R439,
    defect D-2 -- out of the picture entirely.
    """

    name = "sda"

    def __init__(self, dap: Dap) -> None:
        self.dap = dap

    def assert_reset(self) -> None:
        gates.hold_in_functional_reset(self.dap)

    def release_reset(self) -> None:
        gates.release_functional_reset(self.dap)


class PinReset:
    """Literal replication of the plugin's RESET_B toggle, as a fallback.

    The pulse reaches the SBC/PMIC (D-2), which is why this is not the
    default. Needs the DLL's SetRESET/ClrRESET exports.
    """

    name = "pin"

    def __init__(self, transport: object) -> None:
        self.transport = transport

    def assert_reset(self) -> None:
        self.transport.set_reset_pin(True)  # type: ignore[attr-defined]

    def release_reset(self) -> None:
        self.transport.set_reset_pin(False)  # type: ignore[attr-defined]


@dataclass
class LoadHooks:
    """Callbacks into the CLI/session layer, so this module stays console-free."""

    note: Callable[[str], None]
    #: cli._attach_core closure: preflight + OS-unlock + §5.7 sanitation.
    attach_core: Callable[[runcontrol.Core], None]
    #: cli._discover_cores closure: (re)discovers, refreshing session cache.
    discover_cores: Callable[[], list[runcontrol.Core]]
    #: session.note_system_reset when in a session, else a no-op.
    on_system_reset: Callable[[], None] = lambda: None
    #: session.note_halted when in a session, else a no-op.
    note_halted: Callable[[str], None] = lambda name: None


@dataclass
class SegmentResult:
    paddr: int
    nbytes: int
    path: str  # "dll" | "adiv6" | "dll+adiv6"
    seconds: float
    verified: str  # "sampled" | "full" | "entry+edges"


@dataclass
class LoadReport:
    image: str
    entry: int
    entry_pc: int
    total_bytes: int
    seconds: float = 0.0
    kb_per_s: float = 0.0
    segments: list[SegmentResult] = field(default_factory=list)
    pc: int = 0
    dspsr: int = 0
    dspsr_t_fixed: bool = False
    verify: str = "sampled"
    core1: str = "leave"
    fast_path: str = "unused"  # "used" | "demoted" | "unused"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "image": self.image,
            "entry": f"0x{self.entry:08X}",
            "pc": f"0x{self.pc:08X}",
            "dspsr": f"0x{self.dspsr:08X}",
            "total_bytes": self.total_bytes,
            "seconds": round(self.seconds, 3),
            "kb_per_s": round(self.kb_per_s, 1),
            "verify": self.verify,
            "core1": self.core1,
            "fast_path": self.fast_path,
            "segments": [
                {
                    "lma": f"0x{s.paddr:08X}",
                    "bytes": s.nbytes,
                    "path": s.path,
                    "seconds": round(s.seconds, 3),
                    "verified": s.verified,
                }
                for s in self.segments
            ],
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Init-action executor
# ---------------------------------------------------------------------------


def run_init_actions(
    dap: Dap,
    actions: tuple[rtuboot.InitAction, ...],
    *,
    ap_base: int = AP_AXI,
    note: Callable[[str], None] = lambda s: None,
) -> None:
    """Execute one verbatim bring-up table through the checked policy layer.

    Every write is `memaccess.write_word` (guarded, fault-checked) so an XRDC
    denial -- the known risk on the 0x76xxxxxx slice -- aborts loudly with the
    existing hint instead of silently not landing. Callers must have run
    `memaccess.preflight(dap, ap_base, write=True)` once beforehand.
    """
    for a in actions:
        try:
            _run_one_action(dap, ap_base, a, addr=a.addr)
        except DapError as exc:
            if a.fallback_addr is not None:
                note(
                    f"{a.label or hex(a.addr)}: access at 0x{a.addr:08X} faulted; "
                    f"retrying through the alias 0x{a.fallback_addr:08X}"
                )
                _run_one_action(dap, ap_base, a, addr=a.fallback_addr)
                continue
            if a.optional:
                note(
                    f"{a.label or hex(a.addr)}: skipped -- XRDC denies the "
                    f"debugger 0x{a.addr:08X} ({exc}); the register keeps its "
                    "reset default"
                )
                continue
            raise


def _run_one_action(dap: Dap, ap_base: int, a: rtuboot.InitAction, *, addr: int) -> None:
    if a.op == "write32":
        memaccess.write_word(dap, ap_base, addr, a.value)
    elif a.op == "rmw":
        current = memaccess.read_words(dap, ap_base, addr, 1)[0]
        memaccess.write_word(
            dap, ap_base, addr, (current & ~a.mask) | (a.value & a.mask)
        )
    elif a.op == "poll":
        _poll_word(dap, ap_base, a, addr=addr)
    elif a.op == "sleep":
        time.sleep(a.timeout_ms / 1000)
    else:  # pragma: no cover -- table typo guard
        raise LoadError(f"unknown init action op {a.op!r} ({a.label})")


def _poll_word(dap: Dap, ap_base: int, a: rtuboot.InitAction, *, addr: int | None = None) -> None:
    # The gta tables carry small ms timeouts; floor at 1 s -- a longer timeout
    # only slows failure down, and our polls pay USB round trips gta did not.
    target = a.addr if addr is None else addr
    deadline = time.monotonic() + max(a.timeout_ms, 1000) / 1000
    last = None
    while time.monotonic() < deadline:
        last = memaccess.read_words(dap, ap_base, target, 1)[0]
        if (last & a.mask) == a.value:
            return
        time.sleep(0.005)
    raise LoadError(
        f"bring-up poll timed out: {a.label or hex(target)} -- wanted "
        f"(value & 0x{a.mask:08X}) == 0x{a.value:08X}, last read "
        f"{'nothing' if last is None else f'0x{last:08X}'} at 0x{target:08X}"
    )


# ---------------------------------------------------------------------------
# SRAMC v2 ladder (edge-trigger fix + witness verify)
# ---------------------------------------------------------------------------


def sramc_init_v2(
    dap: Dap, *, note: Callable[[str], None] = lambda s: None, timeout_s: float = 1.0
) -> None:
    for addr, value in rtuboot.SRAMC_V2_WRITES:
        memaccess.write_word(dap, AP_AXI, addr, value)
    deadline = time.monotonic() + timeout_s
    statuses = [0, 0, 0]
    while True:
        for i, addr in enumerate(rtuboot.SRAMC_V2_STATUS_ADDRS):
            statuses[i] = memaccess.read_words(dap, AP_AXI, addr, 1)[0]
        if all(s & 1 for s in statuses):
            break
        if time.monotonic() > deadline:
            raise LoadError(
                "SRAMC IDONE poll timed out after %.2fs: D0=0x%08X D1=0x%08X D2=0x%08X"
                % (timeout_s, statuses[0], statuses[1], statuses[2])
            )
        time.sleep(0.005)
    # A REAL init zero-fills with valid ECC, so these words must read exactly
    # 0. Garbage here means the init was an edge-less no-op with perfect-
    # looking registers -- fail LOUDLY instead of booting into a guaranteed
    # DataRAM-ECC panic (root cause of the plugin's Os_Hal_CoreFreeze).
    for addr in rtuboot.SRAMC_V2_VERIFY_ADDRS:
        v = memaccess.read_words(dap, AP_AXI, addr, 1)[0]
        if v != 0:
            raise LoadError(
                f"SRAMC init verify FAILED: [0x{addr:08X}] = 0x{v:08X} (expected 0) "
                "-- DataRAM ECC init did not actually run (RAMCR.INIT is "
                "edge-triggered; see data/rtuboot.py)"
            )
    note("SRAMC D0/D1/D2 ECC init verified (witness words read 0)")


# ---------------------------------------------------------------------------
# Segment planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentPlan:
    segment: elf.Segment
    addr: int
    words: tuple[int, ...]


def check_windows(image: elf.Image) -> None:
    """Refuse an image whose LMAs fall outside the plugin-documented RAM map."""
    for seg in image.segments:
        if not any(
            lo <= seg.paddr and seg.end - 1 <= hi
            for lo, hi, _name in rtuboot.LOADABLE_WINDOWS
        ):
            windows = ", ".join(
                f"0x{lo:08X}-0x{hi:08X} ({name})"
                for lo, hi, name in rtuboot.LOADABLE_WINDOWS
            )
            raise LoadError(
                f"segment {seg.index} at LMA 0x{seg.paddr:08X}..0x{seg.end - 1:08X} "
                f"is outside every loadable RAM window ({windows}). This looks like "
                "an image linked for flash or for another part; refusing to write it."
            )


def plan_segment_words(image: elf.Image, blob: bytes) -> list[SegmentPlan]:
    """Turn segments into word lists, zero-padding one ragged tail per segment.

    The padded bytes land past ``filesz`` inside the final word. With every
    LMA word-aligned (asserted -- true of this ELF and anything GCC links for
    these cores) no other segment can claim bytes inside that word, and the
    assertion below keeps that reasoning honest rather than assumed.
    """
    plans: list[SegmentPlan] = []
    for seg in image.segments:
        if seg.paddr % 4:
            raise LoadError(
                f"segment {seg.index} starts at unaligned LMA 0x{seg.paddr:08X}; "
                "this loader writes whole words and supports word-aligned "
                "segments only"
            )
        data = blob[seg.offset : seg.offset + seg.filesz]
        tail = len(data) % 4
        if tail:
            pad_start = seg.paddr + len(data)
            pad_end = seg.paddr + len(data) + (4 - tail)
            for other in image.segments:
                if other is not seg and other.paddr < pad_end and other.end > pad_start:
                    raise LoadError(
                        f"segment {other.index} claims bytes inside the padded "
                        f"final word of segment {seg.index} at 0x{pad_start:08X} "
                        "-- refusing to zero them"
                    )
            data = data + b"\x00" * (4 - tail)
        words = tuple(
            int.from_bytes(data[i : i + 4], "little") for i in range(0, len(data), 4)
        )
        plans.append(SegmentPlan(seg, seg.paddr, words))
    return plans


# ---------------------------------------------------------------------------
# Segment writer: WriteMem fast path with read-back proof and demotion
# ---------------------------------------------------------------------------


class SegmentWriter:
    """Writes word blocks, preferring the DLL's WriteMem while it earns trust.

    The binding is hand-written and unverifiable on this machine (see
    transport/dll.py), so trust is earned per session: the **first** fast
    block is fully read back through the independent ADIv6 path; later blocks
    get sampled read-back (first, last, every {stride}th word). Any exception
    or mismatch demotes the writer to the pure ADIv6 path for the rest of the
    load, after re-writing the suspect block there. The DLL moves SELECT/CSW
    beneath the Dap during a burst, so both caches are invalidated after each.
    """

    def __init__(
        self,
        dap: Dap,
        transport: object,
        *,
        use_fast: bool,
        note: Callable[[str], None],
        ap: int = AP_AXI,
        ap_type: str = "AXI-AP",
    ) -> None:
        self.dap = dap
        self.transport = transport
        self.note = note
        #: The memory AP every access routes through. The default is the load
        #: flow's system AXI-AP; the flash flow points this at the SMU M33's
        #: AHB-AP instead, whose view holds the applet RAM and the QSPI
        #: controller. Nothing else changes: the trust model is per-AP-agnostic.
        self.ap = ap
        self.ap_type = ap_type
        self.fast_alive = bool(use_fast and getattr(transport, "can_block_write", False))
        self.fast_ever_used = False
        #: True once any WriteMem burst was ISSUED, successful or not. The
        #: DLL wraps even a failing access in its stop-mode halt+access+go,
        #: so the resume side effect (Finding 6) rides failed bursts too --
        #: measured 2026-08-06: the first staging burst failed rc -1, fell
        #: back to ADIv6, and the SMU was left running.
        self.fast_ever_attempted = False
        self.fast_demoted = False
        self.fast_strikes = 0
        self._first_fast_block = True
        if self.fast_alive:
            # Tell the DLL which memory AP to route WriteMem through (guide
            # §7.2 command strings). Best-effort: mid-session use of these
            # strings is undocumented, and the first-block full read-back
            # below is the actual proof either way.
            configure = getattr(transport, "configure_memory_ap", None)
            if configure is not None:
                label = (
                    "the AXI-AP"
                    if self.ap == AP_AXI
                    else f"the {self.ap_type} at 0x{self.ap:08X}"
                )
                try:
                    reply = configure(self.ap, self.ap_type)
                    note(
                        f"registered {label} with the DLL for WriteMem"
                        + (f" ({reply})" if reply else "")
                    )
                except S32EError as exc:
                    note(f"could not register {label} with the DLL ({exc})")
                self.dap.invalidate_select_cache()
                self.dap.invalidate_csw_cache()
            self._sync_dll_halt_state(reason="arming the fast path")

    def _sync_dll_halt_state(self, *, reason: str) -> None:
        """Move the DLL's internal target model to "halted" (which is true).

        SEGGER's own doc (the kb page the DLL logs): on Cortex-R, WriteMem
        wraps every call in stop-mode halt+access+go because the DLL believes
        the target runs. That wrapper is the resume-behind-our-back side
        effect AND the back-to-back-load wedge -- its open-loop state model
        eventually diverges and every WriteMem fast-fails -1 in microseconds
        with no wire traffic. Its own Halt() -- a wire no-op on our already
        CTI-halted core -- resynchronises the model.
        """
        sync = getattr(self.transport, "cpu_halt_sync", None)
        if sync is None:
            return
        verdict = sync()
        self.dap.invalidate_select_cache()
        self.dap.invalidate_csw_cache(self.ap)
        self.note(
            f"synced the DLL's target model ({reason}): DLL Halt issued"
            + ("" if verdict is None else f", IsHalted={int(verdict)}")
        )

    def write_block(self, addr: int, words: list[int]) -> str:
        """Write one block; returns the path that actually landed it.

        Failure policy, tiered by what the failure means:

        * a TransportError (WriteMem returned an ambiguous rc -- measured:
          -1 in bursts over the FETK region, consistent with bus contention
          from the measurement hardware) -- retry with a pause, then fall
          back **for this block only** and keep the fast path. The cost of a
          hostile region is bounded to its own blocks; healthy blocks stay
          fast. There is no permanent demotion for this class: the failed
          attempts cost milliseconds, the ADIv6 rewrite of one block costs
          seconds, and a demote-forever policy turned three clustered
          failures into minutes of word writes for the rest of the load.
        * a verify MISMATCH (the DLL claimed success and the data is wrong)
          or an UnsupportedOperation (missing export) -- permanent demotion
          immediately: a lying writer is never trusted again this load.
        """
        if self.fast_alive:
            self.fast_ever_attempted = True
            try:
                self._write_fast_with_retry(addr, words)
                self.fast_ever_used = True
                return "dll"
            except UnsupportedOperation as exc:  # missing export never heals
                self._fall_back(addr, exc, permanent=True)
            except TransportError as exc:
                self.fast_strikes += 1
                self._fall_back(addr, exc, permanent=False)
            except S32EError as exc:  # verify mismatch, DapError
                self._fall_back(addr, exc, permanent=True)
        memaccess.write_words(self.dap, self.ap, addr, words)
        return "adiv6"

    def _write_fast_with_retry(self, addr: int, words: list[int]) -> None:
        last: TransportError | None = None
        for attempt in range(FAST_ATTEMPTS):
            if attempt:
                # Let a contention window pass. Deliberately no DLL-state
                # resync here: measured (Finding 9), once the DLL session's
                # WriteMem budget is exhausted neither retries nor its own
                # Halt() revive it -- only the fresh-connection-per-load the
                # CLI performs. The retry earns its keep against the FETK
                # bus-contention transients only.
                time.sleep(FAST_RETRY_PAUSE_S)
            try:
                self._write_fast(addr, words)
                return
            except UnsupportedOperation:
                raise
            except TransportError as exc:
                last = exc
                # The failed call may have moved SELECT/CSW anyway.
                self.dap.invalidate_select_cache()
                self.dap.invalidate_csw_cache(self.ap)
        assert last is not None
        raise last

    def _fall_back(self, addr: int, exc: Exception, *, permanent: bool) -> None:
        if permanent:
            self.fast_alive = False
            self.fast_demoted = True
            self.note(
                f"fast path demoted at 0x{addr:08X} ({type(exc).__name__}: {exc}) "
                "-- rewriting this block via ADIv6 and staying there"
            )
        else:
            self.note(
                f"WriteMem failed at 0x{addr:08X} ({exc}) -- rewriting this "
                f"block via ADIv6; the fast path stays on for the next block "
                f"(block fallback #{self.fast_strikes})"
            )
        # u4: a misbehaving DLL call may have disturbed more than SELECT;
        # prove the memory AP is still usable before continuing.
        self.dap.invalidate_select_cache()
        self.dap.invalidate_csw_cache(self.ap)
        memaccess.preflight(self.dap, self.ap, write=True)

    def _write_fast(self, addr: int, words: list[int]) -> None:
        data = b"".join(w.to_bytes(4, "little") for w in words)
        self.transport.write_mem_block(addr, data)  # type: ignore[attr-defined]
        # u3: the DLL moved SELECT/CSW/TAR beneath the Dap object.
        self.dap.invalidate_select_cache()
        self.dap.invalidate_csw_cache(self.ap)
        if self._first_fast_block:
            got = memaccess.read_words(self.dap, self.ap, addr, len(words))
            if got != list(words):
                bad = next(i for i, (a, b) in enumerate(zip(got, words)) if a != b)
                raise LoadError(
                    f"WriteMem probe block mismatched at 0x{addr + bad * 4:08X}: "
                    f"wrote 0x{words[bad]:08X}, read 0x{got[bad]:08X}"
                )
            self._first_fast_block = False
            self.note(
                f"WriteMem fast path proven by full read-back of the first block "
                f"({len(words)} words)"
            )
            return
        idxs = sorted({0, len(words) - 1} | set(range(0, len(words), FAST_SAMPLE_STRIDE)))
        for i in idxs:
            got = memaccess.read_words(self.dap, self.ap, addr + i * 4, 1)[0]
            if got != words[i]:
                raise LoadError(
                    f"WriteMem sampled verify mismatched at 0x{addr + i * 4:08X}: "
                    f"wrote 0x{words[i]:08X}, read 0x{got:08X}"
                )


def _pin_down_parked_core(
    dap: Dap, core: runcontrol.Core, index: int, hooks: "LoadHooks"
) -> None:
    """Convert the EDBGREQ hold into a stable, acknowledged CTI halt.

    The mechanism (measured 2026-08-04: an EDBGREQ-held core reads
    EDPRSR.HALTED without being architecturally in Debug state, and clearing
    the latch resumes it) lives in ``runcontrol.pin_down_edbgreq_hold`` --
    shared with `core catch`. Here: unlock + sanitise first via the attach
    hook, then the conversion, then the session bookkeeping.
    """
    hooks.attach_core(core)
    try:
        runcontrol.pin_down_edbgreq_hold(dap, core)
    except runcontrol.RunControlError as exc:
        raise LoadError(str(exc)) from exc
    hooks.note_halted(core.name)


# ---------------------------------------------------------------------------
# Waiting for the parked halt
# ---------------------------------------------------------------------------


def wait_halted(
    dap: Dap, core: runcontrol.Core, *, timeout_s: float = 2.0, what: str = "core"
) -> None:
    """Poll the raw EDPRSR.HALTED bit until the core is in Debug state.

    Deliberately not ``read_state().halted``: straight out of cold reset the
    parked core is OS-locked (guide §5.2), EDSCR faults, and the composed
    ``halted`` is None -- while EDPRSR, which is lock-exempt, already shows
    HALTED. The unlock happens *after* this wait, in the attach step.
    """
    deadline = time.monotonic() + timeout_s
    state = None
    while time.monotonic() < deadline:
        state = runcontrol.read_state(dap, core.debug_base)
        if state.edprsr is not None and state.edprsr & r52.EDPRSR_HALTED:
            core.state = state
            return
        time.sleep(0.005)
    raise LoadError(
        f"{what} ({core.name}) did not enter Debug state within {timeout_s:.1f}s "
        "of leaving reset with EDBGREQ pending -- EDPRSR "
        + (f"0x{state.edprsr:08X}" if state is not None and state.edprsr is not None else "unreadable")
    )


def halt_m33s(dap: Dap, note: Callable[[str], None], *, smu_fatal: bool = True) -> None:
    """P0's M33 quiescing, mirroring the plugin's three DHCSR halt writes.

    The SMU is the system manager: failing to stop it is fatal for the flow --
    *when nothing stronger follows*. Callers about to run the EDBGREQ-latched
    reset pass ``smu_fatal=False``: on a blank-flash board the SoC sits in the
    BootROM fail cascade at attach and the SMU AHB-AP answers nothing
    (measured 2026-08-05, right after the first chip erase), yet the reset
    with EDBGREQ latched catches the SMU out of reset regardless -- the
    pre-reset halts replicate the plugin's ordering, not the mechanism that
    persists. The CE cores' AHB-APs can legitimately be unclocked on a quiet
    part, so those two are always best-effort with a note.
    """
    cores = m33_mod.discover(dap)
    for c in cores:
        try:
            for n in m33_mod.preflight(c, write=True):
                note(f"{c.name}: {n}")
            m33_mod.halt(dap, c)
            note(f"{c.name} halted")
        except S32EError as exc:
            if c.name == "smu" and smu_fatal:
                raise LoadError(
                    f"could not halt the SMU M33 ({exc}) -- the system manager "
                    "must be quiet before the load's reset"
                ) from exc
            if c.name == "smu":
                note(
                    f"smu: could not quiesce before the reset ({exc}) -- "
                    "expected on a blank-flash board sitting in the BootROM "
                    "fail cascade (docs/virgin-board.md); proceeding, the "
                    "reset with EDBGREQ latched catches it out of reset"
                )
                continue
            note(f"{c.name}: could not halt ({exc}) -- continuing; the "
                 "EDBGREQ latched over the reset will hold it anyway")


# ---------------------------------------------------------------------------
# Finding 19 -- boards that boot their own application
# (br1_cabin C sample, 2026-10-29; docs/handover-cabin-ram-load.md)
# ---------------------------------------------------------------------------


def disarm_frentc(dap: Dap, note: Callable[[str], None]) -> bool:
    """P0b: clear MC_RGM.FRENTC if a running application armed it. True if written.

    Finding 18: the NXP RTD ``Mcu_Init`` arms the functional-reset *entry*
    timeout at zero (``FRENTC = FRET_EN(1) | FRET_TIMEOUT(0)``). A debugger
    functional reset taken with debug requests latched then does not complete
    inside the timeout, MC_RGM escalates it to a destructive reset, and the
    JTAG-DP disappears ~200 ms into the reboot -- the load dies in P3-P5 with
    "no Cortex-R52 debug frames answered" and the board reboots from flash.
    Measured on the br1_cabin board 2026-10-29: with FRENTC written to 0
    first, the very same reset stays functional. This is AXI-AP traffic, so
    it can only run while the system is out of reset; under a held reset
    nothing is running to have armed it, and the register may not be touched
    anyway (RM Table 957: no AXI during reset).
    """
    try:
        memaccess.preflight(dap, AP_AXI, write=True)
    except UnsafeOperation:
        note(
            "MC_RGM.FRENTC not checked: the system is held in functional reset, so "
            "the AXI-AP is off limits -- and nothing is running to have armed it"
        )
        return False
    try:
        value = memaccess.read_words(dap, AP_AXI, rtuboot.RGM_FRENTC, 1)[0]
    except DapError as exc:
        note(
            f"MC_RGM.FRENTC unreadable ({exc}) -- if the running application armed "
            "the functional-reset entry timeout, the reset below escalates to a "
            "destructive one and drops the debug port (test-plan Finding 18)"
        )
        return False
    if not value & rtuboot.RGM_FRENTC_FRET_EN:
        return False
    memaccess.write_word(dap, AP_AXI, rtuboot.RGM_FRENTC, 0)
    back = memaccess.read_words(dap, AP_AXI, rtuboot.RGM_FRENTC, 1)[0]
    if back & rtuboot.RGM_FRENTC_FRET_EN:
        raise LoadError(
            f"MC_RGM.FRENTC reads 0x{back:08X} after writing 0: the functional-reset "
            "entry timeout stays armed, and the reset this load needs would escalate "
            "to a destructive reset and drop the JTAG-DP (Finding 18). Refusing to "
            "reset. Routes in: `flash program --reset none` plus a power cycle, or a "
            "power cycle followed by `load` before the application runs."
        )
    note(
        f"MC_RGM.FRENTC read 0x{value:08X}: the running application armed the "
        "functional-reset entry timeout (NXP RTD Mcu_Init, Finding 18) -- disarmed, "
        "so the reset below stays functional"
    )
    return True


def _debug_hold_missing(report: gates.GateReport, *, r52_latches: bool) -> list[str]:
    """What P1 wrote into the MDM-AP that no longer reads back.

    Empty on a board whose MDM-AP survived the functional reset (guide §3.6,
    the rear-ECU bench). ``r52_latches`` says whether the four-core EDBGREQ
    nibbles are still expected: before P5 they are, after P5 the flow dropped
    them on purpose.
    """
    regs = report.mdm.registers
    missing: list[str] = []
    if not report.mdm.enables_set:
        missing.append("DBGENCNTRL/2/3")
    if (regs.get("CONTROL", 0) & MDM_CONTROL_M33_EDBGREQ_MASK) != MDM_CONTROL_M33_EDBGREQ_MASK:
        missing.append("CONTROL[M33 EDBGREQ]")
    if r52_latches:
        if (regs.get("CONTROL2", 0) & MDM_CONTROL2_EDBGREQ_RTU0_ALL) != MDM_CONTROL2_EDBGREQ_RTU0_ALL:
            missing.append("CONTROL2[RTU0 EDBGREQ]")
        if (regs.get("CONTROL3", 0) & MDM_CONTROL3_EDBGREQ_RTU1_ALL) != MDM_CONTROL3_EDBGREQ_RTU1_ALL:
            missing.append("CONTROL3[RTU1 EDBGREQ]")
    return missing


def regate_if_cleared(
    dap: Dap, hooks: LoadHooks, report: LoadReport, *, when: str, r52_latches: bool
) -> bool:
    """Finding 19 guard: restore the MDM-AP debug hold if the reset took it away.

    Guide §3.6 and RM 135.11 have the MDM-AP enables and latches surviving a
    functional reset, and the rear-ECU bench agrees -- the whole P1 design
    (latch, then reset) rests on it. The br1_cabin C-sample board does not
    (2026-10-29, docs/handover-cabin-ram-load.md): after the P1/P2 reset
    every MDM-AP register read zero, the APBIC2 frames stopped answering and
    the parked core was lost. Whether the reset itself or the HSE/bootloader
    boot that follows it does the clearing is still open -- the bench log has
    the frames dying *after* the park had caught core0, so it is late -- and
    this check therefore runs at every point the flow is about to depend on
    the hold. ~15 AP reads; prints nothing when everything P1 wrote is there.

    ``r52_latches``: before P5 the four-core nibbles must be back (the park
    depends on them); after P5 they were dropped on purpose, and re-arming
    them would recreate the Finding 7 trap (a stale latch halts rtu0.1 the
    moment the application releases it), so only the M33 half is restored.
    """
    status = gates.status(dap)
    missing = _debug_hold_missing(status, r52_latches=r52_latches)
    if not missing:
        return False
    regs = status.mdm.registers
    hooks.note(
        f"the MDM-AP reads cleared {when} ({', '.join(missing)}: DBGENCNTRL "
        f"0x{regs.get('DBGENCNTRL', 0):08X}, CONTROL 0x{regs.get('CONTROL', 0):08X}, "
        f"CONTROL2 0x{regs.get('CONTROL2', 0):08X}) -- re-applying LADDER-1 and "
        "re-latching the debug requests before anything depends on them (Finding 19)"
    )
    after = gates.apply_ladder1(dap)
    if not after.mdm.enables_set:
        regs = after.mdm.registers
        # The br1_cabin bench (2026-10-30) took this branch: the MDM-AP IDR
        # itself read 0x00000000 here, having read 0x001DE040 at `gates apply`
        # minutes earlier -- the whole AP went dark, not just its enables. The
        # LADDER-1 writes are acknowledged on the wire and every readback is
        # zero. That is the HSE re-securing debug during the secure boot the
        # functional reset re-triggered (Finding 19); no register write from
        # here can bring the AP back. Distinguish a dark AP from live-but-
        # rejected enables so the next reader is not sent hunting the wrong
        # layer.
        if after.mdm.idr in (0, None):
            raise LoadError(
                f"re-applying LADDER-1 {when} did not take: the MDM-AP reads all "
                f"zero, IDR included (0x{(after.mdm.idr or 0):08X}; it read "
                f"0x{gates.MDM_IDR_EXPECTED:08X} at `gates apply`). The whole debug "
                "access port went dark -- the HSE re-secured debug during the boot "
                "the functional reset re-triggered, and no register write brings it "
                "back (test-plan Finding 19). On a board that boots its own "
                "application, RAM `load` over a functional reset is not achievable; "
                "use `flash program --reset none` plus a power cycle instead."
            )
        raise LoadError(
            f"re-applying LADDER-1 {when} did not take: DBGENCNTRL/2/3 read "
            f"0x{regs.get('DBGENCNTRL', 0):08X} / 0x{regs.get('DBGENCNTRL2', 0):08X} / "
            f"0x{regs.get('DBGENCNTRL3', 0):08X} after the writes, but the MDM-AP IDR "
            f"still answers (0x{(after.mdm.idr or 0):08X}). The enables are being "
            "rejected, which points at the HSE gate (`gates status` shows AUTHSTTS), "
            "not at this sequence."
        )
    if r52_latches:
        gates.arm_edbgreq(dap)
    else:
        gates.arm_m33_edbgreq(dap)
    gates.invalidate_status_cache(dap)
    report.notes.append(
        f"MDM-AP found cleared {when}; re-gated and re-latched (Finding 19)"
    )
    return True


def repin_m33s_after_reset(
    dap: Dap,
    note: Callable[[str], None],
    report: LoadReport,
    *,
    settle_timeout_s: float = POST_RESET_SMU_SETTLE_TIMEOUT_S,
) -> None:
    """P2b: pin every answering M33 by its own DHCSR once the reset has released.

    P0's halts replicate the plugin's ordering; what carries the M33s across
    the reset in the plugin's flow is the EDBGREQ latch in MDM CONTROL. On
    the br1_cabin board that latch did not survive (Finding 19), so the SMU
    woke into its bootloader, which copies the flashed application over
    CodeRAM and starts rtu0.0 itself (docs/app-boot-contract.md §3) -- a
    race the load cannot win from P3 onwards. A DHCSR C_HALT lives in the
    M33's own debug logic, not in the MDM-AP, so it holds whatever the MDM-AP
    does next. One write that sets C_DEBUGEN|C_HALT (`ensure_halted`): a
    no-op on a core the latch already caught, a halt request on one that is
    running -- never the two-write `halt()`, whose first write would resume
    an already-halted core for a gap. Finding 10: the SMU AHB-AP answers
    zeros for ~0.5 s after a release, so its identity is polled first. Quiet
    on a board where nothing needed doing.
    """
    t0 = time.monotonic()
    while True:
        ident = m33_mod.identify(dap, m33_mod.M33_APS["smu"])
        if ident.is_cortex_m33 or time.monotonic() - t0 > settle_timeout_s:
            break
        time.sleep(0.02)
    waited = time.monotonic() - t0
    if waited > 0.1:
        note(
            f"SMU AHB-AP {'came back' if ident.is_cortex_m33 else 'still dead'} "
            f"{waited:.2f}s after the reset release"
        )
    for core in m33_mod.discover(dap):
        if core.name != "smu" and not core.identity.is_cortex_m33:
            continue  # an unclocked CE: P0 said so already, nothing to pin
        try:
            for n in m33_mod.preflight(core, write=True):
                note(f"{core.name}: {n}")
            _dhcsr, was_running = m33_mod.ensure_halted(dap, core)
        except S32EError as exc:
            note(
                f"{core.name}: could not re-pin after the reset ({exc})"
                + (
                    " -- if a bootloader runs on it, that bootloader races this "
                    "load (Finding 19)"
                    if core.name == "smu"
                    else ""
                )
            )
            continue
        if was_running:
            note(f"{core.name} was running after the reset -- re-pinned by DHCSR (Finding 19)")
            report.notes.append(
                f"{core.name} ran after the reset; re-pinned by DHCSR (Finding 19)"
            )


def _park_core0(
    dap: Dap, hooks: LoadHooks, report: LoadReport, *, timeout_s: float
) -> runcontrol.Core:
    """P5: park rtu0.0 on the `b .` loop and convert the EDBGREQ catch into a CTI halt.

    One retry, for Finding 19 boards: if the catch or the pin-down fails and
    the MDM-AP reads cleared, the gate and the latches are restored, the M33s
    re-pinned, core0 put back into reset and the park run again -- the bench
    saw the hold vanish exactly here, after the park had caught core0.
    """
    for attempt in (1, 2):
        try:
            run_init_actions(dap, rtuboot.ACTIONS_RTU0_CORE0, note=hooks.note)
            core0 = runcontrol.resolve("rtu0.0", hooks.discover_cores())
            wait_halted(dap, core0, timeout_s=timeout_s, what="parked core")
            _pin_down_parked_core(dap, core0, 0, hooks)
            return core0
        except (S32EError, ValueError) as exc:
            # ValueError: resolve() found no rtu0.0 -- the frames stopped
            # enumerating. Everything else is the catch or the pin-down.
            if attempt == 2 or not regate_if_cleared(
                dap, hooks, report, when="while parking rtu0.0", r52_latches=True
            ):
                if isinstance(exc, S32EError):
                    raise
                raise LoadError(f"rtu0.0 is not among the discovered cores: {exc}") from exc
            hooks.note(f"the park had failed under the closed gate: {exc}")
            repin_m33s_after_reset(dap, hooks.note, report)
            hooks.note("putting rtu0.0 back into reset and parking it again")
            run_init_actions(dap, rtuboot.ACTIONS_RTU0_CORE0_RESET, note=hooks.note)
    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


def load_image(
    dap: Dap,
    transport: object,
    image: elf.Image,
    blob: bytes,
    hooks: LoadHooks,
    *,
    reset: ResetDriver,
    verify_full: bool = False,
    use_fast: bool = True,
    core1: str = "leave",
    settle_timeout_s: float = 2.0,
    park_timeout_s: float = 2.0,
    check_frentc: bool = True,
) -> LoadReport:
    """Run P0b-P9. Caller has done P0 (power-up, LADDER-1, M33 halts) already."""
    report = LoadReport(
        image=image.name,
        entry=image.entry,
        entry_pc=image.entry_pc,
        total_bytes=image.total_filesz,
        verify="full" if verify_full else "sampled",
        core1=core1,
    )
    check_windows(image)
    plans = plan_segment_words(image, blob)

    # P0b -- Finding 18: a running application's Mcu_Init arms the MC_RGM
    # functional-reset entry timeout at zero, and the P1 reset -- taken with
    # debug requests latched -- then escalates to a destructive reset that
    # drops the JTAG-DP. Disarm it while the AXI-AP is still usable.
    if check_frentc and disarm_frentc(dap, hooks.note):
        report.notes.append(
            "MC_RGM.FRENTC was armed by the running application; disarmed before "
            "the reset (Finding 18)"
        )

    # P1 -- reset asserted; latch the debug requests. MDM-AP only.
    hooks.note(f"asserting {reset.name} reset and latching EDBGREQ for every R52")
    reset.assert_reset()
    gates.arm_edbgreq(dap)

    # P2 -- release, settle, prove the AXI-AP safe exactly once.
    reset.release_reset()
    wait_reset_deasserted(dap, hooks.note, timeout_s=settle_timeout_s)
    # P2b -- Finding 19: the MDM-AP may not have survived the reset (the
    # br1_cabin board). Restore the hold while RGM still has every R52 in
    # reset, then pin the M33s by DHCSR -- a hold in their own debug logic,
    # whatever the MDM-AP does next.
    regate_if_cleared(dap, hooks, report, when="after the reset release", r52_latches=True)
    repin_m33s_after_reset(dap, hooks.note, report)
    hooks.on_system_reset()
    _report, notes = memaccess.preflight(dap, AP_AXI, write=True)
    for n in notes:
        hooks.note(n)

    # P3 -- partitions + NXP generic scrubs, verbatim.
    hooks.note("MC_ME/RGM partition bring-up (RTU0, RTU1) + NXP generic SRAM scrubs")
    run_init_actions(dap, rtuboot.ACTIONS_RTU0_PARTITION, note=hooks.note)
    run_init_actions(dap, rtuboot.ACTIONS_RTU1_PARTITION, note=hooks.note)
    run_init_actions(dap, rtuboot.ACTIONS_GENERIC_SRAM, note=hooks.note)

    # P4 -- the ladder that actually initialises DataRAM ECC.
    sramc_init_v2(dap, note=hooks.note)

    # Finding 19 guard, again: the park below is the first step since P2b
    # that needs the frames and the latches. Silent when nothing moved.
    if regate_if_cleared(dap, hooks, report, when="before releasing rtu0.0", r52_latches=True):
        repin_m33s_after_reset(dap, hooks.note, report)

    # P5 -- park core0 and catch it with the pending EDBGREQ.
    hooks.note("parking rtu0.0 on a `b .` loop and releasing it into the halt")
    core0 = _park_core0(dap, hooks, report, timeout_s=park_timeout_s)
    if core1 == "leave":
        # The latches have done their job; a stale one would halt rtu0.1 the
        # moment the application's own bring-up releases it (measured), and
        # core0 would then hang at the OS boot barrier. The park/release
        # variants still need core1's latch for their own catch -- they clear
        # everything after it fires.
        gates.clear_all_r52_edbgreq(dap)

    # P6 -- the image.
    writer = SegmentWriter(dap, transport, use_fast=use_fast, note=hooks.note)
    t0 = time.monotonic()
    for plan in plans:
        s0 = time.monotonic()
        paths: set[str] = set()
        words = list(plan.words)
        for off in range(0, len(words), WRITE_SLICE_WORDS):
            block = words[off : off + WRITE_SLICE_WORDS]
            paths.add(writer.write_block(plan.addr + off * 4, block))
        seconds = time.monotonic() - s0
        rate = (plan.segment.filesz / 1024 / seconds) if seconds > 0 else 0.0
        hooks.note(
            f"wrote 0x{plan.addr:08X}  {plan.segment.filesz:>9,} bytes  "
            f"{seconds:6.2f}s  {rate:8.1f} KB/s"
        )
        report.segments.append(
            SegmentResult(
                paddr=plan.addr,
                nbytes=plan.segment.filesz,
                path="+".join(sorted(paths)),
                seconds=seconds,
                verified="pending",
            )
        )
    report.seconds = time.monotonic() - t0
    report.kb_per_s = (
        report.total_bytes / 1024 / report.seconds if report.seconds > 0 else 0.0
    )
    report.fast_path = (
        "demoted" if writer.fast_demoted else ("used" if writer.fast_ever_used else "unused")
    )
    if writer.fast_strikes and not writer.fast_demoted:
        report.notes.append(
            f"{writer.fast_strikes} block(s) fell back to ADIv6 on WriteMem "
            "transients (clustered failures are bus contention, e.g. the FETK "
            "region -- the fast path stayed on)"
        )

    # P8a -- re-pin the core before verifying anything. The DLL's WriteMem
    # resumes the halted core behind the debugger's back (measured 2026-08-04:
    # EDPRSR.SDR set after the first fast block on every run); the core spends
    # the load spinning at the DataRAM park, harmlessly, and is re-halted
    # here. Verify runs after the quiesce so its reads describe a stopped
    # machine.
    #
    # Finding 19 guard, last time: the re-halt, the verify and the PC write
    # all need the frames. Nothing to re-latch for the R52s -- core0 is held
    # by an acknowledged CTI halt and P5 dropped the latches on purpose
    # (re-arming them would recreate the Finding 7 trap for rtu0.1); only
    # core1's own latch comes back when a P9 variant still needs it.
    if regate_if_cleared(
        dap, hooks, report, when="after the image was written", r52_latches=False
    ) and core1 != "leave":
        gates.arm_r52_edbgreq(dap, 0, 1)
    state = runcontrol.read_state(dap, core0.debug_base)
    if state.halted is not True:
        hooks.note(
            "rtu0.0 left Debug state during the load (the DLL's WriteMem "
            "resumes the core it thinks it halted -- known side effect); "
            "re-halting at the park"
        )
        runcontrol.halt(dap, core0)

    # P7 -- verify. Sampled: the word holding the entry point plus the first
    # and last word of every segment, through the checked read path. Full:
    # every word (costly on the pure path -- roughly the write time again).
    _verify(dap, image, plans, report, full=verify_full, note=hooks.note)

    # P8 -- PC to the entry, DSPSR.T to match, stay halted.
    runcontrol.preflight(core0, write=True)
    if not runcontrol.verify_register_path(dap, core0):
        raise LoadError(
            "the register-transfer round trip failed on rtu0.0; refusing to set "
            "the PC after an otherwise-successful load"
        )
    runcontrol.write_pc(dap, core0, image.entry_pc)
    report.pc = runcontrol.read_pc(dap, core0)
    dspsr = runcontrol.read_dspsr(dap, core0)
    if bool(dspsr & r52.PSR_T) != image.entry_thumb:
        # The plugin's park word is T32 and its entry is T32, so its bare
        # `set $pc` never needed this; verify-and-correct keeps the same end
        # state without assuming the parked DSPSR.
        fixed = (dspsr | r52.PSR_T) if image.entry_thumb else (dspsr & ~r52.PSR_T)
        runcontrol.write_dspsr(dap, core0, fixed)
        dspsr = runcontrol.read_dspsr(dap, core0)
        if bool(dspsr & r52.PSR_T) != image.entry_thumb:
            raise LoadError(
                f"DSPSR.T would not take: wrote 0x{fixed:08X}, read back 0x{dspsr:08X}"
            )
        report.dspsr_t_fixed = True
    report.dspsr = dspsr

    # P9 -- core1, when asked. Default `leave`: br1_rear's core0 firmware does
    # the whole core-1 bring-up itself (BrsHw.c:159-212) and asserts core-1
    # reset first, so it tolerates any state we leave. `park`/`release` are
    # the plugin-faithful fallback for firmware that does not.
    if core1 != "leave":
        hooks.note(f"bringing up rtu0.1 ({core1})")
        run_init_actions(dap, rtuboot.ACTIONS_RTU0_CORE1, note=hooks.note)
        cores = hooks.discover_cores()
        core1_obj = runcontrol.resolve("rtu0.1", cores)
        wait_halted(dap, core1_obj, what="parked core1")
        _pin_down_parked_core(dap, core1_obj, 1, hooks)
        gates.clear_all_r52_edbgreq(dap)
        runcontrol.write_pc(dap, core1_obj, image.entry_pc)
        c1_dspsr = runcontrol.read_dspsr(dap, core1_obj)
        if bool(c1_dspsr & r52.PSR_T) != image.entry_thumb:
            fixed = (
                (c1_dspsr | r52.PSR_T) if image.entry_thumb else (c1_dspsr & ~r52.PSR_T)
            )
            runcontrol.write_dspsr(dap, core1_obj, fixed)
        if core1 == "release":
            runcontrol.resume(dap, core1_obj)
            hooks.note("rtu0.1 released, free-running from the entry")
        else:
            hooks.note_halted(core1_obj.name)
            hooks.note("rtu0.1 parked halted at the entry")

    return report


def _verify(
    dap: Dap,
    image: elf.Image,
    plans: list[SegmentPlan],
    report: LoadReport,
    *,
    full: bool,
    note: Callable[[str], None],
) -> None:
    for plan, seg_result in zip(plans, report.segments):
        words = list(plan.words)
        if full:
            got = memaccess.read_words(dap, AP_AXI, plan.addr, len(words))
            if got != words:
                bad = next(i for i, (a, b) in enumerate(zip(got, words)) if a != b)
                raise LoadError(
                    f"full verify mismatched at 0x{plan.addr + bad * 4:08X}: "
                    f"wrote 0x{words[bad]:08X}, read 0x{got[bad]:08X}"
                )
            seg_result.verified = "full"
            continue
        for i in (0, len(words) - 1):
            got = memaccess.read_words(dap, AP_AXI, plan.addr + i * 4, 1)[0]
            if got != words[i]:
                raise LoadError(
                    f"verify mismatched at 0x{plan.addr + i * 4:08X}: "
                    f"wrote 0x{words[i]:08X}, read 0x{got:08X}"
                )
        seg_result.verified = "edges"
    # The word the PC will fetch first, wherever it lives.
    entry_word_addr = image.entry_pc & ~3
    for plan in plans:
        idx = (entry_word_addr - plan.addr) // 4
        if 0 <= idx < len(plan.words):
            got = memaccess.read_words(dap, AP_AXI, entry_word_addr, 1)[0]
            if got != plan.words[idx]:
                raise LoadError(
                    f"the entry word at 0x{entry_word_addr:08X} reads "
                    f"0x{got:08X}, expected 0x{plan.words[idx]:08X}"
                )
            note(f"entry word at 0x{entry_word_addr:08X} verified (0x{got:08X})")
            return
    note(
        f"entry PC 0x{image.entry_pc:08X} is not inside any loaded segment -- "
        "nothing to verify there"
    )


def wait_reset_deasserted(
    dap: Dap, note: Callable[[str], None], *, timeout_s: float = 2.0
) -> None:
    """Poll both reset views (SDA SYSRSTS + MDM STATUS) until neither asserts.

    Public because the flash flow's reset-quiesce shares it with `load`.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        report = gates.status(dap)
        if (
            not report.hse.reset_flags()["functional_reset_asserted"]
            and report.mdm.system_reset_asserted is not True
        ):
            note("functional reset released; both reset flags read clear")
            return
        time.sleep(0.010)
    raise LoadError(
        "the system did not leave functional reset within "
        f"{timeout_s:.1f}s of releasing it"
    )

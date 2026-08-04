"""Bus access policy: which AP, and whether the access is allowed at all.

This module exists to keep the two safety guards in one place and on every
path. ``adiv6.py`` provides the mechanism and deliberately knows nothing about
gates -- it cannot import ``gates.py`` anyway, since gates already imports it.
So the CLI talks to this module, never to ``Dap.mem_*`` directly.

The guards, both quoted from the RM:

* **AXI-AP during functional reset.** Table 957: "During reset, do not access
  peripherals and memory regions via AXI_AP. If such an access occurs, the
  peripherals and memory regions might not be accessible via AXI_AP after reset
  de-assertion. In that case, you must issue a destructive reset." There is no
  override flag for this one.
* **Any SDA-AP write.** RM 135.13.4 makes the HSE challenge/response one-shot,
  so a botched write is unrecoverable until reset. The SDA-AP is read-only here.

And one advisory, not a refusal: RM 135.11 warns that touching debug components
while the debug clocks are gated "might hang the debug APBIC bus". That is a
warning rather than a refusal because the gate state is readable and the
operator may be deliberately probing a cold part.
"""

from __future__ import annotations

from ..data.expected_apmap import (
    AP_ALIASES,
    AP_APB,
    EXPECTED_AP_BY_BASE,
    SYSTEM_BUS_APS,
)
from ..errors import DapError
from . import gates
from .adiv6 import STICKYERR, Dap


def resolve_ap(spec: str) -> int:
    """Turn an --ap value into an AP base address.

    Accepts a short name from RM Table 957 or a 0x-prefixed base address. A
    bare number is rejected like every other address argument: it could be hex
    or decimal, and both guesses have produced confidently-wrong accesses.
    """
    key = spec.strip().lower()
    if key in AP_ALIASES:
        return AP_ALIASES[key]
    if key[:2] == "0x":
        try:
            return int(key, 16)
        except ValueError:
            pass
    raise ValueError(
        f"unknown AP {spec!r}. Use one of {', '.join(sorted(AP_ALIASES))}, "
        "or a 0x-prefixed base address such as 0x01C20000."
    )


def ap_label(ap_base: int) -> str:
    expected = EXPECTED_AP_BY_BASE.get(ap_base)
    return f"0x{ap_base:08X} {expected.name}" if expected else f"0x{ap_base:08X}"


def preflight(dap: Dap, ap_base: int, *, write: bool) -> tuple[gates.GateReport, list[str]]:
    """Check policy before any transfer, and report what the operator should know.

    Returns the gate report -- read once and reused so a block transfer does not
    re-read it per word -- plus advisory notes. Raises UnsafeOperation for the
    two hard refusals.
    """
    # Refuse a forbidden write before issuing any transaction at all, so a
    # refused command leaves the target completely untouched.
    if write:
        gates.guard_write(ap_base)

    report = gates.status(dap)
    notes: list[str] = []

    if ap_base in SYSTEM_BUS_APS:
        gates.assert_axi_safe(report)

    if not report.mdm.enables_set:
        notes.append(
            "the debug-enable gate is closed (MDM-AP DBGENCNTRL1-3 read zero). RM 135.11: "
            "\"Trying to access debug components while the clocks are not enabled might "
            "hang the debug APBIC bus.\" Run `s32edbg gates apply` first if this hangs."
        )
    if report.hse.error is None and not report.hse.app_debug_enabled:
        notes.append(
            "SDA-AP reports application debug disabled, so this part may be in a secured "
            "lifecycle state and some APs will read as zero."
        )
    return report, notes


def read_words(
    dap: Dap, ap_base: int, addr: int, count: int, *, size: int = 32
) -> list[int]:
    """Read ``count`` values, raising rather than returning a faulted zero.

    A bus access that faults does not raise on its own -- it returns
    0x00000000 and latches CTRL/STAT.STICKYERR, after which every later access
    also returns zero. Measured on this target: `mem read 0x76120000 --ap axi`
    reported 0x00000000 for a register XRDC denies the debugger entirely.
    Returning that as a value is exactly the confidently-wrong answer this tool
    exists to avoid, so a faulted access is an error and says which address
    caused it.

    Single reads are checked individually. A block transfer is checked once at
    the end, because the sticky flag is latching -- if any word in the block
    faulted, the flag is still set when the block finishes.
    """
    if count == 1:
        value = dap.mem_read_checked(ap_base, addr, size=size)
        if value is None:
            raise DapError(_fault_message(ap_base, addr))
        return [value]

    dap.clear_sticky()
    values = dap.mem_read_block(ap_base, addr, count, size=size)
    if dap.read_ctrl_stat() & STICKYERR:
        dap.clear_sticky()
        raise DapError(
            _fault_message(ap_base, addr)
            + f" Somewhere in the {count} words from 0x{addr:08X} an access faulted, so "
            "none of the block can be trusted -- a latched sticky error makes every "
            "later word read as zero too."
        )
    return values


def _fault_message(ap_base: int, addr: int) -> str:
    where = " via the AXI-AP" if ap_base in SYSTEM_BUS_APS else ""
    hint = (
        " XRDC can deny the debugger a region the cores reach freely; the "
        "0x76xxxxxx RTU peripheral slice is one such region on this part."
        if ap_base in SYSTEM_BUS_APS
        else " Check `gates apply` has run -- RM 135.11 leaves the debug clocks off."
    )
    return f"the access to 0x{addr:08X}{where} faulted.{hint}"


def write_word(dap: Dap, ap_base: int, addr: int, value: int, *, size: int = 32) -> None:
    """Write one value, raising if the access faulted.

    Callers must have run preflight(write=True) first. A write that faults is
    silently discarded, so without the check a rejected write reads back as a
    read-back mismatch and gets blamed on the register instead of the access.
    """
    gates.guard_write(ap_base)
    if not dap.mem_write_checked(ap_base, addr, value, size=size):
        raise DapError(_fault_message(ap_base, addr))


def write_words(
    dap: Dap, ap_base: int, addr: int, values: list[int], *, size: int = 32
) -> None:
    """Write a block of values, raising if any access faulted.

    Callers must have run preflight(write=True) first. Mirrors read_words'
    block mode: checked once at the end, because STICKYERR is latching -- if
    any word in the block faulted the flag is still set when the block
    finishes. Ordinary memory only (see Dap.mem_write_block on the fallback's
    write-twice behaviour).
    """
    gates.guard_write(ap_base)
    if not values:
        return
    dap.clear_sticky()
    dap.mem_write_block(ap_base, addr, values, size=size)
    if dap.read_ctrl_stat() & STICKYERR:
        dap.clear_sticky()
        raise DapError(
            _fault_message(ap_base, addr)
            + f" Somewhere in the {len(values)} words from 0x{addr:08X} a write "
            "faulted, so the whole block is suspect -- nothing past the faulting "
            "word landed."
        )


def apb_range_note(ap_base: int, addr: int) -> str | None:
    """Advisory for an APB-AP address outside the region the RM maps.

    RM 135.6 puts the debug components in APBIC2's 28 MB; an address past that
    is not necessarily wrong, but it is worth saying so rather than returning a
    confident zero.
    """
    if ap_base != AP_APB:
        return None
    if addr > 0x01C0_0000:
        return (
            f"0x{addr:08X} is above the APBIC2 region RM Table 965 maps; a read here "
            "may return zero rather than fault."
        )
    return None

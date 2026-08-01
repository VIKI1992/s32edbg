"""Access-Port discovery and comparison against RM Table 957.

Two independent methods are used and merged, because each fails in a different
situation:

  ``walk``   -- follow BASEPTR0/1 to the top-level ROM table and enumerate the
                APs it describes. This is the architectural method (the J-Link
                DLL does the same: "Top-level ROM table, that describes AP map,
                found" / "Scanning top-level ROM table and nested ones to find
                APs"). It fails if that ROM is gated or the pointer is invalid.

  ``direct`` -- read the IDR at each address RM Table 957 documents. Cannot
                discover anything undocumented, but works even when the
                top-level ROM is unreadable, and it is a strong identity check
                because Table 957 publishes the expected IDR values.

Discrepancies either way are reported rather than smoothed over.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..data.expected_apmap import (
    APBIC1_ROM_BASE,
    EXPECTED_AP_BY_BASE,
    EXPECTED_APS,
    UNDOCUMENTED_AP_WINDOW,
)
from ..data.known_ids import ApIdr, decode_ap_idr
from ..errors import DapError
from . import romtable
from .adiv6 import AP_IDR, Dap
from .romtable import ARCHID_ROM_TABLE


@dataclass
class DiscoveredAp:
    base: int
    idr: ApIdr | None
    ident: romtable.ComponentId | None = None
    found_by: set[str] = field(default_factory=set)
    error: str | None = None

    @property
    def expected_name(self) -> str | None:
        exp = EXPECTED_AP_BY_BASE.get(self.base)
        return exp.name if exp else None

    @property
    def idr_matches_rm(self) -> bool | None:
        exp = EXPECTED_AP_BY_BASE.get(self.base)
        if exp is None or exp.idr is None or self.idr is None:
            return None
        return self.idr.raw == exp.idr

    def describe(self) -> str:
        if self.idr is None or not self.idr.present:
            return "absent"
        parts = [self.idr.class_name, self.idr.type_name, self.idr.designer_name]
        parts.append(f"rev {self.idr.revision}")
        return ", ".join(parts)


@dataclass
class ApMap:
    aps: list[DiscoveredAp]
    baseptr: int | None
    rom_base_used: int
    walk_error: str | None = None
    notes: list[str] = field(default_factory=list)

    def by_base(self) -> dict[int, DiscoveredAp]:
        return {ap.base: ap for ap in self.aps}

    def diff_against_rm(self) -> dict[str, list[str]]:
        """Compare the discovered map with RM Table 957."""
        found = self.by_base()
        missing: list[str] = []
        unexpected: list[str] = []
        mismatched: list[str] = []

        for exp in EXPECTED_APS:
            ap = found.get(exp.base)
            if ap is None or ap.idr is None or not ap.idr.present:
                missing.append(f"0x{exp.base:08X} {exp.name}")
                continue
            if exp.idr is not None and ap.idr.raw != exp.idr:
                mismatched.append(
                    f"0x{exp.base:08X} {exp.name}: RM says IDR 0x{exp.idr:08X}, "
                    f"read 0x{ap.idr.raw:08X}"
                )

        for ap in self.aps:
            if ap.base in EXPECTED_AP_BY_BASE:
                continue
            if ap.idr is None or not ap.idr.present:
                continue
            note = f"0x{ap.base:08X} ({ap.describe()})"
            lo, hi = UNDOCUMENTED_AP_WINDOW
            if lo <= ap.base <= hi:
                note += " -- inside the 0x01C4_0000-0x01C6_FFFF gap RM Table 957 does not document"
            unexpected.append(note)

        return {"missing": missing, "unexpected": unexpected, "mismatched": mismatched}


def _read_ap_idr(dap: Dap, base: int) -> ApIdr | None:
    try:
        return decode_ap_idr(dap.ap_read(base, AP_IDR))
    except DapError:
        return None


def discover_direct(dap: Dap) -> list[DiscoveredAp]:
    """Read the IDR at each AP address RM Table 957 documents."""
    out: list[DiscoveredAp] = []
    for exp in EXPECTED_APS:
        idr = _read_ap_idr(dap, exp.base)
        ap = DiscoveredAp(base=exp.base, idr=idr)
        if idr is None:
            ap.error = "IDR unreadable"
        else:
            ap.found_by.add("direct")
        out.append(ap)
    return out


def discover_by_walk(dap: Dap, rom_base: int | None = None) -> tuple[list[DiscoveredAp], int, str | None]:
    """Enumerate APs by walking the top-level ROM table in AP space."""
    base = rom_base
    if base is None:
        try:
            base = dap.read_baseptr()
        except DapError:
            base = None
    if base is None:
        base = APBIC1_ROM_BASE
    base &= 0xFFFF_FFFF

    reader = dap.apspace_reader()
    try:
        root = romtable.walk(reader, base)
    except DapError as exc:
        return [], base, f"top-level ROM walk failed: {exc}"

    if not root.is_table:
        return [], base, f"0x{base:08X} is not a ROM table ({root.error or 'unexpected class'})"

    aps: list[DiscoveredAp] = []
    for node in root.flatten():
        if node is root or node.ident is None:
            continue
        ident = node.ident
        is_memap = (
            ident.devarch is not None
            and ident.devarch.present
            and ident.devarch.archid != ARCHID_ROM_TABLE
        )
        if not is_memap:
            continue
        ap = DiscoveredAp(base=node.base, idr=_read_ap_idr(dap, node.base), ident=ident)
        ap.found_by.add("walk")
        aps.append(ap)
    return aps, base, None


def discover(dap: Dap, *, methods: tuple[str, ...] = ("direct", "walk")) -> ApMap:
    """Run both discovery methods and merge by base address."""
    merged: dict[int, DiscoveredAp] = {}
    walk_error: str | None = None
    rom_base = APBIC1_ROM_BASE
    baseptr: int | None = None

    try:
        baseptr = dap.read_baseptr()
    except DapError:
        baseptr = None

    if "direct" in methods:
        for ap in discover_direct(dap):
            merged[ap.base] = ap

    if "walk" in methods:
        walked, rom_base, walk_error = discover_by_walk(dap, baseptr)
        for ap in walked:
            existing = merged.get(ap.base)
            if existing is None:
                merged[ap.base] = ap
            else:
                existing.found_by |= ap.found_by
                existing.ident = existing.ident or ap.ident
                if existing.idr is None:
                    existing.idr = ap.idr
                    existing.error = None

    result = ApMap(
        aps=[merged[k] for k in sorted(merged)],
        baseptr=baseptr,
        rom_base_used=rom_base,
        walk_error=walk_error,
    )
    if baseptr is None:
        result.notes.append(
            "BASEPTR0.VALID is clear, so the top-level ROM address was assumed to be "
            f"0x{APBIC1_ROM_BASE:08X} from RM Table 957."
        )
    return result

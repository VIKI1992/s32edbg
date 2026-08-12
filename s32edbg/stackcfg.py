"""AUTOSAR OS stack configuration out of a project's ECUC ARXML.

The stack monitor's source of truth. Vector MICROSAR's DaVinci resolves every
task/ISR stack request into ``/MICROSAR/Os/OsOS/OsStackSummary/OsStack``
containers -- one per physical stack, carrying the SHORT-NAME the generator
also uses for the backing array (``OsCfg_Stack_<name>_Dyn``), the byte size,
the stack type, the owning core and the consuming tasks/ISRs. Addresses are
deliberately absent here (they are link-time); the scanner joins this list
against the loaded ELF's DWARF globals.

Discovery is two-phase because ECUC exports can be huge: a streamed byte
probe for ``OsStackSummary`` first (a 38 MB pre-merge snapshot without stack
containers is skipped for the cost of a file read), a namespace-agnostic
streaming parse of the probe hits second -- and only files yielding at least
one stack container qualify as candidates.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

#: Every resolved stack container references this ECUC definition path.
STACK_DEF_SUFFIX = "/OsOS/OsStackSummary/OsStack"

#: Directory names never worth descending into during discovery.
_SKIP_DIRS = {".git", ".svn", "node_modules", "__pycache__"}


@dataclass
class StackEntry:
    """One configured stack: what the ARXML knows (no address -- link-time)."""

    name: str
    size: int | None
    stack_type: str | None
    core: str | None
    users: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size": self.size,
            "type": self.stack_type,
            "core": self.core,
            "users": self.users,
        }


def _local(tag: Any) -> str:
    """Element tag without its XML namespace ('{ns}NAME' -> 'NAME')."""
    text = tag if isinstance(tag, str) else ""
    return text.rsplit("}", 1)[-1]


def _stack_from_container(elem: Any) -> StackEntry | None:
    """A StackEntry if this ECUC-CONTAINER-VALUE is an OsStack, else None."""
    def_ref: str | None = None
    name: str | None = None
    for child in elem:
        tag = _local(child.tag)
        if tag == "DEFINITION-REF":
            def_ref = (child.text or "").strip()
        elif tag == "SHORT-NAME":
            name = (child.text or "").strip()
    if not name or def_ref is None or not def_ref.endswith(STACK_DEF_SUFFIX):
        return None

    size: int | None = None
    stack_type: str | None = None
    core: str | None = None
    users: list[str] = []
    for node in elem.iter():
        tag = _local(node.tag)
        if tag in ("ECUC-NUMERICAL-PARAM-VALUE", "ECUC-TEXTUAL-PARAM-VALUE"):
            leaf: str | None = None
            value: str | None = None
            for child in node:
                sub = _local(child.tag)
                if sub == "DEFINITION-REF":
                    leaf = (child.text or "").strip().rsplit("/", 1)[-1]
                elif sub == "VALUE":
                    value = (child.text or "").strip()
            if leaf is None or value is None:
                continue
            if leaf == "OsStackSize":
                try:
                    size = int(value)
                except ValueError:
                    size = None
            elif leaf == "OsStackType":
                stack_type = value
            elif leaf == "OsStackUserName":
                users.append(value)
        elif tag == "ECUC-REFERENCE-VALUE":
            leaf = None
            ref: str | None = None
            for child in node:
                sub = _local(child.tag)
                if sub == "DEFINITION-REF":
                    leaf = (child.text or "").strip().rsplit("/", 1)[-1]
                elif sub == "VALUE-REF":
                    ref = (child.text or "").strip()
            if leaf == "OsStackCore" and ref:
                core = ref.rsplit("/", 1)[-1]
    return StackEntry(name, size, stack_type, core, users)


def parse_os_ecuc(path: str | Path) -> list[StackEntry]:
    """Every OsStackSummary/OsStack container in one ECUC ARXML file.

    Streaming parse with element reclamation: pre-merge ECUC snapshots reach
    tens of MB, and holding their full tree is pointless. Raises
    ``xml.etree.ElementTree.ParseError`` on non-XML input.
    """
    entries: list[StackEntry] = []
    for _event, elem in ET.iterparse(str(path), events=("end",)):
        if _local(elem.tag) != "ECUC-CONTAINER-VALUE":
            continue
        entry = _stack_from_container(elem)
        if entry is not None:
            entries.append(entry)
        # Reclaim the processed container's subtree. Outer containers keep
        # their own direct children (only the inner container is emptied),
        # so their DEFINITION-REF check above still works at their own end.
        elem.clear()
    return entries


def _contains_bytes(path: Path, needle: bytes) -> bool:
    """Streamed substring probe -- bounded memory on arbitrarily large files."""
    tail = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                return False
            if needle in tail + chunk:
                return True
            tail = chunk[-(len(needle) - 1):]


def _probe_files(
    paths: list[Path],
    progress: Callable[[str], None] | None,
) -> list[str]:
    candidates: list[str] = []
    for path in paths:
        try:
            if not _contains_bytes(path, b"OsStackSummary"):
                continue
        except OSError:
            continue
        if progress is not None:
            progress(f"parsing {path.name}")
        try:
            if parse_os_ecuc(path):
                candidates.append(str(path))
        except ET.ParseError:
            continue
    return candidates


def discover_arxml(
    project_dir: str | Path,
    progress: Callable[[str], None] | None = None,
) -> list[str]:
    """ARXML files under the project that define at least one stack.

    Two passes, because a SIP tree carries hundreds of MB of module ARXML
    and the content probe reads a file end-to-end when the needle is
    absent: files whose NAME already suggests an Os ECUC export (both "os"
    and "ecuc" in the basename -- DaVinci names them
    ``<Ecu>_Os_Os_ecuc.arxml``) are probed first, and only if none of them
    defines a stack does the sweep widen to every remaining .arxml. Sorted
    for determinism.
    """
    root = Path(project_dir)
    all_paths = [
        p for p in sorted(root.rglob("*.arxml"))
        if not any(part in _SKIP_DIRS for part in p.parts)
    ]
    plausible = [
        p for p in all_paths
        if "os" in p.name.lower() and "ecuc" in p.name.lower()
    ]
    candidates = _probe_files(plausible, progress)
    if candidates:
        return candidates
    rest = [p for p in all_paths if p not in set(plausible)]
    return _probe_files(rest, progress)

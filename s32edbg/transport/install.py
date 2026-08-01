"""Locate an installed SEGGER J-Link package.

There is no registry key we can rely on across the three installs present on a
typical bench machine, so the directory names are parsed. ``JLink_V962`` -> 9.62.
A plain ``JLink`` directory is accepted with the lowest priority because its
version is only discoverable from the DLL's version resource.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import TransportError

SEARCH_ROOTS = (
    Path(r"C:\Program Files\SEGGER"),
    Path(r"C:\Program Files (x86)\SEGGER"),
)

#: Override with S32EDBG_JLINK_DIR to pin a specific install.
ENV_OVERRIDE = "S32EDBG_JLINK_DIR"

_VERSION_RE = re.compile(r"^JLink_V(\d)(\d\d)([a-z]?)$", re.IGNORECASE)


@dataclass(frozen=True)
class JLinkInstall:
    path: Path
    version: tuple[int, int, str]

    @property
    def version_str(self) -> str:
        major, minor, suffix = self.version
        return f"{major}.{minor:02d}{suffix}"

    @property
    def commander(self) -> Path:
        return self.path / "JLink.exe"

    @property
    def jtagload(self) -> Path:
        return self.path / "JTAGLoad.exe"

    def dll(self, *, want_64bit: bool = True) -> Path:
        """The interface DLL matching the host Python's architecture.

        JLink_x64.dll is the 64-bit build; JLinkARM.dll is 32-bit. A 64-bit
        Python must load the former.
        """
        return self.path / ("JLink_x64.dll" if want_64bit else "JLinkARM.dll")


def _parse_dir(name: str) -> tuple[int, int, str] | None:
    m = _VERSION_RE.match(name)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), m.group(3).lower()


def discover(explicit: str | os.PathLike[str] | None = None) -> JLinkInstall:
    """Return the best available install, newest first.

    Raises TransportError with the roots searched if nothing usable is found.
    """
    candidates: list[JLinkInstall] = []

    override = explicit or os.environ.get(ENV_OVERRIDE)
    if override:
        path = Path(override)
        if not path.is_dir():
            raise TransportError(f"J-Link directory does not exist: {path}")
        version = _parse_dir(path.name) or (0, 0, "")
        return JLinkInstall(path=path, version=version)

    for root in SEARCH_ROOTS:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir():
                continue
            version = _parse_dir(child.name)
            if version is not None:
                candidates.append(JLinkInstall(child, version))
            elif child.name.lower() == "jlink":
                candidates.append(JLinkInstall(child, (0, 0, "")))

    usable = [c for c in candidates if c.commander.is_file()]
    if not usable:
        roots = ", ".join(str(r) for r in SEARCH_ROOTS)
        raise TransportError(
            f"no J-Link installation with JLink.exe found under {roots}. "
            f"Set {ENV_OVERRIDE} to point at one."
        )
    usable.sort(key=lambda c: c.version, reverse=True)
    return usable[0]


def list_installs() -> list[JLinkInstall]:
    found: list[JLinkInstall] = []
    for root in SEARCH_ROOTS:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            version = _parse_dir(child.name)
            if version is not None or child.name.lower() == "jlink":
                found.append(JLinkInstall(child, version or (0, 0, "")))
    return found

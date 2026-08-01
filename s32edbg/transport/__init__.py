"""Probe transports: ctypes DLL, J-Link Commander, and SVF via JTAGLoad."""

from .base import DP_REG_CTRL_STAT, DP_REG_DPIDR, DP_REG_RDBUFF, DP_REG_SELECT, ProbeInfo, Tif, Transport
from .install import JLinkInstall, discover, list_installs

__all__ = [
    "Transport",
    "Tif",
    "ProbeInfo",
    "JLinkInstall",
    "discover",
    "list_installs",
    "DP_REG_DPIDR",
    "DP_REG_CTRL_STAT",
    "DP_REG_SELECT",
    "DP_REG_RDBUFF",
]


def make_transport(kind: str, **kwargs: object) -> Transport:
    """Factory so the CLI does not import backends it will not use."""
    if kind == "dll":
        from .dll import DllTransport

        return DllTransport(**kwargs)  # type: ignore[arg-type]
    if kind == "commander":
        from .commander import CommanderTransport

        return CommanderTransport(**kwargs)  # type: ignore[arg-type]
    raise ValueError(f"unknown transport {kind!r}; expected 'dll' or 'commander'")

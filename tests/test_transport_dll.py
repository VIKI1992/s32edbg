"""The ctypes transport's handshake with the J-Link DLL, without a DLL.

Exists because of the 2026-10-30 bench stall: with no error/warning handler
installed, the DLL shows every ``***** Error:`` / ``***** Warning:`` as a
modal Qt message box (via JLinkGUIServer.exe) and blocks the calling thread
until someone clicks it. The DLL's own connect raises both whenever the R52
debug frames sit behind the still-closed gate -- the normal state before
`gates apply` -- so every scripted first connect after a power cycle hung
with nothing on stdout. No hardware required.
"""

from __future__ import annotations

import ctypes
import inspect
from pathlib import Path

import pytest

from s32edbg.transport import dll


class FakeFn:
    """One DLL export: records its calls, returns a configurable value."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple] = []
        self.returns = None
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.returns


class FakeLib:
    """What ctypes.CDLL hands back: any attribute is an export."""

    def __init__(self, missing: frozenset[str] = frozenset()) -> None:
        self.fns: dict[str, FakeFn] = {}
        self.missing = missing

    def __getattr__(self, name: str) -> FakeFn:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self.missing:
            raise AttributeError(name)
        fns = self.__dict__["fns"]
        if name not in fns:
            fns[name] = FakeFn(name)
        return fns[name]


class FakeInstall:
    def __init__(self, dll_path: Path) -> None:
        self._dll = dll_path
        self.path = dll_path.parent
        self.version_str = "9.62"

    def dll(self, want_64bit: bool = True) -> Path:
        return self._dll


def _open_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lib: FakeLib) -> dll.DllTransport:
    fake_dll = tmp_path / "JLink_x64.dll"
    fake_dll.write_bytes(b"not a dll")
    monkeypatch.setattr(ctypes, "CDLL", lambda path: lib)
    t = dll.DllTransport(install=FakeInstall(fake_dll))  # type: ignore[arg-type]
    t.open()
    return t


def test_open_installs_an_error_and_a_warning_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib = FakeLib()
    t = _open_transport(tmp_path, monkeypatch, lib)

    for name in ("JLINK_SetErrorOutHandler", "JLINK_SetWarnOutHandler"):
        fn = lib.fns[name]
        assert len(fn.calls) == 1, f"{name} called {len(fn.calls)} times"
        (handler,) = fn.calls[0]
        assert isinstance(handler, ctypes._CFuncPtr), f"{name} was not given a ctypes callback"
        assert fn.argtypes == [dll._MSG_HANDLER]

    # What the DLL said on 2026-10-30, as it says it: leading spaces, trailing newline.
    (err_handler,) = lib.fns["JLINK_SetErrorOutHandler"].calls[0]
    (warn_handler,) = lib.fns["JLINK_SetWarnOutHandler"].calls[0]
    err_handler(b"  Could not determine core debug registers + core CTI. Debugging not possible\n")
    warn_handler(b"Found 1 device in JTAG chain, IRPost/DRPost != 0. Reset IRPost/DRPost to 0.")
    err_handler(None)
    assert t.dll_messages == [
        "error: Could not determine core debug registers + core CTI. Debugging not possible",
        "warning: Found 1 device in JTAG chain, IRPost/DRPost != 0. Reset IRPost/DRPost to 0.",
    ]
    assert not t.capability_warnings


def test_the_handlers_outlive_the_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The DLL keeps the raw thunk pointer and ctypes never unloads it, so the
    callback objects must stay referenced after close() and after the
    transport itself is gone."""
    before = len(dll._LIVE_MESSAGE_HANDLERS)
    lib = FakeLib()
    t = _open_transport(tmp_path, monkeypatch, lib)
    (err_handler,) = lib.fns["JLINK_SetErrorOutHandler"].calls[0]
    t.close()
    del t
    assert len(dll._LIVE_MESSAGE_HANDLERS) == before + 2
    assert any(h is err_handler for h in dll._LIVE_MESSAGE_HANDLERS)


def test_a_dll_without_the_exports_still_opens_and_says_the_dialogs_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = FakeLib(
        missing=frozenset(
            {
                "JLINK_SetErrorOutHandler",
                "JLINKARM_SetErrorOutHandler",
                "JLINK_SetWarnOutHandler",
                "JLINKARM_SetWarnOutHandler",
            }
        )
    )
    t = _open_transport(tmp_path, monkeypatch, lib)
    assert t._opened
    assert any("modal dialogs" in w for w in t.capability_warnings)


def test_the_handlers_are_installed_before_the_dll_can_speak() -> None:
    """Order inside open(): bind, handlers, then everything that calls into the
    DLL (SetLogFile, serial select, Open). An Open-time error is still a
    dialog otherwise."""
    src = inspect.getsource(dll.DllTransport.open)
    assert src.index("self._bind()") < src.index("self._install_message_handlers()") < src.index("self._open()")

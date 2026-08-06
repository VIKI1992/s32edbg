"""Machine mode: the NDJSON session protocol for IDE frontends.

``s32edbg debug --machine`` turns the debug session into a line protocol a
frontend (the vscode/ extension) can drive over pipes:

- **stdin** carries the same session command lines the interactive REPL takes,
  one per line: ``halt``, ``bp set 0x321D2EE6``, ``use rtu0.1``, ``load ...``.
- **stdout** carries only JSON objects, one per line, UTF-8, flushed per line.
  Every integer that the JSON contract renders as ``"0x..."`` stays a string.
- **stderr** carries everything a human would have seen -- the whole Rich
  console stream is redirected there, so a frontend can log it verbatim.

The stream on stdout:

- On session open:   ``{"event":"ready", "version":..., "protocol":1, ...}``
- Per input line, exactly one result envelope, in input order::

      {"seq":N, "ok":true,  "cmd":"core halt", "result":<payload|list|null>}
      {"seq":N, "ok":false, "cmd":"...", "error":{"kind":..., "message":...},
       "result":<whatever was captured before the failure, or null>}

  ``error.kind`` mirrors the exit-code contract: ``usage`` (1), ``tool`` (2),
  ``refused`` (3), ``aborted`` (130), plus ``unknown-command`` and
  ``internal``.
- Long operations may emit progress lines *before* their envelope::

      {"seq":N, "event":"progress", "message":"..."}

- On close: ``{"event":"bye", "restore_notes":[...], "kept":false}`` and exit.

``seq`` is the 1-based count of non-empty input lines. The session dispatch is
strictly sequential, so responses arrive in request order and a frontend can
correlate by counting.

Mechanism notes. Rich ``Console()`` instances resolve ``sys.stdout`` lazily
(both the CLI's module-global console and the fresh ones ``report/emit.py``
creates), so pointing ``sys.stdout`` at ``sys.stderr`` for the life of the
session reroutes every human print path in one move; the protocol writes to
the saved real stdout handle. Command payloads are captured by making
``Ctx.json_out`` truthy (the ``JSON_SINK`` sentinel) so every command takes
its existing ``if ctx.json_out:`` branch, with ``cli._emit_json`` handing the
payload here instead of printing it.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

#: Placed in Ctx.json_out while machine mode runs. Truthy -- commands take
#: their JSON branch -- and never a real path, so nothing is written to disk.
JSON_SINK = "<machine>"

#: Bumped when the envelope shapes change incompatibly.
PROTOCOL = 1


class MachineIO:
    """Owns the real stdout and the per-command payload sink."""

    def __init__(self, out: TextIO) -> None:
        self.out = out
        #: 1-based sequence number of the line currently being dispatched;
        #: stamped on progress events and the result envelope.
        self.seq = 0
        self._payloads: list[Any] = []

    def emit(self, obj: dict[str, Any]) -> None:
        self.out.write(json.dumps(obj, default=str, separators=(",", ":")) + "\n")
        self.out.flush()

    def event(self, name: str, **fields: Any) -> None:
        self.emit({"event": name, **fields})

    def progress(self, message: str) -> None:
        self.emit({"seq": self.seq, "event": "progress", "message": message})

    def take_payloads(self) -> list[Any]:
        taken, self._payloads = self._payloads, []
        return taken

    def capture(self, payload: Any) -> None:
        self._payloads.append(payload)


_ACTIVE: MachineIO | None = None


def active() -> MachineIO | None:
    return _ACTIVE


def capture(payload: Any) -> bool:
    """Route a would-be JSON emission into the sink. False when inactive."""
    if _ACTIVE is None:
        return False
    _ACTIVE.capture(payload)
    return True


def progress(message: str) -> None:
    """Emit a progress event for the in-flight command. No-op when inactive."""
    if _ACTIVE is not None:
        _ACTIVE.progress(message)


def begin() -> MachineIO:
    """Enter machine mode: stdout becomes the protocol channel, humans get stderr."""
    global _ACTIVE
    real = sys.stdout
    for stream, line_buffered in ((real, True), (sys.stderr, False)):
        try:
            stream.reconfigure(  # type: ignore[union-attr]
                encoding="utf-8", errors="replace", line_buffering=line_buffered
            )
        except Exception:
            pass  # not a TextIOWrapper (tests, exotic hosts) -- carry on
    sys.stdout = sys.stderr
    _ACTIVE = MachineIO(real)
    return _ACTIVE


def end() -> None:
    """Leave machine mode, restoring the real stdout."""
    global _ACTIVE
    if _ACTIVE is not None:
        sys.stdout = _ACTIVE.out
        _ACTIVE = None

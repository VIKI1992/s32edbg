"""ELF/DWARF symbols, disassembly and stack unwinding for the IDE frontend.

The base tool deliberately reads only PT_LOAD headers (`s32edbg/elf.py`) and
carries no dependencies beyond click+rich. Source-level debugging needs more:
line tables, function names, frame info, a disassembler. That lives here,
behind the ``ide`` optional dependency group (``pip install -e .[ide]`` →
pyelftools + capstone), imported lazily so every existing command keeps
working on an installation without the extras.
"""

from __future__ import annotations

from ..errors import S32EError

INSTALL_HINT = (
    "this command needs the IDE extras (pyelftools + capstone): "
    "pip install -e .[ide]"
)


class SymbolsUnavailable(S32EError):
    """Raised when the ide extras are not installed."""


def require_pyelftools():
    try:
        from elftools.elf.elffile import ELFFile  # noqa: F401
    except ImportError as exc:
        raise SymbolsUnavailable(INSTALL_HINT) from exc


def require_capstone():
    try:
        import capstone  # noqa: F401
    except ImportError as exc:
        raise SymbolsUnavailable(INSTALL_HINT) from exc

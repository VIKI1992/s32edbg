"""Exception types for s32edbg."""


class S32EError(Exception):
    """Base for all tool errors."""


class TransportError(S32EError):
    """Probe/DLL/Commander level failure."""


class ProbeNotFound(TransportError):
    """No J-Link enumerated."""


class UnsupportedOperation(TransportError):
    """This backend cannot do this at all.

    A TransportError rather than NotImplementedError so it travels the same
    route as every other tool error: caught by the CLI, reported as a message,
    exit code 2. A bare NotImplementedError escapes the handlers and surfaces as
    a traceback with the wrong exit code.
    """


class DapError(S32EError):
    """A DP or AP access failed or returned a fault."""


class PowerUpError(DapError):
    """The debug power-up handshake did not complete."""


class UnsafeOperation(S32EError):
    """Refused because it would risk the target or is disabled by policy.

    Raised for the guarded paths: SWD switching without the explicit opt-in,
    AXI-AP traffic while functional reset is asserted (RM Table 957 note --
    recovery needs a destructive reset), any SDA-AP write (the HSE
    challenge/response is one-shot), and the drive-pin boundary-scan
    instructions EXTEST/HIGHZ/CLAMP without their explicit opt-in (each
    asserts the internal MCU system reset, RM 136.4.4).
    """


class RomTableError(S32EError):
    """Malformed ROM table or component ID block."""


class ElfError(S32EError):
    """The file is not an ELF this tool can load.

    Raised by ``elf.parse`` for anything outside the supported shape: not an
    ELF at all, ELF64, big-endian, not ARM, no entry point, a truncated
    program-header table, or overlapping load segments. An S32EError so it
    travels the standard route (message + exit 2 one-shot, printed error inside
    a session); the CLI downgrades argument-quality cases (missing file, not an
    ELF) to a usage error.
    """


class BsdlError(S32EError):
    """The file is not a BSDL this tool can parse.

    Raised by ``bsdl.parse_bsdl`` for anything outside the supported shape:
    not a BSDL at all, a boundary-register list with gaps or the wrong count,
    an IDCODE without the IEEE-mandated LSB of 1, or opcodes whose width
    disagrees with INSTRUCTION_LENGTH. The parser targets the NXP BSDL_GEN
    dialect the bundled file uses, not general VHDL; a foreign but valid BSDL
    failing here is a parser limitation worth reporting, and the error says
    which construct broke.
    """


class LoadError(S32EError):
    """A step of the RAM-load flow failed on the target.

    Distinct from DapError: the access worked at the DAP level but the flow's
    oracle failed -- an init-action poll timed out, the SRAMC post-init witness
    read non-zero, the core never halted in the park loop, or the post-load
    verify mismatched.
    """


class FlashError(S32EError):
    """A step of the external-flash flow failed on the target.

    The flash sibling of LoadError: the DAP access itself worked but the
    flow's oracle failed -- a QSPI controller wait timed out, the JEDEC
    identity was wrong or unreadable, the M33 applet returned a nonzero rc
    (the decoded status mailbox travels in the message), or a verify read
    back something other than what was programmed.
    """

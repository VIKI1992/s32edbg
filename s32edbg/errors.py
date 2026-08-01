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
    instructions EXTEST/HIGHZ/CLAMP.
    """


class RomTableError(S32EError):
    """Malformed ROM table or component ID block."""

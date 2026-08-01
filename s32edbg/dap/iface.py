"""Detect which debug protocol is actually live.

The old `iface show` selected JTAG, read the DP, and reported "JTAG is
reachable" -- which is an assertion about what it just did, not an observation
about the target. This module gathers evidence instead, and is willing to
answer "unknown".

The discriminator is the JTAGC IDCODE. The system JTAG controller sits in
parallel with the SWJ-DP on the same pins (RM 135.5.1) and is reachable *only*
over JTAG, so a matching IDCODE proves the port is in JTAG mode. A DPIDR read
alone does not: the DAP answers over either protocol.

Nothing here ever switches protocol. Probing SWD means sending the switch
sequence, and RM 135.5.1 makes that a one-way door -- so it stays behind the
same opt-in as `iface swd` itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..data.known_ids import JTAG_IDCODES
from ..errors import S32EError
from ..transport.base import Transport
from .adiv6 import Dap


@dataclass
class InterfaceState:
    reported: str = "unknown"  #: "jtag" | "swd" | "unknown"
    confidence: str = "none"  #: "proven" | "inferred" | "none"
    idcode: int | None = None
    dpidr: int | None = None
    evidence: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "reported": self.reported,
            "confidence": self.confidence,
            "idcode": f"0x{self.idcode:08X}" if self.idcode is not None else None,
            "dpidr": f"0x{self.dpidr:08X}" if self.dpidr is not None else None,
            "evidence": self.evidence,
            "limitations": self.limitations,
        }


def detect(t: Transport, dap: Dap | None) -> InterfaceState:
    """Work out the live protocol from what answers, without switching."""
    state = InterfaceState()

    # 1. JTAGC IDCODE -- proof of JTAG when it matches, because the JTAGC TAP
    #    is unreachable over SWD.
    from ..jtag import tap as tap_mod

    try:
        state.idcode = tap_mod.read_idcode(t)
    except (S32EError, NotImplementedError) as exc:
        state.evidence.append(f"no IDCODE: {exc}")
    else:
        if state.idcode in JTAG_IDCODES:
            state.reported, state.confidence = "jtag", "proven"
            state.evidence.append(
                f"IDCODE 0x{state.idcode:08X} matches {JTAG_IDCODES[state.idcode]} "
                "(RM Table 961). The JTAGC TAP only answers over JTAG, so the port "
                "is in JTAG mode."
            )
        else:
            state.evidence.append(
                f"IDCODE read as 0x{state.idcode:08X}, which RM Table 961 does not list"
            )

    # 2. DPIDR -- shows the DAP is answering, but over either protocol.
    if dap is not None:
        try:
            state.dpidr = dap.read_dpidr().raw
        except S32EError as exc:
            state.evidence.append(f"no DPIDR: {exc}")
        else:
            state.evidence.append(f"DPIDR 0x{state.dpidr:08X} -- the DAP is answering")
            if state.reported == "unknown" and state.dpidr:
                state.reported, state.confidence = "jtag", "inferred"
                state.evidence.append(
                    "assuming JTAG because that is the reset default (RM 135.5.1) and "
                    "nothing in this session switched away from it"
                )

    # 3. Pipelined AP reads corroborate a JTAG-DP. JLink.exe's own ReadDP help:
    #    "SWD: Data is returned immediately. JTAG: Data of the previous read is
    #    returned." Corroboration only -- whether the DLL wrapper hides the
    #    pipeline is the very thing calibrate_ap_read() exists to settle.
    if dap is not None and dap.ap_read_pipelined:
        state.evidence.append(
            "AP reads behave as pipelined, which is JTAG-DP behaviour (corroborating, "
            "not conclusive)"
        )

    if state.reported == "unknown":
        state.limitations.append(
            "Neither the JTAGC nor the DAP answered. That may mean the port is already "
            "in SWD, or that the target is unpowered or held in reset. SWD cannot be "
            "probed without the one-way opt-in, since probing it means switching."
        )
    for cap in getattr(t, "capability_warnings", []):
        state.limitations.append(cap)
    return state

"""Rules-based diagnosis of a non-responding target.

Written from a real failure on this bench: probe healthy, target rail at 3.3 V,
and yet ``TotalIRLen = ?, IRPrint = 0x..000000000000000000000000`` with every
subsequent DP/AP read returning 0 and "(0 read repetitions needed)".

The point of encoding it is that the probe-side symptoms are nearly identical for
several very different root causes, and the distinguishing measurements are on
the board, not on the PC. This module turns the observations into a ranked list
of checks with the RM and schematic references attached.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Observation:
    vtref_mv: int | None
    pins: dict[str, int]
    idcode: int | None
    total_ir_len: int | None
    ir_print_all_zero: bool
    connect_failed: bool
    capability_warnings: list[str] = field(default_factory=list)
    #: Repeated ITarget readings. Movement here *may* mean the board is cycling
    #: rather than statically held -- but see current_is_moving for the two ways
    #: that inference goes wrong.
    itarget_samples: list[int] = field(default_factory=list)
    flash_blank: bool = False
    #: True when a connect (and therefore a reset-pin toggle) happened while the
    #: current was being sampled. Measured on this bench: with no connects the
    #: current sat at a flat 38 mA, and interleaving connects made it swing
    #: 38-51 mA. Without this flag the tool reads its own reset toggling as
    #: evidence that the target is rebooting.
    resets_issued_during_sampling: bool = False

    @property
    def current_is_moving(self) -> bool:
        """Whether supply current genuinely indicates the target is cycling.

        Two false positives to avoid. A connect toggles the reset pin, and on
        this board MCU_RESET_b is tied to SBC-PMIC_RSTb through R439 (S3 DNP),
        so sampling around connects measures the debugger, not the target. And a
        *running application* varies its own current perfectly normally -- a
        board happily transmitting CAN swings just as much as one rebooting.
        """
        if len(self.itarget_samples) < 2 or self.resets_issued_during_sampling:
            return False
        return (max(self.itarget_samples) - min(self.itarget_samples)) >= 5


@dataclass
class Finding:
    severity: str  # "blocker" | "warning" | "ok"
    title: str
    detail: str
    checks: list[str] = field(default_factory=list)


#: RM Table 961 values for this family.
KNOWN_IDCODES = (0x0831B01D, 0x0831F01D)


def diagnose(obs: Observation) -> list[Finding]:
    findings: list[Finding] = []

    # ---- power ----------------------------------------------------------
    if obs.vtref_mv is None:
        findings.append(
            Finding(
                "blocker",
                "No VTREF reading",
                "The probe cannot measure a target reference voltage, so it may not be "
                "connected to the board at all.",
                [
                    "Confirm the ribbon is seated and oriented correctly.",
                    "J9 pin 2 carries +3V3 directly; J3 pin 2 carries DBG_VCC through "
                    "ferrite L13. Measure at TP55 for J3.",
                ],
            )
        )
    elif obs.vtref_mv < 2000:
        findings.append(
            Finding(
                "blocker",
                f"VTREF is only {obs.vtref_mv} mV",
                "The debug interface is a 3.3 V domain on this device (RM Table 979).",
                ["Check the +3V3 rail and, on J3, ferrite L13 and caps C542/C543."],
            )
        )
    else:
        findings.append(
            Finding("ok", f"VTREF {obs.vtref_mv} mV", "Target I/O rail is present and at 3.3 V.")
        )

    # ---- activity: cycling vs statically held ---------------------------
    chain_dead = obs.ir_print_all_zero or obs.idcode in (None, 0x00000000)

    if chain_dead and obs.current_is_moving:
        lo, hi = min(obs.itarget_samples), max(obs.itarget_samples)
        findings.append(
            Finding(
                "blocker",
                f"Device may be cycling rather than statically dead (ITarget swings {lo}-{hi} mA)",
                "Confirm this before acting on it: sample the current with no connect in "
                "between, because a connect toggles the reset pin and a running application "
                "varies its own current anyway. A flat reading means the target is fine and "
                "the swing was the debugger.\n"
                "If the swing is real, moving supply current with a dead TAP is the "
                "important combination. The "
                "silicon is executing and resetting, so this is not a broken TDO wire. RM "
                "Table 301 is the key: destructive reset does NOT reset the JTAGC -- only "
                "POR does. So a dead TAP on a live board means the device is being held in, "
                "or repeatedly driven through, POR.\n"
                "        With external flash blank, BootROM fails every attempt and issues a "
                "functional reset each time (RM 62.12). Functional resets escalate to "
                "destructive (RM 60.5.2.4), and destructive resets escalate to the DEST0 "
                "state which, per RM 60.5.6, the device \"remains there until a POR event "
                "starts a POR sequence\".",
                [
                    "FIRST -- power-cycle the ECU. RM 60.5.6 says destructive-reset "
                    "escalation persists until a POR, and a power cycle is the documented "
                    "way out. Re-run `s32edbg doctor` immediately afterwards, and remember "
                    "LADDER-1 must be re-applied after power loss.",
                    "THEN -- measure MCU_POR_b at the S32E ball AC28. If it is low the device "
                    "is in POR and no JTAG activity is possible. It is driven by AND gate U20 "
                    "(74LVC1G08GW-Q100), whose other input is SBC-PMIC_PGOOD -- so a latched "
                    "FS8613 or PF5030 fault holds POR_B low and a debugger CANNOT override it.",
                    "Measure SBC-PMIC_PGOOD directly. If it is low, the SBC/PMIC is unhappy "
                    "and that is the whole problem; the S32E is a bystander.",
                    "Assert WDGDIS on J9 pin 28 to stop the SBC and PMIC watchdogs. The "
                    "schematic says exactly this: \"WDGDIS signal is used to enter DEBUG mode "
                    "via ETAS debugger, if required\". It needs at least 3.5 V (level shifter "
                    "U1), and it is only reachable on J9 -- not on J3.",
                    "Root cause is the blank flash. The bootloader that runs on the M33 and "
                    "releases the R52 cores is gone, so the boot-failure cascade repeats "
                    "forever. RM 62.3.1 names Serial Boot as the recovery path for "
                    "non-responsive parts; BOOTMOD[1:0] is strapped 0b00 with TP40/TP41 "
                    "available, and the schematic notes \"BOOT from RCON (requires manual "
                    "TP40 PU)\".",
                ],
            )
        )

    if chain_dead:
        findings.append(
            Finding(
                "blocker",
                "JTAG chain is not responding (TDO never rises during an IR scan)",
                "An all-zero IRPrint means TDO stayed low for the whole instruction-register "
                "scan. RM L421141 says Capture-IR loads the instruction shift register with "
                "0b1, so a live TAP must shift out at least one 1. Note that TDO reading 0 "
                "while idle proves nothing on its own: RM Table 987 says the TDO buffer is "
                "disabled outside Shift-IR/Shift-DR, and the board's TDO pull-up R445 is DNP, "
                "so the net floats when the TAP is inactive.\n"
                "        RM Table 301 narrows this down hard: the JTAGC is reset by exactly "
                "two things -- POR, and JCOMP going low. Destructive and functional resets do "
                "NOT touch it. So with the I/O rail up and the probe healthy, either JCOMP is "
                "low or the device is sitting in POR. Both are one measurement each.",
                [
                    "CHECK 0 -- are the onboard PMIC and SBC enable switches ON? This is the "
                    "first thing to rule out and it has bitten this bench already. With the "
                    "PMIC disabled, BUCK1 is off, so the S32E's +0V8 core rail is down and the "
                    "device sits in POR with the JTAGC held in reset -- while VTREF still reads "
                    "a perfectly healthy 3.3 V from a different source. A dead TAP on an "
                    "apparently powered board is the expected result, not a fault.",
                    "CHECK 1 -- is the 0.8 V core rail up? MEASURE +0V8 (sheet 11 appears to "
                    "put a test point on it, TP66 -- verify). This is the highest-value "
                    "measurement and the easiest to get wrong, because VTREF reading a healthy "
                    "3.3 V tells you NOTHING about it: on this SOM the 3.3 V comes from the "
                    "FS8613 SBC's VPRE pre-regulator, while the S32E core rail +0V8 comes from "
                    "a completely separate source, PMIC (PF5030) BUCK1 via L10. The SBC can be "
                    "happily running VPRE with the PMIC's bucks off. RM Table 295 lists both "
                    "\"Core supply (0.8 V): VDD\" and \"Critical LVDs\" as POR sources, and POR "
                    "is the one reset that holds the JTAGC in reset (Table 301). A missing core "
                    "rail therefore reproduces every symptom seen here exactly: 3.3 V present, "
                    "current flowing, TAP stone dead. Check +1V1 (PMIC BUCK2 via L11) too.",
                    "CHECK 2 -- is JCOMP low? Active-high with only a weak internal pull-down "
                    "(RM 60.5.4.1, 136.5.5). It should sit at ~3.3 V, held up by R447 "
                    "(4.7 kOhm). No test point exists, so probe R447 or ball AG8 directly. "
                    "If the probe reaches J9 through an adapter, note J9 pin 10 is AUR_JCOMP "
                    "while pin 10 of a standard ARM 20-pin connector is GND -- an adapter "
                    "that grounds it beats the pull-up and holds the TAP in reset. Also "
                    "confirm R447 is fitted and R448 (pull-down, DNP) is not.",
                    "CHECK 2 -- is the device in POR? Measure MCU_POR_b at ball AC28; low "
                    "means POR is asserted and no JTAG is possible. It is the output of AND "
                    "gate U20 (74LVC1G08GW-Q100) whose other input is SBC-PMIC_PGOOD, so a "
                    "latched FS8613/PF5030 fault holds it low and a debugger CANNOT override "
                    "it. Measure SBC-PMIC_PGOOD too -- if that is low, the SBC/PMIC is the "
                    "whole problem and the S32E is a bystander.",
                    "Check the five series resistors in the J9 path are still populated: "
                    "R5 (TCK), R6 (TMS), R7 (JCOMP), R8 (TDI), R15 (TDO). The schematic "
                    "carries the note \"0 Ohm resistors to be removed after verification\", "
                    "so on a verified board they may have been depopulated.",
                    "Check TDO continuity from TP59 to the S32E ball AF8.",
                    "Confirm the core supply is up, not just the 3.3 V I/O rail. RESET_B is "
                    "tied to SBC-PMIC_RSTb through R439 (0 Ohm, populated; the S3 isolation "
                    "jumper is DNP), so a latched FS8613/PF5030 fault can hold the device in "
                    "reset. RM Table 295 lists RESET_B as a POR source, and POR is the one "
                    "reset that does hold the JTAGC in reset (RM Table 301).",
                ],
            )
        )
    elif obs.idcode in KNOWN_IDCODES:
        findings.append(
            Finding(
                "ok",
                f"TAP alive, IDCODE 0x{obs.idcode:08X}",
                "Matches RM Table 961 for this family.",
            )
        )
    elif obs.idcode is not None:
        findings.append(
            Finding(
                "warning",
                f"Unexpected IDCODE 0x{obs.idcode:08X}",
                "RM Table 961 lists 0x0831B01D (S32E270) and 0x0831F01D (S32E277/8/9). "
                "Note RM L412549: bits[20:17] come from the secure fuse box and are not "
                "valid immediately out of reset, so re-read before concluding.",
                ["Re-read the IDCODE; if it is stable, confirm the part number."],
            )
        )

    # ---- IR width -------------------------------------------------------
    # Both widths are normal, and which one you see says which chain you are on.
    # Measured on this bench: cold the chain is JTAGC alone at 8 bits; after the
    # RM Table 984 preamble writes TLM_TAP_SELECT_TDR = 0x80000011 the TAP
    # Linking Module adds JTAGC_AE and the chain becomes 16 bits, and stays
    # there. So this is a state report, not a contradiction to resolve.
    if obs.total_ir_len == 8:
        findings.append(
            Finding(
                "ok",
                "TotalIRLen = 8 (JTAGC alone)",
                "The cold chain: the Application Extension die's TAP is not linked in, so "
                "RM Table 958's 8-bit opcodes address the JTAGC. Boundary scan needs the "
                "RM Table 984 preamble first, which switches this to 16.",
            )
        )
    elif obs.total_ir_len == 16:
        findings.append(
            Finding(
                "ok",
                "TotalIRLen = 16 (JTAGC + JTAGC_AE)",
                "The Application Extension TAP is linked into the chain, so RM Table 959's "
                "16-bit opcodes apply. Expected after the RM Table 984 boundary-scan "
                "preamble, which selects it via TLM_TAP_SELECT_TDR and persists. Note the "
                "AE TAP is internal to the SIP -- the board routing no AE JTAG port does "
                "not contradict this.",
            )
        )

    # ---- probe limitations ---------------------------------------------
    for warning in obs.capability_warnings:
        findings.append(
            Finding(
                "blocker",
                f"Probe/DLL limitation: {warning}",
                "This target is a CoreSight SoC-600 DAP reporting DPv3, and its SWJ-DP "
                "requires a dormant-state transition to switch protocols. Support for both "
                "is gated by probe model and firmware.",
                ["Update the J-Link firmware, or use a probe that supports SoC-600."],
            )
        )

    if obs.connect_failed and not chain_dead:
        findings.append(
            Finding(
                "warning",
                "Chain scanned but connect failed",
                "This is expected on this device while the debug-enable gate is closed: the "
                "APB ROM at 0x01BFE000 reads empty, so J-Link cannot identify a core. The "
                "DAP itself is usable and raw DP/AP access still works.",
                ["Run `s32edbg gates apply`, then retry `romdump`."],
            )
        )

    return findings


def summarise(findings: list[Finding]) -> str:
    blockers = sum(1 for f in findings if f.severity == "blocker")
    if blockers:
        return f"{blockers} blocker(s) found"
    warnings = sum(1 for f in findings if f.severity == "warning")
    return f"no blockers, {warnings} warning(s)" if warnings else "all checks passed"

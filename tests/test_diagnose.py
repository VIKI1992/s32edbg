"""Tests for the rules-based diagnosis. No hardware required.

These lock in the reasoning that took a full bench session to work out, so a
future refactor cannot quietly drop a check or reorder the ranking.
"""

from __future__ import annotations

import pytest

from s32edbg import diagnose as diag

HEALTHY_PINS = {"TCK": 0, "TDI": 1, "TDO": 0, "TMS": 1, "TRES": 1, "TRST": 1}


def obs(**kw: object) -> diag.Observation:
    base: dict[str, object] = dict(
        vtref_mv=3307,
        pins=dict(HEALTHY_PINS),
        idcode=0x0831F01D,
        total_ir_len=8,
        ir_print_all_zero=False,
        connect_failed=False,
    )
    base.update(kw)
    return diag.Observation(**base)  # type: ignore[arg-type]


def titles(findings: list[diag.Finding]) -> str:
    return " | ".join(f.title for f in findings)


def all_checks(findings: list[diag.Finding]) -> str:
    return " ".join(c for f in findings for c in f.checks)


def blockers(findings: list[diag.Finding]) -> list[diag.Finding]:
    return [f for f in findings if f.severity == "blocker"]


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_healthy_target_has_no_blockers() -> None:
    findings = diag.diagnose(obs())
    assert blockers(findings) == []
    assert "TAP alive" in titles(findings)
    assert diag.summarise(findings) == "all checks passed"


def test_known_idcode_is_recognised() -> None:
    for idcode in diag.KNOWN_IDCODES:
        findings = diag.diagnose(obs(idcode=idcode))
        assert f"0x{idcode:08X}" in titles(findings)


# --------------------------------------------------------------------------
# The failure this bench actually hit: core rail off, TAP in POR
# --------------------------------------------------------------------------


def test_dead_chain_leads_with_the_power_switches() -> None:
    """CHECK 0 must be the PMIC/SBC switches -- the real root cause here.

    A dead TAP alongside a healthy VTREF is the signature of an unpowered core,
    because the 3.3 V and 0.8 V rails come from different regulators.
    """
    findings = diag.diagnose(obs(idcode=None, ir_print_all_zero=True))
    assert blockers(findings)
    chain = next(f for f in findings if "not responding" in f.title)
    assert chain.checks[0].startswith("CHECK 0")
    assert "PMIC and SBC enable switches" in chain.checks[0]


def test_dead_chain_explains_the_two_rail_trap() -> None:
    findings = diag.diagnose(obs(idcode=None, ir_print_all_zero=True))
    checks = all_checks(findings)
    # The core rail and its source must be named explicitly.
    assert "+0V8" in checks
    assert "BUCK1" in checks
    assert "VPRE" in checks


def test_dead_chain_cites_the_two_jtagc_reset_sources() -> None:
    """RM Table 301: only POR and JCOMP reset the JTAGC. That deduction is the
    whole reason the check list is as short as it is."""
    findings = diag.diagnose(obs(idcode=None, ir_print_all_zero=True))
    detail = " ".join(f.detail for f in findings)
    assert "Table 301" in detail
    assert "POR" in detail and "JCOMP" in detail


def test_dead_chain_offers_jcomp_and_por_measurements() -> None:
    checks = all_checks(diag.diagnose(obs(idcode=None, ir_print_all_zero=True)))
    assert "R447" in checks  # JCOMP pull-up
    assert "AG8" in checks  # JCOMP ball
    assert "AC28" in checks  # POR_B ball
    assert "SBC-PMIC_PGOOD" in checks


@pytest.mark.parametrize("idcode", [None, 0x00000000])
def test_absent_or_zero_idcode_both_count_as_dead(idcode: int | None) -> None:
    assert blockers(diag.diagnose(obs(idcode=idcode)))


def test_ir_print_zero_alone_is_enough_to_flag() -> None:
    """Even with a plausible IDCODE, an all-zero IRPrint means the scan failed."""
    findings = diag.diagnose(obs(ir_print_all_zero=True))
    assert any("not responding" in f.title for f in blockers(findings))


# --------------------------------------------------------------------------
# Activity sampling: cycling vs statically held
# --------------------------------------------------------------------------


def test_moving_current_reports_cycling() -> None:
    findings = diag.diagnose(
        obs(idcode=None, ir_print_all_zero=True, itarget_samples=[38, 51, 38, 51])
    )
    assert any("cycling" in f.title for f in findings)


def test_stable_current_does_not_claim_cycling() -> None:
    """Must not overclaim: identical samples are not evidence of a boot loop."""
    findings = diag.diagnose(
        obs(idcode=None, ir_print_all_zero=True, itarget_samples=[38, 38, 38, 38])
    )
    assert not any("cycling" in f.title for f in findings)


def test_single_sample_is_never_movement() -> None:
    o = obs(itarget_samples=[51])
    assert not o.current_is_moving


def test_small_jitter_is_not_movement() -> None:
    assert not diag.Observation(
        vtref_mv=3300, pins={}, idcode=1, total_ir_len=8,
        ir_print_all_zero=False, connect_failed=False, itarget_samples=[50, 52],
    ).current_is_moving


def test_cycling_is_not_reported_when_chain_is_alive() -> None:
    findings = diag.diagnose(obs(itarget_samples=[38, 51]))
    assert not any("cycling" in f.title for f in findings)


# --------------------------------------------------------------------------
# Power
# --------------------------------------------------------------------------


def test_missing_vtref_is_a_blocker() -> None:
    findings = diag.diagnose(obs(vtref_mv=None))
    assert any("VTREF" in f.title for f in blockers(findings))


def test_low_vtref_is_a_blocker_naming_the_33v_domain() -> None:
    findings = diag.diagnose(obs(vtref_mv=1800))
    blocker = next(f for f in blockers(findings) if "1800" in f.title)
    assert "3.3 V" in blocker.detail


# --------------------------------------------------------------------------
# IR width -- the RM's own contradiction
# --------------------------------------------------------------------------


def test_ir_len_8_is_reported_as_the_cold_chain() -> None:
    """8 bits is JTAGC alone, before the RM Table 984 preamble links the
    Application Extension TAP in. Normal, and it says which opcode table
    applies."""
    findings = diag.diagnose(obs(total_ir_len=8))
    entry = next(f for f in findings if "TotalIRLen = 8" in f.title)
    assert entry.severity == "ok"
    assert "Table 984" in entry.detail


def test_ir_len_16_is_reported_as_the_post_preamble_chain_not_a_warning() -> None:
    """Measured on silicon: the preamble's TLM_TAP_SELECT_TDR write adds
    JTAGC_AE to the chain and the IR width goes 8 -> 16 and stays there.

    This used to warn "the board schematic shows no AE JTAG port, so confirm
    before trusting it". That is true and irrelevant -- the AE TAP is internal
    to the SIP and is linked by the TLM, never routed to a connector -- so the
    warning fired on entirely correct behaviour.
    """
    findings = diag.diagnose(obs(total_ir_len=16))
    entry = next(f for f in findings if "TotalIRLen = 16" in f.title)
    assert entry.severity == "ok"
    assert "JTAGC_AE" in entry.title
    assert "internal to the SIP" in entry.detail


def test_unexpected_idcode_warns_about_fuse_timing() -> None:
    findings = diag.diagnose(obs(idcode=0x12345678))
    entry = next(f for f in findings if "Unexpected IDCODE" in f.title)
    assert entry.severity == "warning"
    assert "secure fuse box" in entry.detail


# --------------------------------------------------------------------------
# Probe limitations and the expected connect failure
# --------------------------------------------------------------------------


def test_capability_warning_becomes_a_blocker() -> None:
    findings = diag.diagnose(obs(capability_warnings=["does not support ARM CoreSight SoC-600"]))
    assert any("SoC-600" in f.title for f in blockers(findings))


def test_connect_failure_with_live_chain_is_only_a_warning() -> None:
    """Expected on this device while the debug gate is closed -- not a blocker."""
    findings = diag.diagnose(obs(connect_failed=True))
    entry = next(f for f in findings if "connect failed" in f.title)
    assert entry.severity == "warning"
    assert "gates apply" in " ".join(entry.checks)


def test_connect_failure_is_not_duplicated_when_chain_is_dead() -> None:
    findings = diag.diagnose(obs(idcode=None, ir_print_all_zero=True, connect_failed=True))
    assert not any("connect failed" in f.title for f in findings)


# --------------------------------------------------------------------------
# Summary line
# --------------------------------------------------------------------------


def test_summarise_counts_blockers_then_warnings() -> None:
    assert "blocker" in diag.summarise(diag.diagnose(obs(vtref_mv=None)))
    assert diag.summarise(diag.diagnose(obs(idcode=0x12345678))) == "no blockers, 1 warning(s)"
    assert diag.summarise([]) == "all checks passed"


def test_every_finding_has_a_valid_severity() -> None:
    for case in (obs(), obs(idcode=None, ir_print_all_zero=True), obs(vtref_mv=None)):
        for f in diag.diagnose(case):
            assert f.severity in {"blocker", "warning", "ok"}
            assert f.title


# ---------------------------------------------------------------------------
# The self-inflicted-evidence regression
# ---------------------------------------------------------------------------


def test_a_current_swing_measured_around_connects_is_not_called_cycling() -> None:
    """Measured on the bench: with no connects the current sat flat at 38 mA,
    and interleaving connects made it swing 38-51 mA. A connect toggles the
    reset pin, and on this board MCU_RESET_b is tied to SBC-PMIC_RSTb through
    R439 with the S3 jumper DNP -- so sampling around connects measures the
    debugger resetting the target, not the target rebooting on its own. Without
    this guard the tool reports its own side effect as a blocker.
    """
    o = obs(
        idcode=None,
        ir_print_all_zero=True,
        itarget_samples=[38, 51, 38, 51],
        resets_issued_during_sampling=True,
    )
    assert not o.current_is_moving
    assert not any("cycling" in t.lower() for t in titles(diag.diagnose(o)))


def test_a_clean_current_swing_still_counts_as_cycling() -> None:
    o = obs(
        idcode=None,
        ir_print_all_zero=True,
        itarget_samples=[38, 51, 38, 51],
        resets_issued_during_sampling=False,
    )
    assert o.current_is_moving


def test_a_flat_current_is_never_called_cycling() -> None:
    assert not obs(itarget_samples=[38, 38, 38, 38]).current_is_moving

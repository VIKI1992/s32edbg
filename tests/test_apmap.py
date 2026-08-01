"""AP discovery and RM Table 957 diffing tests."""

from __future__ import annotations

from conftest import FakeTransport

from s32edbg.dap import apmap as apmap_mod
from s32edbg.dap.adiv6 import AP_IDR, Dap
from s32edbg.data.expected_apmap import (
    AP_APB,
    AP_MDM,
    AP_SDA,
    EXPECTED_APS,
    UNDOCUMENTED_AP_WINDOW,
)


def populate_full_map(t: FakeTransport) -> None:
    """Load every AP IDR that RM Table 957 documents."""
    for exp in EXPECTED_APS:
        if exp.idr is not None:
            t.ap_space[exp.base + AP_IDR] = exp.idr


def test_direct_discovery_finds_the_documented_map() -> None:
    t = FakeTransport(pipelined=False)
    populate_full_map(t)
    dap = Dap(t)
    aps = apmap_mod.discover_direct(dap)
    present = [ap for ap in aps if ap.idr and ap.idr.present]
    assert len(present) == len(EXPECTED_APS)
    assert all(ap.idr_matches_rm for ap in present)


def test_diff_is_clean_when_map_matches_rm() -> None:
    t = FakeTransport(pipelined=False)
    populate_full_map(t)
    result = apmap_mod.discover(Dap(t), methods=("direct",))
    diff = result.diff_against_rm()
    assert diff == {"missing": [], "unexpected": [], "mismatched": []}


def test_missing_ap_is_reported() -> None:
    t = FakeTransport(pipelined=False)
    populate_full_map(t)
    del t.ap_space[AP_SDA + AP_IDR]
    result = apmap_mod.discover(Dap(t), methods=("direct",))
    missing = result.diff_against_rm()["missing"]
    assert len(missing) == 1
    assert f"0x{AP_SDA:08X}" in missing[0]
    assert "SDA-AP" in missing[0]


def test_wrong_idr_is_reported_as_mismatch_not_missing() -> None:
    t = FakeTransport(pipelined=False)
    populate_full_map(t)
    t.ap_space[AP_APB + AP_IDR] = 0x12345678
    diff = apmap_mod.discover(Dap(t), methods=("direct",)).diff_against_rm()
    assert diff["missing"] == []
    assert len(diff["mismatched"]) == 1
    assert "0x12345678" in diff["mismatched"][0]
    assert "0x24770006" in diff["mismatched"][0]


def test_ap_kind_decoded_from_idr() -> None:
    t = FakeTransport(pipelined=False)
    populate_full_map(t)
    result = apmap_mod.discover(Dap(t), methods=("direct",))
    by_base = result.by_base()
    # APB-AP IDR 0x24770006: CLASS 0b1000 -> MEM-AP, TYPE 0x6
    apb = by_base[AP_APB]
    assert apb.idr is not None
    assert apb.idr.class_name == "MEM-AP"
    assert apb.idr.ap_type == 0x6
    assert apb.idr.designer_name == "Arm"
    # MDM-AP IDR 0x001DE040: designer is NXP, class has no defined meaning
    mdm = by_base[AP_MDM]
    assert mdm.idr is not None
    assert mdm.idr.designer_name == "NXP (Freescale)"


def test_unreadable_ap_records_an_error() -> None:
    t = FakeTransport(pipelined=False)
    populate_full_map(t)
    t.unreadable.add(AP_MDM + AP_IDR)
    result = apmap_mod.discover(Dap(t), methods=("direct",))
    mdm = result.by_base()[AP_MDM]
    assert mdm.error == "IDR unreadable"
    assert f"0x{AP_MDM:08X}" in " ".join(result.diff_against_rm()["missing"])


def test_undocumented_window_is_called_out() -> None:
    """RM Table 957 lists nothing between 0x01C4_0000 and 0x01C6_FFFF."""
    t = FakeTransport(pipelined=False)
    populate_full_map(t)
    rogue = UNDOCUMENTED_AP_WINDOW[0]
    t.ap_space[rogue + AP_IDR] = 0x34770017
    dap = Dap(t)

    result = apmap_mod.discover(dap, methods=("direct",))
    # direct discovery only probes documented addresses, so inject it manually
    result.aps.append(
        apmap_mod.DiscoveredAp(base=rogue, idr=apmap_mod.decode_ap_idr(0x34770017))
    )
    unexpected = result.diff_against_rm()["unexpected"]
    assert len(unexpected) == 1
    assert "does not document" in unexpected[0]


def test_absent_ap_reports_absent_not_a_crash() -> None:
    t = FakeTransport(pipelined=False)
    ap = apmap_mod.discover_direct(Dap(t))[0]
    assert ap.describe() == "absent"


def test_notes_mention_assumed_rom_base_when_baseptr_invalid() -> None:
    t = FakeTransport(pipelined=False)
    populate_full_map(t)
    # FakeTransport returns 0 for banked DP reads, so BASEPTR0.VALID is clear.
    result = apmap_mod.discover(Dap(t), methods=("direct",))
    assert result.baseptr is None
    assert any("BASEPTR0.VALID is clear" in n for n in result.notes)

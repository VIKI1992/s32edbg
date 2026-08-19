"""BSDL parser and pin-model tests, against the real bundled file. No hardware.

The bundled SIP_S32S277n8n9_S32E277n8n9_BGA27_831F01D.bsdl is both package
data and the test fixture: every structural assertion here (cell numbers,
opcodes, the lone output2 cell) was read out of that file by hand first, so a
parser regression cannot drift silently.
"""

from __future__ import annotations

import pytest

from s32edbg import bsdl
from s32edbg.errors import BsdlError


@pytest.fixture(scope="module")
def raw() -> bsdl.BsdlFile:
    """The file exactly as NXP wrote it (494 cells)."""
    loaded = bsdl.load_bundled(0x0831_F01D, apply_quirks=False)
    assert loaded is not None
    return loaded


@pytest.fixture(scope="module")
def b() -> bsdl.BsdlFile:
    """The model the tool actually uses: silicon-quirked (464 cells)."""
    loaded = bsdl.load_bundled(0x0831_F01D)
    assert loaded is not None
    return loaded


# --------------------------------------------------------------------------
# Whole-file structure
# --------------------------------------------------------------------------


def test_bundled_file_is_discoverable() -> None:
    names = bsdl.bundled_names()
    assert "SIP_S32S277n8n9_S32E277n8n9_BGA27_831F01D.bsdl" in names


def test_entity_and_identity(raw: bsdl.BsdlFile) -> None:
    assert raw.entity == "SIP_S32S277n8n9_S32E277n8n9_BGA27"
    assert raw.idcode == 0x0831_F01D
    assert raw.instruction_length == 16
    assert raw.instruction_capture == 0x0101
    assert raw.boundary_length == 494
    assert len(raw.cells) == 494
    assert raw.tap_scan_hz == 10.0e6
    assert raw.compliance_patterns == "(POR_B, JCOMP) (11)"


def test_no_parse_notes_on_the_raw_bundled_file(raw: bsdl.BsdlFile) -> None:
    """Every cell's port resolves against the declared ports."""
    assert raw.notes == []


def test_opcodes(b: bsdl.BsdlFile) -> None:
    assert b.opcodes["BYPASS"] == 0xFFFF
    assert b.opcodes["IDCODE"] == 0x0001
    assert b.opcodes["SAMPLE"] == b.opcodes["PRELOAD"] == 0x0202
    assert b.opcodes["EXTEST"] == 0x0404
    # The one whose AE byte is NOT a duplicate -- the reason the BSDL is
    # authoritative at shift time.
    assert b.opcodes["HIGHZ"] == 0x0509
    assert b.opcodes["CLAMP"] == 0x0C0C
    assert b.opcodes["EXT_TEST_CTRL"] == 0x0A00
    assert b.opcodes["JTAGDP_DPACC"] == 0xFA00
    # Commented-out entries (JTAGC_EXTEST_PULSE/TRAIN) must not parse.
    assert "JTAGC_EXTEST_PULSE" not in b.opcodes


def test_private_opcodes_are_patterns(b: bsdl.BsdlFile) -> None:
    assert len(b.private_opcodes) > 100
    assert all(len(p) == 16 for p in b.private_opcodes)
    assert any("X" in p for p in b.private_opcodes)


def test_register_access_carries_declared_lengths(b: bsdl.BsdlFile) -> None:
    assert b.register_access["BOUNDARY"] == (None, ("EXTEST", "SAMPLE", "PRELOAD"))
    # 215 here, 205 in the DESIGN_WARNING literal, 208 on proven silicon --
    # recorded, never shifted (see data/bscan.py EXT_TEST_CTRL_PAYLOAD_*).
    assert b.register_access["EXT_TEST_CTRL_TDR"] == (215, ("EXT_TEST_CTRL",))


def test_design_warnings_survive_verbatim(b: bsdl.BsdlFile) -> None:
    assert "AUR_TXn_N/_P" in b.design_warnings
    assert "DDR0_D_nn pins cannot SAMPLE" in b.design_warnings


# --------------------------------------------------------------------------
# Ports and pin map
# --------------------------------------------------------------------------


def test_port_directions_and_balls(b: bsdl.BsdlFile) -> None:
    assert b.ports["JCOMP"].direction == "in"
    assert b.ports["JCOMP"].balls == ("AG8",)
    assert b.ports["RESET_B"].direction == "inout"
    assert b.ports["DDR0_RESET_N"].direction == "buffer"
    assert b.ports["TDO"].direction == "out"


def test_vector_ports_carry_all_balls(b: bsdl.BsdlFile) -> None:
    sar = b.ports["SAR01_VRH"]
    assert sar.width == 2
    assert sar.balls == ("AF24", "AG24")
    vdd = b.ports["VDD"]
    assert vdd.width == 80
    assert len(vdd.balls) == 80


def test_port_names_are_whitespace_stripped(b: bsdl.BsdlFile) -> None:
    """The file writes ``ETH_0_MII_RXER `` with a stray trailing space."""
    assert "ETH_0_MII_RXER" in b.ports
    assert not any(p != p.strip() for p in b.ports)


# --------------------------------------------------------------------------
# Boundary cells
# --------------------------------------------------------------------------


def test_spot_cells(raw: bsdl.BsdlFile) -> None:
    assert raw.cells[5] == bsdl.BoundaryCell(5, "BC_7", "PBC_07", "bidir", "X", 4, 0, "Z")
    assert raw.cells[145].port == "BOOTMOD_0" and raw.cells[145].ccell == 144
    assert raw.cells[167].port == "RESET_B" and raw.cells[167].ccell == 166
    assert raw.cells[493].port == "DSPI_2_PCS2"
    assert raw.cells[0].function == "internal"
    # Cell 186 is internal with safe *1* -- the safe vector must honor it.
    assert raw.cells[186].function == "internal" and raw.cells[186].safe == "1"


def test_the_lone_output2_cell(raw: bsdl.BsdlFile) -> None:
    c = raw.cells[187]
    assert (c.port, c.function, c.ccell) == ("DDR0_RESET_N", "output2", None)
    others = [c for c in raw.cells if c.function == "output2"]
    assert others == [raw.cells[187]]


def test_cell_functions_are_the_known_set(raw: bsdl.BsdlFile) -> None:
    assert {c.function for c in raw.cells} == {"internal", "control", "bidir", "output2"}
    assert {c.cell for c in raw.cells} == {"BC_1", "BC_2", "BC_7"}


# --------------------------------------------------------------------------
# The silicon quirk: 464 of the 494 cells (bench 2026-08-19)
# --------------------------------------------------------------------------


def test_the_bundled_model_is_the_silicon_quirked_464(
    b: bsdl.BsdlFile, raw: bsdl.BsdlFile
) -> None:
    """Measured with the marker-delay oracle (which read BYPASS=1 and
    IDCODE=32 exactly): this part chains 464 bits. The 30 missing cells are
    the BSDL's single run of 30 consecutive internal cells (262..291), so no
    pins are lost -- everything above shifts down by 30."""
    assert raw.boundary_length == 494
    assert b.boundary_length == 464 and len(b.cells) == 464
    assert any("dropped cells 262..291" in n for n in b.notes)
    assert len(bsdl.pin_rows(b)) == len(bsdl.pin_rows(raw)) == 228
    assert b.cells[167].port == "RESET_B"          # below the cut: unchanged
    assert b.cells[263].port == "LIN_6_TX"         # was 293
    assert b.cells[463].port == "DSPI_2_PCS2"      # was 493


def test_drop_cells_renumbers_control_references(raw: bsdl.BsdlFile) -> None:
    d = bsdl.drop_cells(raw, 262, 30)
    lin6 = next(p for p in bsdl.pin_rows(d) if p.name == "LIN_6_TX")
    assert (lin6.cell, lin6.control_cell) == (263, 262)
    gtm = next(p for p in bsdl.pin_rows(d) if p.name == "GTM_033")
    assert (gtm.cell, gtm.control_cell) == (347, 346)  # was 377/376


def test_drop_cells_refuses_to_lose_information(raw: bsdl.BsdlFile) -> None:
    with pytest.raises(BsdlError, match="belongs to pin"):
        bsdl.drop_cells(raw, 167, 1)  # RESET_B's data cell
    with pytest.raises(BsdlError, match="control cells"):
        bsdl.drop_cells(raw, 166, 1)  # RESET_B's control cell
    with pytest.raises(BsdlError, match="only"):
        bsdl.drop_cells(raw, 490, 10)  # runs off the end


def test_an_explicit_bsdl_path_is_taken_verbatim(tmp_path) -> None:
    """--bsdl is an explicit choice: no quirks are applied to it. The length
    verify then arbitrates against silicon."""
    from importlib import resources

    p = tmp_path / "explicit.bsdl"
    p.write_bytes(
        resources.files("s32edbg.data")
        .joinpath("SIP_S32S277n8n9_S32E277n8n9_BGA27_831F01D.bsdl")
        .read_bytes()
    )
    loaded = bsdl.resolve(str(p), idcode=0x0831_F01D)
    assert loaded is not None and loaded.boundary_length == 494


# --------------------------------------------------------------------------
# Pin view
# --------------------------------------------------------------------------


def test_pin_rows_shape(b: bsdl.BsdlFile) -> None:
    pins = bsdl.pin_rows(b)
    by_name = {p.name: p for p in pins}
    assert len(pins) == 228
    boot = by_name["BOOTMOD_0"]
    assert (boot.cell, boot.control_cell, boot.ball) == (145, 144, "AJ1")
    assert (boot.disval, boot.disable_result) == (0, "Z")
    # No cells at all for compliance pins and linkage ports.
    assert "JCOMP" not in by_name
    assert "POR_B" not in by_name
    assert "AUR_TX0_N" not in by_name


def test_pin_rows_carry_the_siul2_pad_identity(b: bsdl.BsdlFile) -> None:
    """Joined by ball against the generated IOMUX table: PAD_xxx on the main
    die, 'AE MSCR n' on the AE die (the pad name there IS the pin name), None
    for dedicated pins."""
    by_name = {p.name: p for p in bsdl.pin_rows(b)}
    assert by_name["DSPI_2_SCK"].pad == "PAD_086"
    assert by_name["DSPI_2_SCK"].pad_detail == "SIUL2_1 MSCR 86"
    assert by_name["BOOTMOD_0"].pad == "PAD_009"
    assert by_name["PBA_00"].pad == "AE MSCR 0"
    assert by_name["PBA_00"].pad_detail == "SIUL2_AE MSCR 0"
    assert by_name["RESET_B"].pad is None and by_name["RESET_B"].pad_detail is None
    d = by_name["DSPI_2_SCK"].as_dict()
    assert d["pad"] == "PAD_086" and d["pad_detail"] == "SIUL2_1 MSCR 86"


def test_ddr_sample_input_blocked_flags(b: bsdl.BsdlFile) -> None:
    by_name = {p.name: p for p in bsdl.pin_rows(b)}
    assert by_name["DDR0_DQ0"].sample_input_blocked
    assert by_name["DDR0_DMI1"].sample_input_blocked
    assert by_name["DDR0_DQS0_T"].sample_input_blocked
    assert not by_name["DDR0_CA0"].sample_input_blocked
    assert not by_name["DDR0_RESET_N"].sample_input_blocked
    assert not by_name["RESET_B"].sample_input_blocked


def test_decode_sample_maps_cells_to_pins(b: bsdl.BsdlFile) -> None:
    bits = [0] * 496  # byte-rounded capture, as the SVF path produces
    bits[167] = 1     # RESET_B data cell
    bits[166] = 1     # its control cell: driver on (disval 0)
    states = {s.pin.name: s for s in bsdl.decode_sample(b, bits)}
    assert states["RESET_B"].level == 1
    assert states["RESET_B"].driver_enabled is True
    assert states["BOOTMOD_0"].level == 0
    assert states["BOOTMOD_0"].driver_enabled is False
    # output2 has no control cell -> None
    assert states["DDR0_RESET_N"].driver_enabled is None


def test_decode_sample_reverse_orientation(b: bsdl.BsdlFile) -> None:
    bits = [0] * b.boundary_length
    bits[b.boundary_length - 1 - 167] = 1
    states = {s.pin.name: s for s in bsdl.decode_sample(b, bits, reverse=True)}
    assert states["RESET_B"].level == 1


def test_decode_sample_rejects_short_captures(b: bsdl.BsdlFile) -> None:
    with pytest.raises(BsdlError, match="464"):
        bsdl.decode_sample(b, [0] * 100)


def test_name_cells(b: bsdl.BsdlFile) -> None:
    named = bsdl.name_cells(b, [167, 166, 0, 495])
    assert named == ["RESET_B", "cell 166 (control for RESET_B)", "cell 0 (internal)", "cell 495 (pad)"]


# --------------------------------------------------------------------------
# Drive vector
# --------------------------------------------------------------------------


def test_safe_vector_honors_per_cell_safe_values(b: bsdl.BsdlFile) -> None:
    v = bsdl.safe_vector(b)
    assert (v >> 186) & 1 == 1  # internal cell with safe 1
    assert (v >> 145) & 1 == 0  # X counts as 0
    assert (v >> 144) & 1 == 0  # control at disval: driver off


def test_build_drive_vector_single_pin(b: bsdl.BsdlFile) -> None:
    vec, plan = bsdl.build_drive_vector(b, {"BOOTMOD_0": "1"})
    assert (vec >> 145) & 1 == 1
    assert (vec >> 144) & 1 == 1  # control flipped to enable
    # Nothing else moved relative to the safe vector.
    assert vec ^ bsdl.safe_vector(b) == (1 << 145) | (1 << 144)
    assert plan == [
        {"name": "BOOTMOD_0", "ball": "AJ1", "cell": 145, "control_cell": 144,
         "level": 1, "driving": True}
    ]


def test_build_drive_vector_zero_and_z(b: bsdl.BsdlFile) -> None:
    vec, plan = bsdl.build_drive_vector(b, {"RESET_B": "0", "BOOTMOD_1": "z"})
    assert (vec >> 167) & 1 == 0 and (vec >> 166) & 1 == 1  # driven low
    assert (vec >> 147) & 1 == 0 and (vec >> 146) & 1 == 0  # explicit Z
    by_name = {r["name"]: r for r in plan}
    assert by_name["RESET_B"]["driving"] is True and by_name["RESET_B"]["level"] == 0
    assert by_name["BOOTMOD_1"]["driving"] is False and by_name["BOOTMOD_1"]["level"] is None


def test_build_drive_vector_is_case_insensitive(b: bsdl.BsdlFile) -> None:
    vec, _ = bsdl.build_drive_vector(b, {"bootmod_0": "1"})
    assert (vec >> 145) & 1 == 1


def test_drive_refusals(b: bsdl.BsdlFile) -> None:
    with pytest.raises(BsdlError, match="no boundary-scan cell"):
        bsdl.build_drive_vector(b, {"JCOMP": "1"})
    with pytest.raises(BsdlError, match="no boundary-scan cell"):
        bsdl.build_drive_vector(b, {"AUR_TX0_N": "1"})
    with pytest.raises(BsdlError, match="unknown pin"):
        bsdl.build_drive_vector(b, {"NOT_A_PIN": "1"})
    with pytest.raises(BsdlError, match="cannot .*be put in Z|no control cell"):
        bsdl.build_drive_vector(b, {"DDR0_RESET_N": "Z"})
    with pytest.raises(BsdlError, match="0, 1 or Z"):
        bsdl.build_drive_vector(b, {"RESET_B": "9"})


def test_drive_output2_without_control_cell(b: bsdl.BsdlFile) -> None:
    """DDR0_RESET_N is always driven; 0/1 work, only Z is impossible."""
    vec, plan = bsdl.build_drive_vector(b, {"DDR0_RESET_N": "1"})
    assert (vec >> 187) & 1 == 1
    assert plan[0]["control_cell"] is None


# --------------------------------------------------------------------------
# Bundled lookup and RM cross-check
# --------------------------------------------------------------------------


def test_load_bundled_matches_exact_and_fuse_masked() -> None:
    assert bsdl.load_bundled(0x0831_F01D) is not None
    # Bits [20:17] are fuse-sourced and invalid right out of reset.
    fuzzed = 0x0831_F01D ^ 0x0006_0000
    assert bsdl.load_bundled(fuzzed) is not None
    assert bsdl.load_bundled(0xDEAD_BEEF) is None
    assert bsdl.load_bundled(None) is None


def test_resolve_prefers_explicit_path(tmp_path, b: bsdl.BsdlFile) -> None:
    p = tmp_path / "copy.bsdl"
    from importlib import resources

    p.write_bytes(
        resources.files("s32edbg.data")
        .joinpath("SIP_S32S277n8n9_S32E277n8n9_BGA27_831F01D.bsdl")
        .read_bytes()
    )
    loaded = bsdl.resolve(str(p), idcode=None)
    assert loaded is not None and loaded.idcode == b.idcode
    assert loaded.path == str(p)


def test_resolve_falls_back_to_single_bundled_file() -> None:
    if len(bsdl.bundled_names()) == 1:
        assert bsdl.resolve(None, idcode=None) is not None


def test_opcode_disagreements_is_exactly_censor_ctrl(b: bsdl.BsdlFile) -> None:
    """RM Table 959 says 0x0107, the BSDL says 0x0100. Boundary scan never
    issues it, but the divergence must be loud, not silent."""
    assert bsdl.opcode_disagreements(b) == [
        "ENABLE_CENSOR_CTRL: BSDL 0x0100 vs RM Table 959 0x0107"
    ]


# --------------------------------------------------------------------------
# Malformed input
# --------------------------------------------------------------------------

_MINIMAL = """
entity TINY is
  generic (PHYSICAL_PIN_MAP: string:= "PKG");
  port (
    PA : inout bit ;
    TCK : in bit
  );
  constant PKG: PIN_MAP_STRING := "PA : A1," & "TCK : B1" ;
  attribute INSTRUCTION_LENGTH of TINY: entity is 4;
  attribute INSTRUCTION_OPCODE of TINY: entity is
    "BYPASS (1111)," &
    "IDCODE (0001)," &
    "SAMPLE (0010)," &
    "EXTEST (0100)";
  attribute IDCODE_REGISTER of TINY: entity is
    "00000000000000000000000000000011";
  attribute BOUNDARY_LENGTH of TINY: entity is 2;
  attribute BOUNDARY_REGISTER of TINY: entity is
    "0 (BC_1, *, control, 0)," &
    "1 (BC_7, PA, bidir, X, 0, 0, Z)";
 end TINY;
"""


def test_minimal_synthetic_bsdl_parses() -> None:
    t = bsdl.parse_bsdl_text(_MINIMAL, "test:minimal")
    assert t.entity == "TINY"
    assert t.boundary_length == 2
    assert t.opcodes["EXTEST"] == 0b0100
    assert bsdl.pin_rows(t)[0].ball == "A1"
    # A 4-bit IR never matches JTAGC_IR16, so no disagreement lines.
    assert bsdl.opcode_disagreements(t) == []


def test_not_a_bsdl_at_all() -> None:
    with pytest.raises(BsdlError, match="not a BSDL"):
        bsdl.parse_bsdl_text("just some text", "test:garbage")


def test_boundary_count_mismatch_is_fatal() -> None:
    bad = _MINIMAL.replace("entity is 2;", "entity is 3;", 1).replace(
        "BOUNDARY_LENGTH of TINY: entity is 2", "BOUNDARY_LENGTH of TINY: entity is 3"
    )
    with pytest.raises(BsdlError, match="BOUNDARY_LENGTH"):
        bsdl.parse_bsdl_text(bad, "test:short")


def test_idcode_parity_is_enforced() -> None:
    bad = _MINIMAL.replace(
        "00000000000000000000000000000011", "00000000000000000000000000000010"
    )
    with pytest.raises(BsdlError, match="LSB must be 1"):
        bsdl.parse_bsdl_text(bad, "test:parity")


def test_opcode_width_is_enforced() -> None:
    bad = _MINIMAL.replace('"BYPASS (1111),"', '"BYPASS (111),"')
    with pytest.raises(BsdlError, match="not 4 bits"):
        bsdl.parse_bsdl_text(bad, "test:width")


def test_bad_ccell_reference_is_fatal() -> None:
    bad = _MINIMAL.replace("bidir, X, 0, 0, Z", "bidir, X, 7, 0, Z")
    with pytest.raises(BsdlError, match="control cell 7"):
        bsdl.parse_bsdl_text(bad, "test:ccell")


def test_comments_are_stripped_outside_strings_only() -> None:
    commented = _MINIMAL.replace(
        '"SAMPLE (0010)," &', '-- gone\n    "SAMPLE (0010)," &  -- also gone'
    )
    t = bsdl.parse_bsdl_text(commented, "test:comments")
    assert "SAMPLE" in t.opcodes

"""The generated ball -> SIUL2 pad table (data/pads_bga27.py). No hardware.

Generated from NXP's S32E2_IOMUX.xlsx (embedded in the RM PDF) by
tools/gen_pads.py; these tests pin the shape and the spot values that were
cross-checked against independent sources during bring-up.
"""

from __future__ import annotations

import re

from s32edbg.data.pads_bga27 import BALL_TO_PAD

_BALL = re.compile(r"^[A-Z]{1,2}[0-9]{1,2}$")


def test_table_shape() -> None:
    assert len(BALL_TO_PAD) > 250
    for ball, (pad, instance, mscr) in BALL_TO_PAD.items():
        assert _BALL.match(ball), ball
        assert pad and isinstance(pad, str)
        assert instance.startswith("SIUL2"), instance
        assert isinstance(mscr, int) and mscr >= 0


def test_spot_joins_cross_checked() -> None:
    """D17 = DSPI_2_SCK in the BSDL, and RM Table 5 says DSPI_2_SCK -> PAD_086
    -- three sources meeting on one ball."""
    assert BALL_TO_PAD["D17"] == ("PAD_086", "SIUL2_1", 86)
    assert BALL_TO_PAD["AD32"] == ("PBA_00", "SIUL2_AE", 0)   # BSDL: PBA_00 ball
    assert BALL_TO_PAD["AJ1"] == ("PAD_009", "SIUL2_0", 9)    # BOOTMOD_0 is a pad
    # Even the JTAG pins are SIUL2 pads (RM 135.15.1: the debug pads "can be
    # later reassigned" -- because they are ordinary muxable pads).
    assert BALL_TO_PAD["AH8"] == ("PAD_173", "SIUL2_0", 173)  # TCK
    # Truly dedicated pins are absent by design.
    assert "AC29" not in BALL_TO_PAD  # RESET_B
    assert "AG8" not in BALL_TO_PAD   # JCOMP
    assert "AC28" not in BALL_TO_PAD  # POR_B


def test_ae_pads_use_the_port_name_as_pad_name() -> None:
    ae = {ball: v for ball, v in BALL_TO_PAD.items() if v[1] == "SIUL2_AE"}
    assert len(ae) > 100
    assert all(v[0].startswith("PB") for v in ae.values())

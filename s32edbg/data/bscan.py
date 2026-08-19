"""JTAGC instruction opcodes and the mandatory boundary-scan preamble.

RM Table 958 (L412414, S32Z2, 8-bit opcodes), Table 959 (L412474, S32E2, 16-bit
opcodes) and Table 984 (L421032, the S32E2x BSDL/preamble table).

Two RM statements about opcode width conflict:

  * Table 959 defines S32E2 opcodes as **16 bits**, footnote L412541: "The first
    8 bits of the opcode are for the main JTAGC. The second 8 bits of the opcode
    are for JTAGC_AE in the Application Extension subsystem."
  * Table 984, for the same 27x27 S32E2 parts, says "IR-SHIFT opcode: 0Ah",
    "21h", "20h" -- i.e. **8 bits**.

**Settled on hardware, and the two tables are not actually in conflict: the IR
length changes when the preamble runs.** Measured on an S32E277:

    s32edbg doctor, before any preamble  ->  TotalIRLen 8
    s32edbg bscan preamble --run-svf     ->  "Download successful. SVF ok"
    s32edbg doctor, after the preamble   ->  TotalIRLen 16   (and it persists)

The Table 984 preamble's last step writes TLM_TAP_SELECT_TDR = 0x8000_0011,
which is exactly what the TAP Linking Module needs to bring JTAGC_AE in the
Application Extension die into the scan chain. Before that write the AE TAP is
not in the chain and the IR is 8 bits; after it, the chain is JTAGC + JTAGC_AE
and the IR is 16. So Table 958's 8-bit opcodes describe the *pre-preamble* chain
and Table 959's 16-bit opcodes describe the *post-preamble* one, which is the
chain every boundary-scan instruction is actually issued on.

That also disposes of an earlier red herring. "The board routes no AE JTAG port"
is true and irrelevant: the AE TAP is internal to the SIP and is chained by the
TLM, not brought out to a connector. Reading a TotalIRLen of 8 cold therefore
says nothing about the opcode width -- which is why this had to be measured by
behaviour rather than inferred.

`jtagc_ir()` still takes a width so an 8-bit part stays expressible, but 16 is
the default for S32E2 because the 8-bit form silently reads the device ID
instead of the boundary-scan register.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Arm DAP IR codes -- RM Table 956 (L412306). IR length 8.
# --------------------------------------------------------------------------

DAP_IR_ABORT = 0xF8
DAP_IR_DPACC = 0xFA
DAP_IR_APACC = 0xFB
DAP_IR_IDCODE = 0xFE

# --------------------------------------------------------------------------
# JTAGC IR codes, 8-bit form (main JTAGC), RM Table 958
# --------------------------------------------------------------------------

JTAGC_IR_IDCODE = 0x00
JTAGC_IR_CENSOR_CTRL = 0x01
JTAGC_IR_SAMPLE_PRELOAD = 0x02
JTAGC_IR_SAMPLE = 0x03  # RM: "do not use the SAMPLE instruction"
JTAGC_IR_EXTEST = 0x04
JTAGC_IR_HIZ = 0x05
JTAGC_IR_EXT_TEST_CTRL = 0x0A
JTAGC_IR_CLAMP = 0x0C
JTAGC_IR_TLM_TAP_SELECT = 0x20
JTAGC_IR_TLM_TDR = 0x21
JTAGC_IR_BYPASS = 0xFF

#: 16-bit S32E2 forms, RM Table 959, verbatim. Where the AE byte is "xxxx_xxxx"
#: (don't care) we use 0x00.
JTAGC_IR16: dict[int, int] = {
    JTAGC_IR_IDCODE: 0x0001,
    JTAGC_IR_CENSOR_CTRL: 0x0107,
    JTAGC_IR_SAMPLE_PRELOAD: 0x0202,
    JTAGC_IR_SAMPLE: 0x0303,
    JTAGC_IR_EXTEST: 0x0404,
    JTAGC_IR_HIZ: 0x0509,
    JTAGC_IR_EXT_TEST_CTRL: 0x0A00,  # AE byte is don't-care
    JTAGC_IR_CLAMP: 0x0C0C,
    JTAGC_IR_TLM_TAP_SELECT: 0x2020,
    JTAGC_IR_TLM_TDR: 0x2121,
    JTAGC_IR_BYPASS: 0xFFFF,
}


#: IR width to use for boundary scan, and the RM contradiction it settles.
#:
#: RM Table 984 gives the preamble as 8-bit opcodes (0Ah/21h/20h) while RM Table
#: 959 defines every S32E2 opcode as 16 bits, main JTAGC concatenated with
#: JTAGC_AE. **Table 959 is the one that matches silicon.** Measured on an
#: S32E277 (IDCODE 0x0831F01D) by shifting each instruction and reading which
#: data register it selected, via SVF TDO compare:
#:
#:     instruction        SIR 8      SIR 16
#:     IDCODE             0x1D       0x1D
#:     SAMPLE/PRELOAD     0x1D       0x06     <- 8-bit form fell through to IDCODE
#:     EXT_TEST_CTRL      --         0x02
#:     BYPASS             zeros      zeros
#:
#: At 8 bits SAMPLE/PRELOAD returns the IDCODE, meaning the BSR is never
#: selected and any "boundary scan" done that way is really reading the device
#: ID. At 16 bits each instruction selects a distinct register. Note J-Link's
#: chain scan still reports TotalIRLen 8, so the scan length alone is not
#: evidence -- only the behaviour is.
BSCAN_DEFAULT_IRLEN = 16

#: A starting guess for the BSR width when nothing better is known. RM 136.3.4
#: puts the real length only in the BSDL file, which RM Table 984 names but does
#: not supply. Sized for a 27x27 BGA; widen it and look for the capture to stop
#: changing.
BSCAN_PROBE_BITS = 512


def jtagc_ir(opcode8: int, irlen: int) -> int:
    """Map an 8-bit JTAGC opcode onto the measured IR width.

    Raises ValueError for a width we have no RM table for, rather than guessing.
    """
    if irlen == 8:
        return opcode8
    if irlen == 16:
        try:
            return JTAGC_IR16[opcode8]
        except KeyError:
            raise ValueError(
                f"RM Table 959 defines no 16-bit form for JTAGC opcode 0x{opcode8:02X}"
            ) from None
    raise ValueError(f"no RM opcode table for an IR length of {irlen} bits")


#: Instructions that assert the internal MCU system reset or float outputs.
#: RM 136.4.4.3/.4/.5: EXTEST, HIGHZ and CLAMP each "assert the internal system
#: reset for the MCU"; HIGHZ additionally places "all output drivers ... in an
#: inactive drive state". Gated behind an explicit opt-in flag; without it the
#: refusal carries the reason recorded here.
DRIVE_PIN_INSTRUCTIONS: dict[str, tuple[int, str]] = {
    "EXTEST": (JTAGC_IR_EXTEST, "asserts internal MCU system reset (RM 136.4.4.3)"),
    "HIGHZ": (JTAGC_IR_HIZ, "tri-states every output driver and asserts internal reset (RM 136.4.4.4)"),
    "CLAMP": (JTAGC_IR_CLAMP, "asserts internal MCU system reset (RM 136.4.4.5)"),
}


# --------------------------------------------------------------------------
# The preamble -- RM Table 984 (L421036-421051), verbatim
# --------------------------------------------------------------------------

#: RM Table 984 step 2, verbatim (the RM wraps it across two printed lines as
#: "...07C0_0000_0" / "003_3041_..."; joined here):
#:   DR-SHIFT: 1800_0800_000C_0004_0000_07C0_0000_0003_3041_1800_01A0_0280_0000h
#:
#: That is 13 groups x 4 hex digits = 52 hex digits = 208 bits. The RM gives no
#: separate length, so the literal width of the constant *is* the shift length.
#: 208 is not a multiple of 32, so a chunked 32-bit-per-call shift needs
#: 6 x 32 + 1 x 16; the SVF path shifts it in one SDR.
EXT_TEST_CTRL_PAYLOAD_HEX = (
    "1800_0800_000C_0004_0000_07C0_0000_0003_3041_1800_01A0_0280_0000"
)

#: Number of DR bits to shift for the EXT_TEST_CTRL payload. Derived from the
#: literal hex string above rather than hardcoded, so the two cannot drift.
EXT_TEST_CTRL_PAYLOAD_VALUE = int(EXT_TEST_CTRL_PAYLOAD_HEX.replace("_", ""), 16)
EXT_TEST_CTRL_PAYLOAD_BITS = len(EXT_TEST_CTRL_PAYLOAD_HEX.replace("_", "")) * 4

TLM_TDR_PAYLOAD_VALUE = 0x8000_0001
TLM_TDR_PAYLOAD_BITS = 32

TLM_TAP_SELECT_PAYLOAD_VALUE = 0x8000_0011
TLM_TAP_SELECT_PAYLOAD_BITS = 32


def preamble_steps(irlen: int) -> list[tuple[str, int, int, int, int]]:
    """The Table 984 preamble as (label, ir_opcode8, ir_value, dr_value, dr_bits).

    RM Table 984, S32E270 / S32E277/8/9, 27x27:
        1. IR-SHIFT opcode: 0Ah to call the main JTAGC's EXT_TEST_CTRL
        2. DR-SHIFT: 1800_0800_...
        3. IR-SHIFT opcode: 21h to call the TLM_TDR register
        4. DR-SHIFT: 8000_0001h
        5. IR-SHIFT opcode: 20h to call the TLM_TAP_SELECT_TDR register
        6. DR-SHIFT: 8000_0011h
    """
    return [
        (
            "EXT_TEST_CTRL",
            JTAGC_IR_EXT_TEST_CTRL,
            jtagc_ir(JTAGC_IR_EXT_TEST_CTRL, irlen),
            EXT_TEST_CTRL_PAYLOAD_VALUE,
            EXT_TEST_CTRL_PAYLOAD_BITS,
        ),
        (
            "TLM_TDR",
            JTAGC_IR_TLM_TDR,
            jtagc_ir(JTAGC_IR_TLM_TDR, irlen),
            TLM_TDR_PAYLOAD_VALUE,
            TLM_TDR_PAYLOAD_BITS,
        ),
        (
            "TLM_TAP_SELECT_TDR",
            JTAGC_IR_TLM_TAP_SELECT,
            jtagc_ir(JTAGC_IR_TLM_TAP_SELECT, irlen),
            TLM_TAP_SELECT_PAYLOAD_VALUE,
            TLM_TAP_SELECT_PAYLOAD_BITS,
        ),
    ]


#: RM Table 984 names these files but does not say where to obtain them. The
#: names here are the RM's, kept as the citation record; matching an actual
#: file is done by the IDCODE parsed from its contents (``bsdl.load_bundled``),
#: never by filename -- NXP ships the 831F01D file under the longer name
#: ``SIP_S32S277n8n9_S32E277n8n9_BGA27_831F01D.bsdl``, which is what
#: ``s32edbg/data/`` bundles verbatim.
BSDL_FILES: dict[int, str] = {
    0x0831_B01D: "SIP_S32E270_BGA27_831B01D.bsdl",
    0x0831_F01D: "SIP_S32E277n8n9_BGA27_831F01D.bsdl",
}

#: Measured deviations between the bundled BSDL and this silicon's chain,
#: applied by ``bsdl.load_bundled``: idcode -> (first cell dropped, count, why).
#:
#: 2026-08-19 bench (S32E277, IDCODE 0x0831F01D, marker-delay verify + byte
#: probing): with the RM Table 984 preamble the SAMPLE/PRELOAD register is
#: **464 bits, not the BSDL's 494**. BYPASS measured exactly 1 and IDCODE
#: exactly 32 with the same oracle, so the method is sound. Locating the 30
#: missing bits against physical invariants (RESET_B=1; with the R52s off,
#: output-enable cells overwhelmingly read "driver off") converged on a
#: single cut at cell 262 -- exactly the BSDL's one and only run of 30
#: consecutive `internal` BC_2 cells (262..291, between the DDR0_CA block
#: and LIN_6_TX). No pins are lost; cells above shift down by 30, confirmed
#: by pins above the cut decoding coherently (GTM_033/LFAST_0_EXT_REF/
#: ETH_1_MII_TXER driver-on states). The bundled file's own DESIGN_WARNING
#: says it targets the S32S277/8/9 SIP; the S32E die evidently omits that
#: internal segment from the chain.
BSDL_CELL_QUIRKS: dict[int, tuple[int, int, str]] = {
    0x0831_F01D: (
        262,
        30,
        "S32E277 silicon chains 464 of the BSDL's 494 cells; the 30-cell "
        "internal segment at 262..291 is absent (bench 2026-08-19)",
    ),
}

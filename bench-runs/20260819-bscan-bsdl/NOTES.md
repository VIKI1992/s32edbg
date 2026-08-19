# 2026-08-19 — BSDL-driven boundary scan: first silicon pass

Board state: powered, bootloader running on the SMU core, all R52 cores off.
Probe: J-Link, SVF/JTAGLoad path, 4 MHz. All runs one-shot (no debug session).
Everything below happened after the RM Table 984 preamble, so the DAP was gone
from the first command on (power-cycle restores it — reconfirmed at the end of
the session by the operator as usual).

## Headline

**The bundled BSDL says BOUNDARY_LENGTH 494; this S32E277 chains 464.** The 30
missing bits are the BSDL's single run of 30 consecutive `internal` BC_2 cells
(262..291, between the DDR0_CA block and LIN_6_TX). No pins are lost. Recorded
as `data.bscan.BSDL_CELL_QUIRKS[0x0831F01D] = (262, 30, ...)`, applied by
`bsdl.load_bundled`; with the quirk every bench check passes.

The bundled file's own DESIGN_WARNING targets "S32S277/S32S278/S32S279
BGA27x27 ... SIP configuration with AE120 die" — it is the S32S/S32E combined
SIP file (`SIP_S32S277n8n9_S32E277n8n9_...`), while RM Table 984 names a
purely-S32E file (`SIP_S32E277n8n9_...`) we do not have. The S32E die
evidently omits that internal segment from the chain. If the S32E-named file
ever turns up, expect it to say 464.

## Method and evidence (chronological)

1. `bscan length` (expecting the BSDL's 494) — **FAILED**
   (`length-verify-494-FAILED.json`). Probed IDCODE 0x0831F01D correct.
2. Oracle sanity on registers of known length, same marker-delay verify:
   - BYPASS: length 1 **MATCH**; 2, 3 no.
   - IDCODE: length 32 **MATCH**; 33, 64, 65 no.
   So the oracle discriminates exactly, and the post-preamble chain presents
   a single 1-bit bypass / single 32-bit IDCODE — the linked chain behaves as
   one TAP at the DR level, as the BSDL models it.
3. SAMPLE/PRELOAD sweep 480..560: **no match** — the register is not near 494.
4. Direct measurement: one 8192-bit scan per probe, TDI = marker (low 32)
   then all-ones fill; byte-probed the TDO stream via the deliberate-mismatch
   read. Capture region (mixed values incl. 0x55/0x15 near bits 192–263),
   then the marker bytes emerged **byte-aligned at bytes 58..61 = 5A A5 99 66**,
   then pure 0xFF. Marker start = bit 464. Exact verify: **length 464 MATCH**.
5. Cut location: captured all 464 bits (`capture-464-raw.json`, raw
   0x...B8C2000010...006) and scored every contiguous 30-cell cut of the
   BSDL's 494 against physical invariants (RESET_B must read 1; with the
   R52s off, output-enable cells overwhelmingly read driver-off). Unique
   best: **cut at cell 262** (220 drivers-off vs 219 for every neighbour,
   0 pins lost). Cells 262..291 are the BSDL's only run of ≥10 consecutive
   internal cells — a natural segment boundary.
6. Cross-checks of the shifted mapping (cells ≥292 down by 30): the pins
   decoding as "driver on" are exactly a bootloader's set — LIN_0_TX=0,
   CAN_11_TX=1, LIN_9_TX=1, CAN_9_TX=1 (TX idle-recessive), GTM_033=1,
   LFAST_0_EXT_REF=1, ETH_1_MII_TXER=0 — with three of them ABOVE the cut
   (GTM_033 @377→347, LFAST_0_EXT_REF @389→359, ETH_1_MII_TXER @413→383),
   which only decode coherently under the −30 shift.
7. With the quirk recorded: `bscan length` → **464 bits (verified on
   silicon, one run)** (`length-verify-464-ok.json`); `bscan sample` →
   named table (`sample-464-named.json`): RESET_B=1 driver-off,
   BOOTMOD_0=1 / BOOTMOD_1=0 (strap read — cross-check against the
   schematic when convenient), DDR0_RESET_N=0 always-driven (DDR held in
   reset on a bootloader-only board — physically right), 22 DDR pins
   flagged SAMPLE-input-blocked, 14/228 pins high.
8. Adaptive verify: re-verify seeded with the capture confirmed the whole
   register **unchanged in ONE JTAGLoad run** (sub-second) — the fast
   `--watch` path works; the capture is bit-stable with the R52s off.

## Costs measured

- JTAGLoad is far cheaper than assumed: **~80 ms per run**. IDCODE probe:
  4 runs (<1 s). Full 464-bit sample: 58 runs, ~5 s total (the two
  hardware-marked pytest tests — one-run length verify + a full sample —
  completed together in 4.4 s).
- One-run length verify / one-run unchanged-verify: ~0.1 s each.

## Re-check with the application running (same day, later session)

After flashing the app (RTU0 cores running, board power-cycled for the flash),
the question "does the 30-cell segment appear in some other chip state?" was
re-tested: oracle sanity again clean (IDCODE byte0 0x1D, BYPASS==1,
IDCODE==32), then **SAMPLE==464 MATCH, SAMPLE==494 no**. The quirk is a
property of the part, not of boot/power state.

The app-state sample (`sample-464-app-running.json`) doubles as a mapping
validation in a second, independent chip state: **106 of 228 pins now have
their output drivers on** (vs 7 under the bootloader) and the 166 changed
cells versus the bootloader capture are exactly the app's pin mux coming up —
whole peripheral groups enable coherently on BOTH sides of the cut (CAN0-13,
LIN, SENT, DSPI_2 and the ETH_1 MII block above the cut; QSPI_1 driving its
CS/CK/DATA lines; CLKOUT_1 enabled; GTM channels). RESET_B still 1, BOOTMOD
straps unchanged (1/0), DDR0_RESET_N still driven low. A wrong cell mapping
cannot produce group-coherent enables like that.

## Not exercised this session

The gated drive set (EXTEST/HIGHZ/CLAMP) — needs a deliberately chosen safe
pin and a scope on the ball; do it on a bench with nothing hanging off the
chosen net (test-plan §7.3 step 4). The cell-orientation escape hatch
(`--reverse-cells`) was NOT needed: the default IEEE orientation decoded
correctly (marker byte order and every anchor agree).

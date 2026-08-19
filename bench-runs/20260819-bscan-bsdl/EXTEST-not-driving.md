# 2026-08-19 — EXTEST does not override pads on this S32E277 (with the RM Table 984 preamble)

User reported driving PAD_015 / PAD_017 (FR_1_TXE_A_B ball R4, FR_1_RXD_A ball N1)
via `bscan drive` showed no change on a scope. Reproduced and root-caused via read-back.

## The tool's shift path is correct (ruled out)

- EXTEST opcode 0x0404 selects a **464-bit** register (marker-verify), = the BSR. HIGHZ 0x0509
  and CLAMP 0x0C0C each select a **1-bit** register (= bypass), exactly as the BSDL REGISTER_ACCESS
  declares. So the instructions decode right.
- The drive vector is built correctly: for FR_1_TXD_A (data cell 119, ctl 118) driven to 1, the
  464-bit vector has bit 119 = 1 and bit 118 = 1 (driver-enable, disval 0 → enable 1). Verified by
  assertion before shifting.
- The PRELOAD → SIR EXTEST → Update sequence is IEEE-correct and matches `emit_drive`.

## EXTEST is not reaching the pads (the finding)

Read-back **while EXTEST is latched** (capture-DR samples the live pin during the same scan that
re-loads the vector):

| state | RESET_B (cell 167) | byte14 (FR/CAN ctl+data cells 112-119) |
|---|---|---|
| SAMPLE (functional, app running) | **1** | 0x55 (app driving; control cells set) |
| EXTEST, drive FR_1 pins = 1 | **0** | 0x00 |
| EXTEST, drive FR_1 pins = 0 | **0** | 0x00 |
| EXTEST + extra update-DR cycles | **0** | 0x00 |

Decisive point: **driving all-1 and all-0 produced identical captures (all-0)**. If EXTEST were
applying the preloaded data to the pins, the two would differ and the driven data cells would read
back their driven value. They do not. A pin explicitly driven to 1 (FR_1_TXD_A) captured 0 under
EXTEST in every attempt.

What EXTEST *does* do: the capture changes vs SAMPLE (RESET_B 1→0, byte14 0x55→0x00), i.e. the
register content is different — consistent with EXTEST asserting the internal MCU system reset
(RM 136.4.4.3), which releases the app's functional pin drivers so the pads fall to their
reset/Hi-Z state (reading ~0). But the boundary-scan **update cells are not being connected to the
pad output drivers** — the pads follow the reset default, not the preloaded vector.

## Most likely cause

The RM Table 984 preamble's `EXT_TEST_CTRL` payload
(`1800_0800_000C_0004_0000_07C0_0000_0003_3041_1800_01A0_0280_0000`) is described as selecting the
**BSR segments** for this package (it is what makes SAMPLE work and sets the length to 464). It
appears to configure those segments for **observe/SAMPLE**, not for **EXTEST output-drive**. Two
concrete leads:

1. `EXT_TEST_CTRL` is a 208–215-bit register with per-segment control; a different payload (or an
   additional field) may be required to enable BSR→pad output for EXTEST. Its bit-field definition
   is **not** in the RM text we have.
2. The BSDL lists — but comments out (i.e. NXP-private) — `JTAGC_EXTEST_PULSE (0x0800)` and
   `JTAGC_EXTEST_TRAIN (0x0900)`. On NXP JTAGC blocks these are the instructions that actually
   apply/clock the external drive; plain EXTEST may not, or may need one of them after it.

Both are exactly the kind of thing the S32E-specific boundary-scan app note (or the S32E-named BSDL
we never received — see the 494-vs-464 ticket) would settle. SAMPLE is fully functional; only the
EXTEST *drive* path is affected.

## Tool status

Shift mechanics verified correct, so no code bug found. `bscan drive/highz/clamp` remain available
(they may work once the correct EXT_TEST_CTRL enable / EXTEST_PULSE step is known). Recommend: mark
EXTEST drive "unconfirmed on S32E silicon" in the help/docs and raise the NXP question below before
relying on it.

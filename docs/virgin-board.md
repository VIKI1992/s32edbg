# Attaching to a virgin board

How to get a debug session on an S32E27x whose flash is blank — and why the
obvious approaches fail.

## The problem, stated precisely

A virgin board will not hold still long enough to be debugged, and the reason is
a specific chain in the RM:

1. Blank flash means the BootROM finds no valid image, so boot fails and issues
   a **functional reset** (RM 62.12).
2. Functional resets escalate through FREC to a **destructive reset**
   (RM 60.5.2.4).
3. Destructive escalation lands in **DEST0**, where RM 60.5.6 says the device
   *"remains there until a POR event starts a POR sequence"*.
4. **RM Table 301 is the punchline: POR is one of exactly two things that hold
   the JTAGC in reset** — the other being JCOMP low. Functional and destructive
   resets do *not* touch it.

So of all the ways this chip can reset, the blank-flash cascade lands on the one
that specifically kills the JTAG controller. The TAP is alive only in brief
windows, and a healthy 3.3 V VTREF tells you nothing, because that rail comes
from the SBC's pre-regulator and not from the core supply.

There is a second, independent way to lose the TAP once software *does* run.
RM 135.15.1: the debug pads *"operate in JTAG functionality out of reset but can
be later reassigned to their alternate functionalities."* An application that
remuxes those pins removes the TAP just as effectively. Different cause, same
symptom, and the same fix.

## The fix

```
s32edbg attach
```

It retries the chain scan until a window opens, then immediately asserts
**`SDA_AP.SDASYSRSTC[SYSFUNCREQ]`**, which RM 135.11 describes as the field that
*"allows the debugger to hold the system in functional reset."* That stops the
reboot cascade and stops software ever reaching the pad configuration — and
because RM Table 301 says a functional reset leaves the JTAGC and SWJ-DP alone,
the debug connection survives it.

Then it applies the LADDER-1 debug-enable sequence, because RM 135.11 also warns
the debug clocks are off by default and *"trying to access debug components while
the clocks are not enabled might hang the debug APBIC bus."*

```
s32edbg release        # let it run again; never issues a destructive reset
```

**Verified on hardware** against a board running an application (CAN traffic
confirming it was live):

```
attached and holding on attempt 1 (SDASYSRSTC 0x00000020)
```

and then, in a separate session while still held:

```
functional_reset_asserted   yes
destructive_reset_asserted  no
Debug-enable gate: open
```

The hold took on the first attempt, the debug connection survived it, the gate
stayed open, and no destructive reset was issued. `release` returned the board
to running.

The hold runs through the **dll** backend. The commander backend's
`WriteDP`/`WriteAP` sequence returns zeros for every AP on this target, while
the dll path reads all nine APs and lands writes correctly — so the batch
backend stays what it is elsewhere, a cross-check oracle.

### Why this needed a policy change

This tool otherwise never writes the SDA-AP, because RM 135.13.4 makes the HSE
challenge/response one-shot: *"HSE allows one only attempt for C/R comparison. If
it fails, no further attempts are accepted."*

That risk belongs to the **authentication** registers — `AUTHCTL` and the
`KEYRESP` block — which remain permanently forbidden. `SDASYSRSTC` is reset
control, carries none of that risk, and is the only software route onto a board
that will not stay still. `gates.guard_write()` enforces the distinction per
register rather than per AP, and `tests/test_sda_policy.py` pins it.

## A live TAP can look dead

J-Link V9.62 reports the IDCODE from chain detection as:

```
JTAG chain detection found 1 devices:
 #0 Id: 0x0831F01D, IRLen: 04, JTAG-DP
```

not as the `JTAG Id: 0x...` form. A parser matching only the latter reports a
perfectly live TAP as "no response" — which had `attach` spinning through its
whole retry budget against a window that was open the entire time, and made
`doctor` print "chain: no response" on the same line as a successfully parsed
`TotalIRLen 8`. Both spellings are matched now; `tests/test_parsing.py` covers
each.

The lesson generalises: when the tool says a target is not responding, check the
raw transcript before believing it.

## Do not trust a current swing

The single most misleading signal on this bench. Measured here:

| Condition | ITarget |
|---|---|
| Sampling with no connects | flat **38 mA**, swing 0 |
| Sampling with connects interleaved | swings **38–51 mA** |

**A connect toggles the reset pin**, and on this board `MCU_RESET_b` is tied to
`SBC-PMIC_RSTb` through R439 (0 Ω, populated; the S3 isolation jumper is DNP).
So sampling around connects measures the debugger resetting the target, not the
target rebooting on its own. A running application also varies its own current
perfectly normally — a board happily transmitting CAN swings as much as one in a
boot loop.

`doctor` samples before connecting and now refuses to call it "cycling" when
resets were issued during sampling. If you need to know whether a target is
genuinely rebooting, sample with **no** connect in between.

## When `attach` cannot find a window

Two non-software escapes, both from the schematic and the RM:

**Assert WDGDIS on J9 pin 28.** This stops the SBC and PMIC watchdogs. The
schematic says exactly this: *"WDGDIS signal is used to enter DEBUG mode via ETAS
debugger, if required"*, and notes *"SBC & PMIC need at least 3.5 V to enter
DEBUG mode"* because of level shifter U1. **Only reachable on J9** — it does not
exist on the J3 MINI-10 connector.

**Let FREC reach 8.** RM 62.12: after enough failed boots the part enters
Fail-Safe serial boot, where it stops looping. BOOTMOD[1:0] is strapped `0b00`
with TP40/TP41 available, and the schematic notes *"BOOT from RCON (requires
manual TP40 PU)"*.

And check the things a debugger cannot override: `MCU_POR_b` at ball AC28 is the
output of AND gate U20 whose other input is `SBC-PMIC_PGOOD`, so a latched
FS8613 or PF5030 fault holds the part in POR no matter what the probe does.

## Connector choice matters

Prefer **J9**. `JCOMP` (pin 10) and `WDGDIS` (pin 28) are both driveable there
and neither exists on J3. On J3 you also cannot beat a JCOMP problem, and note
that J9 pin 10 is `AUR_JCOMP` while pin 10 of a standard ARM 20-pin connector is
GND — an adapter that grounds it will hold the TAP in reset (RM 136.5.5).

## What still is not proven

Raw JTAG shifting has **not** been validated on silicon. `calibrate_tdo_order()`
shifts a known pattern through the one-bit bypass register and, on the attempts
so far, no byte-order interpretation reproduced the expected one-clock delay — so
it refused rather than returning a plausible-looking number. Until it passes,
`bscan length` and `bscan sample` cannot be trusted, and the gate is there to
stop you trusting them by accident.

# s32edbg

CoreSight/JTAG command-line tool for the **NXP S32E27x** over a **SEGGER J-Link**.

It does six things:

1. **Get and keep a debug connection** on a board that reboots, remuxes its debug pads, or comes
   out of the factory with blank flash — `attach`, `release`, `doctor`.
2. **Open the debug gates** that the S32E leaves shut by default — `gates`.
3. **Enumerate the debug topology** — every access port against RM Table 957, every CoreSight ROM
   table — `apmap`, `romdump`.
4. **Read and write every bus** the debug ports reach — `mem`.
5. **Debug the cores.** Halt, resume, single-step, step over a call, read and write registers, and
   manage hardware breakpoints and watchpoints — on the Cortex-R52 cores (`core`, `bp`, `wp`) and
   on the Cortex-M33 subsystems (`m33`), which use a completely different debug model.
6. **Run a boundary scan** — SAMPLE/PRELOAD only, including the mandatory RM Table 984 preamble —
   `bscan`.

Plus interface selection (`iface`), raw TAP access (`tap`), and a one-shot artifact collector
(`report`).

```
pip install -e .
s32edbg --help
```

Requires 64-bit Python 3.11+ (it loads `JLink_x64.dll`; `JLinkARM.dll` is 32-bit) and an installed
SEGGER J-Link package. No compiler and no J-Link SDK licence are needed.

Every output block below is real output from a bench session on an S32E277 SOM, lightly trimmed for
width.

---

## Quick start

```bat
s32edbg doctor                  :: is the target even answering?
s32edbg gates apply             :: open the debug clocks (once per power cycle)
s32edbg core list               :: which cores are there, and what state are they in
s32edbg romdump                 :: walk the CoreSight tree
s32edbg report --json out.json  :: everything in one file
```

To actually debug something:

```bat
s32edbg core sample --core rtu0.1     :: find a hot address without stopping anything
s32edbg bp set 0x321D2EE6 --core rtu0.1
s32edbg core halt --core rtu0.1
s32edbg core regs --core rtu0.1       :: r0-r14, PC, DSPSR
s32edbg core step --core rtu0.1 --over
s32edbg core resume --core rtu0.1
```

If `doctor` says the TAP is dead, read *Read this before you connect* below — on this board the
cause is usually a power switch, not the tool. If the target will not hold still long enough to
finish a command, use `s32edbg attach`.

---

## Read this before you connect

### A healthy VTREF does NOT mean the MCU is powered

This one cost a full bench session. **The 3.3 V and 0.8 V rails come from different regulators:**

- **+3V3** — what VTREF measures (J9 pin 2, or J3 pin 2 via ferrite L13) — comes from the
  **FS8613 SBC's VPRE** pre-regulator.
- **+0V8** — the S32E **core** rail — comes from **PF5030 PMIC BUCK1** via L10. (+1V1 is BUCK2
  via L11.)

So VTREF can read a perfect 3.307 V with the MCU core completely unpowered. And an unpowered core
means the TAP is dead, legitimately: **RM Table 295** lists *"Core supply (0.8 V): VDD"* and
*"Critical LVDs"* as **POR** sources, and **RM Table 301** says POR is the only reset — other than
JCOMP going low — that holds the JTAGC in reset. Destructive and functional resets do not touch it.

Symptoms of this exact state, all of which are *correct silicon behaviour* rather than a fault:

```
VTref=3.307V                 <- looks fine
ITarget=38mA                 <- current flowing
TotalIRLen = ?, IRPrint = 0x..000000000000000000000000
JTAG Id: 0x00000000: INVALID
```

**First thing to check is always the onboard PMIC and SBC enable switches.** `s32edbg doctor`
reports this as CHECK 0. It is unaffected by interface speed, `--device`, JLinkScripts or TRST
toggling, so no amount of software will work around it.

### JCOMP must be high, or nothing works

The S32E has **no TRST pin**. `JCOMP` (ball AG8) does that job, it is **active-high with a weak
internal pull-down**, and while it is low both the JTAGC and the SWJ-DP stay in reset
(RM §60.5.4.1, §136.5.5, Table 301). A dead TAP and a JCOMP problem look identical.

On the `SOM_3xx_Gen1_G0 BR1` board, **R447 (4.7 kΩ to +3V3) is populated** and the competing
pull-down R448 is DNP, so JCOMP sits high with nothing attached. The schematic notes:

> "JCOMP must be pulled up to 3.3 V for proper debug operations, when standard 10-pin MIPI
> connector is used."

Meter it anyway. There is **no test point on JCOMP**.

### Pick the right connector — they are not equivalent

| | **J9** — `ASP-130367-01`, 40-pin | **J3** — `CLP-105-02-F-D`, ARM MINI-10 |
|---|---|---|
| TCK / TMS / TDI / TDO | pins 4 / 8 / 14 / 16 | pins 3 / 1 / 7 / 5 (**mirrored numbering**) |
| **JCOMP** | **pin 10, driveable** | **absent** |
| **WDGDIS** (watchdog disable) | **pin 28, driveable** | **absent** |
| RESET / POR | 20 & 22 / 26 | 9 / — |
| VTREF | pin 2 (`+3V3`) | pin 2 (`DBG_VCC`, via ferrite L13) |
| GNDDetect | — | pin 10, strap R449 is **DNP → floats** |

**Prefer J9.** On J3 you cannot drive JCOMP and — more importantly — you cannot reach `WDGDIS`.

### The SBC and PMIC can reset the target underneath you

`SBC-PMIC_RSTb` is tied to `MCU_RESET_b` through **R439 (0 Ω, populated)**, and the isolation
jumper **S3 is DNP**. Both the FS8613 SBC (U7 pin 17) and the PF5030 PMIC (U13 pin 18) can assert
S32E reset at any moment, including mid-session. `POR_B` is gated by AND gate U20 with
`SBC-PMIC_PGOOD` as the other input, which a debugger cannot override.

**This has a practical consequence for scripting.** Every `s32edbg` run opens a J-Link session, and
every session pulses reset through R439. A dll connect that cannot identify the target pulses it
again on its own (*"Failed to identify target. Resetting via Reset pin and trying again."*). Enough
of those back to back and the TAP stops answering entirely. If commands start failing with
`CTRL/STAT=0x00000000`, **power-cycle rather than retrying** — retrying makes it worse.

The escape hatch is `WDGDIS` on **J9 pin 28** (default pulled low by R34, watchdogs active). The
schematic says: *"WDGDIS signal is used to enter DEBUG mode via ETAS debugger, if required"* and
*"SBC & PMIC need at least 3.5 V to enter DEBUG mode."*

### Four hazards this tool refuses to walk into

| Hazard | Policy |
|---|---|
| **SWD is a one-way door.** RM §135.5.1: *"Switching back from SWD mode to JTAG mode requires POR."* | `iface swd` requires `--i-understand-swd-is-one-way`. |
| **AXI-AP during functional reset.** RM Table 957: an access now can leave peripherals unreachable *"until you issue a destructive reset"*. | AXI-AP traffic is blocked while reset is asserted. No override flag. |
| **The HSE challenge/response is one-shot.** RM §135.13.4: *"HSE allows one only attempt … If it fails, no further attempts are accepted."* | The SDA-AP **authentication path** (`AUTHCTL`, `KEYRESP`) is never written. `SDASYSRSTC` is permitted — it is reset control (RM §135.11), not authentication, and is the only software route onto a virgin board. |
| **EXTEST / HIGHZ / CLAMP reset the chip.** RM §136.4.4: each *"asserts the internal system reset"*; HIGHZ also floats every output. | Not implemented. Asking for one gives a refusal with the RM citation. |

All four exit **3**, so a script can tell "the tool declined" from "the tool broke".

### Debug clocks are gated until you open them

RM §135.11: the debug clocks are off by default and are enabled by any MDM-AP `DBGENCNTRL{,2,3}`
bit, and *"Trying to access debug components while the clocks are not enabled might hang the debug
APBIC bus."* Run **`s32edbg gates apply`** before `romdump` or `mem`. The bits are **sticky across
functional resets and lost on power loss**, so re-apply after a power cycle, not after every reset.

---

## Session order

Ordering is not cosmetic here. Five rules, each with a consequence:

1. **`gates apply` before anything touching APBIC2** — see above.
2. **Anything that needs the application running goes before `attach`**, which stops it.
3. **Keep halts short, and inside one connection.** Every one-shot `s32edbg` run is a separate
   J-Link session, so halting in one command and resuming in another leaves the core stopped for
   seconds. On this board that deadlocked the application: CAN stopped, and it did **not** come
   back when the core was resumed — the other core had been left spinning on shared state.
   Recovery was a functional reset (`attach` then `release`), not a resume. Halt, look, resume —
   **and for any run-control work beyond a single command, use `s32edbg debug`**, which holds one
   connection for the whole halt-step-resume cycle and restores the target when it closes.
4. **Boundary scan goes after all DAP work.** The RM Table 984 preamble links `JTAGC_AE` into the
   scan chain, taking `TotalIRLen` from 8 to 16 — and J-Link's DAP addressing assumes the chain it
   scanned at connect. After the preamble, `connect`, `gates`, `apmap`, `romdump`, `mem`, `core`
   and `attach` all stop working on **both** backends until you power-cycle. Nothing is damaged.
   `bscan` warns about this itself.
5. **`iface swd` last of all**, because returning to JTAG needs a power cycle.

A full session therefore looks like:

```bat
s32edbg doctor
s32edbg gates apply
s32edbg apmap
s32edbg romdump
s32edbg mem ...
s32edbg core list                  :: run-control, R52 and M33
s32edbg m33 list
s32edbg attach                     :: stops the application
s32edbg release                    :: restarts it
s32edbg report --json out.json
s32edbg bscan preamble --run-svf   :: from here on the DAP is gone until power-cycle
s32edbg bscan sample --bsr-length 64
```

`tools\bench-acceptance.ps1` runs exactly this, in order, keeping per-step JSON and wire-level logs.

---

## Global options

Every option below goes **before** the subcommand: `s32edbg --json out.json romdump`, not
`s32edbg romdump --json out.json`.

| Option | Default | What it does |
|---|---|---|
| `--transport {dll,commander}` | `dll` | Probe backend. See below — they are not interchangeable. |
| `--serial TEXT` | auto | J-Link serial number, when more than one is attached. |
| `--speed INT` | `4000` | Interface speed in kHz. |
| `--jlink-dir PATH` | auto | Pin a specific SEGGER install. The environment variable `S32EDBG_JLINK_DIR` does the same thing for a whole shell session. |
| `--log PATH` | — | Write a J-Link wire-level trace. Keep this on for anything you might have to diagnose later. |
| `--json PATH` | — | Write JSON output. `-` means stdout. |
| `--echo` | off | Echo Commander traffic (commander transport only). |
| `--irlen INT` | measured | Override the JTAG IR length instead of measuring it. |
| `--device TEXT` | `CORTEX-R52` | Device name for J-Link's connect sequence. J-Link needs one to scan the chain and init the DAP even for DAP-only work. |

### The two transports are not interchangeable

**`dll`** (default) binds `JLink_x64.dll` in-process via ctypes. It is the only one that can do
interactive DAP work — single DP/AP transactions, and therefore `gates`, `apmap`, `romdump`, `mem`,
`core`, `m33` and `attach`.

**Run-control does not use J-Link's own halt/step API**, though that API is genuinely available:
`JLINK_Halt`, `Go`, `Step`, `ReadRegs`, `SetBPEx` and friends are all in the real PE export table,
unlike the `JLINK_CPU_*` and `JLINK_TARGET_*` variants, which are `.JLinkScript`-only. It is not
used for two reasons: it depends on J-Link identifying the core, which has never succeeded on this
target, and it hides the comparator state that managing breakpoints requires you to see. Everything
here is programmed directly through the debug registers instead.

**`commander`** runs `JLink.exe` in **batch mode** (`-CommanderScript`) and serves as a cross-check
oracle. It cannot do single DP/AP transactions, because DP `SELECT` state does not survive between
runs — those methods raise with an explanation rather than returning something wrong. Interactive
piping was tried and is impossible on Windows: `JLink.exe` writes its `J-Link>` prompt with no
trailing newline *and* block-buffers stdout when it is not a console, so the pipe stays empty until
the process exits.

Running `connect` on both and comparing DPIDR is what validates the hand-written ctypes signatures,
since there is no SDK header to check them against.

**Raw JTAG is commander-only, and currently blocked.** No `JLink_x64.dll` export can shift an
arbitrary TMS/TDI vector with a discoverable ABI. `JLINK_JTAG_ReadWriteBits` and `JLINK_JTAG_Reset`
appear in the binary with full C prototypes and look ideal — they are **not exported**, they belong
to the `.JLinkScript` API. `JLINK_JTAG_StoreRaw`/`StoreGetRaw` *are* exported but carry no prototype
anywhere. That leaves Commander's `WJTAGRaw <NumBits> <TMS> <TDI>`, which drives the pins but does
**not** report TDO reliably on this probe — so `calibrate_tdo_order()` refuses, and `tap ir`,
`tap dr` and `bscan length` are unavailable. Boundary scan runs through SVF/JTAGLoad instead, which
works.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | usage error — bad `--ap`, misaligned dump, missing required argument, malformed address or value |
| `2` | tool error — no DAP, power-up refused, transport failure. Also the fallback for an internal error: anything that escapes a command is reported as `internal error: <type>: <message>` with this exit code, never as a Python traceback (`S32EDBG_TRACE=1` re-raises for debugging) |
| `3` | **refused by safety policy** |
| `130` | aborted at a confirmation prompt |

### Address and value syntax

Three kinds of numeric argument, three rules:

| Kind | Rule | Examples |
|---|---|---|
| **Address** (`bp set`, `wp set`, `m33 bp/wp set`, `mem read/dump/write` ADDR, `romdump --rom`, a numeric `--ap`) | **`0x`-prefixed hex only.** A bare number is rejected with an error — `32169874` could be hex or decimal, and both guesses have burned users: base-0 silently read it as decimal and armed a comparator at `0x01EADF92` instead of `0x32169874`, while unprefixed hex like `321D2EE6` crashed the whole session. | `bp set 0x321D2EE6` ✓ · `bp set 0X32100020` ✓ · `bp set 32169874` ✗ (error, session survives) |
| **Data value** (`mem write` VALUE, `core reg` VALUE, `tap ir/dr`) | Decimal, or `0x`/`0o`/`0b`-prefixed (Python `int(x, 0)`), unchanged. | `mem write 0x1000 57005` ✓ · `mem write 0x1000 0xDEAD` ✓ · `mem write 0x1000 banana` ✗ (error) |
| **Comparator index** (`bp clear`, `wp clear`, `m33 bp/wp clear`) | Bare decimal. | `m33 bp clear 2` ✓ |

Malformed input of any kind exits `1` one-shot and prints an error inside a
`debug` session — **it never terminates the session**.

---

# Command reference

## `installs` — find SEGGER installations

```bat
s32edbg installs
```

```
  C:\Program Files\SEGGER\JLink            v0.00   JLink.exe  JTAGLoad.exe
  C:\Program Files\SEGGER\JLink_V794b      v7.94b  JLink.exe  JTAGLoad.exe
* C:\Program Files\SEGGER\JLink_V962       v9.62   JLink.exe  JTAGLoad.exe
  C:\Program Files (x86)\SEGGER\JLink_V794 v7.94   JLink.exe  JTAGLoad.exe

* = the install this tool will use
```

Confirms both helper executables are present. `JTAGLoad.exe` is required for boundary scan; without
it `bscan` cannot run at all.

Selection is by **highest version number**, across `C:\Program Files\SEGGER` and
`C:\Program Files (x86)\SEGGER`. Override it with `--jlink-dir PATH` for one command, or by setting
`S32EDBG_JLINK_DIR` for a whole shell session:

```bat
set S32EDBG_JLINK_DIR=C:\Program Files\SEGGER\JLink_V794b
```

Note the 32-bit `Program Files (x86)` install can never serve the `dll` backend — it ships
`JLinkARM.dll`, not `JLink_x64.dll`.

---

## `probe` — probe identity and pin states

Does **not** touch the DAP, so it is safe on a target in any state.

```bat
s32edbg probe
```

```
            serial: 603000869
  hardware version: V13.00
          firmware: J-Link V13 compiled Jun 25 2026 10:24:04
       DLL version: V9.62
             VTREF: 3304 mV
              pins: TCK=0 TDI=1 TDO=0 TMS=1 TRES=1 TRST=1
note: J-Link install: C:\Program Files\SEGGER\JLink_V962 (v9.62)
note: VTREF and pin states come from JLINK_GetHWStatus, whose struct layout is not
      documented on this machine; treat them as best-effort and cross-check with
      the commander transport.
```

VTREF below 2000 mV produces a warning pointing at the connector pin it comes from. Remember that a
healthy VTREF says nothing about the 0.8 V core rail.

---

## `doctor` — diagnose a target that will not answer

Read-only. Runs a chain scan through `JLink.exe`, samples target current, and turns the result into
a **ranked** list of checks with RM and schematic citations attached. This is the first thing to run
when something is wrong.

```bat
s32edbg doctor
s32edbg --json doctor.json doctor
```

```
probe: V13.00  firmware: J-Link V13 compiled Jun 25 2026 10:24:04
VTREF: 3306 mV   pins: TCK=0 TDI=1 TDO=0 TMS=1 TRES=1 TRST=1
ITarget samples: [38, 51, 51, 38, 38] mA
chain: IDCODE 0x0831F01D, TotalIRLen 8
────────────────────────────── diagnosis ──────────────────────────────
OK  VTREF 3306 mV
        Target I/O rail is present and at 3.3 V.

OK  TAP alive, IDCODE 0x0831F01D
        Matches RM Table 961 for this family.

OK  TotalIRLen = 8 (JTAGC alone)
        The cold chain: the Application Extension die's TAP is not linked in,
        so RM Table 958's 8-bit opcodes address the JTAGC. Boundary scan needs
        the RM Table 984 preamble first, which switches this to 16.

WARNING  Chain scanned but connect failed
        This is expected on this device while the debug-enable gate is closed:
        the APB ROM at 0x01BFE000 reads empty, so J-Link cannot identify a core.
        The DAP itself is usable and raw DP/AP access still works.
        1. Run `s32edbg gates apply`, then retry `romdump`.

no blockers, 1 warning(s)
```

Two subtleties worth knowing:

- **"Chain scanned but connect failed" is normal** before `gates apply`. J-Link cannot identify a
  core while the debug clocks are gated, but the DAP works fine.
- **Current swing is not evidence on its own.** A connect toggles the reset pin, so interleaving
  connects with current samples makes a static board look like it is rebooting. `doctor` tracks
  whether it issued resets during sampling and discounts the reading accordingly. (Measured: flat
  38 mA with no connects; 38–51 mA with them.)

---

## `connect` — bring up the DAP and report its identity

```bat
s32edbg connect
s32edbg --transport commander connect     :: cross-check
s32edbg connect --system-power            :: also request CSYSPWRUPREQ
```

Over JTAG:

```
! DPIDR unavailable. DP address 0x0 cannot be read over JTAG: ADIv6 Figure A1-2
  makes JTAG-DP DPACC address 0b00 Reserved. That covers DPIDR, DPIDR1 and
  BASEPTR0/1, which all live there and are selected by DPBANKSEL. Reading it
  anyway returns a plausible-looking value that is not the register asked for --
  on this target 0x4BA07477 rather than 0x4C013477. Over JTAG, take DPIDR from
  J-Link's connect banner instead.
  CTRL/STAT: 0xF0000000
note: AP reads are direct on this transport (determined by reading a known IDR both ways).
```

**That is the correct output, not a failure.** DPIDR, DPIDR1 and BASEPTR0/1 all live at DP address
`0x0`, which ADIv6 marks Reserved on JTAG-DP. The tool reports them as absent rather than printing
the reserved register's value. `CTRL/STAT` is at DP `0x4`, stays readable on both protocols, and is
what actually answers "is the DAP alive" — `0xF0000000` means both power-up requests are
acknowledged.

To get the real DPIDR, use the commander backend, which reads it from J-Link's connect banner:

```bat
s32edbg --transport commander connect
```

```
DPIDR      0x4C013477  (DPv3)
CTRL/STAT  0xF0000020
batched read: this backend cannot do single DP transactions.
```

`--system-power` additionally requests `CSYSPWRUPREQ`. ADIv6 is explicit that it must never be
asserted without `CDBGPWRUPREQ` ("the response to this combination of requests is UNPREDICTABLE"),
so the tool always sets both.

---

## `attach` / `release` — a foothold on a board that will not hold still

`attach` is for the case where the TAP is only alive in brief windows. Two causes, same symptom:

- **Blank flash.** The BootROM fails → functional reset (RM §62.12) → FREC escalates → destructive
  reset (RM §60.5.2.4) → DEST0, which persists *"until a POR event"* (RM §60.5.6) — and RM Table
  301 makes POR one of exactly two things that hold the JTAGC in reset.
- **Software running.** RM §135.15.1: the debug pads *"operate in JTAG functionality out of reset
  but can be later reassigned"*, so an application that remuxes them removes the TAP.

`attach` retries until a window opens, then immediately holds the system in functional reset. That
stops the reboot **and** stops software reaching the pad configuration. RM Table 301 confirms a
functional reset leaves the JTAGC and SWJ-DP running, which is what makes it safe.

```bat
s32edbg attach                        :: retry, hold, then open the debug gate
s32edbg attach --attempts 30          :: more patience on a fast-cycling board
s32edbg attach --no-hold              :: just wait for a window, do not stop the target
s32edbg attach --no-gate              :: hold, but skip `gates apply`
```

| Option | Default | Meaning |
|---|---|---|
| `--attempts INT` | `20` | How many times to retry while waiting for a live window. |
| `--hold / --no-hold` | `--hold` | Hold the system in functional reset once attached. |
| `--gate / --no-gate` | `--gate` | Apply the LADDER-1 debug-enable sequence after attaching. |

> **`attach --hold` stops the application.** Use `release` to restart it.

```bat
s32edbg release
s32edbg release --attempts 20
```

```
released on attempt 1; the system is running again.
```

`release` retries for the same reason `attach` does: the DAP powers up only in windows, and a
release that gives up after one try leaves the target stopped with no obvious way back. It **never**
issues a destructive reset. If the DAP is unreachable entirely, a power cycle clears the hold
unconditionally.

Confirm the state either way with `gates status`, which reports `functional_reset_asserted`.

---

## `gates` — the debug-enable gates

RM Figure 1013 draws two gate layers. This command reports both and drives the one that is safe to
drive.

### `gates status` — read-only

```bat
s32edbg gates status
```

### `gates apply` — open the debug clocks

Writes the four "LADDER-1" MDM-AP registers. **Idempotent**, and required before `romdump` or `mem`
will see anything.

```bat
s32edbg gates apply
```

```
  MDM-AP (APB debug-enable gate)
┌─────────────┬────────────┐
│ Register    │ Value      │
├─────────────┼────────────┤
│ IDR         │ 0x001DE040 │
│ STATUS      │ 0xF08001C0 │
│ CONTROL     │ 0x00800000 │
│ DBGENCNTRL  │ 0x3CF3CF00 │
│ DBGENCNTRL2 │ 0x3CF3CF00 │
│ DBGENCNTRL3 │ 0xFFF03CF0 │
└─────────────┴────────────┘
STATUS: system_reset=no, cdbgpwrupreq=yes, csyspwrupreq=yes, aux_tap_trst_released=yes
Debug-enable gate: open

       SDA-AP (HSE security gate, read-only)
┌────────────────────────────┬────────────────────┐
│ IDR                        │ 0x001DE001         │
│ AUTHSTTS                   │ 0x40000005         │
│ APPDBGEN                   │ yes                │
│ CHALRDY                    │ yes                │
│ SDASYSRSTS                 │ 0x00000000         │
│ functional_reset_asserted  │ no                 │
│ destructive_reset_asserted │ no                 │
│ SDARSTCTRL                 │ 0xF0F30000         │
│ UID                        │ 0x1E09300B5563B9BC │
└────────────────────────────┴────────────────────┘
! SDA-AP AUTHSTTS[CHALRDY] is set: the HSE is offering a debug-authentication
  challenge. This tool never answers it -- the RM states only one attempt is
  ever accepted.
debug-enable gate is open (sticky until power loss)
```

**Order matters inside this command.** `CONTROL[MSK_DBG_FAULT]` is written first, because RM §135.12
says the VFCCU raises a debug fault when debug-enable signals assert with no debugger attached —
masking first prevents a fault storm as the enables go high.

**If the registers read back zero**, that does not mean the sequence is wrong. It means MDM-AP
*writes* are being rejected, which points at the HSE gate rather than at this sequence — check
`APPDBGEN` above.

The SDA-AP is **read-only** here, with one deliberate exception (`SDASYSRSTC`, used by
`attach`/`release`). The authentication path is never written.

---

## `apmap` — discover access ports and diff against the RM

```bat
s32edbg apmap
s32edbg apmap --method direct        :: read each expected AP's IDR
s32edbg apmap --method walk          :: walk the AP-space ROM table
s32edbg --json apmap.json apmap
```

Two independent discovery methods, merged, then diffed against RM Table 957. They fail differently,
so agreement between them is the evidence — and when D-1 corrupted `BASEPTR`, this was the one
command that survived, because the direct method carried it.

```
│ 0x01CA0000 │ 0x34770008 │ match │ MEM-AP, AMBA AHB5 │ AHB-AP (reserved) │ direct │
│ 0x01CA2000 │ 0x34770008 │ match │ MEM-AP, AMBA AHB5 │ AHB-AP (reserved) │ direct │
└────────────┴────────────┴───────┴───────────────────┴───────────────────┴────────┘
AP map matches RM Table 957 exactly.
```

The nine ports, and what each one masters:

| Base | Role | Bus reached |
|---|---|---|
| `0x01C10000` | MDM-AP | none — debug enables, cross-trigger, reset status |
| `0x01C20000` | AXI-AP | **the full system memory map** |
| `0x01C30000` | APB-AP | **APBIC2** — 28 MB of debug components |
| `0x01C70000` | SDA-AP | none — authentication. Read-only in this tool |
| `0x01C80000` | AHB-AP | SMU Cortex-M33 subsystem |
| `0x01C90000` / `0x01C92000` | AHB-AP | FlexLLCE CE Cortex-M33 core 0 / core 1 |
| `0x01CA0000` / `0x01CA2000` | AHB-AP | reserved — present in silicon, undocumented |

---

## `romdump` — walk every reachable CoreSight ROM table

Handles both ADIv6 class 0x1 and class 0x9 tables, follows nested tables, and guards against
circular references with a visited set and a depth cap.

```bat
s32edbg gates apply                             :: prerequisite
s32edbg romdump                                 :: both trees
s32edbg romdump --source apb                    :: only the CoreSight tree behind the APB-AP
s32edbg romdump --source apspace                :: only the AP-map ROM
s32edbg romdump --ap 0x01C30000 --rom 0x01BFE000
s32edbg --json rom.json romdump
```

| Option | Default | Meaning |
|---|---|---|
| `--source {both,apspace,apb}` | `both` | `apspace` = the AP-map ROM; `apb` = the CoreSight tree behind the APB-AP. |
| `--ap TEXT` | APB-AP | Override the MEM-AP to walk through. |
| `--rom TEXT` | from `BASE` | Override the ROM table address to start from. |
| `--max-depth INT` | `16` | Recursion cap. |

```
    ├── 0x01470000 class 0x9 (CoreSight component), part 0x9ED, Arm, CTI
    └── 0x01480000 class 0xE (generic IP component), part 0x000
12 tables, 74 components, 12 errors
```

**Error nodes are recorded, not fatal — and on this part 12 of them are correct.** Eleven are
unmapped APBIC2 regions whose reads echo address bits (`0x01050000` → CIDR `05 05 05 05`); the
twelfth is a real component whose `CIDR1` is `0x09` when the low nibble must be zero. ADIv6 says a
debugger meeting an invalid ID block should warn and keep going, which is what happens.

Note `romdump` prints CIDR **MSB-first** in its error text, so a component reading
`CIDR0=0x0D, CIDR1=0x09, CIDR2=0x05, CIDR3=0xB1` appears as `CIDR 0xb1 0x05 0x09 0x0d`. That looks
like a reversed capture and is not one — cross-check with `mem read` if in doubt.

If the APB tree comes back empty, the gate is shut.

---

## `mem` — read and write every bus

The APB-AP reaches the CoreSight debug components on APBIC2; the AXI-AP reaches the whole system
memory map. `--ap` takes a name or a raw base address, and defaults to `apb`:

```
mdm  axi  apb  sda  ahb-smu  ahb-ce0  ahb-ce1  ahb-r0  ahb-r1
```

### `mem info` — AP identity and capabilities

```bat
s32edbg mem info --ap apb
```

```
AP        0x01C30000 APB-AP (debug APB -> APBIC2)
IDR       0x24770006
CFG       0x000101A0  big_endian=False large_address=False large_data=False
CSW       0x00800042 (as found, before any access)
BASE      0x01BFE000
```

CSW is reported **before any access**, because reading it afterwards would show what this tool set
rather than how the AP was configured.

### `mem read` — one or more values

```bat
s32edbg mem read 0x00410FF0 --ap apb --count 4        :: CoreSight CIDR0-3
s32edbg mem read 0x41850014 --ap axi                  :: a peripheral register
s32edbg mem read 0x41850014 --ap axi --size 8 --count 4
```

```
0x00410FF0  0x0000000D
0x00410FF4  0x00000090
0x00410FF8  0x00000005
0x00410FFC  0x000000B1
```

That is the CoreSight identification preamble — `0x0D`, `0x?0`, `0x05`, `0xB1` — which ADIv6 fixes
for every component. It makes `+0xFF0` a useful known-answer probe when you want to confirm a bus
is reachable at all.

`--size 8` and `--size 16` handle byte-lane placement within DRW (ADIv6 C2.6.6): a sub-word transfer
appears in the lane the low address bits select, not right-aligned.

### `mem dump` — hex-dump a region

Address and length must both be 4-byte aligned.

```bat
s32edbg mem dump 0x01BFE000 256 --ap apb
```

```
0x00410FD0  00000004 00000000 00000000 00000000  |................|
0x00410FE0  00000013 000000BD 0000004B 00000000  |........K.......|
0x00410FF0  0000000D 00000090 00000005 000000B1  |................|
```

Block reads rewrite `TAR` at every 1 KB boundary. ADIv6 C2.6.4 only guarantees auto-increment
*within* a 1 KB window, and an AP may wrap rather than carry — a reader that ignores this returns
plausible values from the wrong addresses with no error raised anywhere. If you ever want to verify
it yourself, dump across a boundary and compare against `mem read --count N` of the same range.

### `mem write` — one value, with confirmation and read-back

```bat
s32edbg mem write 0x00410FA0 0x1 --ap apb            :: prompts first
s32edbg mem write 0x00410FA0 0x1 --ap apb --yes      :: no prompt
```

```
0x00410FA0  wrote 0x00000001, reads back 0x000000FF
! read-back differs. That is normal for a write-1-to-clear or read-only field,
  but check it is what you meant.
```

**A differing read-back is often correct.** The example above writes a CoreSight `CLAIMSET` tag;
`CLAIMSET` reads back the *number of implemented claim tags*, not the value claimed. The claim
actually landing is visible at `CLAIMCLR` (`+0xFA4`). Reporting the written value instead would make
a rejected write look successful, which is the failure mode this warning exists for.

`--yes` skips the *prompt*, never the *policy*.

### What `mem` refuses

| Situation | Result |
|---|---|
| Any write to the SDA-AP | exit **3** — RM §135.13.4, the HSE C/R is one-shot |
| Any AXI-AP access while functional reset is asserted | exit **3** — RM Table 957, recovery needs a destructive reset |
| Debug gate closed | **warning**, not a refusal — you may be deliberately probing a cold part |

The AXI refusal has a consequence worth stating plainly: because `attach --hold` asserts functional
reset, the AXI-AP is only reachable when the system is *running*. That is the right trade — the
refused state is the unrecoverable one — but it means AXI writes are never routine.

---

## `core` — run-control for the Cortex-R52 cores

Each R52 has its own 4 KB external debug frame on APBIC2, reached through the APB-AP, plus a CTI at
`+0x10000`. `gates apply` is a prerequisite. `--core` accepts an index, a name (`rtu0.1`), or a
debug-frame base address.

### `core list` — what cores are there

```bat
s32edbg core list
```

```
┌───┬────────┬─────────────┬────────────┬────────────┬──────────┬───────┬──────────┬─────────┐
│ # │ Name   │ Debug frame │ CTI        │ Part       │ Identity │ BP/WP │ Invasive │ State   │
├───┼────────┼─────────────┼────────────┼────────────┼──────────┼───────┼──────────┼─────────┤
│ 0 │ rtu0.0 │ 0x00410000  │ 0x00420000 │ Cortex-R52 │ ok       │ 8/8   │ granted  │ running │
│ 1 │ rtu0.1 │ 0x00510000  │ 0x00520000 │ Cortex-R52 │ ok       │ 8/8   │ granted  │ locked  │
│ 2 │ rtu1.0 │ 0x00C10000  │ 0x00C20000 │ Cortex-R52 │ ok       │ 8/8   │ granted  │ locked  │
│ 3 │ rtu1.1 │ 0x00D10000  │ 0x00D20000 │ Cortex-R52 │ ok       │ 8/8   │ granted  │ locked  │
└───┴────────┴─────────────┴────────────┴────────────┴──────────┴───────┴──────────┴─────────┘
```

Names come from `DEVAFF0`, which carries the core's MPIDR affinity — so they say which core a frame
*belongs to* rather than being inferred from its address.

**`Identity: ok` means three anchors agree** — `MIDR` decodes as Cortex-R52, `DEVTYPE` is `0x15`,
`DEVARCH` archid is `0x6A05`. Nothing is written to a frame that fails this, because a wrong base
address does not fault; it reads and writes some other component.

**`BP/WP` is read from `EDDFR`, not assumed.** A wrong offset would also produce a number, and
programming comparator 7 on a core that implements four writes into a reserved region.

`locked` is normal: most cores come up with the OS lock set, and `EDSCR` faults while it is. `core
halt` clears it automatically; `core unlock` does it on its own if you want to see whether it will.

Only four frames answer on this part. See *What silicon settled* below — that is lockstep, and it
is a boot-time configuration.

### `core status` — decoded state, and where it stopped

```bat
s32edbg core status --core rtu0.1
```

```
EDSCR   0x03054113  halted: external debug request
        halting_debug_enabled=True sticky_error=False instruction_transfer_empty=True
EDPRSR  0x00000011  powered_up=True halted=True os_locked=False in_reset=False
stopped at 0x321D2EE6  (the single armed breakpoint comparator)
```

`halted` requires **both** status registers to agree. They are independent reports of the same
fact, so disagreement means a bit map is wrong — it is reported as unknown rather than resolved by
preferring one.

### `core halt` / `core resume` / `core unlock`

```bat
s32edbg core halt --core rtu0.1
s32edbg core resume --core rtu0.1
s32edbg core halt --all
s32edbg core resume --all --sync      :: one CTM pulse restarts every halted core (guide §5.9)
s32edbg core unlock --core rtu1.0     :: clear the OS lock without stopping anything
```

Armv8 has no halt bit — the request is a CTI trigger — so this programs the core's cross-trigger
interface and pulses the channel wired to the debug request. Once the halt has taken, the request
is acknowledged and its deassertion confirmed through `CTITRIGOUTSTATUS` — guide §5.5: a
still-asserted request makes the core swallow the next restart. The restart itself is confirmed
through `EDPRSR.SDR`, the architected sticky-debug-restart flag, not by watching the status change.

**A resume can legitimately come back halted.** If a breakpoint sits on a hot address, the core
re-enters Debug state faster than any poll can see it running. That is reported as *"ran and
immediately re-halted at 0x…"*, not as a failure — it is a working breakpoint, and `SDR` proves
the core did leave Debug state.

`--sync` implements the guide's §5.9 synchronized restart: each halted core's restart channel is
gated onto the cross-trigger matrix, one pulse releases them all, and the gates are closed again
so later single-core resumes stay local.

### `core regs` / `core reg`

```bat
s32edbg core regs --core rtu0.1
s32edbg core reg r3 --core rtu0.1
s32edbg core reg r3 0x1234 --core rtu0.1
s32edbg core reg pc --core rtu0.1               :: read DLR, the Debug-state PC
s32edbg core reg pc 0x321D2EE6 --core rtu0.1    :: redirect execution before a resume
s32edbg core reg dspsr --core rtu0.1
```

```
│ r0  │ 0x3178DC34 │ r4  │ 0x00000000 │ r8  │ 0x00000000 │ r12   │ 0x0000000A │
│ r1  │ 0x3178DC48 │ r5  │ 0x00000000 │ r9  │ 0x00000000 │ r13   │ 0x3040A400 │
│ r2  │ 0x321DBC23 │ r6  │ 0x00000000 │ r10 │ 0x00000000 │ r14   │ 0x321DBC23 │
│ r3  │ 0x321D2EE7 │ r7  │ 0x00000000 │ r11 │ 0x00000000 │ dspsr │ 0x0000003F │
PC 0x321D2EE6  (DLR)   mode System, Thumb, flags -
```

There is no register file to read. In Debug state the core executes whatever instruction is written
to `EDITR`, so every value is fetched by making the core move it into the debug transfer register —
through a scratch register that is saved and restored, or inspecting the context would corrupt it.

**That path is proved against a known pattern before any value is reported**, and it calibrates the
instruction encoding (T32 or A32) while doing so. If the round trip fails, nothing is printed. Same
discipline as `calibrate_tdo_order()` for raw JTAG.

### `core step`

```bat
s32edbg core step --core rtu0.1                 :: one instruction
s32edbg core step --core rtu0.1 --over          :: run a call to completion
s32edbg core step --core rtu0.1 --count 10
```

`--into` (the default) is an architectural halting step. `--over` reads the instruction at the PC
first, and if it is a branch-with-link it puts a temporary breakpoint at the return address and lets
the call run — so you do not step through the whole callee. The temporary comparator is always
removed, including on timeout.

Three things happen automatically, each of which makes stepping useless if omitted:

- **Interrupts are masked** (`EDSCR.INTdis`), and restored afterwards. Without it a step on a system
  with a periodic tick executes one instruction, immediately takes the interrupt, and stops in the
  vector table — so stepping teleports into handlers and never advances through your code. Use
  `--allow-interrupts` when you want to step into the handler deliberately.
- **Armed breakpoints are suspended for the duration**, and restored. A comparator at the current PC
  re-triggers the instant the core restarts, before any instruction executes, so the PC never moves.
- **The instruction set is read from `DSPSR.T`**, not assumed — an interrupt can take a core from a
  T32 loop straight into an A32 exception handler, and the two have different instruction widths.

### `core sample` — find a hot address without stopping anything

```bat
s32edbg core sample --core rtu0.0 --count 400
```

```
  0x321D2EE8    184   46.0%  #######################
  0x321D2EE6     57   14.2%  #######
  0x323E6118     54   13.5%  ######
```

`EDPCSR` is a profiling register: valid while the core runs, `0xFFFFFFFF` while it is halted. This
is the safe way to pick a breakpoint target — halting repeatedly to hunt for one risks the deadlock
described in *Session order*.

### `core xtrigger` — halt every core together

```bat
s32edbg core xtrigger on
s32edbg core xtrigger off
```

Routes each core's "entered Debug state" trigger onto a shared CTI channel and that channel to every
core's debug request, so one core stopping stops them all — a coherent multicore snapshot. It is
**off by default and single-core halts deliberately do not propagate**; `CTIGATE` is cleared for the
single-core channels so an ordinary halt cannot disturb anything else.

Inside a `debug` session, prefer the stateful form: **`sync on`** arms the same group *and*
teaches the session about it — bare `halt` waits for the partners to stop with the group, bare
`continue` restarts the whole group on one pulse (with the beyond-guide teardown that masks the
halted cores' level requests for the restart window — §5.9 never says how to restart under an
armed group), `step` steps one core while the others stay halted, and `quit` disarms. A raw
`xtrigger on` gives the session none of that and survives the session's close.

### `core catch` — halt a core at its first instruction when the *application* releases it

`reset-halt` (below) owns a core across a **debugger-driven** reset. `catch` is its counterpart
for the release the debugger does not perform: on this application core0's firmware releases
rtu0.1 at runtime (`BrsHwEarlyInitPowerOn` — it asserts core-1 reset first, then releases), and
`catch` stops core1 at instruction 0 of that release. The whole dual-core wiped-flash session:

```bat
s32edbg --speed 16000 debug
s32edbg[-]> load C:\br1_rear_ecu\build\bin\br1_rear.elf
s32edbg[rtu0.0]> catch --core rtu0.1
s32edbg[rtu0.0]> continue                      :: core0 boots; its firmware releases core1
s32edbg[rtu0.0]> catch --core rtu0.1 --wait    :: core1 caught at its first instruction
s32edbg[rtu0.0]> use rtu0.1
s32edbg[rtu0.1]> regs
s32edbg[rtu0.1]> bp set 0x...
s32edbg[rtu0.1]> continue
s32edbg[rtu0.1]> quit
```

**Mechanism.** The catch arms the core's **MDM EDBGREQ latch** — a level debug request in the
MDM-AP, which survives the core's own cold reset. That property is the whole choice: the
application asserts core-1's reset before releasing it, and a core-local cold reset wipes
`EDECR` — so the architected `EDECR.RCE` reset catch cannot survive an application-driven
release (the guide's §5.10 covers only debugger-driven releases; gap surfaced in the test
plan). The latch halting a core on the application's own release is measured silicon behaviour:
test-plan Finding 7, discovered as a bug, used here as the feature.

**The hold is not yet Debug state.** An EDBGREQ-held core reads `EDPRSR.HALTED` without being
architecturally halted — registers refuse, and dropping the latch would resume it. `catch
--wait` converts: unlock (the cold reset re-set the OS lock), a real acknowledged CTI halt, drop
the latch, re-sanitise the comparators the reset wiped. Any write command (`bp set`, `halt`)
against a fired catch converts automatically first.

**Safety contract.** Arming without `--wait` needs a `debug` session, because `quit` drops every
unfired latch — a latch left behind halts core1 on the next boot's release with nobody attached,
which is exactly Finding 7. Standalone `catch --wait` drops the latch itself on timeout;
`catch --disarm` drops it on demand.

```bat
s32edbg core catch --core rtu0.1                        :: arm only (session required)
s32edbg core catch --core rtu0.1 --wait --timeout 60    :: arm + wait + convert (standalone ok)
s32edbg core catch --core rtu0.1 --disarm               :: drop the latch on demand
```

What the catch looks like when it fires (measured 2026-08-05, wiped-flash board):

```
catch armed on rtu0.1 (MDM EDBGREQ latched).
rtu0.0 running -- running (non-debug state)
rtu0.1: cleared the OS lock
rtu0.1 caught at its first instruction -- halted: external debug request  PC 0x32100020
```

### `core reset-halt` — own a core from its first instruction

```bat
s32edbg core reset-halt --core rtu0.1
```

The guide's §5.10.2 **hold flow**: the core's `SDARSTCTRL.RSTREL` bit is cleared so it stays in
reset while `SDASYSRSTC` functional-resets the system (the same proven mechanism
`attach`/`release` use — the debug connection survives and everything else boots normally), then
`EDECR.RCE` is armed on the held core (EDECR is OS-lock-exempt) and the bit is set back. The
catch fires before the first instruction; the OS lock the cold reset re-set is cleared, and the
core sits at its reset vector having fetched nothing. Proven: caught rtu0.1 at `PC 0x32100020`.

**The functional reset is system-wide**: every core reboots; only the chosen one is caught, and
comparators on the other cores are wiped (functional reset resets RTU debug state — and the
§5.10.3 SDARSTMASK protection the guide describes measurably does **not** work on this silicon;
worse, a reset taken with the mask set wedged the boot. See the test plan's finding 1). The
catch bit and the release bit are restored whatever happens.

---

### Worked example — breakpoint, step, continue, from a power cycle

```bat
:: 1. Debug clocks are lost on power loss. Nothing else works until this runs.
s32edbg gates apply

:: 2. Find an address the core actually executes. Stops nothing.
s32edbg core sample --core rtu0.0 --count 200
::    0x321D2EE8   80  40.0%
::    0x321D2EE6   36  18.0%      <- the idle loop

:: 3. Arm it. Clears the OS lock first if the core came up locked.
s32edbg bp set 0x321D2EE6 --core rtu0.0
::    breakpoint 0 at 0x321D2EE6 (DBGBVR 0x321D2EE4, DBGBCR 0x00000187, BAS 0b1100)

:: 4. The core is running, so it stops on its own.
s32edbg core status --core rtu0.0
::    EDSCR 0x03054107  halted: breakpoint
::    stopped at 0x321D2EE6  (the single armed breakpoint comparator)

:: 5. Look around.
s32edbg core regs --core rtu0.0
::    PC 0x321D2EE6  (DLR)   mode System, Thumb

:: 6. Step. The armed comparator is suspended and restored for you.
s32edbg core step --core rtu0.0
::    step 1  0x321D2EE6 -> 0x321D2EE8  (halted: halting step, normal)

:: 7. Continue past it: clear, then resume.
s32edbg bp clear --all --core rtu0.0
s32edbg core resume --core rtu0.0
```

Step 6's status is the thing to check. **`halting step, normal` means it worked**; `halting step, no
syndrome` with a jump into the vector table means an interrupt was taken instead of your
instruction, and a PC that does not move at all means a comparator re-triggered.

**Halting the core that runs your application can leave it stuck**, whatever the debugger does
correctly — the other cores keep running against shared state it was in the middle of. Recovery is a
functional reset, not a resume:

```bat
s32edbg attach --no-gate
s32edbg release
```

---

## `bp` / `wp` — hardware comparators

Per core, not global. A breakpoint on `rtu0.0` does not catch code running on `rtu0.1`, and in
lockstep the pair is one logical target.

```bat
s32edbg bp list  --core rtu0.1
s32edbg bp set 0x321D2EE6 --core rtu0.1
s32edbg bp set 0x32104A80 --core rtu0.1 --arm      :: A32 target, not Thumb
s32edbg bp clear 0 --core rtu0.1
s32edbg bp clear --all --core rtu0.1

s32edbg wp list  --core rtu0.1
s32edbg wp set 0x3178F000 --size 4 --on store --core rtu0.1
s32edbg wp clear --all --core rtu0.1
```

```
breakpoint 0 at 0x321D2EE6 on rtu0.1 (DBGBVR 0x321D2EE4, DBGBCR 0x00002187, BAS 0b1100)
```

Four things handled for you, each of which silently produces a comparator that never fires:

- **The Thumb bit is stripped.** A function pointer to T32 code has bit 0 set to mark the
  instruction set; it is not part of the address.
- **`DBGBVR` is word-aligned**, so which halfword within the word is selected by `BAS` — note
  `0b1100` above, the high halfword. Selecting all four bytes would also cover the neighbouring
  instruction.
- **The comparator matches at any exception level.** The control words carry the guide §5.7/§5.8
  any-EL qualification `{HMC,SSC,PMC} = {1,00,11}` — "the default choice for a halting debugger" —
  so a breakpoint also fires in EL2 (hypervisor) code. Proven on silicon 2026-08-02: DBGBCR
  `0x2067` fired at `0x321D2EE8`, and DBGWCR `0x3FF7` fired with EDWAR reporting the exact
  watched address.
- **Every write is verified by read-back.** Comparator writes that read back as zero are the classic
  sign that invasive debug is not actually authorised.
- **Halting debug (`EDSCR.HDE`) is enabled by both `bp set` and `wp set`.** With HDE clear the
  event routes to self-hosted handling: the comparator arms, reads back perfectly, and never
  halts anything (guide §5.2 item 3 — measured for breakpoints; watchpoints carry the same fix).

Arming while the core is running prints an advisory (guide §5.7/§5.8): comparator changes are
only guaranteed visible after a context synchronization event, so halt first when it matters —
though arming-while-running has fired on this silicon.

Free slots are shown as well as used ones, because *"how many are left"* is the question that
matters when a breakpoint cannot be inserted. On a core that is OS-locked or still held in
reset, `bp/wp list` names the reason (and the fix) instead of leaving per-slot read errors
unexplained.

---

## `m33` — the Cortex-M33 subsystems

A different debug model, not an extension of `core`. An M33 is reached through its own AHB-AP as
ordinary memory — the debug registers live in the processor's own address map at `0xE000E000` — so
halting is a single write and reading a register is two, with no instruction-transfer register and
no cross-trigger interface.

```bat
s32edbg m33 list
s32edbg m33 halt   --core smu
s32edbg m33 regs   --core smu
s32edbg m33 step   --core smu --count 3
s32edbg m33 resume --core smu
s32edbg m33 sample --core smu
s32edbg m33 bp list --core smu
s32edbg m33 bp set 0x25001F70 --core smu
s32edbg m33 bp clear 0 --core smu
s32edbg m33 wp set 0x250BFF40 --size 4 --on store --core smu
s32edbg m33 wp list --core smu
s32edbg m33 reset-halt --core smu
```

```
┌──────┬────────────┬────────────┬───────┬─────────┬─────────┐
│ Name │ AP         │ Part       │ BP/WP │ Debug   │ State   │
├──────┼────────────┼────────────┼───────┼─────────┼─────────┤
│ smu  │ 0x01C80000 │ Cortex-M33 │ 8/4   │ enabled │ running │
│ ce0  │ 0x01C90000 │ Cortex-M33 │ 8/4   │ off     │ running │
│ ce1  │ 0x01C92000 │ Cortex-M33 │ 8/4   │ off     │ running │
└──────┴────────────┴────────────┴───────┴─────────┴─────────┘
```

Three of them: the System Manager, and FlexLLCE CE cores 0 and 1. Comparator counts come from
`FP_CTRL` and `DWT_CTRL` — the TRM allows four or eight breakpoints, so the count is read.

`Debug: enabled` means `DHCSR.C_DEBUGEN` is set. It is deliberately left set after a resume: clearing
it would also disarm every breakpoint currently armed.

Registers are simpler here than on the R52 — a selector and a data register, no instruction
execution and no scratch register, so reading them cannot disturb the halted context. The PC is just
selector 15.

`m33 sample` uses `DWT_PCSR`, the M33's equivalent of `EDPCSR`. Note that on this board the two CE
cores report every sample invalid — they are not executing at all:

```
ce0: 100 samples, 100 invalid, 0 distinct addresses
! every sample was invalid, so no PC was captured: the core is not executing --
  held in reset, or its clock is gated.
```

`m33 wp` arms a DWT comparator as a halting data watchpoint (guide §6.7): `DEMCR.TRCENA` is
enabled first, the address must be naturally aligned to the watched size, and a halt reports
`DFSR.DWTTRAP`. DWT watchpoints are **asynchronous** — the core halts *near* the access, not at
it. Halts report their cause from `DFSR` (halt request, breakpoint, watchpoint, vector catch,
external), and the reported bits are cleared so the next halt's cause is fresh.

`m33 reset-halt` is the architected reset-and-halt (guide §6.8): `C_DEBUGEN` +
`DEMCR.VC_CORERESET`, then `AIRCR.SYSRESETREQ`. **Blocked on this silicon**: measured 2026-08-02,
the AIRCR write is accepted but SYSRESETREQ takes no effect — no reset occurs and the core keeps
running; the command reports the failure cleanly and disarms the vector catch. The SoC gates M33
SYSRESETREQ somewhere outside the guide's sources; the SDA-AP `RSTWAIT*` CPUWAIT hold is the
documented candidate for a future SoC-level flow.

---

## `debug` — one session, many commands

Every other command opens and closes its own J-Link connection, and on this board **every connect
pulses reset through R439 into the SBC and PMIC** (test-plan defect D-2) — while a halt held
*across* invocations once deadlocked the application, and three separate run-control bugs came
from state straddling invocations. `debug` opens the connection once, caches core discovery, and
closes once.

```bat
s32edbg debug --core rtu0.1
```

```
session open -- 4 R52 core(s): rtu0.0 rtu0.1 rtu1.0 rtu1.1; M33: smu ce0 ce1
s32edbg[rtu0.1]> halt
rtu0.1 halted -- halted: external debug request (EDSCR 0x03054113, EDPRSR 0x00000011)
s32edbg[rtu0.1]> bp set 0x321D2EE6
breakpoint 0 at 0x321D2EE6 on rtu0.1 (DBGBVR 0x321D2EE4, DBGBCR 0x00002187, BAS 0b1100)
s32edbg[rtu0.1]> resume
rtu0.1 running -- running (non-debug state)
s32edbg[rtu0.1]> step
step 1  0x321D2EE6 -> 0x321D2EE8  (halted: halting step, normal)
s32edbg[rtu0.1]> quit
restore: cleared breakpoint 0 on rtu0.1
restore: resumed rtu0.1
session closed
```

Inside the prompt every normal command works, minus the global options — `bp set …`, `wp list`,
`mem read 0x00410FF0`, `m33 halt --core smu`, `gates status`. The focused core (set with
`--core` or `use <name>`) is the default target, and the bare aliases `halt`, `resume`
(`continue`), `step`, `regs`, `reg`, `status`, `unlock`, `sample`, `reset-halt` and `catch`
act on it.

**`sync on|off|status`** is the stop-the-world switch for software-coupled cores (this
application's pair deadlocks when one core halts alone — see the `core` section). Armed: any
halt, manual or a breakpoint hit on either core, stops both; bare `continue` restarts both on
one pulse; a breakpoint hit noticed later is taken into the session's ownership so close
resumes everything. `quit` always disarms. Measured 2026-08-05 — the first cross-trigger halt
ever fired on this silicon:

```
s32edbg[rtu0.0]> sync on
rtu0.0: cross-trigger enabled (CTIGATE 0x0000000C)
rtu0.1: cross-trigger enabled (CTIGATE 0x0000000C)
sync on: any halt -- manual or a breakpoint hit -- now stops all cores together;
bare `continue` restarts the whole group; `quit` disarms.
s32edbg[rtu0.0]> halt
rtu0.0 halted -- halted: external debug request (EDSCR 0x03054193, EDPRSR 0x00000011)
sync: rtu0.1 stopped with the group
s32edbg[rtu0.0]> step
sync: cross-halt masked for the step; the other cores stay halted
step 1  0x321D5B98 -> 0x321D5B9A  (halted: halting step, normal)
s32edbg[rtu0.0]> continue
synchronized restart: one pulse released 2 core(s)
rtu0.0 running -- running (non-debug state)
rtu0.1 running -- running (non-debug state)
```

**Closing restores the target.** On `quit`, end of piped input, or Ctrl-C at the prompt, the
breakpoints and watchpoints the session set are cleared *first*, then the cores the session
halted are resumed — in that order, so a resumed core does not trip over a session breakpoint.
`--keep-on-exit` leaves everything exactly as the session left it instead.

**First touch of a core sanitises its comparators.** Guide §5.7: every comparator field is
UNKNOWN after cold reset, *including the enable bit*, so the session starts each core it drives
from a known state and reports what it discarded. `--keep-breakpoints` skips this for workflows
that deliberately carry comparators between sessions.

Non-interactive use — for scripts and for the bench harness:

```bat
s32edbg debug --core rtu0.1 -c "halt" -c "regs" -c "resume"
type script.txt | s32edbg debug --core rtu0.1
```

Two commands cannot run inside a session and are refused with their reasons: `bscan` (the RM
Table 984 preamble changes the JTAG chain under the held connection — ordering rule 3), and
`doctor`/`attach`/`release`/`iface`, which open their own transports or tear the connection down.
Raw-JTAG `tap` subcommands need the commander backend and are refused the same way. A session
requires the `dll` backend — the commander backend runs one `JLink.exe` process per operation and
cannot hold a connection open.

**No input ends the session.** A malformed address (`bp set 32169874` — bare numbers are
rejected, see *Address and value syntax*), a refused operation, a target error, even an internal
tool bug: all of them print and the prompt returns. The session is the asset on this board;
losing it costs a reconnect and another D-2 reset pulse.

---

## `load` — write a build into RAM and park at its entry

The fast build/debug loop: compile, `load`, breakpoint, `resume` — no flashing.

```bat
s32edbg debug
s32edbg[-]> load C:\br1_rear_ecu\build\bin\br1_rear.elf
```

What it does, in order (the reference S32-Debug-Probe plugin's flow, replicated register for
register — see `s32edbg/data/rtuboot.py` for every value with its citation):

1. **P0** — debug power-up, the LADDER-1 debug enables, and halting the three M33s.
2. **P1/P2** — a system reset (SDA-AP functional reset by default) with **MDM EDBGREQ latched
   for every R52** across it, so each core halts the instant it leaves reset. All AXI-AP
   traffic waits until both reset flags read clear (guide §9 #3).
3. **P3** — MC_ME/RGM partition bring-up for RTU0 and RTU1, plus NXP's generic SRAM scrubs.
4. **P4** — the SRAMC D0/D1/D2 ECC init **with the edge-trigger fix**: `RAMCR.INIT` is
   edge-triggered, so each controller gets `RAMCR=0` first, and three witness words must read
   exactly `0x00000000` afterwards or the load aborts — uninitialised DataRAM ECC is the
   documented `Os_Hal_CoreFreeze` panic.
5. **P5** — rtu0.0 is parked on a `b .` loop via its MC_ME boot address and caught halted by
   the pending EDBGREQ, then unlocked and sanitised like any session core.
6. **P6/P7** — every `PT_LOAD` segment is written at its **LMA** (GDB-`load` semantics: file
   bytes only, no BSS fill; one ragged tail is zero-padded into its final word). The J-Link
   DLL's `WriteMem` fast path is used while it proves itself — the first block is fully read
   back through the independent ADIv6 path, later blocks are sample-verified — and any
   mismatch demotes the whole load to pure ADIv6 word bursts. `--verify` compares every word.
7. **P8** — PC = the ELF entry (Thumb bit stripped into DSPSR.T), and the core **stays
   halted**. Set breakpoints, then `resume`.

```
load: SRAMC D0/D1/D2 ECC init verified (witness words read 0)
load: parking rtu0.0 on a `b .` loop and releasing it into the halt
load: WriteMem fast path proven by full read-back of the first block (512 words)
load: wrote 0x3210C580    781,560 bytes    6.76s     112.9 KB/s
...
total 3,677,786 bytes in 33.58s (107 KB/s), fast path used, verify sampled
rtu0.0 halted at the entry  PC 0x32100020  DSPSR 0x400001FA
focus: rtu0.0 -- set breakpoints, then `resume` (or `continue`) when ready.
```

(Measured 2026-08-04, V13 probe at `--speed 16000` — the reference plugin's own
JTAG clock; the tool's default is 4 MHz, which loads at ~71 KB/s. The pure
ADIv6 fallback runs at ~4 KB/s — the fast path is what makes the loop fast.)

**Flags.** `--verify` full word-for-word read-back; `--no-fast` pure ADIv6 writes only;
`--reset {sda,pin}` — `pin` replicates the plugin's RESET_B toggle literally (the pulse reaches
the SBC/PMIC through R439); `--core1 {leave,park,release}` — default `leave`, because core0's
own firmware performs the complete rtu0.1 bring-up (`BrsHwEarlyInitPowerOn()`,
`BrsHw.c:159-212`, verified in the br1_rear sources, and it asserts core-1 reset first so any
debugger pre-state is tolerated). `park`/`release` are the plugin-faithful fallback.

**Run it inside `debug`.** A one-shot `s32edbg load app.elf` works and says so loudly — but the
*next* connect pulses reset into the SBC/PMIC (D-2) and reboots away what was loaded. Inside a
session, quitting resumes the parked core (the normal restore contract) unless `--keep-on-exit`.

**Back-to-back loads are first-class.** Each in-session `load` reopens the probe connection
first — the J-Link DLL's WriteMem has a ~4 MB budget per connection (test-plan Finding 9), and a
fresh one per load keeps every load at full speed. The reopen's connect pulse lands moments
before the load's own SoC reset, so it costs a couple of seconds and disturbs nothing.

**On a blank-flash board, `attach` first.** With nothing bootable the BootROM fail cascade
escalates the longer it runs, and its resets race the load's bring-up — measured as partition
polls dying with every later read displaced, and once as WriteMem wedging on a fresh connection
(test-plan Finding 13). `s32edbg attach` stops the cascade under a held functional reset; the
load's own quiesce then starts from a quiet SoC. Starting within seconds of a power cycle works
too; starting minutes into the cascade is the failure mode.

**This resets the whole SoC.** Everything running stops; every core reboots. The image must be
linked for the RTU0 RAM windows (DataRAM `0x3178_0000`+, extended `0x3180_0000`+, CodeRAM
`0x3210_0000`+); an ELF whose LMAs fall outside — a flash-linked image — is refused before a
single write.

---

## `flash` — the external QSPI NOR

Program, erase, verify, read back and identify the 32 MiB Macronix MX25UW25A45G the SoC boots
from. This is the s32multitool raw-QSPI route ported onto this tool's transport stack — same
registers, same values, same applet, different plumbing. `s32edbg/data/qspiflash.py` carries
every number with its citation into the reference sources.

```bat
s32edbg flash jedec
s32edbg flash program s32e2xx_bootloader_M33_BLOB.bin --offset 0x0 --yes
s32edbg flash program C:\br1_rear_ecu\build\bin\br1_rear.elf --area app --yes
s32edbg flash verify s32e2xx_bootloader_M33_BLOB.bin --offset 0x0
s32edbg flash read --offset 0x300000 --length 0x10000 --out dump.bin
s32edbg flash erase --area app --yes
s32edbg flash erase --all --yes
```

**How it works.** The QuadSPI_0 controller at `0x42320000` is driven by direct register writes
(PLL, MC_CGM muxes, 19 SIUL2_4 pads, then the NXP controller sequence with its DLL-lock poll),
followed by the raw-ops touch-up that **forces `SFACR = 0`** — S32DS leaves `0x00010803` there,
which silently rewrites the flash offset the controller emits and aliased multi-page programs on
the reference bench (NXP AN13563). Every program/erase/buffered-read then runs inside a vendored
2 KiB Cortex-M33 RAM applet on the SMU (`bootcfg_raw_qspi_m33_applet.bin`, SHA-256-pinned,
toolchain-locked, never rebuilt): arguments in r0–r3, terminates in `bkpt #0`, rc in r0, and on
failure a 28-word status mailbox is decoded into the error. Chip erase (`--all`) alone is
host-driven — WREN, the slot-0 `CHIP_ERASE` command, then an RDSR WIP poll that may legitimately
run minutes. Reads and verify go through the memory-mapped AHB window; verify is byte-exact —
no CRC anywhere in the chain, same as the reference.

Every subcommand first brings the SoC up the way s32multitool does on every connect — fresh
probe connection (Finding 9), LADDER-1, a pin (RESET_B) reset with every debug request latched,
then the reference's own bring-up: MC_ME/RGM partition enables, the NXP SRAM scrubs, and the
MC_ME PRTN0 core-0 release that boots the SMU onto a debugger-written stub (Finding 15 — this
is why a completely wiped board is flashable). The SMU is then pinned by its own DHCSR and its
EDBGREQ latch dropped (multitool-exact: `CONTROL &= ~0x10000100`, the CE latches left as the
reference leaves them; left set, the SMU re-halts the instant it is resumed into the applet),
and the flow proves the memory AP actually serves the controller and the applet RAM before
anything destructive. Destructive commands also gate on the JEDEC identity first: anything but
`0xC23989C2` is refused unless `--force-jedec`.

**Destinations are always explicit** — `--offset 0x...` or a named `--area` from the reference
project's `flash_address_sets.json`; there is no default:

| `--area` | offset | size | |
|---|---|---|---|
| `blob` | `0x000000` | 2 MiB | the bootloader BLOB with its IVT at offset 0 |
| `bl` | `0x002000` | ~1 MiB | BL Area |
| `shared` | `0x100000` | 8 KiB | Shared Flash |
| `selftest` | `0x1DC000` | 16 KiB | SelfTest Area |
| `sysimg` | `0x1E0000` | 128 KiB | SysImg Area |
| `selftestbck` | `0x1FC000` | 16 KiB | SelfTestBck Area |
| `app` | `0x300000` | 6 MiB | Application Area |
| `cert` | `0xA00000` | 20 KiB | Cert Area |

**Inputs.** A `.bin` is written verbatim at the offset — the s32multitool contract: the image
already carries whatever IVT/headers it needs, and this tool invents none. An ELF (detected by
magic, `--format bin` overrides) is **flattened LMA-relative** first: one contiguous image from
its lowest LMA, gaps filled `0xFF`, then written verbatim like any `.bin`. `br1_rear.elf`
flattens to a ~5.8 MiB span that fits `--area app`, but only ~3.5 MiB of it actually programs,
because erase-before-write is unconditional and all-`0xFF` 256-byte pages are skipped — erased
NOR already reads `0xFF` (`--no-skip-blank` to program them anyway; the full verify covers the
skipped pages either way).

**`erase --all` un-boots the board until something is programmed back — and this tool can now
program it back.** Measured 2026-08-05 (test-plan Finding 14): with nothing bootable in flash
the BootROM fail cascade escalates to a destructive reset and the **SMU M33 reads as powered
down** at attach. Since Finding 15 that state is recoverable here: every flash op boots the SMU
itself (s32multitool's MC_ME PRTN0 core-0 release, `data/smuboot.py`), so
`flash program <BLOB> --offset 0x0` restores the bootloader on a fully blank board —
**bench-proven 2026-08-06** (wiped board → jedec → full BLOB program + byte-exact verify →
power cycle → bootloader running). RAM `load` keeps working throughout; range erases that keep
offset 0 never un-boot the board in the first place.

**Known issue (Finding 16): virgin-board flashing works but is slow.** On a wiped board the
flow currently needs `--ap axi` (the recovered SMU AHB-AP's SRAM view stays cold) and
`--no-fast` (the DLL's WriteMem fails every burst on this flow and damages the SMU state even
when failing), so the whole recovery rides pure ADIv6 at ~1.3 KB/s — ~37 minutes for the
1.9 MiB BLOB where s32multitool's baseline is ~1.5–4.5 minutes. The measurements and the TODO
list (root-cause WriteMem, auto-failover to the AXI-AP, fast-read-only verify) live under
test-plan **Finding 16**.

**Programming for normal boot needs more than the image.** The M33 bootloader starts the app
only when the app-valid flag `0x55AA55AA` sits at flash `0x100000`, the app is laid out
flash-`0x300000`-equals-RAM-`0x32100000`, and the BLOB is at 0 — the whole contract, the
ordering discipline (flag last), and the exact commands are in **`docs/app-boot-contract.md`**.

**Programming at offset 0** is how a board gets its bootloader: byte 0 must be the BootROM IVT
tag `0xD1`, and the tool warns before writing an image that lacks it, and confirms it in place
after the verify. If a program at offset 0 *fails partway*, do not power-cycle: the flash is now
half-written, and a blank/corrupt boot flash escalates the BootROM failure cascade. Keep the
session, re-run `flash program`, and see `docs/virgin-board.md` for the `attach` foothold if the
board does reboot into the cascade.

**Try the scratch offset first on a new setup.** Everything Tier-2 here is unproven at the bench
(test-plan u-f1..u-f9: which AP reaches what, the real AHB window base, ReadMem's rc convention
and budget…). The acceptance sequence exercises `0x300000` (the app area — no bootloader there)
before anything touches offset 0, and so should a first manual run.

**Flags** (all subcommands): `--ap smu|axi|0x...` picks the memory AP for the whole flow
(default: the SMU's AHB-AP — every address involved is an M33-view address); `--ahb-base 0x...`
overrides the read-window base (default `0x0`, automatic one-time failover to `0x08000000` —
the reference profile carries both); `--no-fast` disables the WriteMem/ReadMem fast paths (pure
ADIv6, ~4 KB/s — the fast paths carry the same first-block-proof + sampled-cross-check + demote
discipline as `load`); `--reset pin|sda|none` picks the reset (default `pin`, the
reference's RESET_B toggle — Finding 15; `sda` is the chip-internal fallback; `none` attaches
as-is — cannot recover a wiped board, and unproven while firmware may own the controller).
`program`: `--verify full|sampled|none` (default full), `--no-skip-blank`. `erase`: `--length`,
`--all`, `--no-verify` (skip the blank check). Destructive ops prompt unless `--yes`.

**Power-cycle to boot what you flashed.** No flash subcommand resets into the new image on
purpose: the reference project documents that a debug reset is not equivalent to the cold boot
the BootROM needs. The SoC is left quiesced and halted; unplug/replug power when you want it to
boot.

**Inside `debug`** the whole group works as-is and each op reopens the probe connection first,
exactly like `load` (Finding 9 — the applet plus a multi-megabyte payload rides the same ~4 MB
WriteMem budget):

```bat
s32edbg debug
s32edbg[-]> flash jedec
s32edbg[-]> flash program br1_rear.elf --area app --yes
s32edbg[-]> quit
```

---

## `bscan` — boundary scan

**SAMPLE/PRELOAD only.** RM §136.4.4.2 on PRELOAD: *"System operation is not affected."* By contrast
EXTEST, HIGHZ and CLAMP each *"assert the internal system reset for the MCU"*, and HIGHZ additionally
puts *"all output drivers … in an inactive drive state"*. On a live ECU that is a reset and a bus
float, so they are not implemented — asking for one gives a refusal with the citation.

> **Run boundary scan last.** The preamble links `JTAGC_AE` into the scan chain and takes DAP access
> away until you power-cycle. The commands warn about this themselves.

### `bscan preamble` — the mandatory RM Table 984 sequence

Before SAMPLE/PRELOAD does anything meaningful, three IR/DR pairs must be shifted in — they select
the BSR segments for this package:

| Step | IR | DR |
|---|---|---|
| 1 | `EXT_TEST_CTRL` | 208 bits, `1800_0800_000C_0004_0000_07C0_0000_0003_3041_1800_01A0_0280_0000` |
| 2 | `TLM_TDR` | 32 bits, `0x80000001` |
| 3 | `TLM_TAP_SELECT_TDR` | 32 bits, `0x80000011` |

```bat
s32edbg bscan preamble --run-svf              :: generate SVF and play it (the route that works)
s32edbg bscan preamble --svf preamble.svf     :: just write the file
s32edbg bscan preamble                        :: shift directly (needs raw JTAG — currently blocked)
```

```
Executing file [C:\...\s32edbg-preamble.svf]...
Download successful.
SVF ok
! the preamble links JTAGC_AE into the scan chain (TLM_TAP_SELECT_TDR), which
  takes the JTAG IR from 8 bits to 16 and persists. J-Link's DAP addressing
  assumes the chain it scanned at connect, so DP/AP access -- connect, gates,
  apmap, romdump, mem, attach -- will stop working until you power-cycle.
  Run boundary scan last.
```

### `bscan sample` — capture pin states

```bat
s32edbg bscan sample --bsr-length 64
s32edbg bscan sample --bsr-length 64 --watch      :: pause, you change something, capture again
s32edbg --json bscan.json bscan sample --bsr-length 512
```

```
! the preamble links JTAGC_AE into the scan chain ... Run boundary scan last.
capture 1: 8/8 bytes
captured 64 bits: 0x0035025555A02006
Bit indices are shift positions, not pin names. RM 136.3.4 puts the BSR length
and bit ordering in the BSDL file, which is not available.
```

Progress is reported per byte, since a wide scan takes a while. A shorter scan returns the low bits
of a longer one — `--bsr-length 16` on the same board gives `0x2006`, which is the tail of the 64-bit
capture above. That is a cheap way to sanity-check the byte indexing.

**`--bsr-length` is required.** RM §136.3.4 puts the length only in the BSDL file, and measuring it
needs a TDO-reporting raw shift no backend here provides. Start at 64 and widen.

**Cost: one `JTAGLoad.exe` invocation per byte.** JTAGLoad has no "print TDO" command — its only
read channel is the mismatch report, so a read is a deliberately-failing compare that reveals one
byte at a time. 64 bits is 8 runs; 512 bits is 64 runs and takes minutes.

**How to tell a real capture from a stuck one.** Equality between captures proves nothing — a stuck
path is perfectly repeatable. Three outcomes:

| Observation | Conclusion |
|---|---|
| identical every time | ambiguous — live and quiet, or stuck |
| **a few bits move, the rest hold** | **live**: static pins plus one or two toggling |
| everything moves | noise, not a capture |

Measured on a board running an application: `0x0035025555A02006` then `0x0015025555A02006` — one bit
of 64 moved, 63 held. A quiescent board gave `0x0000000000000006`. That is what "the boundary scan
works" looks like.

### `bscan length` — currently unavailable

```bat
s32edbg bscan length
```

Needs a TDO-reporting raw shift. It fails with an explanation of which capability is missing rather
than a traceback.

---

## `tap` — raw TAP access

```bat
s32edbg --transport commander tap info    :: IDCODE + IR length
s32edbg tap ir 0x0A --bits 8              :: raw IR shift    (blocked)
s32edbg tap dr 0x0 32                     :: raw DR shift    (blocked)
```

**`tap info` needs `--transport commander`.** Unlike `tap ir` and `tap dr` it does not switch
backend for you, and on the default `dll` backend it fails with:

```
error: dll transport cannot measure the IR length: it has no way to report the
TDO capture from a raw shift
```

With the commander backend it works, because it prefers J-Link's own connect banner over a raw
shift:

```
                               JTAG TAP
┌───────────┬────────────────────────────────────────────────────────┐
│ IDCODE    │ 0x0831F01D                                             │
│ Part      │ S32E277/8/9 (27x27)                                    │
│ IR length │ 8 (J-Link chain scan (TotalIRLen reported at connect)) │
└───────────┴────────────────────────────────────────────────────────┘
note: IDCODE taken from J-Link's own chain scan at connect rather than a raw
shift, so it does not depend on the backend reporting TDO.
note: IR length 8 selects the 8-bit Table 958 opcode set.
! BYPASS DR length not measured: expected 2 WJTAGRaw TDO capture(s) but parsed 1
RM Table 984 names SIP_S32E277n8n9_BGA27_831F01D.bsdl for IDCODE 0x0831F01D.
```

The BYPASS warning is the raw-TDO limitation showing through: the IDCODE and IR length come from the
banner and are trustworthy, the BYPASS measurement needs a capture and is not available. `tap info`
reports what it could and could not get rather than failing outright.

`tap ir` and `tap dr` need `WJTAGRaw` to report TDO, which it does not do reliably on this probe, so
they fail with an explanation. They do auto-switch to the commander backend and say so.

---

## `iface` — interface selection

### `iface show` — detect the live protocol

Never switches. Probing SWD would mean *sending* the switch sequence, and that is a one-way door.

```bat
s32edbg iface show
```

```
interface: jtag (proven)
  - IDCODE 0x0831F01D matches S32E277/8/9 (27x27) (RM Table 961). The JTAGC TAP
    only answers over JTAG, so the port is in JTAG mode.
  - no DPIDR: DP address 0x0 cannot be read over JTAG: ADIv6 Figure A1-2 ...
```

The discriminator is the **JTAGC IDCODE**, not DPIDR. The system JTAG controller sits in parallel
with the SWJ-DP on the same pins (RM §135.5.1) and is reachable *only* over JTAG, so a matching
IDCODE proves the port is in JTAG mode. A DPIDR read alone would not — the DAP answers over either
protocol. Confidence is reported as `proven`, `inferred` or `none`, and the command is willing to
answer "unknown".

### `iface jtag` — select JTAG

```bat
s32edbg iface jtag
```

The reset default, and always safe. Over JTAG it reports `CTRL/STAT` rather than DPIDR, for the
reason in *What silicon settled*: DP address `0x0` is not readable through DPACC there.

### `iface swd` — switch to SWD

```bat
s32edbg iface swd --i-understand-swd-is-one-way
```

Without the flag it refuses with exit 3 and the citation. RM §135.5.1: *"Switching back from SWD
mode to JTAG mode requires POR."* In SWD, TDI and TDO become GPIO (RM Table 980).

The command prints the ADIv6 B5.2/B5.3 sequence it is asking the probe to perform before doing it.
`--no-dormant` selects the deprecated direct sequence instead; Arm deprecates it on parts that
implement the dormant state, and RM §135.14.1.1 says this one does.

After any switch the DP register state is UNKNOWN per ADIv6 B5.2, so the tool re-runs the power-up
handshake and clears sticky errors rather than reading anything first.

The switch runs on probe firmware. There is no host-side fallback: no exported DLL symbol can drive
those sequences. If the probe declines, the command says so rather than pretending.

---

## `report` — everything in one artifact

```bat
s32edbg --json report.json report --apply-gates
```

Collects probe identity, TAP info, DAP info, gate state, the AP map, and both ROM trees in a single
run. `--apply-gates` opens the debug gate first; without it the report is read-only.

**Sections that fail are recorded as errors rather than aborting the report.** A diagnostic artifact
that refuses to exist because one probe failed is worthless exactly when it is needed. Typical
output on this target includes a `tap` section carrying an error (the dll backend cannot measure IR
length) alongside a complete `romtables` section with 12 tables and 74 components.

This is the right thing to attach to a bug report.

---

## IDE integration — machine mode, symbols, and the VS Code extension

Everything above speaks to a human at a terminal. `vscode/` in this repo is a VS Code extension
("S32E Debug") that speaks to the same tool from its own Activity Bar container — no Run-and-Debug
tab, no DAP, no GDB stub. It rides on three additions to the CLI.

### `debug --machine` — the NDJSON session protocol

```bat
s32edbg --speed 16000 debug --machine        (stdin must be a pipe)
```

One persistent session (the D-2 rule unchanged), driven as a line protocol: **stdin** takes the
same command lines the interactive REPL takes; **stdout** carries only JSON, one object per line;
**stderr** carries everything a human would have seen. After a `ready` event, every non-empty
input line gets exactly one result envelope, in order:

```
{"event":"ready","version":"0.1.0","protocol":1,"cores":["rtu0.0",...],"m33":["smu","ce0","ce1"],...}
{"seq":1,"ok":true,"cmd":"core halt","result":{"halt":[{"core":"rtu0.0",...}]}}
{"seq":2,"ok":false,"cmd":"bp set","error":{"kind":"usage","message":"address 12345 must be 0x-prefixed hex, ..."}}
{"seq":3,"event":"progress","message":"parking rtu0.0 on a `b .` loop ..."}   (long ops, before their envelope)
{"event":"bye","restore_notes":["cleared breakpoint 0 on rtu0.0","resumed rtu0.0"],"kept":false}
```

`error.kind` mirrors the exit-code contract (`usage`, `tool`, `refused`, `aborted`, plus
`unknown-command` and `internal`); the payloads inside `result` are exactly the `--json` payloads,
captured instead of printed. `quit` (or EOF) runs the same restore-on-close the REPL runs — a
frontend must always end the process this way, never kill it, or comparators stay armed and a
stale catch latch breaks the application's own core release (Finding 7).

`poll` is the frontend's 1 Hz heartbeat: one cheap sweep of every core's run state
(EDSCR/EDPRSR per R52, DHCSR per M33, halt locations for halted cores) with no instruction
injection and no PC reads on running cores.

### The symbols layer — `sym`, `stack`, `disasm`, `stepline`, `stepout`, `runto`

The base tool treats every address as a bare number, by design. The IDE needs files, lines and
function names, so `pip install -e .[ide]` adds two pure-Python dependencies (pyelftools,
capstone) and unlocks:

```
sym load <elf>          parse the ELF's DWARF once, cache it on the session
sym addr2line 0x...     address -> function + file:line          sym line2addr <file> <line>
stack [--max-frames N]  unwind the halted focused core: DWARF CFI, then the GCC frame-pointer
                        chain, then LR once for a leaf -- each frame says which (`via`), and the
                        walk stops with a stated reason instead of inventing frames
disasm [--addr 0x...]   capstone around the PC (or an address); bytes from the ELF when it covers
                        the address, target memory otherwise; T32/A32 from the mapping symbols,
                        else DSPSR.T
stepline [--over]       one SOURCE line: instruction-step until the line-table row changes
stepout                 unwind one frame, temp hardware breakpoint at the return address, resume,
                        wait, clear -- on timeout the core is LEFT RUNNING and the temp slot freed
runto 0x...             the run-to-cursor primitive, same temp-breakpoint mechanics
```

`resume`/`continue` steps off the comparator it was halted on. A breakpoint halts the PE
*before* the matched instruction executes, so DLR still points at it and a bare restart
re-matches the same comparator having made no progress — there is no skip-one bit on the
R52. Two breakpoints on consecutive lines otherwise behave as "the first is hit, continue
does nothing, the second is never reached", which reads as a lost breakpoint rather than a
working one. Verified on an S32E278 at `cdd_btn9970.c:437`/`:443`: before, continue re-halted
at `0x323DC472` indefinitely; after, it lands on `0x323DC4CA`. Pass
`step_off_breakpoint=False` at the API level for a literal restart (what the halt-group
paths want, since those cores were stopped by the cross-trigger, not by their own
comparator).

All of these work on the R52s and the M33s (`m33 step --over` uses a capstone call-detect plus a
temporary FPB comparator — the M33 twin of `runcontrol.step_over`). The unwinder and stepline are
best-effort by nature: they refuse to invent frames, and `stepline` stops with a reason
(`line-change`, `loop`, `max-steps`, `no-line-info`, `no-progress`).

`loop` means the PC came back to an address it had already visited without ever leaving the
line — the line's own code cycles, so a source step can never finish. The case that motivated
it is the AUTOSAR idle task: `-Og` folds `Os_CoreIdle` → `Os_Hal_CoreIdle` → `Os_Hal_CoreNop`
→ `Os_Hal_Nop()` into `nop; b.n .-2`, two instructions on one source line. `no-progress` does
not catch that (it compares against the *previous* PC only, and in a two-instruction cycle the
PC always differs), so before the guard this ran the full 128-step budget — the better part of
a minute on hardware — and reported `max-steps`. It now stops on the second instruction.

The guard bails at the **first** revisited address, so a data-dependent loop written entirely
on one line (`for (i = 0; i < 10; i++) sum += i;`) reports `loop` rather than running to
completion — the PC alone cannot distinguish the two. Pass `--no-loop-guard` for the old
exhaustive behaviour.

### The extension

```bat
cd vscode && npm install && npm run build && npx vsce package --no-dependencies
code --install-extension s32e-debug-0.1.0.vsix
```

Point `s32e.pythonPath` at the interpreter that has `s32edbg[ide]` installed. The container gives
you: manual **Connect** (session + `gates apply` + discovery), a Cores view over all 7 cores, an
always-visible **focused core** (status bar `⦿ rtu0.0`, click to switch — gutter breakpoints,
stepping and the Registers/Stack views act on it), native gutter breakpoints armed on the focused
core with retarget/duplicate in the Breakpoints view (the view, not the gutter dot, is the truth
about armed/slot/failed — VS Code cannot render verified states without a DAP), watchpoints,
source + instruction stepping with F5/F10/F11/Shift+F11, a per-core read-only disassembly document
that follows the PC, call stack + raw SP window, register editing, Load-ELF-to-RAM with streamed
progress (breakpoints re-armed after — the SoC reset wiped the comparators), catch-at-first-
instruction with automatic conversion when it fires, a memory viewer, PC profiling, and the
doctor/probe/report diagnostics (available only while disconnected — they open their own probe
connection, which is a D-2 reset pulse).

Disconnect (and window close) runs the session restore; if the backend dies instead, the extension
says so and offers reconnect — it never auto-reconnect-loops into the D-2 defect.

---

## Validation

**`docs/test-plan.md` is the authority** — it carries a per-command evidence table saying what has
been proven on silicon, what has only run against test doubles, and what has never been executed.

```
pytest                              761 tests, no probe and no SEGGER install needed
pytest -m hardware -v -x            21 bench gates, in dependency order
.\tools\bench-acceptance.ps1        the same sequence, with per-step JSON and wire-level logs
.\tools\flash-state-matrix.ps1      debugging proven in every flash state (nothing / BL only /
                                    BL+app / BL+app+flag / app only) -- see the test plan
```

The matrix runs all five states in order by default; each state reprograms the flash with the
tool's own commands, prompts for a power cycle, then runs the full battery (attach/connect,
gates, discovery, a run-control session, `flash jedec`, RAM `load --verify`):

```bat
.\tools\flash-state-matrix.ps1 -Yes
.\tools\flash-state-matrix.ps1 -Yes -States S1,S2               :: a prefix subset
.\tools\flash-state-matrix.ps1 -BlobPath C:\blobs\bl.bin -OutDir C:\temp\matrix
```

Run-control was built in five phases, each ending at a hardware checkpoint before the next began.
All five passed, plus the M33:

| | Evidence on silicon |
|---|---|
| discovery, identity | `MIDR 0x411FD133`, `EDDFR` gives 8 BP / 8 WP, cores named from `DEVAFF0` |
| halt / resume | `EDSCR` and `EDPRSR` independently agree |
| registers, PC | encoding calibrated to T32; `DLR` via `MRC p15,3,r0,c4,c5,1` |
| breakpoints | fired, and the PC equalled the address it was set at |
| step, step-over, cross-trigger | PC advances by exactly one instruction |
| M33 (`smu`) | halt, registers, step, BPU breakpoint hit at the target address |

Still unproven: `tap ir`/`tap dr` and `bscan length` (blocked on raw-TDO capability), `iface swd`
(deliberately unrun — it costs a power cycle), `attach` on genuinely blank flash, DWT watchpoints on
an M33, and run-control on the two CE cores, which are not executing at all.

---

## What silicon settled, against or beyond the manuals

**The JTAG IR width is 8 *or* 16, and the preamble is what changes it.** This read as an RM
self-contradiction for a long time — Table 958 gives 8-bit opcodes, Table 959 gives 16-bit ones, for
the same 27×27 parts. They describe two different chains. `TLM_TAP_SELECT_TDR` links `JTAGC_AE` in,
taking `TotalIRLen` from 8 to 16, and it persists. At 8 bits `SAMPLE/PRELOAD` silently falls through
to `IDCODE`, so the BSR is never selected and the "boundary scan" is really reading the device ID.

The standing objection — *"but the board routes no AE JTAG port"* — is true and irrelevant: that TAP
is internal to the SIP and linked by the TLM, never brought to a connector.

**DP address `0x0` is unreadable over JTAG.** ADIv6 Figure A1-2 makes DPACC `0b00` Reserved, and
DPIDR, DPIDR1 *and* BASEPTR0/1 all live there, selected by DPBANKSEL. Reading anyway does not fault;
it returned `0x4BA07477` against a real DPIDR of `0x4C013477`, close enough to pass a glance, and
the bad BASEPTR then sent the AP-space ROM walk to a garbage address. The tool now reports them as
absent, and takes DPIDR from J-Link's connect banner.

**Four cores answer, not eight — and that is lockstep, set at boot.** RM Table 965 lists four R52
cores per RTU with debug frames at `0041/0051/0061/0071`; cores 2 and 3 fault on every access,
including `EDPRCR` in the debug power domain, so a debugger cannot wake them. RM 9.2.1.2,
`RTU.GPR CFG_CORE` bit 0 `SPLT_LCK`, explains it: in Lockstep mode *"Core0 and Core2 operate as a
lockstep pair… Core1 and Core3 operate as a lockstep pair"*, and **it resets to 0 = Lockstep**. To
get eight independently debuggable cores you need `SPLT_LCK = 1`, and RM 6.4.1 says the choice is
made *"before the Cortex-R52 core cluster is released from reset"* and cannot be changed
dynamically. That is a boot-configuration change, not something a debugger can do. Discovery already
probes all eight addresses, so a board booted split-lock needs no code change.

The same register's bit 2, `THUMB`, resets to 1 — which independently explains the next entry.

**In Debug state this core executes T32, and `EDITR` wants it halfword-swapped.** The A32 encoding
of `MCR p14,0,r0,c0,c5,0` sets `EDSCR.ERR`; the same instruction as a 32-bit T32 value with the
*first* halfword in the low 16 bits executes and transfers. Writing the halfwords the other way
round does not fault — it executes something else. The tool round-trips a known pattern through a
scratch register and calibrates T32 versus A32 per core rather than hardcoding either.

**The PC is a coprocessor register, not a special register.** Cortex-R52 TRM §4.2.31: the AArch32
debug registers are accessed with `coproc==0b1111`, so `DLR` (the PC in Halting Debug state) is
`MRC p15,3,Rt,c4,c5,1` and `DSPSR` is the same with `Op2=0`. Every `MRS` form fails, including a
64-point sweep of the banked-register space — that space contains the banked SP and LR, and never
contained the PC. `EDPCSR` is a profiling sample and reads `0xFFFFFFFF` while halted; it is useful
only on a *running* core, which is what `core sample` uses it for.

**On a hot breakpoint, "still running" is never observable.** Resume a core onto a breakpoint in its
idle loop and it re-enters Debug state faster than any poll can see it running. A resume that waits
for "not halted" therefore reports a working breakpoint as a failure — which cost two debugging
sessions here, once on the R52 and once on the M33. The discriminator is whether the core
*executed*: a changed halt reason on the R52, `DHCSR.S_RETIRE_ST` on the M33.

**DPv3 vs the DLL's ceiling.** `JLink_x64.dll` contains the literal string *"Detected DPv3 or later
but J-Link only supports up to DPv2. Debugging not possible"*, yet this DAP is DPv3 and V9.62
enumerates its AP map. `connect` surfaces any such banner message verbatim.

**AP-read pipelining.** ADIv6 JTAG-DP returns the *previous* result from an APACC read. Whether
`JLINK_CORESIGHT_ReadAPDPReg` hides that is documented nowhere on this machine, so at connect the
tool reads the MDM-AP IDR both ways and keeps whichever produces RM Table 957's documented
`0x001DE040`.

**XRDC can deny a debugger what it grants a core.** A `0x76xxxxxx` RTU peripheral read that faults
for the debugger while identical writes succeed from the core is *not* a bug — that is XRDC
debugger-domain policy in the post-reset default state. It is reported as access-denied, not as a
transport failure.

**BSDL.** RM Table 984 names `SIP_S32E277n8n9_BGA27_831F01D.bsdl` but does not say where to get it,
and RM §136.3.4 puts the BSR length and bit ordering only in that file. So `bscan sample` emits an
indexed raw bit vector with no pin names. Supply the BSDL and pin mapping becomes possible.

---

## Documentation note

The `UM08001_JLink.pdf` that ships with the J-Link package is a **36-page "Getting Started"
booklet** — it documents no Commander commands, no script API and no DLL API, and `UM08036`
forward-references a `UM08002` that is not installed. Every J-Link syntax fact this tool relies on
was therefore read out of the shipped binaries (help tables, embedded C prototypes, the exec-command
string table) and is treated as an implementation fact validated at the bench — not as published
API. Silicon facts cite the S32E2 Reference Manual Rev. 5, ADIv6.0 (IHI0074B), CoreSight v3.0
(IHI0029G) and the CoreSight SoC-600 TRM; board facts cite the `SOM_3xx_Gen1_G0 BR1` schematic,
sheet 10.

## Layout

Three layers throughout: `dap/adiv6.py` supplies the mechanism and knows nothing about policy,
`memaccess`/`runcontrol`/`m33` own the register semantics and the guards, and `cli.py` talks only to
those. The CLI never calls `Dap.mem_*` directly.

```
s32edbg/
  cli.py              click command groups
  machine.py          the `debug --machine` NDJSON protocol (payload sink, stdout swap)
  elf.py              minimal ELF32 reader for `load`/`flash` (stdlib struct; parse, flatten)
  symbols/            the [ide]-extra layer: dwarf (SymbolIndex: lines, functions, mapping
                      symbols, lazy CFI), unwind (CFI -> FP chain -> LR), disasm (capstone)
  transport/          base ABC, ctypes DLL, Commander subprocess, SVF/JTAGLoad, install discovery
  dap/                adiv6 (SELECT, power-up, MEM-AP), apmap, romtable, gates,
                      memaccess (bus policy), iface (protocol detection),
                      runcontrol (Cortex-R52), m33 (Cortex-M33),
                      loader (the RAM-load flow: bring-up, SRAMC, segment writer),
                      flasher (external QSPI NOR: controller bring-up, M33 applet ops)
  jtag/               tapseq (pure TMS/TDI vectors), tap, bscan
  data/               RM / ADIv6 / SoC-600 / TRM tables, each citing its source:
                      known_ids, expected_apmap, ladder1, bscan, swj,
                      coresight (shared component offsets), r52debug, m33debug,
                      a32 (just enough decode to step over a call),
                      rtuboot (the verbatim MC_ME/RGM/SRAMC bring-up tables for `load`),
                      qspiflash (the verbatim QuadSPI/LUT/applet tables for `flash`)
                      + the vendored, SHA-pinned M33 flash applet binary
  report/             rich + JSON rendering
docs/
  system-debug-interface.md   how the interface is built, the switching sequences, the preamble
  test-plan.md                evidence table, bench procedure, open defects, handover
  virgin-board.md             getting a foothold on blank flash
  app-boot-contract.md        what must be in flash (app flag, layout, order) for the
                              M33 bootloader to boot the app; probe programming procedure
  handover-virgin-board-flashing.md
                              OPEN DEFECT: a fully erased board cannot be reflashed by
                              this tool (the SMU that runs the applet is powered down);
                              evidence, routes tried, and what to fix
tools/
  bench-acceptance.ps1        runs the hardware sequence, keeps the artifacts
  flash-state-matrix.ps1      the flash-state × debugging matrix (S1..S5)
  bench-lib.ps1               Invoke-Step/Hex, shared by both bench scripts
vscode/               the "S32E Debug" VS Code extension (TypeScript; own Activity Bar
                      container; spawns one `debug --machine` session)
tests/                761 hardware-free tests (fixtures/ holds the committed symbol-layer
                      ELF), plus 21 bench gates behind `-m hardware`
```

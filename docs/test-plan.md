# s32edbg test plan and handover

This document is written for someone picking the tool up cold at the bench. It
says what has been proven, what has not, how to run each tier of test, and — for
every hardware step — what a failure actually *means*, because on this target
most failures are board-level rather than tool-level.

The single most important thing in here is the evidence table below. Read it
first.

---

## 1. Evidence: what is proven, and how

Three sources of confidence, and they are not interchangeable. An offline test
proves the *logic* is right against a model of the hardware. A hardware run
proves the model was right. Neither substitutes for the other.

| command | offline coverage | hardware evidence | status |
|---|---|---|---|
| `installs` | — | ran; both `JLink.exe` and `JTAGLoad.exe` found | proven |
| `probe` | — | ran; VTREF ≈ 3300 mV | proven |
| `doctor` | `test_diagnose.py` | ran; a false positive on current swing was found and fixed | proven |
| `connect` (commander) | `test_cli.py` | DPIDR `0x4C013477` (DPv3 / ADIv6) from the connect banner | proven |
| `connect` (dll) | `test_cli.py`, `test_adiv6.py` | reports DPIDR unavailable with its citation; `CTRL/STAT 0xF0000000` | proven (D-1 fixed) |
| `iface show` | `test_cli.py` (help only) | `jtag (proven)` from the JTAGC IDCODE | proven |
| `iface jtag` | — | never run | **never run** |
| `iface swd` | `test_swj.py` (sequences), `test_cli.py` (refusal) | never run — one-way door | **never run, by choice** |
| `gates status` | `test_sda_policy.py`, `test_cli.py` | ran | proven |
| `gates apply` | `test_cli.py` | gate opened; all four registers read back exactly | proven |
| `attach` | — | held; `functional_reset_asserted=yes`, `destructive=no`, gate open | proven on a *running* board |
| `release` | `test_cli.py` | `functional_reset_asserted` yes → no, on attempt 1 | proven |
| `apmap` | `test_apmap.py`, `test_cli.py` | 0 missing / 0 unexpected / 0 mismatched vs RM Table 957 | proven |
| `romdump` | `test_romtable.py`, `test_cli.py` | 12 tables, 74 components, incl. RTU1 and all 7 peripheral-group ROMs | proven |
| `mem info` | `test_memaccess.py`, `test_cli.py` | APB-AP IDR `0x24770006`, BASE `0x01BFE000` | proven |
| `mem read` | `test_memaccess.py`, `test_cli.py` | `0x00410FF0` → `0D 90 05 B1`, the CoreSight preamble | proven |
| `mem dump` | `test_memaccess.py`, `test_cli.py` | 260-word block crossed a 1 KB boundary and stayed correct | proven |
| `mem write` | `test_memaccess.py`, `test_cli.py` | CLAIM tag set and cleared, observed through CLAIMCLR | proven |
| `tap info` | `test_cli.py` | ran (via J-Link's connect banner, not a raw shift) | proven |
| `tap ir` / `tap dr` | `test_commander_raw.py` | `calibrate_tdo_order()` refuses on this silicon | **blocked** |
| `bscan preamble --run-svf` | `test_bscan.py`, `test_svf.py` | "Download successful. SVF ok" | proven |
| `bscan length` | `test_bscan.py` | blocked on the same raw-TDO capability | **blocked** |
| `bscan sample` | `test_bscan.py` | liveness proven — one bit of 64 moved, 63 held | proven |
| `report` | `test_cli.py` | all 8 sections; both ROM trees walk (9 APs; 12 tables / 74 components) | proven |
| `debug` (session) | `test_session.py` | one connection, 10 commands, restore-on-close resumed the core | proven |
| `core reset-halt` | `test_doc_alignment.py` | reset catch at PC `0x32100020` (hold flow; the mask flow failed — see below) | proven |
| `core resume --sync` | `test_doc_alignment.py` | one pulse released rtu0.0+rtu0.1, SDR on both, gates closed after | proven |
| `core reg pc` / `dspsr` (write) | `test_doc_alignment.py` | round-tripped on a halted rtu0.1 | proven |
| `m33 wp` | `test_doc_alignment.py` | programs and read-back verifies (RO ID masked); never seen to *fire* — SMU makes no stores near SP | partial |
| `m33 reset-halt` | `test_doc_alignment.py` | **SYSRESETREQ takes no effect on this silicon** — no reset, core keeps running, command fails cleanly | **blocked** |
| `load` | `test_loader.py`, `test_session.py`, `test_elf.py` | 3.6 MiB in 33.6 s at 16 MHz, fast path proven, app boots — see "The `load` flow" | proven (2026-08-04) |
| `flash` (all subcommands) | `test_flash.py`, `test_cli.py`, `test_session.py` | never run on silicon — see "The `flash` flow" for the u-f1..u-f9 uncertainties and the bench procedure | **offline only** |

**Three facts settled on silicon**, recorded here because they will otherwise be
re-discovered the hard way:

1. **The IR width is 8 *or* 16, and the preamble is what changes it.** This was
   read as an RM self-contradiction — Table 958 gives 8-bit opcodes, Table 959
   gives 16-bit ones, for the same 27×27 parts. They describe two different
   chains. The RM Table 984 preamble's last step writes
   `TLM_TAP_SELECT_TDR = 0x80000011`, which links `JTAGC_AE` into the scan
   chain; `doctor` reports `TotalIRLen 8` before and `16` after, and it
   persists. So Table 958 describes the cold chain and Table 959 the
   post-preamble one, and boundary scan needs 16 because that is the chain its
   instructions are actually issued on. At 8 bits `SAMPLE/PRELOAD` falls through
   to `IDCODE` — the BSR is never selected, and a "boundary scan" done that way
   is really reading the device ID.

   The long-standing objection "but the board routes no AE JTAG port" is true
   and irrelevant: the AE TAP is internal to the SIP and linked by the TLM,
   never brought to a connector. See `s32edbg/data/bscan.py`.

2. **That chain change takes the DAP away** until a power cycle. It is
   ordering rule 3 in §3, and the most expensive trap in the tool.

3. **DP address `0x0` cannot be read over JTAG at all.** ADIv6 Figure A1-2 makes
   JTAG-DP DPACC address `0b00` *Reserved*, and DPIDR, DPIDR1 **and BASEPTR0/1**
   all live there, selected by DPBANKSEL. Reading anyway does not fault — it
   returned `0x4BA07477` against a real DPIDR of `0x4C013477`, close enough to
   pass a glance, and the same bad read corrupted BASEPTR and with it the
   AP-space ROM walk. That was defect **D-1**, now fixed: the DAP reports
   identity as absent with the citation, and `CTRL/STAT` at DP `0x4` carries the
   liveness answer. Over JTAG, take DPIDR from J-Link's connect banner.

---

## 2. Bench preconditions

These are board facts, not tool settings. Every one of them has produced a
"dead TAP" that was not a tool problem.

- **Power switches.** The PMIC and SBC enable switches must be ON. A healthy
  3.3 V VTREF proves only that one regulator is up; the 0.8 V core rail comes
  from a different one, and an unpowered core is indistinguishable from a dead
  TAP at the probe (RM Table 295, Table 301).
- **`MCU_RESET_b` is tied to `SBC-PMIC_RSTb`** through R439 (0 Ω, with the S3
  jumper not fitted). Every J-Link connect therefore resets the SBC and the
  PMIC. This is why repeated connects look like the board is rebooting — it is.
- **`POR_B` is AND-gated with `SBC-PMIC_PGOOD`** through U20. A debugger cannot
  override power-on reset on this board.
- **`WDGDIS` is on J9 pin 28 only.** It does not exist on J3. If the SBC or PMIC
  watchdog is resetting the board, J3 gives you no way to stop it.
- **JCOMP is active-high with a weak internal pull-down** (RM 60.5.4.1, 136.5.5).
  While it is low, both the JTAGC and the SWJ-DP stay in reset.

---

## 3. Ordering rules

Not stylistic. Each one has a consequence.

1. **`gates apply` before anything touching APBIC2.** RM 135.11: the debug
   clocks are off by default, and "trying to access debug components while the
   clocks are not enabled might hang the debug APBIC bus." The bits are sticky
   across functional resets and lost only on power loss — so this is once per
   *power cycle*, not once per command.
2. **Anything needing the application running goes before `attach`.** `attach
   --hold` asserts functional reset and stops the cores.
3. **Boundary scan goes after *all* DAP work, and before `iface swd`.**
   Discovered on 2026-08-01, and it is the rule most likely to waste an
   afternoon. The RM Table 984 preamble's last step writes
   `TLM_TAP_SELECT_TDR = 0x80000011`, which links `JTAGC_AE` into the scan
   chain. Measured:

   | | `doctor` reports | `CTRL/STAT` |
   |---|---|---|
   | before the preamble | `TotalIRLen 8` | `0xF0000000` |
   | after the preamble | `TotalIRLen 16` | `0x00000001` |

   The change **persists**. J-Link's DAP addressing is set up for the chain it
   scanned at connect, so once the chain grows a TAP it cannot identify, every
   DP/AP path stops working — `connect`, `gates`, `apmap`, `romdump`, `mem` and
   `attach` all fail, on **both** backends. Nothing is damaged; a power cycle
   restores the 8-bit chain.

   `bscan preamble` and `bscan sample` now print this warning themselves.

4. **`iface swd` is last in any session.** RM 135.5.1: "Switching back from SWD
   mode to JTAG mode requires POR." In SWD, TDI and TDO become GPIO (RM Table
   980).
5. **`mem --ap axi` only works when the system is *not* held.** `assert_axi_safe`
   refuses AXI-AP traffic while functional reset is asserted, because RM Table
   957 says such an access can leave peripherals unreachable "until you issue a
   destructive reset."

   Worth stating plainly: this means the tool's own safety policy makes the AXI
   write path reachable only in the state where it is most dangerous. That is the
   right trade — the refused state is the unrecoverable one — but it means AXI
   writes are never routine, and the acceptance suite deliberately does not
   perform one.
6. **Never open a J-Link session without a device name.** Measured twice on
   2026-08-01, and it costs a power cycle every time. A session that skipped
   `select_device("CORTEX-R52")` failed its own power-up, and then *every*
   subsequent session failed identically — new process, full connect, both the
   `dll` and `commander` backends — with `CTRL/STAT=0x00000000` and `doctor`
   reporting `chain: no response`. Only a power cycle cleared it.

   The state is held in the probe, not the target, which is why retrying the
   tool cannot recover it and why the symptom outlives the process that caused
   it. `_bring_up_link` now refuses an empty `--device` rather than allowing it.

   Worth recording how this was misdiagnosed, because the wrong answer was very
   plausible. The first occurrence coincided with both RTU0 cores being halted,
   `ITarget` was swinging 38↔51 mA, and `doctor` reads that swing as "the device
   is cycling through resets" — so it was written up as risk **R9**, a watchdog
   reset, and this rule originally said "never halt both cores at once". Two
   pieces of evidence killed that: the bench's SBC is disabled at the switch and
   no watchdog is active, and `rtu0.0` alone was later held halted for a full
   60 s — 10 511 EDSCR polls, `SDASYSRSTS` reading `0x00000000` throughout — with
   no reset and no release. Halting cores was never the cause. The `ITarget`
   swing was the red herring `doctor` itself warns about in that same message:
   "a running application varies its own current anyway."

---

## 4. Tier 0 — the offline suite

```bat
pytest -q
```

**684 tests, no probe required, no SEGGER install required.** The CLI tests
patch transport discovery precisely so they run on a machine that has never seen
a J-Link.

What it covers: ROM-table decoding, AP discovery and the RM Table 957 diff,
ADIv6 DP/AP composition, MEM-AP byte lanes and the 1 KB auto-increment rule,
the SVF emitter, the SWJ switching sequences, the diagnosis ranking, the
Commander output parsers, the whole CLI surface including the exit-code
contract, the `load` flow against the SyntheticTarget's MC_ME/SRAMC/EDBGREQ
model, and the `flash` flow against its QSPI-controller + NOR + M33-applet
model (see "The `flash` flow" for what that model enforces).

What it cannot cover: anything about whether the *model* is right. The fakes
answer the way the RM says silicon should. Every case where silicon disagreed —
the IR width, the reserved DPIDR, the raw-TDO capability — was found at the
bench, and no amount of offline testing would have found them.

---

## 5. Tier 1 — the CLI contract

Part of Tier 0 (`tests/test_cli.py`), called out separately because it is the
layer that carries the safety policy. The exit-code contract:

| exit | meaning | raised by |
|---|---|---|
| 0 | success | — |
| 1 | usage error | `click.ClickException` — bad `--ap`, misaligned dump, missing `--bsr-length`, malformed address/value/index (addresses require the `0x` prefix; bare numbers are rejected, never guessed) |
| 2 | tool error | any `S32EError` — no DAP, power-up refused, transport failure. Also any *other* escaped exception: reported as `internal error`, never a traceback (`S32EDBG_TRACE=1` re-raises) |
| 3 | **refused by safety policy** | `UnsafeOperation` |
| 130 | aborted at a prompt | `click.Abort` |

Inside a `debug` session none of these end the session: `ClickException`,
`S32EError`, `UnsafeOperation` and even an escaped `Exception` are printed and
the REPL continues (2026-08-04 fix: `bp set 32169874` — a bare number — used to
raise an uncaught `ValueError` that unwound the REPL, killed the process, and
cost a reconnect-with-reset-pulse, defect D-2; the all-digit variant was worse,
silently arming a comparator at the decimal reading of a hex address).

The refusals that must stay refused:

| command | outcome | authority |
|---|---|---|
| `iface swd` without `--i-understand-swd-is-one-way` | exit 3 | RM 135.5.1 — return needs POR |
| `mem write --ap sda` (even with `--yes`) | exit 3 | RM 135.13.4 — the HSE C/R is one-shot |
| any AXI-AP access while functional reset is asserted | exit 3 | RM Table 957 — needs a destructive reset to undo |
| `bscan` EXTEST / HIGHZ / CLAMP | not implemented | RM 136.4.4.3/.4/.5 — each asserts internal system reset |

`--yes` skips the *prompt*, never the *policy*. There is a test for exactly that.

---

## 6. Tier 2 — hardware acceptance

Two ways to run it.

**Scripted**, which is what a handover should use:

```bat
.\tools\bench-acceptance.ps1
```

It runs the steps in dependency order, writes a `--json` artifact and a
wire-level `--log` per step into a timestamped folder, stops at the first
failure, and prompts before each step that changes target state. Use
`-SkipStateChanging` for a read-only first pass, `-Yes` to run unattended.

**As pytest**, for the assertions rather than the artifacts:

```bat
pytest -m hardware -v -x
```

21 tests. File order is dependency order; `-x` matters.

### The steps, and what a failure means

| # | step | pass criterion | if it fails |
|---|---|---|---|
| 0 | `probe` | VTREF > 2000 mV, serial present | Cable, or VTREF not connected. Says nothing about the core rail. |
| 1 | `doctor` | IDCODE `0x0831F01D`, no blockers | Read the ranked findings — it exists to answer this question. |
| 2 | `connect` on **both** backends | identical DPIDR `0x4C013477` | Disagreement means a hand-written ctypes signature in `transport/dll.py` is wrong. There is no SDK header to check against, so this cross-check is the only evidence they are right. |
| 3 | `gates apply` | `enables_set` true, exact read-back | Zeros mean MDM-AP **writes** are being rejected — that points at the HSE gate, not at this sequence. Check `gates status` for `APPDBGEN`. |
| 4 | `apmap` | diff empty against RM Table 957 | A missing AP usually means the gate did not open. An unexpected one is genuinely interesting — record it. |
| 5 | `romdump` | ≥ 12 tables, 0 errors | An empty APB tree means the gate is shut. Errors on individual components are recorded as error nodes and are not fatal (see below). |
| 6 | `mem read` of `+0xFF0` | CIDR `0x0D 0x?? 0x05 0xB1` | A wrong preamble means the MEM-AP transfer is landing somewhere else. Cross-check the same address through `romdump`. |
| 7 | block read across a 1 KB boundary | block == word-at-a-time reads | **The failure here is silent.** ADIv6 C2.6.4 only guarantees auto-increment inside a 1 KB window; without a TAR rewrite you get correct-looking values from the wrong addresses and no fault anywhere. |
| 8 | `attach` | held, `SYSFUNCREQ` set | See §9 on virgin boards. |
| 9 | `release` | `SYSFUNCREQ` clear, board runs | If the DAP is unreachable, a power cycle clears the hold unconditionally. |
| 9b | `load <app.elf> --verify` (with `-ElfPath`) | SRAMC witnesses read 0; every segment written and full-verified; rtu0.0 halted with PC = entry | See "The `load` flow" section below for the phase order and what each abort means. Resets the whole SoC by design. |
| 9c | `flash jedec` → read → erase/program/verify at `-FlashScratch` (default `0x300000`, never offset 0 first) | JEDEC `0xC23989C2`; applet halts at bkpt with rc 0; pattern byte-exact after power-loss-free round trip | See "The `flash` flow" section below — u-f1..u-f9 are the uncertainties this step exists to settle; failures become Findings 10+. |
| 10 | `bscan preamble --run-svf` | "Download successful. SVF ok" | A parser complaint usually means the 208-bit operand; the SVF path exists because the raw path cannot carry it. |
| 11 | `bscan sample --bsr-length 64` | two captures agree | Disagreement means the target is not quiescent, or the capture is not stable. |
| 12 | `report` | every section present | A section reporting an error is acceptable and by design — see below. |

**One deliberate non-failure worth understanding.** `romdump` and `report` record
per-component errors as error *nodes* and keep going. That is the whole point of
the `DapError`/`TransportError` split: while every failed AP access raised
`TransportError`, none of the `except DapError` recovery sites fired, and the
first unreadable component aborted the entire walk. On silicon that turned a
74-component dump into nothing. A `report` that refuses to exist because one
probe failed is worthless exactly when it is needed.

---

## 7. What has *not* been proven, and the experiments designed for it

Being explicit about this is the point of a handover.

### 7.1 `mem` — proven on 2026-08-01; here is the oracle design, because it matters

Four subcommands that until this run had a full offline model and zero hardware
runs. All four now pass. The oracles are worth keeping, because two of them
nearly gave a false pass:

**The first attempt at the 1 KB boundary test was vacuous.** Dumping across the
first boundary of the R52 debug frame returned all zeros on both sides, so the
block agreed with word-at-a-time reads whether or not TAR had been rewritten.
The fix is to end the block on data that is known and non-zero — run from
`+0xBF0` to the end of the 4 KB page, crossing `0xC00` and finishing on the
identification block. A wrapping reader would take those last words from the
`+0x800` window and return zeros. Measured: 260 words, and `0x00410FF0` still
read `0D 90 05 B1`.

**`mem write`'s read-back "mismatch" was the correct answer.** Writing `0x1` to
`CLAIMSET` reads back `0xFF`, because CLAIMSET reports the *number of implemented
claim tags*, not the value claimed. The claim actually landing is visible at
`CLAIMCLR`, which went `0x0 → 0x1 → 0x0`. The tool's "read-back differs" warning
fired both times and was right both times.

The two oracles, which need no external reference:

- **Read correctness**: CIDR0/2/3 are architecturally fixed at `0x0D`/`0x05`/`0xB1`
  for every CoreSight component (ADIv6), so `+0xFF0` has an answer that does not
  depend on this silicon.
- **The 1 KB boundary**: compare a straddling block read against word-at-a-time
  reads. Self-consistent, always available, and it targets exactly the failure
  mode that produces wrong data with no error.

**The write target is a CoreSight CLAIM tag** (`CLAIMSET` `+0xFA0`, `CLAIMCLR`
`+0xFA4`). These are architecturally software-use-only ownership tags with *no
hardware effect* — which is what makes them the one safe thing to write on a live
ECU. Check `LSR` `+0xFB4` first: if the software lock is engaged the write is
ignored, and that is a legitimate result, not a failure.

### 7.2 Boundary-scan liveness — resolved 2026-08-01

This was the longest-open question in the project, and the reason it stayed open
is that the obvious test is the wrong one. **Equality between captures proves
nothing**: a stuck path is perfectly repeatable. Three outcomes have to be told
apart, and only the middle one means the scan works:

| observation | conclusion |
|---|---|
| identical every time | ambiguous — live-and-quiet, or a stuck path |
| a few bits move, the rest hold | **live**: static pins plus one or two toggling |
| everything moves | noise, not a capture |

Measured on a board running an application:

```
0x0035025555A02006
0x0015025555A02006
```

One bit of 64 moved; the other 63 held. A quiescent board gave
`0x0000000000000006`. **The scan reaches real boundary cells.** No hands were
needed in the end — the running application supplied the change.

`s32edbg bscan sample --watch` still exists for a manual pin toggle. Without the
BSDL the bit indices are shift positions, not pin names, so the check is "some
bit changed", never "bit N changed".

Cost: one `JTAGLoad.exe` invocation **per byte**, because JTAGLoad stops at the
first TDO mismatch and that mismatch report is its only read channel. 64 bits is
8 runs; 512 bits is 64 runs and takes minutes.

### 7.3 BSR length — unresolved, with a designed experiment

RM 136.3.4 puts the length only in the BSDL file, which RM Table 984 names
(`SIP_S32E277n8n9_BGA27_831F01D.bsdl`) but does not supply. Measuring it needs a
TDO-reporting raw shift, which no backend here provides — so `--bsr-length` is
currently required.

An earlier attempt shifted an all-ones TDI fill expecting the tail to read
`0xFF`; it returned an unexplained `0x55` pattern. **Not resolved, not
explained.**

The designed replacement, not yet implemented: a **marker-delay measurement in a
single SVF file**. Shift a distinctive pattern in on scan 1, then read scan 2 and
find the byte offset at which the marker emerges — that offset is the register
length. It must be one file, because each `JTAGLoad` run begins with `STATE
RESET` and would discard the shifted marker. This needs a small addition to
`transport/svf.py` (a two-scan emitter); `jtag/tapseq.find_marker_delay()`
already exists for the decode.

### 7.4 Raw JTAG

`WJTAGRaw` drives arbitrary TMS/TDI vectors but does not report TDO consistently
on this probe/target pair, so `calibrate_tdo_order()` refuses. That is correct
behaviour and the four affected hardware tests are marked non-strict `xfail`
rather than deleted — on different probe firmware they may start working, and if
they do, that is news. Do not "fix" them by relaxing the assertions.

### 7.5 The virgin board — the actual requirement

`attach` has only ever been proven on a board running an application. The case it
was built for is a board with **blank flash**, and the mechanism is worth
recording because it explains why flashing a bootloader made the tool start
working:

> Blank flash → the BootROM fails → functional reset (RM 62.12) → FREC escalates
> → destructive reset (RM 60.5.2.4) → DEST0, which persists "until a POR event"
> (RM 60.5.6) — and RM Table 301 makes POR one of exactly two things that hold
> the JTAGC in reset. So the TAP is alive only in brief windows.

There is a second, independent cause with the same symptom: RM 135.15.1 says the
debug pads "operate in JTAG functionality out of reset but can be later
reassigned", so an application that remuxes them removes the TAP.

`attach` addresses both — retry until a window opens, then immediately hold
functional reset, which stops the reboot *and* stops software reaching the pad
configuration. RM Table 301 confirms a functional reset leaves the JTAGC and
SWJ-DP running, which is what makes it safe.

If a window never opens: let FREC reach 8 so the part lands in Fail-Safe serial
boot where it stops looping (RM 62.12), or assert `WDGDIS` on **J9 pin 28** to
stop the SBC and PMIC watchdogs. See `docs/virgin-board.md`.

### 7.6 `iface swd`

Never run, deliberately. The switch works through probe firmware
(`SetDormantModeHandling` then `TIF_Select`); there is no fallback, because the
sequences run on the probe and no exported DLL symbol can drive them from the
host. `s32edbg/data/swj.py` holds the ADIv6 B5.2/B5.3 sequences, verified
byte-for-byte against `JLink_x64.dll`, and they are used for the operator
description the command prints.

To run it: do it **last**, and be ready to power-cycle. ADIv6 B5.2 also requires
that DP register state be treated as UNKNOWN after any switch and programmed
absolutely — `reinitialise_after_protocol_switch()` does that.

---

## 8. Artifacts a handover should include

- The whole `bench-runs\<timestamp>\` folder: per-step `.json`, per-step `.log`
  (wire-level J-Link trace), `console.txt`, `summary.json`.
- `s32edbg report --json report.json` from the end of the session.
- The result table in §9, filled in.
- Anything surprising, in plain words. A `0x76xxxxxx` RTU peripheral read that
  faults for the debugger while identical writes succeed from the core is *not* a
  bug — that is XRDC debugger-domain policy in the post-reset default state, and
  it must be reported as access-denied rather than as a transport failure.

---

## 9. Defects

### D-1 — the dll backend reported the reserved register as DPIDR — **FIXED**

**Found by:** the first run of this test plan, 2026-08-01. **Fixed and verified
on silicon the same day.**

The `connect` command was fixed for the *commander* backend to take DPIDR from
J-Link's connect banner, because ADIv6 Figure A1-2 makes JTAG-DP DPACC address
`0b00` Reserved. **The dll path was not fixed.** It still calls
`Dap.read_dpidr()`, and over JTAG that reads the reserved address. Observed side
by side in one session:

| backend | DPIDR reported | correct? |
|---|---|---|
| commander | `0x4C013477`, DPv3 | yes |
| dll | `0x4BA07477`, version `reserved(0x7)` | **no** |

**It is not confined to DPIDR.** DPIDR, DPIDR1, BASEPTR0 and BASEPTR1 all live at
DP address `0x0`, selected by DPBANKSEL — so over JTAG *every one of them* reads
the Reserved address. The same session showed `DPIDR1 asize 119` and `BASEPTR
0x4BA074774BA07000`, both nonsense, both unwarned, exit code 0.

Measured blast radius:

| command | effect |
|---|---|
| `connect` (dll) | wrong DPIDR, wrong DPIDR1, wrong BASEPTR |
| `iface show` | quotes the same wrong DPIDR |
| `report` | **the AP-space ROM tree fails to walk** — it starts from the bad BASEPTR |
| `romdump --source apspace` | same failure |
| `apmap` | survives — it falls back to `APBIC1_ROM_BASE` *and* has a second discovery method |

Two things make this worse than cosmetic. The wrong value is *close* to the
right one, so it passes a glance. And `connect`'s whole job is to answer "is the
DAP there and what is it" — a confidently-wrong answer there is the failure mode
this tool exists to avoid.

`apmap` surviving is worth noting as design vindication: it is the one command
with two independent discovery methods, and that is exactly why it was the only
one unaffected.

The commander path already had the correct behaviour and the correct
explanation; the dll path did not.

**The fix.** Transports now record which protocol they selected
(`Transport.selected_tif`), and `Dap` consults it:

- `dp_address0_readable` is False over JTAG, True over SWD, and True when no
  interface was selected — an unknown interface is not a reason to refuse a read
  that may be valid.
- `read_dpidr()` / `read_dpidr1()` raise `DapError` carrying the ADIv6 citation.
- `read_baseptr()` returns **None**, not an exception, because every caller
  already falls back to `APBIC1_ROM_BASE` when BASEPTR is absent. An absent
  pointer degrades gracefully; a wrong one does not.
- `DapInfo.dpidr` is optional and carries an `unavailable` explanation.
  `CTRL/STAT` is at DP `0x4` and stays readable on both protocols, so the
  liveness answer survives.

The interface-awareness needed care: over SWD, DPACC `0b00` **is** DPIDR, so a
blanket refusal would break the protocol the switch exists to reach. There is a
test for that case specifically.

**Verified on hardware, 2026-08-01:**

| | before | after |
|---|---|---|
| `connect` (dll) | `DPIDR 0x4BA07477`, version `reserved(0x7)` | "DPIDR unavailable" + citation, `CTRL/STAT 0xF0000000` |
| `romdump --source apspace` | "0x4BA07000 is not a ROM table" | **1 table, 9 components, 0 errors** |
| `report` → `romtables.apspace` | error | **1 table, 9 components** |
| `iface show` | quoted `0x4BA07477` | "no DPIDR: …", still `jtag (proven)` |

Pinned by `tests/test_adiv6.py` (five tests, including the SWD case),
`tests/test_cli.py` (`connect` and `report`), and
`tests/test_hardware.py::test_reading_dpidr_through_dpacc_over_jtag_is_refused`.

`iface show` still reports `jtag (proven)`, because it never depended on DPIDR
for that — its discriminator is the JTAGC IDCODE, reachable only over JTAG. The
DPIDR line was always corroboration, and it now reads as an explained absence
rather than a wrong number.

### D-2 — a bench run degrades the target it is measuring

**Found by:** the same run.

Every step is a separate `s32edbg` invocation, so every step opens a J-Link
session. On this board `MCU_RESET_b` is tied to `SBC-PMIC_RSTb` through R439, so
**each session resets the SBC and PMIC**. Worse, a dll connect that cannot
identify the target pulses reset again on its own:

```
TotalIRLen = ?, IRPrint = 0x..000000000000000000000000
Failed to identify target. Resetting via Reset pin and trying again.
```

In the first run, steps 0–6 passed cleanly; then `gates status` failed with
`CTRL/STAT=0x00000000`; then roughly sixteen further connects (a 12-attempt
`attach` plus diagnostics) drove the TAP from "alive, IDCODE `0x0831F01D`,
TotalIRLen 8" to "no response" — a state that needs a power cycle.

The diagnostic value here is the ordering: the commander backend read
`CTRL/STAT = 0xF0000000` at the moment the dll backend read `0x00000000`, which
proves the DAP was alive and the dll bring-up was the problem, not the target.
`CORESIGHT_Configure` was tested as a hypothesis for the missing bring-up step
and ruled out — all four configurations still read zero.

**Operational rule, now printed by the script:** if steps start failing with
`CTRL/STAT=0x00000000`, power-cycle before retrying. Retrying makes it worse,
because each retry is another reset pulse.

---

## 10. Results

Both runs: 2026-08-01, S32E277 SOM, s32edbg 0.1.0, application flashed and
running. Offline suite the same day: **285 passed, 21 deselected**.

**Run 1** stopped at step 3 with `CTRL/STAT=0x00000000`, then ~16 further
connects drove the TAP to "no response" — see **D-2**. It confirmed steps 0–2
and found defects **D-1** and **D-2**.

**Run 2**, after a power cycle, with `gates apply` moved to the front:

| # | step | result | evidence |
|---|---|---|---|
| 0 | `installs`, `probe` | **pass** | V13.00, VTREF 3303–3306 mV, serial 603000869 |
| 1 | `doctor` | **pass** | IDCODE `0x0831F01D`, TotalIRLen 8, no blockers |
| 2b | `connect` (commander) | **pass** | DPIDR `0x4C013477`, CTRL/STAT `0xF0000020` |
| 2 | `connect` (dll) | **FAIL** | `0x4BA07477` — defect **D-1**, since fixed |
| — | `iface show` | **pass** | jtag, "proven" |
| 3 | `gates apply` | **pass** | all four registers exact; `DBGENCNTRL 0x3CF3CF00`, `DBGENCNTRL2 0x3CF3CF00`, `DBGENCNTRL3 0xFFF03CF0`; MDM IDR `0x001DE040`, SDA IDR `0x001DE001`, UID `0x1E09300B5563B9BC` |
| 4 | `apmap` | **pass** | "AP map matches RM Table 957 exactly" — 9/9, 0 missing, 0 unexpected, 0 mismatched |
| 5 | `romdump --source apb` | **pass** | 12 tables, 74 components, 12 error nodes (see note) |
| 6 | `mem info --ap apb` | **pass** | IDR `0x24770006`, CFG `0x000101A0`, BASE `0x01BFE000` |
| 6 | `mem read` CIDR | **pass** | `0x00410FF0` → `0D 90 05 B1`, the exact CoreSight preamble |
| 7 | 1 KB boundary | **pass** | 260-word block from `0x00410BF0` crossed `0xC00` and still returned `0D 90 05 B1` at `0x00410FF0` |
| 7b | `mem write` CLAIM tag | **pass** | CLAIMCLR `0x0 → 0x1 → 0x0`; read-back warning fired correctly |
| 10 | `bscan preamble --run-svf` | **pass** | "Download successful. SVF ok" |
| 11 | `bscan sample` ×2 | **pass** | `0x0035025555A02006` then `0x0015025555A02006` |
| 11b | **liveness discriminator** | **pass** | see below — the outstanding question, now answered |
| — | chain change | **confirmed** | `TotalIRLen` 8 → 16, `CTRL/STAT` `0xF0000000` → `0x00000001`; ordering rule 3 |
| 8, 9, 12 | `attach`, `release`, `report` | deferred | blocked by the chain change; done in run 3 |

**Run 3**, after a second power cycle, to finish the deferred steps:

| # | step | result | evidence |
|---|---|---|---|
| — | `doctor` | **pass** | `TotalIRLen 8` — confirms a power cycle reverses the chain change |
| 8 | `attach` | **pass** | held; `functional_reset_asserted=yes`, `destructive_reset_asserted=no`, gate open |
| 9 | `release` | **pass** | "released on attempt 1"; `functional_reset_asserted` back to `no` |
| 12 | `report --apply-gates` | **pass** | all 8 sections; gates open; apmap 9/9 clean; APB ROM 12 tables / 74 components |
| — | `iface swd`, virgin board | not run | one-way / needs blank flash |

`report` also recorded two *degraded* sections rather than failing, which is the
designed behaviour: `tap` carries an error (the dll backend cannot measure IR
length), and `romtables.apspace` could not be walked — the latter because of
**D-1**, which is how the defect's real blast radius came to light.

**Run 4**, immediately after fixing D-1, on the same powered board:

| step | result | evidence |
|---|---|---|
| `connect` (dll) | **pass** | "DPIDR unavailable" + citation; `CTRL/STAT 0xF0000000` |
| `romdump --source apspace` | **pass** | 1 table, 9 components, 0 errors (was: not a ROM table) |
| `report --apply-gates` | **pass** | `apspace` 1/9 **and** `apb` 12/74 — both trees now walk |
| `iface show` | **pass** | `jtag (proven)`; the DPIDR line is now an explained absence |

Offline after the fix: **292 passed, 21 deselected**.

### Run-control Phase 1 checkpoint — passed, 2026-08-01

The read-only identity pass that validates the Cortex-R52 external-debug offset
map before anything is written to a core.

| Check | Result |
|---|---|
| Frames discovered | 4 — `0x00410000`, `0x00510000`, `0x00C10000`, `0x00D10000` |
| `MIDR` | `0x411FD133` on all four → Arm, part `0xD13`, **Cortex-R52** |
| `EDDFR` | `0x10707100` on all four → **8 breakpoints, 8 watchpoints**, matching the guide |
| `DBGAUTHSTATUS` | `0x00000F0F` → non-secure invasive debug **granted** |
| `DEVAFF0` | names the cores authoritatively: `rtu0.0`, `rtu0.1`, `rtu1.0`, `rtu1.1` |
| Identity anchors | `MIDR` + `DEVTYPE 0x15` + `DEVARCH 0x6A05` agree on all four |

**The 4-versus-2 core question, answered.** Four debug frames exist, two per RTU,
and all four are real Cortex-R52s. The by-stride probe of `0x0061`, `0x0071`,
`0x00E1`, `0x00F1` found nothing. So the datasheet's four cores per RTU and the
ROM table's two frames per RTU are both right — consistent with lockstep pairing,
where each pair presents one debug frame (guide §6.2).

**Only one core is currently reachable for run-control.** `EDPRSR` per core:

| Core | `EDPRSR` | State |
|---|---|---|
| `rtu0.0` | `0x00000801` | powered, running the application, registers readable |
| `rtu0.1` | `0x000002AB` | powered, **OS lock set**, external debug access disabled |
| `rtu1.0`, `rtu1.1` | `0x000002AF` | same, **plus held in reset** |

Clearing `OSLAR` is a Phase 2 concern (guide §5.5). Until then only `rtu0.0`
can be halted.

### Run-control Phase 2 checkpoint — passed, 2026-08-01

Halt and resume, driven through the core's cross-trigger interface. Run against
`rtu0.1` rather than `rtu0.0`, so the application core was never disturbed.

| Step | Evidence |
|---|---|
| `core unlock --core rtu0.1` | `EDPRSR 0x000002AB → 0x00000001` — OS lock cleared, and `EDAD` cleared with it |
| `core halt --core rtu0.1` | `EDSCR 0x03054113` → STATUS `0b010011` **"halted: external debug request"**; `EDPRSR 0x00000011` → `HALTED` set |
| Independent agreement | both status registers say halted, which is the pass criterion |
| `EDSCR.HDE` | set by the halt path, as guide §5.6 requires |
| `core resume --core rtu0.1` | back to "running (non-debug state)" |
| Blast radius | `rtu0.0` still running, `rtu1.*` still locked — nothing else touched |

This closes three risk-register rows: **R2** (EDSCR/EDPRSR bit positions — the
halt reason decoded to exactly the value a CTI-driven halt should produce),
**R3** (CTI trigger assignment — channel 0 → trigger out 0 → debug request), and
**R8** (OS lock handling).

`EDSCR.ITE` reads 1 on the halted core, which is the precondition Phase 3's
register-transfer path needs.

**One design correction the bench forced.** `unlock()` and `read_state()`
originally *predicted* reachability from `EDPRSR` and skipped the `EDSCR` read
when `EDAD` was set. That refused an unlock which had in fact worked. Both now
attempt the read and use `EDPRSR` only to explain a failure — verify the thing
you care about, rather than a proxy for it.

### Run-control Phase 3 checkpoint — passed for r0-r14, 2026-08-01

The register-transfer path, and the highest-risk item in the whole run-control
plan: the instruction encodings were derived from the MCR/MRC layout, not read
from an Arm document.

**The oracle earned its keep on the first run.** It refused to report anything:

```
error: rtu0.1: instruction 0xEE000E15 faulted in Debug state (EDSCR 0x03054153).
```

Probing both candidates settled it:

| Encoding | Result |
|---|---|
| A32 `MCR p14,0,r0,c0,c5,0` = `0xEE000E15` | **EDSCR.ERR** |
| T32, halfword-swapped = `0x0E15EE00` | **ok** — TXfull set, `DTRTX 0x321C1774` |
| A32 NOP `0xE320F000` | EDSCR.ERR |
| T32 NOP `0x8000F3AF` | ok |

**In AArch32 Debug state this core executes T32, and EDITR takes a 32-bit T32
instruction with the *first* halfword in the low 16 bits** — the opposite order
from how the encoding is normally written down. Writing the halfwords the other
way round does not fault; it executes something else.

Rather than hardcode T32, `verify_register_path()` now tries both and remembers
which round-tripped, per core. The execution state is a property of the target,
not of the tool.

Registers read from `rtu0.1`:

```
r0  0x321C1774   r5  0x31786244   r10 0x00000000
r1  0x321BD4F4   r6  0x00000004   r12 0x317884CC
r2  0x00000002   r7  0x00000012   r13 0x3178F654   <- SP
```

`r13` landing in `0x3178xxxx` matches `WorkRAMAddr="0x31780000"` in SEGGER's own
device definition for this part — independent corroboration that these are real
register values and not transfer-register leftovers.

**Two gaps, recorded rather than hidden:**

- **CPSR is unreadable.** The T32 `MRS Rt, CPSR` form `0x8000F3EF` sets EDSCR.ERR
  while DTR transfers in the same encoding work. Probably the M-profile MRS
  encoding rather than the A/R-profile one, or CPSR is simply not readable this
  way in Debug state, where the architecture provides DSPSR instead. Reported as
  a note; r0-r14 do not depend on it.
- **PC is unavailable.** `EDPCSR` reads `0xFFFFFFFF` on a halted core — the
  architectural "sample not valid" value. It is a profiling aid, not a
  debug-state PC. The correct source is `DLR`, which needs the same unresolved
  MRS-family encoding. Where a core stopped is still knowable from the
  breakpoint that stopped it, so Phase 4 is not blocked, but "halt and show me
  the PC" does not work yet.

### Lockstep versus split-lock, and why only four cores answer — settled

**The RM answers this outright, and my earlier "probably lockstep" hedge was
unnecessary.**

RM Table 965 lists **four Cortex-R52 cores per RTU** with debug frames at
`0041/0051/0061/0071` (RTU0) and `00C1/00D1/00E1/00F1` (RTU1). The frames exist
by design. On this board, cores 2 and 3 of each RTU **fault on every access**,
including `EDPRCR` in the debug power domain — the APB slot does not respond at
all, so they are disabled rather than merely powered down. Writing
`EDPRCR.COREPURQ` at them faults too; a debugger cannot wake them.

RM 9.2.1.2, **RTU.GPR `CFG_CORE`** (RTU0 base `7612_0000h`, RTU1 `7692_0000h`),
offset `0h`, bit 0 `SPLT_LCK`:

> **Lockstep mode (0b):** "Core0 and Core2 operate as a lockstep pair. Core2
> functions as redundant copy of Core0. Core1 and Core3 operate as a lockstep
> pair. Core3 functions as redundant copy of Core1."
> **Split-Lock mode (1b):** "all four cores operate independently."

**The reset value of `SPLT_LCK` is 0 — Lockstep.** That predicts exactly what is
measured: cores 0 and 1 (the mains) answer, cores 2 and 3 (their shadows) do
not. The application running on "cores 0 and 1 of RTU0" is the two logical
cores of a lockstep configuration.

`CFG_CORE` bit 2, `THUMB`, resets to **1 = T32** — which independently explains
why `EDITR` accepts T32 encodings and rejects A32. Two separate predictions from
one register's reset value, both confirmed by measurement.

**To debug eight cores you need `SPLT_LCK = 1`.** RM 6.4.1: "Selection of the
operational mode occurs before the Cortex-R52 core cluster is released from
reset. After reset is released, lockstep mode cannot be dynamically changed
without first entering reset." So this is a boot-configuration change (DCF
record), not something a debugger can do at runtime. `CFG_CORE` itself cannot be
read back over the debugger's AXI-AP — XRDC denies the `0x76xxxxxx` RTU
peripheral slice, which is the same restriction the prior bench evidence
recorded.

The tool needs no change for split-lock: `discover()` already probes all eight
stride addresses and reports whatever answers.

### How many cores answer today — measured, 2026-08-01

Prompted by the observation that iSYSTEM winIDEA with a BlueBox debugs 8 cores.
Every by-stride candidate probed directly, `CIDR` + `MIDR` + `EDPRSR`, sticky
cleared between each:

| Address | Result |
|---|---|
| `0x00410000` | preamble ok, DEVTYPE `0x15`, MIDR `0x411FD133`, EDPRSR `0x0801` |
| `0x00510000` | preamble ok, DEVTYPE `0x15`, MIDR `0x411FD133`, EDPRSR `0x0011` |
| `0x00610000`, `0x00710000` | **no component** |
| `0x00C10000` | preamble ok, DEVTYPE `0x15`, MIDR `0x411FD133`, EDPRSR `0x02AD` |
| `0x00D10000` | preamble ok, DEVTYPE `0x15`, MIDR `0x411FD133`, EDPRSR `0x02AD` |
| `0x00E10000`, `0x00F10000` | **no component** |

Not a bad preamble — nothing mapped at all, which the section above explains:
this board boots in lockstep, so cores 2 and 3 are shadows without debug frames.

Worth noting separately: RM Table 957 also gives this part three **Cortex-M33**
subsystems — SMU at AP `0x01C80000`, FlexLLCE CE core 0 / core 1 at
`0x01C90000` / `0x01C92000` — plus a CEVA SPF2 DSP. Those are reached through
their own AHB-APs and use the M-profile debug model (DHCSR, DEMCR) rather than
the CoreSight external-debug frames this module drives. Debugging them is real
additional work, not a configuration difference.

### Reading the PC — solved via the Cortex-R52 TRM

**Cortex-R52 TRM §4.2.31**, "AArch32 debug registers ... 32-bit wide debug
registers accessed with (coproc==0b1111)":

```
DSPSR   CRn=c4  Op1=3  CRm=c5  Op2=0    Debug Saved Program Status Register
DLR     CRn=c4  Op1=3  CRm=c5  Op2=1    Debug Link Register
                                        "reset value is the value of the PC in
                                         Halting Debug state"
```

**They are coprocessor registers reached with `MRC p15`, not special registers
reached with `MRS`.** Confirmed on silicon:

```
DSPSR  MRC p15,3,r0,c4,c5,0   EDITR 0x0F15EE74  ->  0x0000003F   System mode, Thumb
DLR    MRC p15,3,r0,c4,c5,1   EDITR 0x0F35EE74  ->  0x321D2EE6   the PC
```

Corroborated independently: on that halt `r3` read `0x321D2EE7` — the same
address with the Thumb bit set.

**The dead ends, recorded so nobody repeats them.** All on a halted `rtu0.1`
with the register path verified:

| Route | Result |
|---|---|
| `EDPCSR` (`+0x0A0`) | `0xFFFFFFFF` — the architectural "sample not valid". A profiling aid, not a debug-state PC |
| `MRS Rd, CPSR` T32 | EDSCR.ERR |
| `MRS Rd, SPSR` T32 | works — returns a real PSR, but it is SPSR, not the PC |
| MRS banked sweep, 64 combinations of R:M:m1 | four hits, all **banked SP/LR** (`m1=5` → r13, `m1=6` → r14). No PC — it was never in that space |
| `MOV r0,pc` / `r0,sp` / `r0,lr`, 16-bit T32 | all EDSCR.ERR — EDITR requires 32-bit instructions |
| `ADR.W`, `SUB.W r0,pc,#0`, `MOV.W r0,pc`, `LDR.W r0,[pc]` | all EDSCR.ERR — Armv8 makes reading the PC in Debug state CONSTRAINED UNPREDICTABLE |

The lesson: three hours of sweeping an encoding space answered nothing that one
page of the TRM answered outright. The MRS sweep was searching a space the
answer was never in.

**Also delivered:** `core status` reconstructs the stop location from *why* the
core stopped — when `EDSCR.STATUS` says breakpoint, the address is whichever
comparator is armed. Useful independently of the PC, and it still works if a
future part hides DLR.

### Run-control Phase 4 checkpoint — passed, 2026-08-01

The breakpoints guide's own §8 test, with the strongest available evidence.

**Finding a target safely.** Halting repeatedly to hunt for a hot address is what
deadlocked the application earlier. `EDPCSR` does it without touching the core —
it is a *profiling* register, valid while running and `0xFFFFFFFF` while halted,
which is why it looked useless when first read on a stopped core. 400 samples per
core, zero invalid:

```
rtu0.0:  0x321D2EE8  46.0%      rtu0.1:  0x321D2EE8  49.0%
         0x321D2EE6  14.2%               0x321D2EE6  19.5%
```

A two-instruction idle loop both cores sit in ~65% of the time. Exposed as
`s32edbg core sample`.

**The test:**

```
armed breakpoint 0 at 0x321D2EE6  DBGBVR 0x321D2EE4  DBGBCR 0x00000187  BAS 0b1100
HIT: EDSCR 0x03054107 -> halted: breakpoint
     DLR (PC) 0x321D2EE6   target 0x321D2EE6   MATCH
     halt_location: 0x321D2EE6  (the single armed breakpoint comparator)
```

Three independent confirmations: `EDSCR.STATUS` reports *breakpoint* specifically
rather than a generic halt, `DLR` equals the address the comparator was set at,
and `halt_location` reconstructs the same address from the armed comparator. That
closes **R5** (PC readout) and **R6** (DBGBCR field layout) — a comparator with a
wrong control word is accepted, reads back correctly and never fires, so a
breakpoint that actually fires at the right address is the only proof.

Note `BAS 0b1100` and `DBGBVR 0x321D2EE4`: the target is the *high* halfword of
that word, and selecting all four bytes would have covered the neighbouring
instruction too.

**A bug this found in `resume()`.** The first run reported "did not resume within
2.0s" — but `EDSCR` read `0x03054107`, status *breakpoint*. The core had resumed,
hit the breakpoint in the idle loop before the poll could see it running, and
re-halted. `resume()` polled only for "not halted" and called a working
breakpoint a failed resume. It now compares the halt *reason* before and after: a
changed reason means the core executed. This is the guide's §7.5 case, and the
CLI now says "ran and immediately re-halted" with the address.

### Run-control Phase 5 checkpoint — passed, 2026-08-01

Single-step, step-over and cross-trigger. The pass criterion is a real one now
that DLR works: the PC must move by exactly the width of the instruction that
was at it.

```
step 0: 0x321D2EE6 -> 0x321DD098   2-byte T32   halting step, no syndrome   <- interrupt taken
step 1: 0x321DD098 -> 0x321D126C   4-byte A32   halting step, normal        <- branch to handler
step 2: 0x321D126C -> 0x321D1270   4-byte A32   halting step, normal        [OK]
step 3: 0x321D1270 -> 0x321D1274   4-byte A32   halting step, normal        [OK]
step 4: 0x321D1274 -> 0x321D1278   4-byte A32   halting step, normal        [OK]
```

The two steps that moved by more than the instruction width are a taken
exception and a taken branch — both correct, and both worth showing rather than
filtering out.

**Cross-trigger** was verified by programming and reading back, deliberately
*without* letting it fire: `CTIGATE` gains bit 2 on all four cores when enabled
and loses it when disabled, and nothing halted. Enabling it for real stops every
core including the one running the application, so that is left to the operator.

**Two bugs the bench found, both about instruction fetch:**

1. **Halfword alignment.** T32 instructions are halfword-aligned, so one can
   begin in the *high* half of a word — and its second halfword then lives in
   the *next* word. Reading a 32-bit word at the instruction address and slicing
   it as (low, high) is wrong for exactly those, which is why offline tests
   missed it. Caught when an instruction at `0x3241F522` decoded 2 bytes wide and
   stepping proved it 4.

2. **The instruction set is a fact to read, not assume.** An interrupt took the
   core from a T32 idle loop into an **A32** exception handler — the words at
   `0x321D126C` are `SUB lr, lr, #4` and `STMDB`, unmistakably ARM state.
   Assuming Thumb decoded them as 2-byte and made three correct 4-byte steps look
   like decoder errors. `step_over` reads `DSPSR.T`; the diagnostic script that
   hardcoded Thumb was the thing at fault, and that is worth recording because
   the same mistake is easy to make again.

### Cortex-M33 support — identity checkpoint passed, 2026-08-01

A different debug model, not an extension of the R52 work. An M33 is reached
through its own AHB-AP as ordinary memory — the debug registers live in the
processor's own address map at `0xE000E000` — so halting is a single write and
reading a register is two, with no instruction-transfer register and no
cross-trigger interface. Register map read from the **Cortex-M33 TRM**
(100230_0100_08): DHCSR/DCRSR/DCRDR/DEMCR from the debug summary, the BPU from
Table 13-1, the DWT from Table 11-1.

```
┌──────┬────────────┬────────────┬───────┬───────┬─────────┐
│ Name │ AP         │ Part       │ BP/WP │ Debug │ State   │
├──────┼────────────┼────────────┼───────┼───────┼─────────┤
│ smu  │ 0x01C80000 │ Cortex-M33 │ 8/4   │ off   │ running │
│ ce0  │ 0x01C90000 │ Cortex-M33 │ 8/4   │ off   │ running │
│ ce1  │ 0x01C92000 │ Cortex-M33 │ 8/4   │ off   │ running │
└──────┴────────────┴────────────┴───────┴───────┴─────────┘
```

All three identify by `CPUID`, and the comparator counts are read from `FP_CTRL`
and `DWT_CTRL` rather than assumed — the TRM allows four or eight breakpoints.
`Debug: off` means `DHCSR.C_DEBUGEN` is clear, which is expected: nothing has
enabled halting debug on them yet.

**Non-invasive sampling works, and caught a bug immediately.** `DWT_PCSR` is the
M33's equivalent of the R52's `EDPCSR`:

```
smu:  100 samples, 0 invalid   -> 0x25001F70  100%   a genuine idle loop
ce0:  100 samples, 100 invalid -> "the core is not executing -- held in reset,
                                   or its clock is gated"
```

The first run reported `0xFFFFFFFE` as a 100%-hot address on both CE cores.
**`DWT_PCSR` bit 0 is RES0** because a PC is halfword-aligned, so the all-ones
"no sample" marker reads back as `0xFFFFFFFE`, not `0xFFFFFFFF`. The check only
matched the latter, so a made-up number was being presented as a measurement.
Now both spellings are rejected and the reason is reported from `DHCSR` —
sleeping, halted, or not executing.

**Full run-control checkpoint on the SMU — passed**, with the operator's
agreement to stop that core:

```
halted: DHCSR 0x01030003
  PC 0x25001F6C   SP 0x250BFF40   LR 0x2500CE03   xPSR 0x89000000
  step 0: 0x25001F6C -> 0x25001F6E  (+2)     <- Thumb, 16-bit
  step 1: 0x25001F6E -> 0x25001F70  (+2)
  step 2: 0x25001F70 -> 0x25001F72  (+2)
armed BPU comparator 0 at 0x25001F70
resumed; re-halted immediately: True
HIT: PC 0x25001F70  target 0x25001F70  MATCH
cleaned up: comparators cleared, halted=False
```

Note the cross-check: the PC read through `DCRSR`/`DCRDR` after halting landed
in the same idle loop that non-invasive `DWT_PCSR` sampling had reported, by two
completely separate mechanisms.

**And the same `resume()` bug as the R52 path, which I had fixed there and not
carried across.** The first run reported "did not resume within 2.0s" — the core
had resumed, looped back to the armed breakpoint and re-halted faster than the
poll could see it running. On the M33 the discriminator is
`DHCSR.S_RETIRE_ST`: an instruction retired since the last DHCSR read, which
separates "ran and stopped again" from "never started". Reading DHCSR clears it,
so it has to be tested in the same read that checks `S_HALT`.

Worth naming the pattern, since it has now cost two debugging sessions: **on a
hot breakpoint, "still running" is never observable.** Any resume that polls for
it will report a working breakpoint as a failure.

**Still unverified:** the two FlexLLCE CE cores, which are not executing at all
(`DWT_PCSR` invalid on every sample), and DWT watchpoints on any M33.

### Three run-control bugs found by using it, 2026-08-01

All three surfaced from one operator report — *"`bp clear --all` resumed the
execution and stepping didn't seem to work"* — and none of them would have been
caught by the phase checkpoints, because each checkpoint ran inside a single
session and did the operations in an order that hid them.

**1. Stepping did not mask interrupts (R52).** Diagnosed by the operator. A step
from the T32 idle loop landed at `0x321DD018` with status *"halting step, no
syndrome"*: the core executed one instruction, immediately took a pending
interrupt, and stopped in the vector table. On a system with a periodic tick that
happens on essentially every step, so stepping appeared to teleport into handlers
and never advance. Fixed with `EDSCR.INTdis`, masked by default and restored
afterwards.

The M33 path had `DHCSR.C_MASKINTS` from the start. **This is the second time a
fix existed on one path and not the other** — the first was `resume()`. Both
paths need sweeping together.

**2. The halt was never acknowledged, so it was not stable.** `CTIAPPPULSE`
raises the debug request and nothing lowers it. Left asserted, the core was
released and re-halted repeatedly, which is why the PC moved between one CLI
command and the next and why `bp clear` looked like it resumed execution.
Measured:

```
after halt              halted=True  PC= -            <- register path FAILS
after INTACK+APPCLEAR   halted=True  PC= 0x321DD004
reopen a new session    halted=True  PC= 0x321DD004   -> PERSISTED
```

Note the first line: the lingering request also broke the register-transfer path.
`halt()` now acknowledges (`CTIINTACK` + `CTIAPPCLEAR`) once the halt has taken,
after which a halt survives across invocations.

**3. Stepping off a breakpoint did not move.** `step 1 0x321D2EE6 ->
0x321D2EE6`. A comparator armed at the current PC re-triggers the moment the core
is restarted, before any instruction executes — the breakpoints guide §7.5, with
the remedy in §5.8. `step()` now suspends every armed comparator for the duration
and restores them afterwards, including when the step fails. It had also been
*accepting* the re-trigger as a completed step, which is what let it report
success while nothing moved.

**What this says about the checkpoints.** Phases 1-5 all passed, and all three of
these bugs survived them. The checkpoints ran halt → read → step → resume inside
one script with the breakpoint armed only at the end; the operator ran one
command per invocation with a breakpoint already armed. The lesson is not that
the checkpoints were wrong but that **single-session testing cannot find
cross-session state bugs**, and that a checkpoint which mirrors the author's
mental model tests the same assumption twice.

### Halting one core of a coupled pair can deadlock the application

Observed 2026-08-01. `rtu0.1` was halted, a watchpoint set on its live stack, and
the core left halted across several separate CLI invocations. CAN traffic
stopped, and it did **not** come back when the core was resumed and the
comparators cleared — both cores read "running (non-debug state)" while the
application stayed stuck.

The breakpoints guide §5.9 describes the mechanism: the other cores keep running
and corrupt shared state, or spin on a lock the halted core holds. A functional
reset (`attach` then `release`) restarted the application and CAN returned.

Two practical rules: **keep halts short** — halt, read, resume in one session
rather than across invocations — and expect a functional reset to be the
recovery, not a resume.

### D-3 — a faulting APB read silently zeroed every read after it — **FIXED**

**Found by the Phase 1 checkpoint, and it affects the whole tool, not just
run-control.**

A read of unmapped APBIC2 space does **not** raise. It returns `0x00000000`,
latches `CTRL/STAT.STICKYERR` (`0xF0000000` → `0xF0000020`), and from that moment
the DP discards every subsequent AP transfer and returns zeros — still without
raising. Measured directly:

```
MIDR before any probe        0x411FD133
  probe 0x00610000 -> False  CTRL/STAT 0xF0000020
MIDR after probing           0x00000000
CTRL/STAT after clear_sticky 0xF0000000
MIDR after clear_sticky      0x411FD133
```

The first `core list` run therefore reported four healthy Cortex-R52s as
unidentifiable, with no error anywhere — a confidently wrong answer of exactly
the kind this tool exists to avoid.

`runcontrol` now reads through `_read_checked()`, which clears the sticky flag
before each access and checks it afterwards, so an unreadable register comes back
as `None` instead of as a plausible zero. That costs two extra DP accesses per
register and is worth it.

**`mem` was fixed after it produced a wrong answer in front of the user.**
Reading RTU0.GPR `CFG_CORE` at `0x76120000` through the AXI-AP reported
`0x00000000` — for a register XRDC denies the debugger entirely. `memaccess`
now reads through `Dap.mem_read_checked`, and a faulted access raises with the
address and the likely cause instead of returning a plausible zero:

```
error: the access to 0x76120000 via the AXI-AP faulted. XRDC can deny the
debugger a region the cores reach freely...
```

A block read that faults partway is rejected whole, because the latching sticky
flag makes every word after the fault read as zero — returning the block would
mix real values with fabricated ones.

`romdump` and `apmap` still read through plain `Dap.mem_read`. They survive
because their unmapped regions echo address bits rather than faulting, but that
is luck rather than design and is worth closing separately.

### Boundary-scan liveness: resolved

This was the open question — a repeatable capture proves nothing, because a
stuck path is also repeatable. Two consecutive captures of the running board:

```
0x0035025555A02006
0x0015025555A02006
      ^
```

One bit changed (bit 21) and the other 63 held. That is the signature of a live
capture: mostly-static pin states with one toggling. A stuck path gives
identical captures; a broken one gives noise everywhere. Compare also the
`0x0000000000000006` recorded earlier from a quiescent board — the capture
tracks board state.

**The boundary scan reaches real boundary cells.** No pin toggle by hand was
needed in the end; the running application supplied the change.

### Doc-alignment pass and the debug session — implemented 2026-08-02, offline-proven, bench pending

A review against the guide (`S32E27_Debug_HWBreakpoints_RunControl.md`) aligned every run-control
sequence to what the guide specifies, and added the persistent session the D-2 analysis called
for. Everything below passes offline against the enriched synthetic target
(`test_doc_alignment.py`, `test_session.py`), and the bench verification ran the same day — the
results are at the end of this section. The ⚠ markers flag changes to behaviour that was
previously silicon-proven; **both ⚠ items passed on silicon**.

**Changed sequences (guide section — change — bench risk):**

- ⚠ **§5.7/§5.8 — comparator control words are now any-EL.** `{HMC,SSC,PMC}` = `{1,00,11}`, the
  guide's "default choice for a halting debugger": DBGBCR `0x187 → 0x2187` (T32 high halfword),
  DBGWCR `0x1F7 → 0x21F7`. The old words were silicon-proven but matched EL1/EL0 only. HIDEN is
  set by LADDER-1 and `DBGAUTHSTATUS` read `0x00000F0F`, so EL2 matching is authorised — but a
  breakpoint that *fires* with the new word is the only proof. Re-run the §8 breakpoint test.
- ⚠ **§5.5 — restart is confirmed through `EDPRSR.SDR`**, not by watching EDSCR.STATUS change,
  and the debug-request acknowledge now polls `CTITRIGOUTSTATUS` until the trigger deasserts
  before any restart. Both bits are architecturally specified but neither has been read on this
  silicon. If a bench resume times out with "EDPRSR.SDR never set" while the core visibly runs,
  the SDR bit position (11) is the suspect.
- **§6.4 — the M33 step is two writes when masking**: `0xA05F000F` (halted, set step+mask) then
  `0xA05F000D` (release). The old single write changed `C_MASKINTS` in the write that cleared
  `C_HALT`, which the architecture calls UNPREDICTABLE — it happened to work on this silicon.
- **M33 step now suspends armed FPB comparators** and restores them — the same hot-PC re-trigger
  fix the R52 path already had ("the second time a fix existed on one path and not the other" —
  now swept across both).
- **§6.2 — M33 halts report their cause from DFSR** and clear what they report (W1C).
- **§5.3 — a watchpoint halt reports the data address from `EDWAR`** (high word ignored).
- **§5.4/§5.5 — the PC (DLR) and DSPSR are writable**: `core reg pc <addr>` redirects execution
  before a resume, via `MCR p15,3,Rt,c4,c5,1` — the MRC read encoding (silicon-proven) with the
  direction bit cleared.
- **CTI routing writes are read-modify-write.** `_prepare_cti` used to write CTIOUTEN absolutely,
  silently removing the cross-halt channel `core xtrigger on` had routed — a halt after xtrigger
  disarmed the group. Found by review, not by the bench; the offline test now pins it.
- **Step-over's instruction fetch obeys the AXI policy**: `read_instruction` now refuses while
  functional reset is asserted, same as `mem --ap axi` (ordering rule 5 / RM Table 957).

**New capability (all never-run on hardware):**

- **`debug` — the persistent session.** One connection for the whole halt/step/resume cycle;
  restore-on-close (session comparators cleared first, then session-halted cores resumed);
  first-touch comparator sanitation per §5.7 ("every field is UNKNOWN after cold reset --
  including E!"); `--keep-on-exit` / `--keep-breakpoints` to opt out. This is the structural
  answer to D-2 (every invocation pulses reset into the SBC/PMIC) and to the cross-invocation
  bugs above — "single-session testing cannot find cross-session state bugs" now has a
  single-session way to work.
- **§5.9 `core resume --all --sync`** — group restart on one CTM pulse, gates closed afterwards.
- **§5.10.2 `core reset-halt`** — the *hold* flow: clear the core's `SDARSTCTRL.RSTREL` bit,
  functional-reset the system (it boots normally, the core stays held), arm `EDECR.RCE` on the
  held core (EDECR is §5.2 OS-lock-exempt), set `RSTREL` back — the catch fires before the first
  instruction, then the OS lock the cold reset re-set is cleared. The SDA-AP write allowlist
  gained `SDARSTCTRL` and `SDARSTMASK` (reset machinery, not the one-shot auth path). This was
  originally built on the §5.10.3 SDARSTMASK variant; the bench killed that — see below.
- **§6.8 `m33 reset-halt`** — VC_CORERESET + AIRCR.SYSRESETREQ. The reach of SYSRESETREQ on this
  SoC is unknown; treat the system as rebooted until the bench narrows it.
- **§6.7 `m33 wp`** — DWT data watchpoints (TRCENA first, natural alignment enforced,
  ACTION=debug-event). Closes the "DWT watchpoints on any M33" gap this plan recorded.

#### Bench verification run — 2026-08-02, S32E277 SOM, application flashed

Driven by two library checkpoint scripts (one connection each) plus CLI `debug` sessions.
**26/26 scripted checks passed** after two findings were fixed; the session smoke and the R52
reset-halt then passed end to end.

Proven on silicon:

- ⚠ **`EDPRSR.SDR` confirms restarts** (bit 11 correct) — observed on plain resume, on
  resume-onto-a-hot-breakpoint, and on every member of a group restart.
- ⚠ **The any-EL comparator words fire**: DBGBCR `0x2067` hit at `0x321D2EE8`, EDSCR read
  breakpoint, DLR and `halt_location` agreed. DBGWCR `0x3FF7` fired on a stack store and
  **EDWAR reported exactly the watched address** (`0x3040A3F8`).
- **CTITRIGOUTSTATUS deasserts after the acknowledge** (read `0x0` post-halt).
- **§5.9 synchronized restart**: one pulse released rtu0.0+rtu0.1, SDR on both, channel gates
  closed afterwards.
- **M33 two-write masked step** advances the PC (`0x25001F6C → 6E → 70`), stepping off an armed
  FPB comparator moves the PC (suspension works), halts report DFSR causes.
- **The `debug` session**: one connection for status/halt/regs/bp/resume/step/clear/gates,
  correct "ran and immediately re-halted" reporting, restore-on-close resumed the core.
- **`core reset-halt` (hold flow)**: reset catch at PC `0x32100020` — the application's reset
  vector — `EDSCR 0x03050227`, fetched nothing, resumed cleanly into a normal boot.

**Finding 1 — SDARSTMASK does not protect the RTU debug state, and wedges the boot.** The
first reset-halt implementation followed §5.10.3: mask, arm RCE, functional reset. Measured: the
mask write *lands* (reads back `0xF0F00000` exactly), but the reset wiped the debug state anyway
(OS lock back, EDECR cleared, no catch) — and the boot jammed: rtu0.0 stuck executing at
BootROM-range addresses (`0x1B4`), rtu0.1 never released, for as long as we watched. Plausibly
the documented PSTATn-invalidation side effect derails boot software. A plain `attach`/`release`
recovered the board completely. The flow was rebuilt on §5.10.2's RSTREL hold, which passed.
Guide §3.6/§5.10.1's claim that SDARSTMASK preserves debug state is **contradicted by this
silicon** — trust accordingly.

**Finding 2 — `DWT_FUNCTION` reads back its RO ID field.** FUNCTION written `0x815` reads back
`0x58000815`: bits [31:27] are the comparator's capability ID (`0b01011`). The read-back verify
flagged it as a mismatch — correct instinct, wrong mask. Fixed (RO bits masked), and the fake
now models the ID bits so the offline suite would catch a regression.

**Finding 3 — M33 `SYSRESETREQ` takes no effect on this silicon.** `m33 reset-halt` armed
`C_DEBUGEN` + `VC_CORERESET`, wrote `AIRCR = 0x05FA0004` (accepted, no bus fault), and nothing
happened: `S_RESET_ST` never set, the SMU kept running, the rest of the system was untouched.
The command fails cleanly and disarms the catch. The SoC evidently gates M33 SYSRESETREQ
somewhere outside the guide's sources (its own §6.8 note points at SDA-AP `RSTWAIT*` CPUWAIT
holds for SoC-level control — a candidate follow-up, not implemented). Until then, M33
reset-and-halt is **blocked** on this part.

Still unproven: an M33 DWT watchpoint actually *firing* (it programs and verifies; the SMU's
idle loop performs no stores near its SP, so nothing triggered within the poll window), and the
CE cores generally (still not executing).

### The 12 ROM-walk error nodes are correct behaviour

Worth recording so nobody "fixes" them. Eleven are unmapped APBIC2 regions whose
reads echo address bits (`0x01050000` → CIDR `05 05 05 05`, `0x01060000` →
`06 06 06 06`, and so on). The twelfth, `0x01190000`, is a real component with a
malformed ID block — `mem read` confirms `CIDR0=0x0D, CIDR1=0x09, CIDR2=0x05,
CIDR3=0xB1`, and CIDR1's low nibble must be zero, so the preamble check
correctly rejects it.

`romdump` prints CIDR MSB-first in its error text, so `0x01190000` appears as
`CIDR 0xb1 0x05 0x09 0x0d`. That reads like a reversed capture and is not one —
it is display order. The `mem read` cross-check is what settled it, which is a
small illustration of why two independent paths to the same address are worth
having.

Consequence: the hardware test must assert `tables >= 12` and not `errors == 0`.

---

## The `load` (RAM-load) flow and the parse-safety pass — implemented 2026-08-04

Two changes in one pass: every numeric argument is now parse-safe (addresses
require `0x`; nothing the operator types can end a session or escape as a
traceback — see §5), and a new `load` command writes an application ELF
straight into RTU0 RAM and parks rtu0.0 halted at its entry.

### Sources

The user-designated reference is the S32 Debug VS Code plugin
(`C:/veh_ctrl_tools/s32_debug_VScode_plugin`), gta-probe flow, replicated
register for register — every value in `s32edbg/data/rtuboot.py` carries its
citation. Two user-decided divergences: the RESET_B pin toggle is an SDA-AP
functional reset by default (`--reset pin` restores the literal mechanism),
and the flow ends halted at the entry instead of running to a startup catch.
Core-1 policy (`--core1 leave` default) is grounded in the br1_rear firmware
itself: `BrsHwEarlyInitPowerOn()` (`BSw/StartUpCode/src/BrsHw.c:159-212`,
confirmed in the disassembly at 0x32100608) performs the complete rtu0.1
bring-up — assert core-1 reset, boot address `0x4190036C = &brsStartupEntryHyp`,
CCE+CCUPD+key, release — and asserts reset *first*, so any debugger pre-state
is tolerated. One firmware caveat recorded for completeness: its MC_ME key
handshake is coded as `CTL_KEY &= 0x5AF0` (a read-modify-write, confirmed in
the disassembly), so if `CTL_KEY` reads 0 on this silicon the CCE commit rides
on `Mcu_SetMode()`'s correct key writes later in EcuM driver-init — either
way core1 comes up with no debugger involvement.

### Phase order (the load-bearing constraints)

P1 assert reset + latch MDM EDBGREQ (MDM-only traffic — the AXI-AP is
forbidden during functional reset, guide §9 #3, and `gates.assert_axi_safe`
enforces it) → P2 release + poll both reset views clear + ONE AXI preflight →
P3 MC_ME/RGM partitions + NXP generic SRAM scrubs (verbatim, including their
local 0x0-0xFFFF ranges) → P4 SRAMC D0/D1/D2 v2 ladder → P5 park rtu0.0 →
P6 write LMAs → P7 verify → P8 PC/DSPSR.T → P9 core1 (optional).

**P4 is the edge-trigger fix.** `RAMCR.INIT` is edge-triggered (root-caused
on target 2026-07-03 in the reference repo): the P3 scrub leaves INIT=1, so
without a `RAMCR=0` pre-write the re-init is a silent no-op with
perfect-looking registers, DataRAM ECC stays uninitialised, and the OS dies
in `Os_Hal_CoreFreeze` ~1 s after boot. The flow therefore *requires* the
three witness words `0x3178E1A0 / 0x317C0010 / 0x31800010` to read exactly 0
and aborts loudly otherwise. The synthetic target models the edge, and
`tests/test_loader.py` proves both the no-op and the fix.

**P5 before P6** because the park word at `0x79900000` is the same physical
RAM as the ELF's first word at `0x32100000` (`r0_code0_6`, "same memory as
R0-Code0-F"): the image may only overwrite the park loop once the core is
already halted.

### The WriteMem fast path — uncertainties u1-u4, recorded verbatim

- **u1 signature.** `int WriteMem(U32 addr, U32 count, void*)` is conventional
  but unverifiable on this machine (no SDK header); the return convention
  (0 vs bytes-written) varies by DLL vintage. Only an unambiguous success
  counts; anything else is a TransportError the fallback catches.
- **u2 routing.** WriteMem writes through the DLL's own notion of the current
  memory AP. Under this tool's raw connect (no identified core) that routing
  is unknown. The mitigation is empirical: the **first fast block is fully
  read back through the independent ADIv6 path**; later blocks are
  sample-verified (first, last, every 256th word). Any mismatch demotes the
  load to pure ADIv6 for the rest of the run, after rewriting the suspect
  block there.
- **u3 state clobbering.** WriteMem moves SELECT/CSW/TAR beneath the `Dap`
  object; the wrapper invalidates both caches after every burst.
- **u4 side effects on failure.** The DLL is known to pulse reset on its own
  when confused; the fast path runs only after the flow's own reset, and a
  demotion re-runs the AXI preflight before continuing.

### Open bench items for `load`

- **XRDC and the `0x76xxxxxx` slice.** Measured previously: XRDC can deny the
  debugger this slice (SRAMC controllers, RTU GPR) while cores reach it
  freely. The plugin's RDC-unlock ordering plausibly opens it and the tables
  keep that order; every write is checked so a denial aborts with the
  existing XRDC hint. If bench shows the denial, the recorded fallback
  experiment is to compare against the plugin's probe on the same board state.
- **gta `monitor reset debug`.** A closed-binary operation between the init
  replay and the SRAMC ladder in the plugin's flow; unknowable from sources.
  This flow performs the documented explicit actions only — first suspect if
  bench shows a state mismatch after P3.
- **M33s stay halted after load** (EDBGREQ latched in MDM CONTROL, exactly as
  the plugin leaves them). The gta flow ran the application daily in this
  state; if bench disagrees, clearing the M33 request bits post-load is the
  knob.
- Timing: record pure-path vs fast-path KB/s here after the bench run.

### Finding 4 — DPBANKSEL cannot be trusted after the DLL's connect (fixed 2026-08-04)

With a V13 J-Link (firmware "V13 compiled Jun 25 2026"), every DP CTRL/STAT
access through the dll backend silently hit the wrong DP bank: reads returned
`0x00000000`, writes were discarded, and power-up "never acknowledged" — on
either DLL vintage (V9.62 and current), while the commander backend on the
same install worked. Root cause: newer DLL `Connect()` sequences leave
`SELECT.DPBANKSEL` on a non-zero bank, and `Dap` assumed bank 0 at
construction, so the banked-access shortcut never re-selected. The V11 probe
the 2026-08-02 bench used did not leave the bank dirty, which is why this
never showed before. Fix: `Dap` now starts with the bank marked unknown
(`_bank = -1`) and re-selects on the first banked DP access;
`invalidate_select_cache()` restores the same distrust after any operation
that lets the DLL move SELECT (the WriteMem fast path does this per burst).

### Bench results 2026-08-04 — `load` validated on target (V13 probe, 16 MHz JTAG)

`s32edbg --speed 16000 debug` → `gates apply` → `load br1_rear.elf`:

- **3,677,786 bytes in 33.6 s (107 KB/s), fast path used for every segment**,
  first block proven by full ADIv6 read-back, later blocks sample-verified,
  entry word verified. Pure-ADIv6 fallback measured at ~4.1 KB/s (the 311 KB
  FETK segment took 74 s on it in one run after a transient WriteMem
  demotion) — the fast path is a ~26x difference. At the default 4 MHz the
  fast path ran at ~71 KB/s; 16 MHz is the plugin's own configured speed.
- End state exactly as designed: `rtu0.0 halted at the entry PC 0x32100020
  DSPSR 0x400001FA` — the reference's documented parked-at-entry state
  (CPSR 0x1FA: HYP, T32, AIF).
- `bp set 32169874` mid-session → rejected with the 0x-prefix message, session
  alive (the original session-killing defect, re-proven fixed on target).
- `resume` → the loaded application boots.

Three silicon findings folded into the flow during validation, each with an
offline regression:

- **Finding 5 — XRDC denies the RTU0 GPR but not the SRAMC controllers.** The
  plugin's split-lock write at 0x76120000 faults from the J-Link (as
  memaccess.py had recorded for reads). Tolerated as an `optional` action:
  the register keeps its reset default, LOCKSTEP — the mode this ECU boots
  with naturally (its firmware never writes CFG_CORE).
- **Finding 6 — the DLL's WriteMem resumes the halted core.** EDPRSR.SDR set
  after the first fast block on every run; a core parked on the plugin's
  0x7990_0000 address (which aliases the image's first word) escaped into
  the half-written image. Fixes: the park lives in DataRAM (0x317FFFF0,
  ECC-initialised by the P4 ladder, outside every LMA), the EDBGREQ hold is
  converted to a real acknowledged CTI halt (`halt()`) before the MDM latch
  is dropped — an EDBGREQ-held core reads EDPRSR.HALTED without being
  architecturally in Debug state, and clearing the latch resumed it — and
  P8 re-halts before verify/PC if the DLL resumed the core mid-load.
- **Finding 7 — a stale EDBGREQ latch breaks the application's own core-1
  release.** rtu0.1 left reset when core0's firmware released it, then
  halted instantly on the still-latched request; core0 would hang at the OS
  boot barrier. All R52 latches are now dropped once the parked core is
  pinned (the M33 bits in CONTROL stay, exactly as the plugin leaves them).

### Finding 8 — WriteMem's -1 failures cluster over the FETK region (policy changed 2026-08-04)

A user run at 16 MHz hit `JLINK_WriteMem(...) returned -1` for three
consecutive 16 KB blocks at 0x326B0000/0x326B4000/0x326B8000 -- inside
`.FETK_referencePageVars` -- and the very first demotion ever observed was in
the same region (0x326B4000), while three intervening runs wrote it clean.
Clustered, region-specific, intermittent: consistent with bus contention from
the FETK/ETK measurement hardware polling its page, not with a broken writer
(the tool's own single-word ADIv6 writes to the same addresses always land).

Policy consequence: rc-class WriteMem failures no longer demote the fast path
permanently. Each failing block is retried (3 attempts, 25 ms apart, so a
contention window can pass), then rewritten via ADIv6 **individually**, and
the fast path stays on for the next block -- the cost of a hostile region is
bounded to its own blocks. The demote-forever response is reserved for what
it was designed for: a data mismatch (the DLL claimed success and the
read-back disagrees) or a missing export. The old one-strike/three-strike
policies turned three clustered transients into 85 s of word-writing for the
rest of a segment.

### Finding 9 — the DLL's WriteMem has a per-connection budget of ~4 MB (fixed 2026-08-04)

Back-to-back `load`s in one session: the first is clean, then WriteMem starts
returning -1 **in ~14 microseconds with zero wire traffic** (the DLL's own
--log shows successes route into its internal `CPU_WriteMem`; failures log
nothing at all). Onset is not address- or call-pattern-dependent: across five
occurrences it always began once cumulative WriteMem traffic in one DLL
connection reached roughly 3.4-5 MB (including once *within* a first load, at
4 MHz). Neither per-block retries, nor pauses, nor the DLL's own `Halt()`
revive it -- only a fresh DLL connection does. Root cause is inside the
closed DLL (a per-connection resource that WriteMem exhausts); the
observable contract is the budget.

Fix: every in-session `load` reopens the DLL connection at P0 (close, open,
TIF/speed/JTAGConf/Device/Connect, re-prepare + recalibrate). The reopen's
connect pulse lands moments before the load's own commanded SoC reset, so it
disturbs nothing of value, and each load starts with a full budget --
3.6 MiB fits with margin at 16 MHz. Validated: three consecutive loads,
32.16/32.16/32.14 s, fast path used throughout, app boots on both cores after
`resume`.

Two related updates riding along:

- **Finding 6 addendum:** SEGGER's kb (Memory_accesses -- the URL the DLL
  itself logs) documents that on Cortex-R WriteMem always runs stop-mode
  (halt + access + go) when the DLL believes the target is running. Issuing
  the DLL's own `Halt()` when the fast path arms (a wire no-op on the
  already-CTI-halted core) moves its model to "halted by me": the wrapper --
  and with it the resume-behind-our-back side effect -- disappears, loads
  run ~1.5 s faster, and the P8 re-halt is now a dormant safety net.
- The retry-time `Halt()` resync tried first was measured ineffective
  against the budget wedge and removed; the block retry with a 25 ms pause
  stays, for the Finding 8 contention transients only.

---

## The `flash` (external QSPI NOR) flow — implemented 2026-08-05, offline-proven, bench pending

`flash program / erase / verify / read / jedec` work the 32 MiB MX25UW25A45G
on QuadSPI_0 — the boot flash. **Nothing in this section has run on silicon
yet.** The offline suite proves the ported logic against an oracle model; the
bench procedure below is what promotes it. Failures there become Findings 10+.

### Sources

The user-designated references (exclusive): the s32multitool project
(`C:/s32multitool`) — its supported raw-QSPI route, i.e. the NXP flash-profile
init script plus the `RawQspiMx25uw25a45g` driver and the vendored M33 applet —
the example bootloader BLOB (`s32e2xx_bootloader_M33_v3.1.0_BLOB 1.bin`,
same 1,907,168-byte format as the in-repo v3.2.0 golden fixture), and
`br1_rear.elf`. Every register value, LUT halfword, timeout and region offset
in `s32edbg/data/qspiflash.py` carries its citation into those sources; the
applet binary is vendored byte-identical and SHA-256-pinned (toolchain-locked —
S32DS 3.6.1 GCC 9.2 — never rebuilt).

User decisions (2026-08-04): full command set; ELF inputs flattened
LMA-relative with 0xFF gaps and written verbatim (no container/IVT invention);
destinations always explicit (`--offset` or `--area` from
`flash_address_sets.json`, no default). One deliberate divergence from the
reference's *ordering*: the raw touch-up (MTO off, **SFACR=0** — the AN13563
address-aliasing fix) runs before the first JEDEC read rather than after,
because no S32DS `flash()` prepare runs underneath this tool and a cold
controller needs MTO disarmed before its first IP command can be trusted. One
divergence in *defaults*: blank-page skip is ON (the reference GUI defaults it
off) — erase-before-write is unconditional, so a skipped all-0xFF page is
indistinguishable from a programmed one, the full verify covers it either way,
and the ~45% saving on br1_rear is what keeps staging inside the Finding-9
WriteMem budget.

### Phase order (the load-bearing constraints)

F0 fresh DLL connection (Finding 9 — the applet plus payload ride the same
budget as `load`) → LADDER-1 + halt every reachable M33 by DHCSR →
reset-quiesce (assert functional reset, `arm_edbgreq`, release, wait deassert,
re-halt the M33s) → **clear the M33 EDBGREQ latches** (`MDM CONTROL[10:8]`)
*after* the DHCSR pin-down and verify the SMU stayed halted → AP preflight
(controller MCR answers; applet RAM holds a written word) → controller init
(PLL, MC_CGM, pads, QuadSPI + DLL-lock poll) → raw touch-up (SFACR=0) → mode
detect (SPI RDID, OPI on an invalid answer) → slot-1 READ LUT (AHB window
live) → JEDEC gate for destructive ops → per-op applet LUT loads + calls →
byte-exact verify. No reset afterwards: power-cycle boots the image.

**The EDBGREQ-clear ordering is a trap with a name.** `arm_edbgreq` latches
the three M33 debug requests in MDM CONTROL[10:8]; resumed into the applet
with any of them still set, the SMU re-halts before the first instruction
with DFSR = "external debug request". The clear must come after the M33s are
pinned by their own DHCSR (the latch is what was holding them), and the error
path names `gates.clear_m33_edbgreq` when it sees that DFSR. Offline-proven
both ways in `test_flash.py`.

**Register discipline.** Every applet call saves r0-r3/lr/sp/msp/psp/xpsr/pc
plus the packed CONTROL word (DCRSR selector 20) and restores them in a
`finally` — a failed flash op leaves the SMU exactly as the session found it.
The pc is written through DCRSR without bit 0 and EPSR.T is forced via the
xpsr write (the reference sets `$pc = base|1` through GDB, which owns that
convention — same end state; u-f4 confirms on silicon).

### What the offline oracle enforces (tests/test_flash.py, conftest model)

The SyntheticTarget's QSPI model is an oracle, not a recorder: programming
ANDs words into a sparse NOR array (a missing erase corrupts the verify), the
applet model refuses to run unless the physical LUT slots hold the opcodes the
reference loads for each op (slot 0 = command, 2 = WREN, 3 = RDSR; erase rides
slot 0), the AHB window answers only while slot 1 holds a READ sequence, LUT
writes are discarded while LUTKEY/LCKCR is locked, chip erase requires WEL and
answers WIP for three polls, and a latched EDBGREQ re-halts with DFSR=external
instead of executing. 49 tests ride it, plus the CLI/session/elf additions and
the golden BLOB/br1_rear pins (`S32EDBG_FLASH_BLOB` points the golden test at
another BLOB of the same format).

### Uncertainties u-f1..u-f9 — what only the bench can settle

- **u-f1 AP reachability and the window base.** The reference reaches all
  three address classes (controller `0x42320000`, applet RAM `0x2500xxxx`,
  flash window) through its gta stack; this tool defaults everything to the
  SMU AHB-AP `0x01C80000` with AXI `0x01C20000` as the alternative (`--ap`),
  and probes before anything destructive. The window base is `0x0` in the
  reference's raw path and `0x08000000` in its legacy service path — the tool
  starts at `0x0` and fails over once, loudly. **Record the working map.**
- **u-f2 WriteMem routed through an AHB-AP.** The fast write path issues
  `CORESIGHT_AddAP Index=0 Type=AHB-AP` + `SetIndexAHBAPToUse` — undocumented
  for this DLL vintage. The first-block full read-back proof plus sampled
  cross-checks is the net; a lie demotes permanently (same discipline as
  `load`, mirrored for reads).
- **u-f3 ReadMem.** Newly bound export, no SDK header: rc convention unknown
  (only rc==0 is accepted), and whether reads draw on the Finding-9 ~4 MB
  budget is unmeasured — both directions currently count against a shared
  3 MiB proactive ceiling, after which the flow continues on pure ADIv6.
- **u-f4 the applet entry convention.** DCRSR pc write without bit 0 +
  xpsr.T force, stacks at 0x25034000, CONTROL zeroed. Wrong-by-one here reads
  as an instant HardFault/lockup instead of the bkpt halt — the DFSR check
  will name it.
- **u-f5 applet op timing.** Host-side deadlines are sized from datasheet
  worst cases (60 s program / 120 s erase per call); the reference bench
  measured ~9.5 s erase + ~35 s program + ~5.7 s read per MiB on its
  transport. Compare, and re-size if this transport is slower.
- **u-f6 a board left in OPI mode.** The SPI→OPI detection fallback is
  offline-proven against the model; a real board whose firmware switched the
  device to octal is the confirming case.
- **u-f7 chip-erase duration.** The RDSR WIP poll allows 900 s (the
  reference's own deadline). Record the real figure for this part.
- **u-f8 images past the budget ceiling.** br1_rear stages ~3.5 MiB — right
  at the 3 MiB proactive demotion line, so its tail may run at ADIv6 speed
  (~4 KB/s). If bench shows that tail costing minutes, the designed follow-up
  is an opt-in mid-flow reconnect between applet calls (risky with a
  half-written offset 0, hence opt-in and bench-gated).
- **u-f9 `--reset none` (attach mode).** Unproven while firmware may own the
  QSPI controller — the reset-quiesce default exists precisely so nothing
  else touches QSPI during the flow.

### Tier-2 bench procedure (at `--speed 16000`, scratch first — never offset 0 first)

1. `flash jedec` → mode + `0xC23989C2` (u-f1 partially: controller reachable).
2. Preflight AP report — record which AP serves controller/RAM/window (u-f1).
3. `flash read --offset 0x300000 --length 0x10000 --out dump.bin` (u-f3: rc
   convention, budget effect; window base failover if any).
4. `flash erase --offset 0x300000 --length 0x10000` → rc 0, DFSR = bkpt not
   external, blank check passes (u-f4, the EDBGREQ clear).
5. Scratch programs of 123 / 333 / 0x1234 bytes at `0x300000` → byte-exact
   verify (padding, ragged tails; u-f7 timeouts).
6. Fast-path proof block through the AHB-AP routing (u-f2) — demotion is the
   acceptable failure mode, not corruption.
7. 1 MiB program + verify, timed against the reference baseline (u-f5).
8. OPI-state board, if one exists: detection fallback (u-f6).
9. `flash erase --all` on a sacrificial board < 900 s (u-f7).
10. BLOB at offset 0 → byte 0 reads 0xD1 → power cycle → the board boots.
11. `flash program br1_rear.elf --area app` — the full-size image (u-f8).
12. `--reset none` against a running application (u-f9) — expect trouble;
    that is the point of the default.

`tools/bench-acceptance.ps1` carries steps 1-5 as its `-FlashScratch` block
(default `0x300000`; `-FlashBlobPath` adds the BLOB program as a gated step).

### Bench results 2026-08-05 — first silicon run (V13 probe, V9.62 DLL, 16 MHz)

Steps 1-4 of the procedure passed on target, after three findings, each fixed
with an offline regression the same day. Artifacts in
`bench-runs/20260805-flash/`.

- `flash jedec` → **JEDEC 0xC23989C2, SPI mode**, 8.35 s end to end.
- `flash read` 64 KiB at 0x300000 → real content (the programmed application:
  Thumb-2 code) through **AHB window base 0x0 on the SMU AHB-AP** — u-f1's
  window question answered; both the SMU AHB-AP and the AXI-AP reach the
  controller and the applet RAM (preflight proven on both).
- `flash erase --offset 0x300000 --length 0x10000` → **the applet ran**:
  64 KiB erased in 2.93 s, terminating bkpt (DFSR = breakpoint), registers
  restored, full blank check read 0xFF throughout — u-f4's entry convention
  (DCRSR pc without bit 0 + xpsr.T force) proven.

### Finding 10 — the SMU AHB-AP answers dead for ~0.5 s after the reset release (fixed 2026-08-05)

The first `flash jedec` died re-halting the M33s right after the quiesce
reset: every SMU identity register read zero — the AP answers, with nothing —
then read perfectly on the next connect. Measured settle: **0.50 s** on every
subsequent run. Fix: the bring-up polls SMU identity (up to 10 s) before the
loud one-shot `halt_m33s`; the EDBGREQ latch has always already caught the
core by the time the AP wakes. The CE APs stay dead after the reset (nothing
brings their subsystems up any more) — best-effort by design. Offline: the
`m33_dead_reads_after_reset` knob + a CLI regression.

### Finding 11 — BootROM leaves the QSPI tree configured; the cold tables race the peripheral wake-up (fixed 2026-08-05)

Three facts that changed `controller_init` into probe-first:

1. **Warm is the common case.** BootROM boots from this flash, so after the
   quiesce reset the controller usually answers RDID immediately (MCR read
   0x010F004C). The init now probes with just the raw touch-up applied and
   skips the PLL/pad/controller tables when a valid JEDEC answers.
2. **The halt races BootROM.** One run in three caught the SoC before BootROM
   configured QSPI (MCR 0x000F004C, RDID dead) — and the **full cold tables
   then ran clean on silicon**, PLL writes included, and RDID answered. Both
   paths are now silicon-proven.
3. **Too-early PLL access faults; attached PLL access faults.** Immediately
   after the reset release the PLL block at 0x40220000 faults for the
   debugger on *both* APs (the same staggered-wake-up family as Finding 10 —
   the cold path that ran clean had ~4 s of probe timeouts in front of it);
   and with the application *running* (no quiesce, `mem read`), the block
   faults too — the app's own XRDC configuration denies the debugger. Neither
   affects the reset-first flow; recorded because both look like "XRDC denies
   the PLL, full stop" and are not.

### Finding 12 — this DLL's ReadMem: rc 1 always, and the answer depends on the registered AP routing (fixed 2026-08-05)

`JLINK_ReadMem` returns **1** for every 4096-byte window read, on both
routings. With the **SMU AHB-AP registered**, the buffer comes back filled
with SEGGER's 0xAA poison — the first-block ADIv6 proof caught it
(`DLL 0xAAAAAAAA, ADIv6 0xFFFFFFFF`) and demoted permanently, exactly the
designed containment. With the **AXI-AP registered** (`--ap axi`), the same
rc 1 accompanies *correct data* — the full first-block proof passed. So rc 1
is accepted as a success candidate (rc ∉ {0, 1} stays a TransportError) and
the content proof is the real gate, in both directions. Consequence for
operators: **`--ap axi` is the fast configuration** for reads; the SMU-AP
routing works but reads at ADIv6 speed (~1.3 KB/s through the window).

### Finding 6, flash edition — the DLL resumes the SMU after fast-path bursts (fixed 2026-08-05)

The first applet call died with S_REGRDY stuck clear: after the WriteMem
staging burst the SMU was *running* again, despite the arm-time model sync
that cured this for the R52 during `load`. The same resume was later caught
after a fast **ReadMem** burst too. Fix: `m33.ensure_halted` — a single
`C_DEBUGEN|C_HALT` DHCSR write, because `halt()`'s two-write §6.2 sequence
would itself resume an already-halted core for the gap between its writes —
re-pins the SMU after every fast burst (stage, window read, applet-buffer
read), with a loud note when it actually caught the core running. Offline:
the fake now asserts S_REGRDY only while halted, and a resuming-WriteMem
target regression covers the flow.

---

## Dual-core RTU0: `catch` and `sync` — implemented 2026-08-05, offline-proven, bench pending

The wiped-flash RAM-load workflow debugs rtu0.0; this pass makes rtu0.1 a
first-class target too. Requirements set by the user (2026-08-05): per-core
bp/halt/step/regs and watchpoints; catching rtu0.1 at its **first
instruction** on the application's own release; opt-in stop-the-world
(`sync`); the app-driven release kept as the default core-start policy.

### What changed

- **`core catch`** (+ session alias `catch`): arms the core's **MDM EDBGREQ
  latch** and leaves it armed; the application's own release halts the core at
  instruction 0 (Finding 7, measured as a bug, redeployed as the feature).
  `--wait` converts the hold — an EDBGREQ hold is not architecturally Debug
  state — via unlock → real acknowledged CTI halt
  (`runcontrol.pin_down_edbgreq_hold`, hoisted from the loader) → latch drop →
  re-sanitise. Write commands convert a fired catch automatically. Arm-only
  requires a session; `restore()` drops unfired latches before any resume.
  **EDECR.RCE was rejected by design**: the app asserts core-1 reset before
  releasing it, and the cold reset wipes EDECR (measured class, §3.6).
- **Session `sync on|off`**: arms the guide-§5.9 cross-halt group across the
  usable cores and teaches the session: bare `halt` waits for the partners,
  bare `continue` group-restarts with the **beyond-guide teardown**
  (`suspend_cross_halt` masks each halted member's `CTIINEN0` channel-2 bit
  for the restart window, then re-arms — chosen over gate-closing because it
  kills both the CTM drive and the internal self-loop whichever the silicon
  implements), `step` steps one core while the partners stay halted, a
  breakpoint hit is detected on the next refresh and owned for restore, and
  close disarms. `_acknowledge_halt` recognises the armed-group level source
  and skips its deassert poll (it would time out by construction).
- **`wp set` enables `EDSCR.HDE`** like `bp set` (guide §5.2 item 3 — without
  it a watchpoint arms, reads back, never fires), and both warn when arming
  against a running core (§5.7:610/§5.8:662).
- **Held-in-reset is a stated reason** (`EDPRSR.R` in
  `debug_registers_reachable`/`unlock`/`preflight_notes`); `bp/wp list` on a
  locked core names `unlock`; a core the app cold-reset mid-session gets its
  session records dropped and is re-unlocked/re-sanitised on next touch
  (`DebugSession.note_core_reset`).
- **SyntheticTarget** now models: the RGM assert edge as a core-local cold
  reset (comparators + EDECR wiped, OS lock + R set, EDSCR faulting), MDM
  EDBGREQ as a *level* that swallows restarts, and channel-2 cross-halt
  propagation with the gate/self-loop semantics — 13 new offline tests ride
  it (catch end-to-end, sync propagation/teardown/restore, the swallowed
  naive restart).

### Guide gaps surfaced (the guide file itself is not edited)

1. **No coverage of application-driven core release.** §5.10 only describes
   SDA-AP (debugger-driven) releases; whether any catch mechanism survives an
   RGM release by another core is unstated. Resolved here empirically: the
   MDM latch does (Finding 7), EDECR cannot (the app's own reset-assert wipes
   it).
2. **§5.9 gives no group-teardown-for-restart sequence.** With the halt group
   armed, a halted member's DBGTRIGGER is a level that re-drives the channel
   straight through CTIINTACK; the restart recipe never mentions it. The
   CTIINEN-mask teardown implemented here is beyond-guide, with the rationale
   recorded in `runcontrol.suspend_cross_halt`.
3. **§3.6:297 cross-references §5.10.4, which does not exist** (the guide has
   only 5.10.1-5.10.3).
4. **§5.10.1/§3.6's SDARSTMASK claim is contradicted by this silicon**
   (Finding 1: the mask lands, the debug state is wiped anyway, and a reset
   taken with the mask set wedged the boot).
5. **Nothing warns about halting one core of a *software*-coupled pair**
   (spinlocks, the OS boot barrier) — only the hardware lockstep note
   (§5.11). The measured deadlock (2026-08-01) is repo-only knowledge.

Also fixed: nine stale guide citations in repo comments (§7.x/§6.1/§5.5/§5.6
references re-pointed at the sections that actually carry the content —
§5.2/§5.5/§5.7/§3.5/§3.3/§5.9).

### Bench results 2026-08-05 — all items passed (V13 probe, wiped-flash board)

Artifacts `bench-runs/20260805-flash/20-25*.txt`. The passing run (22) started
seconds after a power cycle; runs 20/21/23 hit Finding 13 below first.

1. **`bp set --core rtu0.1` while held** → the designed refusal with real
   values: "held in reset (EDPRSR 0x000002AD) ... `core catch` arms a halt at
   its first instruction". The preflight note printed too.
2. **The catch, against the real application release**: armed → `continue` →
   `catch --wait` converted instantly — **"rtu0.1 caught at its first
   instruction -- halted: external debug request, PC 0x32100020"**, registers
   all zero, mode Hyp/Thumb, OS lock cleared by the conversion. `continue`
   released it and the app completed boot.
3. **First-ever ch2 firing**: `sync on` → `halt` → "sync: rtu0.1 stopped with
   the group", EDSCR external-debug-request on the partner. The old
   "deliberately without letting it fire" line is retired.
4. **Step under sync**: 0x321D5B98 → 0x321D5B9A, halting-step-normal, the
   partner stayed halted. **Group restart under the armed channel**: "one
   pulse released 2 core(s)" — the beyond-guide CTIINEN teardown works on
   silicon.
5. **`wp set` on the running core**: armed with the §5.7/§5.8 advisory, HDE
   set by the wp path itself. A *fire* was not observed in the brief window
   (0x32101300 not stored to at that moment) — partial, same class as the
   M33 wp cell.
6. **Finding-7 regression**: a fresh `load` + `continue` with catch never
   armed → rtu0.1 released by core0's firmware and RUNNING (EDPRSR
   0x000002A9, OS-locked as an untouched core should be). No stale latch.

### Finding 13 — the blank-flash boot cascade races the debugger (rule: attach first)

On a board with nothing bootable, the BootROM fail cascade escalates the
longer it runs, and two distinct failures were measured against it:

- **DP-state displacement mid-bring-up** (runs 20, 23): the load died at the
  P3 `PRTN1_STAT.PCS` poll and *every subsequent AP read returned the same
  displaced word* (EDPRSR reading EDSCR-shaped values, 0x4205xxxx
  everywhere) — a cascade reset yanked the DP under the flow between the
  connect and the load's own quiesce, and the displaced read alignment
  persisted for the rest of the connection.
- **WriteMem wedged on a fresh connection** (run 21): every block -1 in
  microseconds from the first write — the Finding-9 *symptom* with none of
  its budget *cause* — after the board had cascaded for hours. Probe-side
  state, cured by the board power cycle (run 22 hit 111 KB/s again).

**The rule**: on a blank-flash board, stop the cascade before debugging —
`s32edbg attach` (holds functional reset; the load's own quiesce then starts
from a quiet SoC — run 25 proven clean this way), or begin within seconds of
a power cycle (run 22). The flash flows are naturally protected: their
quiesce runs first thing. In the state-matrix battery, `load` runs after
`flash jedec`, which leaves the SoC quiesced-halted — the same protection by
ordering.

---

## The flash-state × debugging matrix — S1 measured 2026-08-05, S2-S5 blocked

**First-run status.** S1 (nothing in flash) ran and its record cells are
measured (below). S2-S5 are blocked by **Finding 14**: their setup steps need
`flash program`, which needs the SMU, which a blank board does not have. The
matrix resumes once the bootloader is restored by one of Finding 14's routes —
from S2 onward every state has a live SMU, so the remaining states should run
as designed.

Measured S1 cells (`bench-runs/flashmatrix-20260805/S1/`), now the pinned
expectations for a cascade board:

| step | S1 result |
|---|---|
| `connect` | pass — CTRL/STAT `0xF0000000` |
| `gates apply` | pass — gate open, exact read-back |
| `apmap` | pass — matches RM Table 957 exactly |
| `core list` | **no R52 debug frames answer at all** (refusal naming `gates apply`) |
| `m33 list` | all three M33 APs read zeros — powered down in DEST0 |
| `flash jedec` | **fails** at the SMU halt (Finding 14) |
| `load --verify` | passes — proven repeatedly today (32.4 s, 111 KB/s) |



`tools/flash-state-matrix.ps1` proves "debugging works as it worked until
now" in **every flash-content state** (user requirement, 2026-08-05). States
per docs/app-boot-contract.md; transitions use the tool's own flash commands
(incremental, S1→S5 in order; every transition is followed by a power-cycle
prompt because a debug reset is not the cold boot the BootROM re-evaluates
flash on):

| state | flash content | setup transition | boot behaviour |
|---|---|---|---|
| S1 | nothing | `flash erase --all` | BootROM fail cascade; SMU AP dead at attach |
| S2 | BL only | + BLOB at `0x0` | BL superloop on the SMU; R52s never released; **powers off after 10 idle minutes** |
| S3 | BL + app, no flag | + ELF `--area app` | same as S2 — the flag gates the app start |
| S4 | BL + app + flag | + `AA 55 AA 55` at `0x100000` | normal boot; app on rtu0.0 + rtu0.1 |
| S5 | app only | erase `0x0..0x200000` (BL + flag; app survives) | same as S1 |

Battery per state, observation before perturbation (`flash jedec` and `load`
quiesce-reset the SoC and replace the state's runtime, so they run last):
entry (`connect`, or `attach` as the cascade foothold on S1/S5) → `gates
apply` + `apmap` (asserted) → `core list` / `m33 list` (**record cells**: the
per-state core states are measured by the first run and pinned into the
script's `-Check` blocks by a follow-up commit — record-then-pin) → one
`debug` batch session sized to the state (S4: halt/regs/bp/wp/step/continue
on **both** R52 cores, breakpoint at the RAM entry `0x32100020`; S2/S3:
held-in-reset status wording recorded + the full SMU M33 battery, safe
because the BL disabled the SBC watchdog; S1/S5: clean refusals only) →
`flash jedec` (JEDEC `0xC23989C2` asserted in every state) → `load --verify`
(asserted in every state — on S1/S5 it exercises the blank-flash tolerances,
`smu_fatal=False` and the Finding-10 settle).

Artifacts: `bench-runs\flashmatrix-<stamp>\<state>\NN-*.json/.log` plus
per-state and top-level `summary.json` and a console transcript. The S2/S3
observation phase is timestamped against the 10-minute window with a warning
past 8 minutes. A full run ends with app-only flash (no bootloader) — restore
per app-boot-contract §4. Failures and measured record-cells become findings
numbered with Workstream A's.

### Finding 14 — a fully erased board cannot be reflashed by this tool (the SMU is gone)

Measured 2026-08-05 while starting the state matrix, on the board left blank by
`flash erase --all`. Every M33 AP (SMU, CE0, CE1) reads **literal zeros** for
CPUID and DHCSR — not a fault, an AP answering with nothing. That is the
`docs/virgin-board.md` chain reaching its end state: blank flash → BootROM
finds no image → functional reset (RM 62.12) → FREC escalation (RM 60.5.2.4) →
**DEST0**, where RM 60.5.6 says the part *"remains there until a POR event"*.
The M33 subsystems are unpowered there.

That is fatal for the flash flow and only for it: **the QSPI applet executes on
the SMU M33**, so with the SMU unreachable `flash jedec/program/erase` all die
at their post-reset SMU halt. Three routes were tried, all failing identically:

1. default quiesce (`--reset sda`) — the flow's own functional reset re-runs
   the failed boot and takes the SMU down with it;
2. `attach` (hold functional reset) first, then the flash op — same;
3. `--reset none` fired within seconds of a POR — same, because **the J-Link
   connect itself pulses reset through R439 (defect D-2)**: connecting *is* a
   boot attempt, so the cascade always wins the race. There is no window in
   which this probe can meet a live SMU on a blank board.

Everything that does not need the SMU still works on the same blank board:
`connect`, `gates apply`, `apmap` (exact RM Table 957 match), AXI-AP `mem`
read/write, and the whole RAM `load` flow with dual-core `catch`/`sync` — the
load path brings the RTU partitions up itself.

**Recovery routes** (none implemented here): s32multitool / S32DS, the
bootloader's own UDS reflash path (needs a running BL), or the hardware
serial-boot strap (`docs/virgin-board.md`: let FREC reach 8, BOOTMOD via
TP40/TP41). A fourth, unimplemented and **unsourced**, would be to bring the
M33 subsystem up from the debugger the way `load` brings up RTU0 — the
vendored tables carry only the M33 *clock mux* (`ACTIONS_M33_CLOCK`,
MC_CGM2 MUX0, measured reading `0x0F000000` = no source selected on the dead
board), not an MC_ME partition-0 core release. That sequence is not in any
source this repo may use; **ask the user/NXP before inventing it**.

Consequence applied: the `flash erase --all` confirmation now states plainly
that the tool cannot put the bootloader back, and points at the recovery
routes. Range erases (which keep offset 0) carry no such trap.

**The full account, and what to fix, is
`docs/handover-virgin-board-flashing.md`** — the open-defect handover for
whoever takes this on.

The next finding number is **15**.

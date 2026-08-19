# The S32E2 System Debug Interface

How the debug interface is built, how to switch between JTAG and SWD, how to run a boundary
scan, and how to reach every bus.

Sources are cited inline. `RM` is the *S32E2 Reference Manual, Rev. 5, 2025-01-28*; `ADIv6` is
*Arm Debug Interface Architecture Specification ADIv6.0* (IHI0074B); `SoC-600 TRM` is the
*CoreSight SoC-600 Technical Reference Manual*. Anything read out of a SEGGER binary rather than
a document is marked as such, because that is an implementation fact rather than a published
contract.

---

## 1. What the debug interface is

The debug architecture is Arm **CoreSight SoC-600** (RM 135.1). Five pins carry it — `JCOMP`,
`TCK`, `TMS`, `TDI`, `TDO`, all 3.3 V (RM Table 979). **There is no TRST pin.**

The JTAG pads feed a **TLM** (TAP Linking Module), whose default path drives two controllers
**in parallel** (RM Figure 1014):

- the Arm **SWJ-DP**, which is the debug port proper, and
- the system **JTAG controller (JTAGC)**, which owns boundary scan and the chip ID.

They are *overlaid*, both have an **IR length of 8**, and the chip presents to the scan chain as
**a single TAP** (RM 135.5.1, 135.5.3). Which of the two answers is decided by the instruction
loaded: four opcodes belong to the DAP and everything else to the JTAGC.

| IR value | Selects | Source |
|---|---|---|
| `0xF8` ABORT, `0xFA` DPACC, `0xFB` APACC, `0xFE` IDCODE | Arm DAP | RM Table 956 |
| `0x00` IDCODE, `0x02` SAMPLE/PRELOAD, `0x0A` EXT_TEST_CTRL, `0x20` TLM TAP select, `0x21` TLM TDR, `0xFF` BYPASS, … | JTAGC | RM Table 958 |

This overlay is why a chain scan reports one TAP with `TotalIRLen 8` and no second device: there
is no second device. Earlier recon read that as a "silent second TAP"; it is not.

### JCOMP gates everything

`JCOMP` (ball AG8) does the job TRST would. It is **active-high with a weak internal pull-down**,
and while it is low **both the JTAGC and the SWJ-DP stay in reset** (RM 60.5.4.1, 136.5.5,
Table 301). A dead TAP and a JCOMP problem are indistinguishable from the debugger side.

The JTAGC is also held in reset by power-on reset, or by five consecutive TMS-high TCK edges
(RM 136.4.8). Entering Test-Logic-Reset reloads the IR with IDCODE — which matters more than it
sounds, because it means any sequence that passes through Test-Logic-Reset silently discards the
instruction you just loaded.

### Below the port: two APB interconnects

The DAP splits the debug fabric in two (RM 135.5.2):

- **APBIC1** — 4 MB at `0x01C00000`. Holds every access port. RM L412334: *"External tool direct
  access is limited to only the 4 MB of memory space of APBIC1."*
- **APBIC2** — the remaining 28 MB of APB debug components: the Cortex-R52 debug frames, CTIs,
  funnels, trace buffers. **A debugger reaches it only through the APB-AP.** Map in RM Table 965.

Cores see the same fabric offset to `0x4C000000` in system memory, which is why the RM lists the
MDM-AP at both `0x01C10000` (debugger) and `0x4DC11000` (cores).

That second address hides a detail worth knowing. The offset is `0x4C000000`, so the core-side
address "should" be `0x4DC10000` — but the RM says `0x4DC11000`, a thousand hex higher. The
SoC-600 TRM §2.2 explains it: every MEM-AP is a **twin AP occupying 8 KB**, with **AP-L0 at
`+0x0000` the debugger view and AP-L1 at `+0x1000` the self-hosted view**, selected by
`paddr_s[12]`. Read-only registers are shared; writable ones are duplicated per view. An external
debugger can only discover one of the twins through the ROM table.

### Two security layers, and a clock gate

RM Figure 1013 draws two distinct gates on the interface:

- **APB security gates**, controlled through the **MDM-AP**, and writable.
- **HSE security gates**, reported by the **SDA-AP**, driven by lifecycle state and
  challenge/response.

Independently of both, **the debug clocks are off by default** (RM 135.11). They are enabled by
asserting the DP's `cdbgpwrupreq`, or by setting any bit in the MDM-AP `DBGENCNTRL{,2,3}`
registers. The RM's warning is blunt: *"Trying to access debug components while the clocks are not
enabled might hang the debug APBIC bus."* Enable them before walking anything.

On this silicon the proven sequence ("LADDER-1") is four MDM-AP writes: `CONTROL = 0x00800000`
(mask debug faults first), then `DBGENCNTRL = 0x3CF3CF00`, `DBGENCNTRL2 = 0x3CF3CF00`,
`DBGENCNTRL3 = 0xFFF03CF0`. The bits are **sticky across functional resets and lost on power
loss**, so re-apply after a power cycle, not after every reset.

---

## 2. Switching between JTAG and SWD

### What the RM says, and what it does not

The port **comes out of reset in JTAG** (RM 135.5.1). Switching to SWD is possible; switching back
is not:

> "Switching back from SWD mode to JTAG mode requires POR." — RM 135.5.1

After the switch the pins are reassigned: **TMS becomes SWDIO, TCK becomes SWCLK, and TDI and TDO
become available as GPIO** (RM Table 980, 135.5.1).

The RM gives no sequences. RM 135.14.1.1 says only that the SoC-600 SWJ-DP **implements the
dormant state**, so *"a transition through dormant state is required to switch protocols"*, and
defers to ADIv6.

### The sequences, from ADIv6 B5.2 / B5.3

All values MSB-first.

| Step | Sequence |
|---|---|
| JTAG → dormant | ≥5 clocks TMS high, then 31 bits `0x2EEEEEE6`. The IR must already hold BYPASS or IDCODE, or behaviour is unpredictable |
| SWD → dormant | ≥50 clocks TMS high (line reset), then 16 bits `0x3DC7` |
| Leave dormant | ≥8 clocks TMS high, then the 128-bit Selection Alert `0x49CF9046_A9B4A161_97F5BBC7_45703D98`, then **4 clocks TMS low**, then an activation code |
| Activation codes | SW-DP `0b01011000`, JTAG-DP `0b01010000`, JTAG-Serial `0b000000000000` |
| Settling | SWD: ≥50 clocks high (line reset). JTAG: one clock low to Run-Test/Idle, or ≥5 high to Test-Logic-Reset |
| Direct, deprecated | JTAG→SWD `0x79E7`, SWD→JTAG `0x3CE7`, each between ≥50-clock TMS-high runs |

Arm **deprecates the direct 16-bit sequences on parts that implement the dormant state**, and this
part is one. They are recorded in `s32edbg/data/swj.py` for documentation, not use.

These constants are verified rather than merely transcribed: `s32edbg/data/swj.py` encodes them to
wire bytes, and `tests/test_swj.py` asserts those bytes appear verbatim inside `JLink_x64.dll`
(the JTAG-to-dormant sequence at `0x00BC8E8B`, the Selection Alert at `0x00BC8E63`). If either the
ADIv6 reading or the bit packing were wrong, the bytes would not match.

### The step everyone forgets

> "For ADIv6 or later, the state of the DP registers after a switch is UNKNOWN, and they must be
> programmed by the debugger before initiating any AP transactions. Programming the DP registers
> might include clearing any sticky errors." — ADIv6 B5.2

So a switch is *not* complete when DPIDR reads. The DP must be reprogrammed from scratch —
`SELECT` written, `CTRL/STAT` written to an **absolute** value rather than read-modify-written
(the read is exactly what is undefined), the power-up handshake redone, and sticky errors cleared.
`Dap.reinitialise_after_protocol_switch()` does this; ordinary `power_up()` must not be used,
because it begins with the undefined read.

### What this tool can and cannot do

`s32edbg iface swd --i-understand-swd-is-one-way` arms `SetDormantModeHandling` and lets the probe
perform the transition, then reprograms the DP as above.

**There is no bit-level fallback, and this is a hard limit rather than an omission.** The switch
runs on probe firmware. Driving it from the host would need a raw SWD primitive, and the DLL
exports none with a discoverable ABI: `JLINK_SWD_ReadWriteBits` has a clean prototype in the
binary's string table but **is not exported** (it belongs to the `.JLinkScript` API), and the
`JLINK_SWD_StoreRaw` family is exported but carries no prototype at all. When the probe refuses —
its message is *"The connected J-Link (S/N: …) does not support dormant mode handling"* — the
correct answer is a different probe or newer firmware. The tool prints the refusal verbatim
alongside the sequence it asked for, and exits non-zero rather than pretending.

`s32edbg iface show` detects the live protocol instead of asserting it. The discriminator is the
JTAGC IDCODE: the JTAGC sits in parallel with the SWJ-DP and is reachable **only over JTAG**, so a
matching IDCODE is proof. A DPIDR read alone is not, because the DAP answers over either protocol.
Pipelined AP reads corroborate a JTAG-DP but do not prove one. Failing everything, the answer is
`unknown` — it never probes SWD, because probing SWD means switching to it.

---

## 3. Boundary scan

Governed by RM chapter 136 (JTAGC), plus a chip-specific preamble in RM 136.1.4.

### The registers

The JTAGC has an 8-bit instruction register, a 1-bit bypass register, the 32-bit device
identification register, the boundary scan register, and — specific to this chip — an **External
JTAGC Test Controller register (EXT_TEST_CTRL)** (RM 136.1.3, 136.3).

Data shifts LSB-first between TDI and TDO (RM 136.4.2). Reading does not require shifting the
whole register: *"Shifting may be terminated after the required number of bits have been
acquired"* (RM 136.4.3.2).

**The BSR length and bit ordering come from the bundled BSDL — corrected by silicon.** RM 136.3.4
puts them only in the BSDL file, which RM Table 984 names (`SIP_S32E277n8n9_BGA27_831F01D.bsdl`
for the 27×27 S32E277/8/9) without saying where to get it. The tool bundles the file NXP actually
ships (`SIP_S32S277n8n9_S32E277n8n9_BGA27_831F01D.bsdl` — the S32S/S32E combined SIP file; its own
DESIGN_WARNING targets the S32S277/8/9) in `s32edbg/data/` and parses it with `s32edbg/bsdl.py`:
IDCODE `0x0831F01D`, INSTRUCTION_LENGTH 16, declared **BOUNDARY_LENGTH 494** — and its
DESIGN_WARNING carries the whole Table 984 preamble verbatim, so the RM and the BSDL confirm each
other on the trickiest sequence in the tool.

**Measured 2026-08-19: this part chains 464 of those 494 cells.** The missing 30 are the file's
single run of consecutive `internal` cells (262..291, between the DDR block and LIN_6) — no pins
lost, everything above shifts down by 30. Recorded in `data/bscan.py:BSDL_CELL_QUIRKS` and applied
automatically by `bsdl.load_bundled`; see test-plan §7.3 and `bench-runs/20260819-bscan-bsdl/` for
the measurement. Selection is by parsed IDCODE, never filename; `--bsdl` overrides the bundled
copy verbatim (no quirks), with the length verify as the arbiter.

### Pin mapping via BSDL

Every observable pin is a **BC_7 `bidir` data cell plus a BC_1/BC_2 `control` cell** (disval 0,
result Z) — 227 of them — plus exactly one `output2` with no control cell (`DDR0_RESET_N`: always
driven, no Z state). The remaining cells are `control` and `internal`. Captured bit *n* is cell
*n* (IEEE 1149.1: cell 0 nearest TDO, LSB shifted first); `--reverse-cells` exists as the
bench-falsifiable escape hatch should silicon disagree. A pin row reports its level and whether
its own output driver was on (control cell ≠ disval).

Caveats the BSDL itself declares: `AUR_*` pins have no BSR cells (untestable); DDR0 data pins
(`DDR0_DQ*`/`DMI*`/`DQS*`) **cannot SAMPLE their inputs in mission mode** — the path to the BSR is
blocked, so their sampled level shows the output latch (PRELOAD is unaffected); a functional POR is
required before BSR instructions. `JCOMP`/`POR_B` are compliance pins ((POR_B, JCOMP) must be 11)
with no cells.

Each pin row also carries its **SIUL2 pad identity** (`PAD_xxx` + instance + MSCR index, or
`AE MSCR n` for the AE-die `PB*` pads), joined by BGA ball from NXP's `S32E2_IOMUX.xlsx` — shipped
as an attachment embedded in the RM PDF, materialized into `s32edbg/data/pads_bga27.py` by
`tools/gen_pads.py`. Dedicated pins (RESET_B, POR_B, JCOMP, EXTAL) have no pad; the JTAG pins do
(TCK = PAD_173 — RM 135.15.1's "can be later reassigned" said as much).

Where the BSDL and RM Table 959 disagree on an opcode, **the BSDL wins at shift time**
(`jtag/bscan.py:ir_for`) and the divergence is printed. Today there is exactly one, and it is
benign: `ENABLE_CENSOR_CTRL` 0x0100 (BSDL) vs 0x0107 (RM) — an instruction boundary scan never
issues. Every opcode the tool does shift agrees, including the non-duplicated `HIGHZ 0x0509`. One
width discrepancy is recorded and deliberately not acted on: EXT_TEST_CTRL's DR is 215 bits per
REGISTER_ACCESS, `205'h…` per the DESIGN_WARNING literal, and **208 bits as proven on silicon** —
the shipped payload stays 208.

### The IR width contradiction, settled on silicon

RM Table 984 gives the preamble as 8-bit opcodes (`0Ah`/`21h`/`20h`) while RM Table 959 defines every
S32E2 opcode as **16 bits** — the main JTAGC concatenated with `JTAGC_AE` in the Application-Extension
die. **Table 959 is the one that matches the hardware.**

Measured on an S32E277 (IDCODE `0x0831F01D`) by loading each instruction and reading which data
register it then selected, via an SVF TDO compare:

| Instruction | `SIR 8` | `SIR 16` |
|---|---|---|
| IDCODE | `0x1D` | `0x1D` |
| SAMPLE/PRELOAD | `0x1D` | **`0x06`** |
| EXT_TEST_CTRL | — | **`0x02`** |
| BYPASS | zeros | zeros |

At 8 bits SAMPLE/PRELOAD returns the **IDCODE** — the boundary-scan register is never selected, so
anything read that way is the device ID, not pin state. At 16 bits each instruction selects a
distinct register. `BSCAN_DEFAULT_IRLEN = 16` in `data/bscan.py` records this with the measurements.

Two traps worth keeping. J-Link's chain scan still reports `TotalIRLen 8` and `IRLen: 04`, so **the
reported scan length is not evidence** — only the behaviour is. And the 8-bit form fails *silently*:
it returns plausible-looking data, so a boundary scan built on it looks like it works.

### The mandatory preamble — RM Table 984

Before any boundary scan, EXT_TEST_CTRL must select the BSR segments for this package. On S32E2
this also involves two TLM registers (opcodes below are the 8-bit forms the RM prints; use the
16-bit encodings from Table 959, which `jtagc_ir()` supplies):

1. IR `0x0A` (EXT_TEST_CTRL), then DR **208 bits**:
   `1800_0800_000C_0004_0000_07C0_0000_0003_3041_1800_01A0_0280_0000`
2. IR `0x21` (TLM_TDR), then DR 32 bits `0x80000001`
3. IR `0x20` (TLM_TAP_SELECT_TDR), then DR 32 bits `0x80000011`

Then load **SAMPLE/PRELOAD** (`0x02`) and shift the BSR.

Each instruction and its data register must be shifted **without re-entering Test-Logic-Reset in
between**, since that would reload IDCODE and discard the instruction. In this tool that is what
`Transport.jtag_sequence()` guarantees; it also matters for the batch backend, where separate
calls would land in separate `JLink.exe` runs.

### Use SAMPLE/PRELOAD, not SAMPLE

RM Table 958 carries an explicit note: *"On this chip, do not use the SAMPLE instruction. Instead,
use the SAMPLE/PRELOAD instruction."*

PRELOAD is non-invasive. RM 136.4.4.2: it loads the boundary-scan update latches, and *"System
operation is not affected."* Those latches drive nothing unless EXTEST or CLAMP is subsequently
selected.

### The instructions behind the drive opt-in, and why

| Instruction | What it does | RM |
|---|---|---|
| **EXTEST** | Drives preloaded data onto the output pins, and **asserts the internal system reset** | 136.4.4.3 |
| **HIGHZ** | Places **all output drivers** in an inactive state, and asserts the internal system reset | 136.4.4.4 |
| **CLAMP** | Drives pins from the BSR while bypass is selected, and asserts the internal system reset | 136.4.4.5 |

**Known limitation — EXTEST drive is unconfirmed on S32E silicon (bench 2026-08-19).** On an
S32E277 the drive path does not work: EXTEST (0x0404) correctly selects the 464-bit BSR (HIGHZ
0x0509 and CLAMP 0x0C0C each select the 1-bit bypass, as REGISTER_ACCESS declares) and asserts the
internal MCU reset, but **the preloaded output data never reaches the pads**. Read-back *under*
EXTEST — a capture-DR taken while EXTEST is latched, which samples the live pad — is bit-for-bit
identical whether the loaded vector is all-1 or all-0, and a pin explicitly driven high reads back
low; only the MCU-reset side-effect is observable (RESET_B 1→0, functional drivers drop). The shift
mechanics are verified correct (SAMPLE reads real pin state fine), so this is not a tool bug. Likely
NXP-held cause: the RM Table 984 `EXT_TEST_CTRL` payload is documented only as "selects the BSR
segments" and appears to configure them for observe/SAMPLE, not output-drive; and/or the private
`JTAGC_EXTEST_PULSE (0x0800)` / `JTAGC_EXTEST_TRAIN (0x0900)` opcodes (present-but-commented in the
BSDL) are needed to apply the drive. An NXP ticket is open. The drive commands remain available
(gated) so the fix can be tried once known; `bench-runs/20260819-bscan-bsdl/EXTEST-not-driving.md`
has the full evidence.

On a live ECU each of those is a reset, and HIGHZ is additionally a bus float. So they sit behind
`--i-understand-boundary-scan-drives-pins`; without it the refusal carries the citation (exit 3).
Two more facts the consent covers. The drive flow is *preload-then-latch* — the vector loads under
SAMPLE/PRELOAD (system still untouched) and the final `SIR` flips every pin at one Update-IR, with
all unnamed cells at their BSDL safe values and their drivers off. And the latched instruction
**persists after the JTAGLoad run exits**: the pins stay driven and the MCU stays in reset until
`bscan release` (a TAP reset — TLR reloads IDCODE, RM 136.3.1) or a power-cycle. The BSDL requires
a functional POR before further BSR work after that.

### Verifying (or measuring) the register length

With the quirked BSDL the length is known — 464 — and *verified* in a single JTAGLoad run: shift
`464 + 32` bits with a 32-bit marker entering first, and let the SVF's own TDO compare expect the
marker exactly 464 clocks later, MASK covering only that window. `Download successful.` is the
proof; any other length puts capture tail or a shifted marker in the window and fails. No TDO
*reporting* is needed at all, which is what frees this from the raw-JTAG limitation. The same
oracle read BYPASS as exactly 1 bit and IDCODE as exactly 32 on this silicon — the calibration
that made the 494-vs-464 discrepancy a fact rather than a suspicion.

The empirical measurement (`bscan length --measure`) remains for a chain with no BSDL, with its
original subtlety: **the marker must not be a run of ones.** The search returns the earliest
match, so any run of ones already present in the captured pin state is mistaken for the marker and
the measured length comes out too short, confidently and with no error. The BSR capture is live pin
state and can trivially contain a byte of ones. This tool uses `0x6699A55A`, which has no run of
identical bits longer than two — and the same asymmetry is what makes the one-run verify alias-proof.

### Proving the scan is real

A stable, repeatable length is not sufficient evidence — it can be produced by a stuck path. The
definitive check is that **sampled bits change when a pin is physically toggled**:
`s32edbg bscan sample --watch` captures, waits, re-captures and diffs.

---

## 4. Reaching every bus

### The nine access ports — RM Table 957

| Debugger base | Core view | What it masters | IDR | XRDC PDAC2 |
|---|---|---|---|---|
| `0x01C00000` | `0x4DC00000` | APBIC1 ROM table (not an AP) | — | 298 |
| `0x01C10000` | `0x4DC11000` | **MDM-AP** — debug enables, cross-trigger, reset status | `0x001DE040` | 299 |
| `0x01C20000` | `0x4DC21000` | **AXI-AP** — the entire system memory map | `0x34770017` | 300 |
| `0x01C30000` | `0x4DC31000` | **APB-AP** — APBIC2, i.e. every debug component | `0x24770006` | 301 |
| `0x01C70000` | `0x4DC71000` | **SDA-AP** — secure debug authentication | `0x001DE001` | 303 |
| `0x01C80000` | `0x4DC81000` | AHB-AP — System Manager subsystem (SMU Cortex-M33) | `0x34770008` | 304 |
| `0x01C90000` | `0x4DC91000` | AHB-AP — FlexLLCE CE Cortex-M33 core 0 | `0x34770008` | 305 |
| `0x01C92000` | `0x4DC93000` | AHB-AP — FlexLLCE CE Cortex-M33 core 1 | `0x34770008` | 306 |
| `0x01CA0000` / `0x01CA2000` | `0x4DCA1000` / `0x4DCA3000` | AHB-AP, reserved | `0x34770008` | 308 / 309 |

There is no Table 957 entry for `0x01C40000`–`0x01C60000`, and no PDAC2 slots 302 or 307. The
tool flags anything discovered in that window.

In practice: **the APB-AP is the route to anything CoreSight** (core debug frames, CTIs, PMUs,
ETMs, funnels, trace buffers), and **the AXI-AP is the route to anything the CPUs can see**
(peripherals, RAM, MC_ME, RGM, the SRAM controllers). The AHB-APs reach the Cortex-M33 subsystems
directly. The MDM-AP and SDA-AP master no bus at all — they are register blocks.

### How an AP is addressed under ADIv6

ADIv6 removed APSEL. **An AP is memory-mapped in a flat 64-bit space**, and reaching register `A`
means:

```
SELECT.ADDR  = A >> 4          (written as SELECT = (A & 0xFFFFFFF0) | DPBANKSEL)
APACC address = (A >> 2) & 3   (the "RegIndex" a J-Link ReadAP/WriteAP takes)
```

The top-level pointer is `BASEPTR0`/`BASEPTR1`, reached with `DPBANKSEL` 2 and 3. `DPIDR.VERSION`
of `0x3` means DPv3, hence ADIv6; this DAP reports `0x4C013477`.

### The MEM-AP register block

| Offset | Register |
|---|---|
| `0xD00` | CSW — size, address increment, and bus attributes |
| `0xD04` / `0xD08` | TAR low / high |
| `0xD0C` | DRW — the data window |
| `0xDF4` | CFG — big-endian, large-address, large-data |
| `0xDF8` | BASE — ROM table pointer |
| `0xDFC` | IDR |

Three rules that produce silently wrong results if ignored:

**CSW must be read-modify-written.** ADIv6 C2.6.4 leaves Prot, Type, Mode and DbgSwEnable
implementation-defined with an SoC-chosen reset value. They carry the bus attributes each access
is issued with — privilege, cacheability, secure state. Overwriting CSW wholesale changes them for
every subsequent transfer.

**Auto-increment is only guaranteed inside a 1 KB window** (ADIv6 C2.6.4), so TAR must be
rewritten at every boundary. An AP is free to wrap to the start of the window rather than carry,
which returns plausible values from the wrong addresses with no error anywhere.

**Sub-word data sits in a byte lane** (ADIv6 C2.6.6), selected by the low address bits, not
right-aligned in DRW.

### The two hazards

**Never touch the AXI-AP while functional reset is asserted.** RM Table 957:

> "During reset, do not access peripherals and memory regions via AXI_AP. If such an access
> occurs, the peripherals and memory regions might not be accessible via AXI_AP after reset
> de-assertion. In that case, you must issue a destructive reset."

**Never write the SDA-AP.** RM 135.13.4 makes the HSE challenge/response one-shot — *"HSE allows
one only attempt … If it fails, no further attempts are accepted."* A failed attempt is
unrecoverable until reset. Read `AUTHSTTS` and `SDASYSRSTS` for status; write nothing.

Both are enforced in `s32edbg/dap/memaccess.py`, on every path, with no override flag.

### XRDC will refuse some reads, legitimately

Each AP is an XRDC PDAC2 slot (298–309), and XRDC also governs what the debugger domain may reach
*through* an AP. Bench evidence records that in the cold, parked-at-entry state the `0x76xxxxxx`
RTU peripheral slice **faults for debugger-issued accesses**, while the identical writes succeed
from the core during a normal boot. That is domain policy in the post-reset default state, not a
dead bus; the same addresses become reachable once firmware has configured XRDC. A faulting
`mem read` there is a correct answer, not a tool bug.

### Using it

```
s32edbg mem info  --ap axi                    # IDR, CFG, BASE, and CSW as found
s32edbg mem read  0x41850014 --ap axi         # one word through the system interconnect
s32edbg mem read  0x40000000 --ap axi --size 8 --count 4
s32edbg mem dump  0x01BFE000 256 --ap apb     # hex dump of the debug APB
s32edbg mem write 0x4DC11004 0x00800000 --ap axi --yes
```

`--ap` takes any of `mdm, axi, apb, sda, ahb-smu, ahb-ce0, ahb-ce1, ahb-r0, ahb-r1`, or a raw base
address. The names resolve through the same Table 957 data the AP-map diff uses, so there is one
source of truth.

Start with the APB-AP — it is the safest bus to be wrong on — and cross-check one address against
`romdump` before trusting the AXI-AP.

---

## 5. Practical notes

**A healthy VTREF does not mean the MCU is powered.** VTREF measures the 3.3 V rail from the SBC's
pre-regulator; the S32E core runs on 0.8 V from a different PMIC buck. RM Table 295 lists core
supply and critical LVDs as POR sources, and RM Table 301 says POR is the only reset other than
JCOMP going low that holds the JTAGC in reset. So VTREF can read a perfect 3.307 V with the core
unpowered and the TAP legitimately dead. Check the PMIC and SBC enable switches first.

**The raw JTAG path goes through the Commander backend, not the DLL.** No DLL export can shift an
arbitrary TMS/TDI vector with a discoverable ABI. `JLINK_JTAG_ReadWriteBits` and
`JLINK_JTAG_Reset` appear in the binary with full prototypes but are **not exported** — they
belong to the `.JLinkScript` API. The `StoreRaw`/`StoreGetRaw` family *is* exported but carries no
prototype anywhere, and its own log strings disagree about argument order. So raw shifting uses
J-Link Commander's `WJTAGRaw <NumBits> <TMS> <TDI>`, which takes arbitrary width and prints the
capture back as ` TDO: <hex>`.

That syntax comes from `JLink.exe`'s embedded help table and **has never been run against this
target** — the prior on-silicon evidence only ever used `ReadAP`/`WriteAP`/`ReadDP`/`WriteDP`.
Treat it as unvalidated until the bench confirms it, and run `calibrate_tdo_order()` first: it
shifts a known pattern through the one-bit bypass register, whose answer is known in advance, to
settle which end of the printed capture was clocked first.

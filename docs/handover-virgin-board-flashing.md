# Handover: bootloader flashing on a blank board must be fixed

**Status: open defect. The board on this bench is currently blank and this tool
cannot reflash it.**

`s32edbg flash` programs, erases, verifies and reads the external QSPI NOR, and
all of it is bench-proven — but **only while a bootable image is already in
flash**. The moment the device is fully erased, every flash command stops
working, and the tool has no way back. That gap is the subject of this
document.

Everything here was measured on 2026-08-05 (V13 J-Link, V9.62 DLL, S32E277 SOM,
`--speed 16000`). Artifacts: `bench-runs/20260805-flash/` and
`bench-runs/flashmatrix-20260805/S1/`. The condensed version lives in
`docs/test-plan.md` as **Finding 14**; this document is the full account for
whoever picks the work up.

---

## 1. What works, and what does not

Proven on silicon, with the bootloader present in flash:

| command | result |
|---|---|
| `flash jedec` | mode + JEDEC `0xC23989C2`, 8.4 s |
| `flash read` | 64 KiB through the AHB window |
| `flash erase --offset … --length …` | applet erase, 64 KiB in 2.9 s, blank check passes |
| `flash erase --all` | whole 32 MiB in 18.3 s, host-driven chip erase |
| `flash program` (BLOB / app / flag) | erase-before-write + byte-exact verify |

Proven on silicon **after** the device was fully erased:

| command | result |
|---|---|
| `connect`, `gates apply`, `apmap` | pass — the DAP and the AP map are healthy |
| `mem read/write --ap axi` | pass |
| `load` (RAM) + `core catch` + `sync` | pass — full dual-core debugging, repeatedly |
| **every `flash` command** | **fails**: "could not halt the SMU M33 … every identity register read as zero" |

So the blank board is still fully debuggable for RAM work. What it has lost is
flash access, and with it the ability to restore its own bootloader.

## 2. Why it fails

The QSPI applet — the vendored 2 KiB Cortex-M33 binary that performs every
program, erase and buffered read — **executes on the SMU M33**. The flash flow
therefore requires a live, haltable SMU.

On a blank board the SMU is not merely running-and-busy; it is **gone**. All
three M33 APs (SMU `0x01C80000`, CE0, CE1) answer with literal zeros for CPUID
and DHCSR — an AP that responds but has nothing behind it, not a fault:

```
{ "name": "smu", "ap": "0x01C80000",
  "cpuid": { "raw": "0x00000000", "is_cortex_m33": false },
  "dhcsr": { "raw": "0x00000000", "halted": false, "debug_enabled": false } }
```

That is the chain `docs/virgin-board.md` describes, reaching its end state:

1. blank flash → the BootROM finds no valid image → **functional reset**
   (RM 62.12);
2. functional resets escalate through FREC → **destructive reset**
   (RM 60.5.2.4);
3. escalation lands in **DEST0**, where RM 60.5.6 says the device *"remains
   there until a POR event starts a POR sequence"*. The M33 subsystems are
   unpowered there.

BootROM is what loads the bootloader into SMU SRAM and starts it
(`C:\s32e2_bootloader`, image at flash `0x2000` → SMU SRAM `0x25000000`). No
image, no SMU program, and after the cascade, no SMU power domain either.

## 3. Three routes tried — all fail identically

1. **Default quiesce (`--reset sda`).** The flow's own functional reset re-runs
   the failed boot; the SMU AP never returns. Instrumented settle time went
   from `0.50 s` (healthy board, Finding 10) to the full 10 s poll timeout with
   the AP still dead.
2. **`attach` first, then the flash op.** Holding functional reset does not
   help: releasing it starts another failed boot.
3. **`--reset none`, fired within seconds of a POR.** Also fails — and this is
   the decisive one. **The J-Link connect itself pulses reset through R439
   (defect D-2)**, so *connecting is a boot attempt*. The cascade always wins
   the race; there is no window in which this probe meets a live SMU on a blank
   board.

Repeated power cycles (four during the investigation) changed nothing except
the flavour of the failure.

## 4. What needs fixing

**The requirement**: `s32edbg flash program <blob> --offset 0` must work on a
board with blank flash. Everything else in the flash feature already works.

**The core question to answer first**: *can the SMU M33 subsystem be brought up
by the debugger, from a DEST0 board, the way `load` brings up RTU0?*

The load flow already does the analogous thing for the R52 side — MC_ME/RGM
partition enable, clock gating, core address, reset release, all from the debug
port (`s32edbg/data/rtuboot.py`, `dap/loader.py` P3/P5). If an equivalent
sequence exists for M33 partition 0 / the SMU core, the flash flow can perform
it whenever the SMU is unreachable, and the virgin board becomes recoverable.

What this repo already has:

- `qspiflash.ACTIONS_M33_CLOCK` — the M33 subsystem **clock mux** only
  (`MC_CGM2_MUX0_CSC = 0x0F000004` at `0x41030300`, plus its CSS poll),
  transcribed from the reference's `config_m33`. On the dead board that
  register reads `0x0F000000`, i.e. **no clock source selected** — consistent
  with a subsystem that is not running.
- No MC_ME partition-0 / SMU-core release sequence anywhere. It is not in
  `rtuboot.py` (RTU partitions only), not in `qspiflash.py`, and not in the
  s32multitool scripts this feature was ported from.

**Do not invent that sequence.** The repo rule is that run-control and bring-up
behaviour comes from the designated sources, and gaps go back to the user. This
one is a genuine gap: ask NXP, or find it in S32DS's own init scripts / the
reference GUI's virgin-board path (if it has one — worth checking whether
s32multitool can program a blank board at all, or whether it also assumes a
booting BootROM).

Two experiments to run first, in order, on a blank board:

1. Write `MC_CGM2_MUX0_CSC = 0x0F000004` via the AXI-AP (`mem write
   0x41030300 0x0F000004 --ap axi --yes`), then `m33 list`. If the SMU AP wakes
   on the clock alone, the fix is small: move `controller_init` ahead of the
   SMU requirement in `_flash_bringup` and re-probe. *This experiment was
   prepared but not executed — it needs a Bash permission grant.*
2. If the clock alone is not enough, the missing piece is the partition/core
   release, i.e. the question above.

**Whatever the answer, one behaviour should change regardless**: the flash flow
currently demands the SMU *before* it does anything else (the post-reset
`halt_m33s` in `cli._flash_bringup`, which is fatal). Bringing the controller up
first and requiring the SMU only when an applet call is actually needed would
let `flash jedec`, `flash read` and `flash verify` work on a blank board — all
three ride the memory-mapped AHB window and the host-driven IP commands, not
the applet. Only `program` and range `erase` truly need the core. That is a
worthwhile, self-contained improvement even if the SMU bring-up never lands.

## 5. Recovering the bench board right now

The board is blank. To make it bootable again, use a route that does not depend
on a live SMU:

- **s32multitool or S32DS** — whatever originally programmed this board;
- **the bootloader's UDS reflash path** — needs a running bootloader, so not
  applicable here;
- **the hardware serial-boot strap** — `docs/virgin-board.md`: let FREC reach 8
  so the part enters Fail-Safe serial boot and stops looping; BOOTMOD[1:0] is
  strapped `0b00` with TP40/TP41 available.

Once a bootloader is back at offset `0x0`, everything in this repo works again,
including the rest of the flash-state matrix (S2-S5), which is blocked on
exactly this.

To finish restoring a working system afterwards, follow
`docs/app-boot-contract.md` §4: BLOB at `0x0` → app at `--area app` → the
app-valid flag `0x55AA55AA` (flash bytes `AA 55 AA 55`) at `0x100000`, **flag
last** → power cycle.

## 6. Guard rails already in place

- `flash erase --all` now refuses to be run casually: its confirmation states
  that the tool cannot put the bootloader back, and names the recovery routes.
- README's `flash` section carries the same warning.
- `docs/test-plan.md` carries Finding 14 (this defect) and Finding 13 (the
  related cascade race: on a blank board the escalating cascade also disturbs
  `load`'s bring-up — `attach` first, or start within seconds of a POR).

## 7. Pointers

| what | where |
|---|---|
| flash flow | `s32edbg/dap/flasher.py`, bring-up in `s32edbg/cli.py` (`_flash_bringup`) |
| vendored tables + applet contract | `s32edbg/data/qspiflash.py`, `s32edbg/data/bootcfg_raw_qspi_m33_applet.bin` |
| R52 bring-up to imitate | `s32edbg/data/rtuboot.py`, `s32edbg/dap/loader.py` |
| the boot contract | `docs/app-boot-contract.md` |
| the cascade and its escapes | `docs/virgin-board.md` |
| findings 10-14 | `docs/test-plan.md` |
| bootloader sources | `C:\s32e2_bootloader` (M33; BL image at flash `0x2000` → SMU SRAM) |
| flash-route reference | `C:\s32multitool` |

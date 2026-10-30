# Handover: `s32edbg load` fails on a board that boots its own application (br1_cabin C sample)

Written 2026-10-29 for the agent that will fix the loader. Everything below was measured on the bench that day
with `s32edbg` from this repository (V13 probe, J-Link DLL, `--speed 16000`), on the br1_cabin C-sample board
(S32E278) that carries the M33 bootloader and a running cabin application in QSPI flash. Nothing in the tool was
changed; the proposed patch at the end is untested.

## 1. What is needed

`s32edbg debug -c "load C:\br1_cabin_ecu\build\bin\br1_cabin.elf" -c "resume"` must put a freshly built
application into RTU0 RAM and run it, on a board that is *already booting its flashed application* (bootloader +
app present, HSE provisioned). This is the fast build-measure loop of the CDD_AIN optimisation campaign
(`C:\br1_cabin_ecu\Tests\XCP\AIN_CDD\load_target.py` drives it). Today the documented flow only works on a
blank-flash board (the state the rear-ECU bench had, `docs\app-boot-contract.md` §7).

## 2. Symptoms, in the order they were hit

### 2.1 Fresh power cycle: gate closed (expected, documented)

```
! no Cortex-R52 debug frames answered. The debug clocks are off by default (RM 135.11) -- run `s32edbg gates apply` first.
```
Fixed by `-c "gates apply"` as the first session command. Not a bug.

### 2.2 With the gate open: the load's functional reset escalates to destructive

Session `gates apply` -> `load` -> `resume`:
```
debug-enable gate is open (sticky until power loss)
load: fresh probe connection for this load (Finding 9: ...)
load: smu halted / ce0 halted / ce1 halted
load: asserting sda reset and latching EDBGREQ for every R52
load: functional reset released; both reset flags read clear
load: MC_ME/RGM partition bring-up (RTU0, RTU1) + NXP generic SRAM scrubs
load: SRAMC D0/D1/D2 ECC init verified (witness words read 0)
load: parking rtu0.0 on a `b .` loop and releasing it into the halt
load: RTU0 GPR CFG_CORE: split-lock ... skipped -- XRDC denies the debugger 0x76120000 ...
error: no Cortex-R52 debug frames answered. ...
```
A following `gates status` (new invocation) read **every MDM-AP register as zero** (`CONTROL 0x00000000`,
`DBGENCNTRL 0x00000000`, gate "gated") and the board had rebooted from flash. This is **Finding 18** applied to
`load`: the running application's NXP RTD `Mcu_Init` arms `MC_RGM.FRENTC = FRET_EN(1) | FRET_TIMEOUT(0)`
(`0x41850028` read `0x00000001`), so the SDA functional reset of P1/P2 escalates to a destructive reset. The
loader survives the first ~200 ms (the AXI-AP still answers for P3/P4) and then loses the debug port.

### 2.3 FRENTC disarmed: the reset stays functional, but the MDM-AP comes back cleared

Session `gates apply` -> `mem read 0x41850028 --ap axi` -> `mem write 0x41850028 0x0 --ap axi --yes` ->
`mem read` -> `load` -> `resume`:
```
0x41850028  0x00000001
0x41850028  wrote 0x00000000, reads back 0x00000000
load: asserting sda reset and latching EDBGREQ for every R52
load: functional reset released; both reset flags read clear
load: MC_ME/RGM partition bring-up (RTU0, RTU1) + NXP generic SRAM scrubs
load: SRAMC D0/D1/D2 ECC init verified (witness words read 0)
load: parking rtu0.0 on a `b .` loop and releasing it into the halt
error: rtu0.0 did not halt within 2.0s (EDSCR 0x00000000, EDPRSR 0x00000000).
```
`EDSCR`/`EDPRSR` reading `0x00000000` means the R52 debug frame does not answer at all (not "running"). A
`gates status` right after the session again showed `CONTROL 0x00000000`, `DBGENCNTRL 0x00000000`,
`Debug-enable gate: gated`, `APPDBGEN yes`, `functional_reset_asserted no`, and the ECU was running its flashed
application again (CAN alive within 16 ms).

So on this board **the SDA functional reset of P1/P2 clears the MDM-AP enables and latches** (`CONTROL`,
`CONTROL2/3` = EDBGREQ, `DBGENCNTRL*`), although guide §3.6 / RM 135.11 document them as surviving a functional
reset. Consequences inside `load_image()` (`s32edbg/dap/loader.py`):

- the LADDER-1 gate is closed again -> the APBIC2 debug frames answer nothing -> P5 `wait_halted` fails;
- the EDBGREQ latches armed in P1 (`gates.arm_edbgreq`) are gone -> even with the gate re-opened, rtu0.0 would
  not be caught at its first instruction;
- the M33 halts of P0 (`halt_m33s`, held via `MDM_CONTROL_LOAD_ASSERT`) are gone -> the SMU runs the BootROM and
  the bootloader after the reset, which copies the *flashed* application into `0x32100000..` and starts rtu0.0
  itself (`docs\app-boot-contract.md` §3) -- a race against P5-P8.

Whether the clearing is done by the HSE/bootloader re-running after the reset or by the reset itself reaching the
SBC/PMIC through R439 (D-2 path, pad-driven) was not determined. A quick way to tell: `attach --hold` (gate
applied while held in functional reset) -> `release` -> `gates status`; if the gate survives a plain release, the
clearing happens during the boot that follows the release.

### 2.4 Reference behaviour that does work

- `gates apply` -> `core list` in one session: `session open -- 4 R52 core(s)`, `rtu0.0: running (non-debug
  state)`, rtu0.1 OS-locked, rtu1.x held in reset. Core discovery and the gate are fine while the application runs.
- `romdump --source apb` with the gate open enumerates the APBIC2 tree (74 components).
- `flash program <elf> --area app --reset none --yes` is the route used instead for now (Finding 18 recipe,
  then a power cycle by the operator). It costs a power cycle per iteration.

## 3. Proposed fix (untested, written against `s32edbg/dap/loader.py` as of commit ee9972e)

Goal: make `load` work when the board comes out of the P1/P2 reset with the MDM-AP cleared, without changing
behaviour on a board that keeps its enables.

1. **Re-gate and re-latch right after the reset is released (new step P2b).** After
   `wait_reset_deasserted(...)` in `load_image()`:

   ```python
   def _regate_after_reset(dap, hooks: LoadHooks) -> None:
       """P2b: re-open the gate and re-latch EDBGREQ if the reset cleared the MDM-AP (see docs/handover-cabin-ram-load.md)."""
       report = gates.status(dap)
       control2 = report.mdm.registers.get("CONTROL2") or 0
       if report.mdm.enables_set and control2 != 0:
           return                      # enables survived: nothing to do, no note
       hooks.note("the reset cleared the MDM-AP debug enables (DBGENCNTRL/CONTROL read 0) -- "
                  "re-applying LADDER-1 and re-latching EDBGREQ before the cores leave reset")
       report = gates.apply_ladder1(dap)
       for w in report.warnings:
           hooks.note(w)
       gates.arm_edbgreq(dap)          # CONTROL = MDM_CONTROL_LOAD_ASSERT, CONTROL2/3 = EDBGREQ for all R52
   ```
   Call it between `wait_reset_deasserted(...)` and `hooks.on_system_reset()`. The R52s are still in reset at
   that point (partitions are released in P3, core0 in P5), so the re-armed latches catch them as in the
   blank-board flow. `gates.apply_ladder1` and `gates.arm_edbgreq` are idempotent.

2. **Re-halt the M33s after the reset** (`loader_mod.halt_m33s(dap, note, smu_fatal=False)` is done in P0 from
   `cli.py` `load_cmd`; after the reset their halt is gone on this board). Either call `halt_m33s` again from P2b
   (it lives in `loader.py`, so it is reachable), or latch them through `MDM_CONTROL_LOAD_ASSERT` (its "M33
   request bits") before they leave reset -- check which of the two the M33 reset-exit actually honours. Without
   this the bootloader races the load (section 2.3, third bullet).

3. **Disarm `MC_RGM.FRENTC` before the reset** so the functional reset cannot escalate on a running
   application: read `0x41850028` over the AXI-AP; if `FRET_EN` is set, write `0x00000000` and note it. Today
   the caller does this with `mem write 0x41850028 0x0 --ap axi --yes` before `load`; moving it into the loader
   (or a `load --disarm-frentc` flag that defaults on when the SoC is running) removes the trap for the next
   user. The AXI write is only possible while the system is running (the AXI refusal during reset is right).

4. **Verify** on the cabin board:
   ```bat
   s32edbg --speed 16000 debug -c "gates apply" -c "load C:\br1_cabin_ecu\build\bin\br1_cabin.elf" -c "resume"
   ```
   Expected: `load: parking rtu0.0 ...` then the segment writes (`load: wrote 0x3210C580 ...`), `rtu0.0 halted at
   the entry  PC 0x32100020`, `focus: rtu0.0 ...`, then `rtu0.0 running` after `resume`; the ECU must come up on
   CAN within ~10 s (`CBCU_APS_FB`, id 0x0DE, 10 ms cyclic on BR1_Backbone; `python
   C:\br1_cabin_ecu\Tests\XCP\AIN_CDD\ProfilingAin.py <rct_port> --auto "alive"` checks it), and the image must be
   the loaded one, not the flashed one (e.g. the `veritas` commit-hash word in `SW_Version.c`, or any XCP read of
   a symbol that only the new build has). Then repeat the load twice in a row (back-to-back loads reopen the probe
   connection). Also re-run the existing blank-board flow or the state-matrix battery to show nothing regressed:
   the new step prints nothing when the enables survived.

5. Record it as a numbered Finding in `docs\test-plan.md` and in the README `load` section ("app-running boards:
   the P1/P2 reset clears the MDM-AP; P2b re-gates"), bench artefacts under `bench-runs\<date>-load-cabin\`.

## 4. Facts that may save time

- `gates status` after any failed load reads the MDM-AP as all zeros **including `IDR 0x00000000` in one case**
  (after a `romdump --source apb` in the same session) -- that instance was probably the APBIC bus hang RM 135.11
  warns about, not evidence for the reset theory; the other readings had `IDR 0x001DE040`.
- The SDA-AP always shows `AUTHSTTS 0x40000005` (`APPDBGEN yes`, `CHALRDY yes`) on this board, identical to the
  rear bench; the tool must never answer the challenge (one-shot, README §"Four hazards").
- The ELF: 13 file-backed PT_LOAD segments, 3,238,237 bytes, entry `0x32100020` (Thumb), all inside the CodeRAM
  window; `check_windows` accepts it. Zero-`filesz` segments at `0x30000000` (TCM stacks) are correctly skipped.
- Every one-shot `s32edbg` invocation while the gate is closed pulses reset through R439 (D-2) and reboots the
  board into the flashed application; with the gate open and the app running, connects identify the cores and do
  not pulse. The board recovers from every failure above by itself (bootloader boot, CAN alive in <100 ms).
- Bench: the J-Link is the only probe; a Vector VN1610 and the Rimac Calibration Tool hold the CAN/XCP side. Do not
  run `s32edbg` while a profiling measurement is being recorded on the board (a connect reset ends it).
- The operator's tool environment sets `NoDefaultCurrentDirectoryInExePath=1`; `s32edbg` is on PATH so it is
  unaffected, but batch files called by bare name are.

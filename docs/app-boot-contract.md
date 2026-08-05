# Booting the application from flash: the bootloader's contract, and how to satisfy it over the probe

What must be in external QSPI flash — and where — for the M33 bootloader to load and
start the R52 application, and the exact `s32edbg flash` procedure that puts it there so a
probe-programmed board boots identically to a UDS-flashed one.

**Source of truth:** the bootloader sources at `C:\s32e2_bootloader` (branch
`development_C_sample`, v3.3.0), read 2026-08-05. Every offset and value below carries its
citation. The flash tooling side is `README.md` § `flash`; the silicon findings behind the
commands are `docs/test-plan.md` ("The `flash` flow", Findings 10-12).

---

## 1. The boot chain, and where flash content enters it

1. **BootROM** reads the IVT at flash offset `0x0`, runs the self-test DCD it points at,
   and loads the **bootloader image** (flash `0x2000`, up to `0xFC000` bytes) into M33
   SRAM at `0x25000000`, entry `0x250C0800` — the bootloader runs from SRAM on the SMU
   Cortex-M33, never XIP (`mdl_bl.h:39-41`, BL header at `mdl_bl.c:403`).
2. The bootloader initialises clocks, flash, SBC/PMIC, HSE (`MDL_BL_init`,
   `mdl_bl.c:484-767`), then reads **4 bytes at flash `0x100000`**. Only if they equal
   **`0x55AA55AA`** does it proceed to the app; otherwise it stays in its UDS superloop
   and, after 10 idle minutes, powers the board off through the SBC regulators
   (`mdl_bl.c:89, 96, 987-999, 1012-1047`).
3. It then copies **flash `0x300000` → RAM `0x32100000`, the full `0x600000` (6 MiB), by
   eDMA in 64-byte bursts** (`MDL_BL_loadApp`, `mdl_bl.c:1901-1941`) — blindly, no CRC,
   the whole area regardless of image size (see §5).
4. It deinitialises itself, opens XRDC wide, and starts **RTU0 R52 core 0 at the
   hardcoded entry `0x32100020`** via MC_ME/RGM (`mdl_bl.h:44`,
   `mdl_bl_specific.c:84-243`). The M33 then spins forever (`main.c:68-71`).

So the *flash-content* contract is exactly three things the operator controls — the
bootloader BLOB, the app image, and the app-valid flag — plus a few areas the bootloader
and HSE manage themselves.

## 2. The flash-content contract

| offset | size | content | who writes it |
|---|---|---|---|
| `0x000000` | 2 MiB | **Bootloader BLOB**: IVT (byte 0 = `0xD1`) + QSPI config blob at `0x200` + BL header/image/signature + HSE firmware | operator (`flash program` at 0) |
| `0x100000` | 4 B | **App-valid flag `0x55AA55AA`** — flash bytes `AA 55 AA 55` (little-endian) | operator, **last** (§4) |
| `0x101000` | — | CAN-ID backup block | bootloader-managed; blank = first-boot state |
| `0x1DC000` / `0x1FC000` | 16 KiB ea. | Self-test DCD main/backup | bootloader self-provisions on first boot and patches the IVT pointers (`mdl_bl.c:716-758`) |
| `0x1E0000` | 128 KiB | HSE SYS-IMG (IVT `hse_fw_config`) | HSE-managed |
| `0x300000` | ≤ 6 MiB | **Application image**, laid out so flash `0x300000 + n` = RAM `0x32100000 + n` | operator (`flash program --area app`) |
| `0x301300` | — | app shared data / CAN IDs (`BL_R52_0_FLASH_SHARED_ADDR`, `mdl_bl.h:47`) — *inside* the app image; the flattened ELF already carries it | comes with the app image |
| `0x900000` | — | App signature (`BL_FLASH_APP_SIGN_ADDR`, `mdl_bl.h:132`) | required **only with secure boot active** (§5) |

Note: this bootloader knows nothing at `0xA00000` — the `--area cert` preset comes from
s32multitool's `flash_address_sets.json` and is not part of this boot contract.

## 3. The app image: flatten alignment is load-bearing

`s32edbg flash program <app.elf> --area app` flattens the ELF LMA-relative from its
**lowest LMA** and writes that flat image verbatim at `0x300000`. The bootloader's DMA
copy maps flash `0x300000 + n` → RAM `0x32100000 + n`, and it starts the core at the
**fixed** address `0x32100020`. Two consequences:

- **The flatten base must be exactly `0x32100000`.** The CLI prints it
  (`flattened LMA-relative: base 0x32100000`) — if a build ever moves its lowest LMA, the
  whole image lands shifted in the copy and the fixed entry points at garbage. Check that
  line; do not proceed past a different base.
- **The image must contain valid code at LMA `0x32100020`** (br1_rear does: its entry is
  `0x32100021`, Thumb). The ELF `e_entry` is irrelevant to the bootloader — only
  `0x32100020` matters.

br1_rear.elf currently flattens to a 6,078,364-byte span (fits the 6 MiB area, pinned by
`tests/test_flash.py`), of which only ~3.5 MiB programs — the 0xFF gap pages are skipped,
and erased flash reads 0xFF, so the bootloader's blind 6 MiB copy sees an identical image
either way.

## 4. The probe procedure

Order matters: **the flag goes last.** It is the commit record — the bootloader boots
whatever `0x300000` holds the moment the flag reads `0x55AA55AA`, so a half-programmed
app must never coexist with a valid flag. (This mirrors the UDS reflash flow, which sets
the flag only after a successful transfer.)

```bat
:: 0. once per file: the 4-byte flag, little-endian 0x55AA55AA
powershell -Command "[IO.File]::WriteAllBytes('appflag.bin', [byte[]](0xAA,0x55,0xAA,0x55))"

:: 1. bootloader BLOB at offset 0 (byte 0 must be 0xD1; the tool checks and reports)
s32edbg --speed 16000 flash program "C:\Users\viktor.marhansky\Downloads\s32e2xx_bootloader_M33_v3.1.0_BLOB 1.bin" --offset 0x0 --ap axi --yes

:: 2. application into the app area (watch for "base 0x32100000" in the flatten note)
s32edbg --speed 16000 flash program C:\br1_rear_ecu\build\bin\br1_rear.elf --area app --ap axi --yes

:: 3. only after 1+2 verified: the app-valid flag
s32edbg --speed 16000 flash program appflag.bin --offset 0x100000 --ap axi --yes

:: 4. power-cycle the board -- a debug reset is not the cold boot the BootROM needs
```

Details worth knowing:

- Every `flash program` erases before writing and full-verifies by default; step 3's
  erase covers exactly the `0x100000-0x100FFF` sector, so the CAN-ID backup at `0x101000`
  is untouched.
- Byte order: the value is compared as a little-endian `uint32` on the M33, so the file
  bytes are `AA 55 AA 55`. Writing `55 AA 55 AA` produces a board that never boots the
  app and powers itself off after 10 minutes — with no error anywhere.
- `--ap axi` selects the AP routing on which the fast ReadMem verify path has worked
  (test-plan Finding 12); if it demotes, verify still completes on ADIv6, just slower.
- To *decommission* an app without erasing it, erase the flag sector alone:
  `s32edbg flash erase --offset 0x100000 --length 0x1000 --yes`.

## 5. What the bootloader checks — and does not

- **Non-secure path (this board today):** secure boot is inactive
  (`MDL_DBG_BL_RECOVERY_BOOT = STD_ON`, `mdl_dbg.h:37`), so the app is started purely on
  the flag — the DMA copy has **no CRC and no signature check** (`mdl_bl.c:1912-1917`).
  The byte-exact `flash verify` at programming time is therefore the *only* integrity
  check the app ever gets. Do not skip it.
- **Secure path:** with HSE secure boot active, the bootloader instead asks HSE to verify
  SMR entry 1 and perform the load (`mdl_bl.c:1908`, `mdl_hse.c:165-189`), which brings
  the signature at `0x900000` into the contract. Probe-programming a secure-boot board
  additionally requires that signature to be valid — out of scope here.
- The bootloader copies all 6 MiB regardless of image size, so RAM beyond the image
  (`0x32100000 + span` up to `0x32700000`) holds `0xFF` after a boot from freshly-erased
  flash. A debugger RAM-`load` leaves those bytes as the ECC init left them instead —
  an app that reads memory it never initialised can behave differently between the two
  paths. That is an app bug, not a tooling difference, but it is *visible* here.

## 6. What must NOT be mimicked in flash

Everything else the bootloader does is **runtime work it repeats on every boot** — none
of it is flash content, and the debugger's RAM-`load` flow performs its own equivalent:

| bootloader (every boot) | debugger RAM-load equivalent |
|---|---|
| PLL lock + `Mcu_SetMode` partition clocks (`mdl_bl.c:489-502`) | `load` P3 MC_ME/RGM tables |
| RTU un-fence `GPR3→RTU0FDC`, RDC interconnect enable (`mdl_bl_specific.c:207-211`) | `load` P3 bring-up tables |
| 6 MiB DMA copy = implicit RAM ECC init | `load` P4 SRAMC ECC ladder + witness check |
| MC_ME core addr `0x32100020` + RGM release (`mdl_bl_specific.c:98-136`) | `load` P5/P8 park + PC write |
| XRDC opened wide before app start (`mdl_bl.c:810`, `mdl_xrdc.c:59-139`) | left at reset defaults — the app runs regardless (bench-proven 2026-08-05) |
| SBC FS86 INIT_FS: watchdog **disabled**, faults **no-action** (`Sbc_Fs86_Ip_E2_ECU_PBcfg.c:177-213`) | nothing — FS86 runs on OTP defaults; benign on this board in practice, and the first suspect if long bootloader-less sessions ever reset spontaneously |
| SMU SWT disable (`startup.s:236-245`), pin mux incl. CAN_EN/STB (`mdl_bl.c:520-526`), QSPI OPI→SPI handover (`mdl_bl.c:779-784`) | not needed / covered by the app's own init |

## 7. Current board state (2026-08-05)

The flash was chip-erased in full (`bench-runs/20260805-flash/11-erase-all.json`): no
bootloader, no flag, no app in flash. The board boots nothing on its own; RAM-`load`
debugging works as described in `README.md` § `load`. Running the §4 procedure end to end
restores normal boot-from-flash.

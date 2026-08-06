<#
.SYNOPSIS
    Proves debugging works in every flash-content state (test-plan: "The
    flash-state × debugging matrix").

.DESCRIPTION
    Five states, from docs/app-boot-contract.md:

      S1  nothing (chip-erased)      -> BootROM fail cascade; SMU AP dead at attach
                                        (flash ops still work: they boot the SMU
                                        themselves, test-plan Finding 15)
      S2  bootloader only            -> BL superloop on the SMU; R52s never released
      S3  bootloader + app, no flag  -> same as S2 (the flag gates the app start)
      S4  bootloader + app + flag    -> normal boot: the app runs on rtu0.0 + rtu0.1
      S5  app only (no BL)           -> same as S1 (nothing reads flash without the BL)

    For each state the script programs the flash into that state (incremental:
    the default full run goes S1 -> S5 in order; a subset run assumes the flash
    already holds the preceding state's content), prompts for a power cycle
    (the clean boot the BootROM needs -- a debug reset is not equivalent), and
    runs the same debug battery: attach/connect, gates + apmap, R52 and M33
    discovery, a debug-session run-control pass sized to what that state can
    run, `flash jedec`, and a full RAM `load --verify` last -- observation
    before perturbation, because jedec and load quiesce-reset the SoC and
    replace the state's runtime.

    Cells whose expected values have not been measured yet (per-state core
    states in S1/S2/S3/S5) run in record mode: the JSON artifacts are the
    record, and the expectations get pinned into this script's -Check blocks
    by a follow-up commit after the first full run (test-plan:
    record-then-pin).

    S2/S3 deadline: with no bootable app the BL powers the board off through
    the SBC after 10 idle minutes. The observation phase of those states must
    finish inside the window; the script timestamps the power cycle and warns
    past 8 minutes. (Any reset-quiescing step reboots the BL and re-arms the
    timer.)

.PARAMETER States
    Which states to run, in order. Default: all five, S1..S5.

.PARAMETER BlobPath
    The bootloader BLOB (byte 0 = 0xD1) for S2..S4.

.PARAMETER ElfPath
    The application ELF for S3..S5 setup and every state's `load` step.

.EXAMPLE
    .\tools\flash-state-matrix.ps1
    .\tools\flash-state-matrix.ps1 -Yes -States S1,S2
    .\tools\flash-state-matrix.ps1 -BlobPath C:\blobs\bl.bin -OutDir C:\temp\matrix
#>

[CmdletBinding()]
param(
    [ValidateSet('S1', 'S2', 'S3', 'S4', 'S5')]
    [string[]] $States = @('S1', 'S2', 'S3', 'S4', 'S5'),
    [string] $BlobPath = "C:\Users\viktor.marhansky\Downloads\s32e2xx_bootloader_M33_v3.1.0_BLOB 1.bin",
    [string] $ElfPath = "C:\br1_rear_ecu\build\bin\br1_rear.elf",
    [string] $OutDir,
    [switch] $Yes
)

$ErrorActionPreference = 'Stop'
$SkipStateChanging = $false  # referenced by Invoke-Step; nothing here is skippable

if (-not $OutDir) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutDir = Join-Path (Get-Location) "bench-runs\flashmatrix-$stamp"
}
$MatrixRoot = $OutDir
New-Item -ItemType Directory -Force -Path $MatrixRoot | Out-Null

$transcript = Join-Path $MatrixRoot 'console.txt'
$results = [System.Collections.Generic.List[object]]::new()
$stepNumber = 0

. (Join-Path $PSScriptRoot 'bench-lib.ps1')

$SPEED = @('--speed', '16000')
$script:cycleAt = $null

function Wait-PowerCycle {
    param([string] $Why)
    Write-Host ""
    Write-Host "POWER-CYCLE THE BOARD NOW: $Why" -ForegroundColor Yellow
    Write-Host "(a debug reset is not the cold boot the BootROM needs -- app-boot-contract.md)" -ForegroundColor Yellow
    Read-Host "  press Enter when the board is back on"
    $script:cycleAt = Get-Date
}

function Note-ObservationWindow {
    param([string] $State)
    if ($null -eq $script:cycleAt) { return }
    $elapsed = (Get-Date) - $script:cycleAt
    Write-Host ("  observation phase: {0:mm\:ss} since the power cycle" -f $elapsed) -ForegroundColor DarkGray
    if (($State -in @('S2', 'S3')) -and $elapsed.TotalMinutes -gt 8) {
        Write-Host "  WARNING: past 8 of the BL's 10 idle minutes -- the SBC power-off is close" -ForegroundColor Yellow
    }
}

function Set-FlashState {
    param([string] $State)
    switch ($State) {
        'S1' {
            Invoke-Step -Name "$State-setup-erase-all" -ChangesTargetState `
                -Arguments ($SPEED + @('flash', 'erase', '--all', '--yes', '--ap', 'axi')) `
                -Expect 'ERASES THE ENTIRE FLASH (bootloader included); ~1-2 min' `
                -Check { param($p) $p.erase.bytes -eq 33554432 }
        }
        'S2' {
            if (-not (Test-Path $BlobPath)) { throw "S2 needs -BlobPath; '$BlobPath' does not exist" }
            Invoke-Step -Name "$State-setup-program-blob" -ChangesTargetState `
                -Arguments ($SPEED + @('flash', 'program', $BlobPath, '--offset', '0x0', '--ap', 'axi', '--yes')) `
                -Expect 'programs the bootloader BLOB at offset 0 (byte 0 = 0xD1), full verify' `
                -Check { param($p) ($p.offset -eq '0x00000000') -and ($p.verify -eq 'full') }
        }
        'S3' {
            if (-not (Test-Path $ElfPath)) { throw "S3 needs -ElfPath; '$ElfPath' does not exist" }
            Invoke-Step -Name "$State-setup-program-app" -ChangesTargetState `
                -Arguments ($SPEED + @('flash', 'program', $ElfPath, '--area', 'app', '--ap', 'axi', '--yes')) `
                -Expect 'flattens the ELF (base must be 0x32100000) into the app area, full verify' `
                -Check { param($p) ($p.offset -eq '0x00300000') -and ($p.program.bytes -gt 0) -and ($p.verify -eq 'full') }
        }
        'S4' {
            $flag = Join-Path $MatrixRoot 'appflag.bin'
            # 0x55AA55AA little-endian: flash bytes AA 55 AA 55. Backwards =
            # a board that never boots the app and powers off after 10 min.
            [IO.File]::WriteAllBytes($flag, [byte[]](0xAA, 0x55, 0xAA, 0x55))
            Invoke-Step -Name "$State-setup-program-appflag" -ChangesTargetState `
                -Arguments ($SPEED + @('flash', 'program', $flag, '--offset', '0x100000', '--ap', 'axi', '--yes')) `
                -Expect 'programs the app-valid flag 0x55AA55AA at 0x100000 (the boot commit)' `
                -Check { param($p) ($p.offset -eq '0x00100000') -and ($p.verify -eq 'full') }
        }
        'S5' {
            # The blob region (0x0..0x200000) contains BL + flag + the
            # BL-managed areas; one erase removes them all, the app survives.
            Invoke-Step -Name "$State-setup-erase-blob-region" -ChangesTargetState `
                -Arguments ($SPEED + @('flash', 'erase', '--offset', '0x0', '--length', '0x200000', '--ap', 'axi', '--yes')) `
                -Expect 'ERASES the bootloader + flag (0x0..0x200000); the app area stays' `
                -Check { param($p) $p.erase.bytes -eq 2097152 }
        }
    }
}

function Invoke-StateBattery {
    param([string] $State)
    $cascade = $State -in @('S1', 'S5')   # no bootloader: BootROM fail cascade
    $blOnly  = $State -in @('S2', 'S3')   # BL superloop; R52s never released

    # -- 1. entry: connect (or attach on a cascade board) --------------------
    if ($cascade) {
        Invoke-Step -Name "$State-connect" -Arguments @('connect') -AllowFailure `
            -Expect 'cascade board: a plain connect may fail; attach below is the entry'
        Invoke-Step -Name "$State-attach" -ChangesTargetState `
            -Arguments @('attach') `
            -Expect 'holds functional reset -- the virgin-board foothold; battery runs under the hold'
    } else {
        Invoke-Step -Name "$State-connect" -Arguments @('connect') `
            -Expect 'DPIDR via the connect banner or an explicit unavailable note' `
            -Check {
                param($p)
                ($p.dpidr -eq '0x4C013477') -or
                (($null -eq $p.dpidr) -and ($p.ctrl_stat -ne '0x00000000'))
            }
    }

    # -- 2. gates + apmap ----------------------------------------------------
    Invoke-Step -Name "$State-gates-apply" -ChangesTargetState `
        -Arguments @('gates', 'apply') `
        -Expect 'debug enables set, exact read-back' `
        -Check { param($p) $p.mdm.enables_set -and ($p.mdm.registers.DBGENCNTRL -eq '0x3CF3CF00') }
    Invoke-Step -Name "$State-apmap" -Arguments @('apmap') `
        -Expect '9 APs, empty diff against RM Table 957' `
        -Check {
            param($p)
            ($p.aps.Count -eq 9) -and
            -not ($p.diff_against_rm.PSObject.Properties | Where-Object { $_.Value.Count -gt 0 })
        }

    # -- 3./4. discovery (record cells: per-state core states get pinned
    #          into these checks after the first full run) --------------------
    Invoke-Step -Name "$State-core-list" -Arguments @('core', 'list') -AllowFailure `
        -Expect "R52 discovery; per-state core states are RECORDED on this run ($State)"
    Invoke-Step -Name "$State-m33-list" -Arguments @('m33', 'list') -AllowFailure `
        -Expect 'M33 discovery; SMU running under the BL (S2-S4); dead APs at attach on the cascade (S1/S5 -- a flash op then boots the SMU itself, Finding 15)'

    # -- 5. run-control battery, one debug session ---------------------------
    if ($State -eq 'S4') {
        # The app is running on both cores; exercise the full per-core set on
        # each. 0x32100020 = the app entry in RAM -- always mapped once booted.
        Invoke-Step -Name "$State-session-r52" `
            -ChangesTargetState `
            -Arguments @(
                'debug', '--core', 'rtu0.0',
                '-c', 'halt', '-c', 'regs',
                '-c', 'bp set 0x32100020', '-c', 'bp list', '-c', 'bp clear --all',
                '-c', 'wp set 0x32101300 --size 4 --on store', '-c', 'wp clear --all',
                '-c', 'step', '-c', 'continue',
                '-c', 'use rtu0.1',
                '-c', 'halt', '-c', 'regs', '-c', 'step', '-c', 'continue'
            ) `
            -Expect 'halt/regs/bp/wp/step/continue on BOTH cores; restore-on-close resumes'
    } elseif ($blOnly) {
        # R52s are held in reset by design (the BL never releases them):
        # status must say so; the M33 battery is the live one. The BL disabled
        # the SBC watchdog (app-boot-contract §6), so halting the SMU is safe;
        # keep it short anyway.
        Invoke-Step -Name "$State-session-status" -AllowFailure `
            -Arguments @(
                'debug',
                '-c', 'status --core rtu0.0', '-c', 'status --core rtu0.1'
            ) `
            -Expect 'held-in-reset notes for both R52s (wording cell: recorded)'
        Invoke-Step -Name "$State-session-m33" `
            -ChangesTargetState `
            -Arguments @(
                'debug',
                '-c', 'm33 halt --core smu', '-c', 'm33 regs --core smu',
                '-c', 'm33 step --core smu', '-c', 'm33 resume --core smu'
            ) `
            -Expect 'full SMU battery against the running bootloader'
    } else {
        Invoke-Step -Name "$State-session-status" -AllowFailure `
            -Arguments @('debug', '-c', 'status --core rtu0.0', '-c', 'status --core rtu0.1') `
            -Expect 'cascade board: clean refusals/notes only, no hangs (recorded)'
    }

    # -- 6./7. the quiesce-resetting steps, last -----------------------------
    Note-ObservationWindow -State $State
    $jedecArgs = @('flash', 'jedec')
    if ($cascade) {
        # Finding 16: on a cascade/DEST0-recovered board the SMU AHB-AP's
        # SRAM view stays cold; the flash flow needs the AXI-AP (measured
        # 2026-08-06 -- the default AP fails the preflight RAM probe).
        $jedecArgs += @('--ap', 'axi')
    }
    Invoke-Step -Name "$State-flash-jedec" -ChangesTargetState `
        -Arguments ($SPEED + $jedecArgs) `
        -Expect 'JEDEC 0xC23989C2 in every state (boots the SMU itself, Finding 15; ends the observation phase)' `
        -Check { param($p) $p.jedec -eq '0xC23989C2' }
    Invoke-Step -Name "$State-load" -ChangesTargetState `
        -Arguments ($SPEED + @('load', $ElfPath, '--verify')) `
        -Expect 'RAM load end-to-end in this flash state; full verify; rtu0.0 parked at entry' `
        -Check {
            param($p)
            ($p.verify -eq 'full') -and
            ($p.segments.Count -gt 0) -and
            ($p.total_bytes -gt 0) -and
            ((Hex $p.pc) -eq ((Hex $p.entry) -band (-bnot [uint64]1)))
        }
}

Start-Transcript -Path $transcript | Out-Null
try {
    Write-Host "s32edbg flash-state x debugging matrix -- artifacts in $MatrixRoot"
    Write-Host "states: $($States -join ', ') (incremental setup: a subset assumes the"
    Write-Host "flash already holds the preceding state's content; the default full run"
    Write-Host "is self-contained)."
    Write-Host ""
    Write-Host "NOTE: every step opens its own probe connection and each connect pulses" -ForegroundColor Yellow
    Write-Host "reset into the SBC/PMIC (D-2). On S2/S3 the observation phase races the" -ForegroundColor Yellow
    Write-Host "bootloader's 10-minute idle power-off." -ForegroundColor Yellow

    foreach ($state in $States) {
        $stateDir = Join-Path $MatrixRoot $state
        New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
        $OutDir = $stateDir  # Invoke-Step writes its artifacts per state

        Write-Host ""
        Write-Host ("#" * 78) -ForegroundColor Magenta
        Write-Host "STATE $state" -ForegroundColor Magenta
        Write-Host ("#" * 78) -ForegroundColor Magenta

        Set-FlashState -State $state
        Wait-PowerCycle -Why "boot the board into state $state"
        Invoke-StateBattery -State $state

        $stateSummary = Join-Path $stateDir 'summary.json'
        $results | Where-Object { $_.Step -like "$state-*" } |
            ConvertTo-Json -Depth 4 | Out-File -FilePath $stateSummary -Encoding utf8
    }
}
finally {
    Stop-Transcript | Out-Null
    $OutDir = $MatrixRoot
    $summary = Join-Path $MatrixRoot 'summary.json'
    $results | ConvertTo-Json -Depth 4 | Out-File -FilePath $summary -Encoding utf8
    Write-Host ""
    Write-Host ("=" * 78)
    $results | Format-Table -AutoSize
    Write-Host "artifacts: $MatrixRoot"
    Write-Host ""
    Write-Host "The final state's flash content is whatever the last transition left;" -ForegroundColor Yellow
    Write-Host "a full S1..S5 run ends with app-only flash (no bootloader). Restore" -ForegroundColor Yellow
    Write-Host "normal boot per docs/app-boot-contract.md section 4." -ForegroundColor Yellow
}

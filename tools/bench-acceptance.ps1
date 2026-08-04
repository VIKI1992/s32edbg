<#
.SYNOPSIS
    Runs the Tier-2 hardware acceptance sequence from docs/test-plan.md.

.DESCRIPTION
    Executes each step in dependency order, writes a JSON artifact and a
    wire-level log per step into a timestamped folder, and stops at the first
    failure. The point is that a handover becomes "run this, send me the
    folder" instead of twenty commands transcribed by hand.

    Ordering is not cosmetic:
      * `gates apply` comes before anything touching APBIC2, because RM 135.11
        leaves the debug clocks off by default and warns that accessing debug
        components before they are enabled "might hang the debug APBIC bus".
      * The steps that need the application running come before `attach`,
        which stops it.
      * `iface swd` is not run at all. RM 135.5.1: "Switching back from SWD
        mode to JTAG mode requires POR." It stays a manual step.

.PARAMETER OutDir
    Where to write artifacts. Defaults to a timestamped folder under .\bench-runs.

.PARAMETER Yes
    Skip the confirmation before the steps that change target state.

.PARAMETER SkipStateChanging
    Run only the read-only steps. Useful for a first pass on a board whose
    application must keep running.

.EXAMPLE
    .\tools\bench-acceptance.ps1
    .\tools\bench-acceptance.ps1 -SkipStateChanging
    .\tools\bench-acceptance.ps1 -OutDir C:\temp\run1 -Yes
#>

[CmdletBinding()]
param(
    [string] $OutDir,
    [switch] $Yes,
    [switch] $SkipStateChanging,
    # Application ELF for the `load` (RAM-load) step. Omit to skip the step.
    # Expected on this bench: C:\br1_rear_ecu\build\bin\br1_rear.elf
    [string] $ElfPath
)

$ErrorActionPreference = 'Stop'

if (-not $OutDir) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutDir = Join-Path (Get-Location) "bench-runs\$stamp"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$transcript = Join-Path $OutDir 'console.txt'
$results = [System.Collections.Generic.List[object]]::new()
$stepNumber = 0

function Hex {
    <# The tool writes register values as "0x1234ABCD" strings in its JSON, and
       PowerShell will not cast those to a number. UInt64 rather than UInt32
       because a boundary-scan capture is wider than a register. #>
    param([string] $Value)
    # The parentheses matter: without them PowerShell hands ToUInt64 three
    # arguments (value, '', 16) and it fails to find an overload.
    return [Convert]::ToUInt64(($Value -replace '^0[xX]', ''), 16)
}

function Invoke-Step {
    <#
        $Check is a scriptblock receiving the parsed JSON artifact; return $true
        to pass. Exit code alone is NOT a sufficient criterion and assuming it
        was already let a wrong answer through once: `connect` on the dll
        backend exits 0 while reporting the *reserved* DPACC register as DPIDR
        (0x4BA07477 rather than 0x4C013477). A step with a known answer must
        assert on the answer.
    #>
    param(
        [Parameter(Mandatory)] [string]   $Name,
        [Parameter(Mandatory)] [string[]] $Arguments,
        [string]      $Expect,
        [scriptblock] $Check,
        [switch] $ChangesTargetState,
        [switch] $AllowFailure
    )

    $script:stepNumber++
    $id = '{0:d2}-{1}' -f $script:stepNumber, ($Name -replace '[^\w\-]', '-')

    if ($ChangesTargetState -and $SkipStateChanging) {
        Write-Host "SKIP  $Name (changes target state)" -ForegroundColor DarkGray
        $script:results.Add([pscustomobject]@{ Step = $Name; Result = 'skipped'; Exit = $null })
        return
    }
    if ($ChangesTargetState -and -not $Yes) {
        Write-Host ""
        Write-Host "NEXT STEP CHANGES TARGET STATE: $Name" -ForegroundColor Yellow
        Write-Host "  $Expect"
        $answer = Read-Host "  proceed? [y/N]"
        if ($answer -notmatch '^[Yy]') {
            Write-Host "SKIP  $Name (declined)" -ForegroundColor DarkGray
            $script:results.Add([pscustomobject]@{ Step = $Name; Result = 'declined'; Exit = $null })
            return
        }
    }

    $json = Join-Path $OutDir "$id.json"
    $log  = Join-Path $OutDir "$id.log"
    $full = @('--json', $json, '--log', $log) + $Arguments

    Write-Host ""
    Write-Host ("=" * 78)
    Write-Host "STEP $script:stepNumber  $Name" -ForegroundColor Cyan
    if ($Expect) { Write-Host "  expect: $Expect" -ForegroundColor DarkGray }
    Write-Host "  s32edbg $($full -join ' ')" -ForegroundColor DarkGray
    Write-Host ("=" * 78)

    & s32edbg @full
    $code = $LASTEXITCODE

    # 0 = ok, 2 = tool error, 3 = refused by safety policy, 130 = aborted.
    $verdict = if ($code -eq 0) { 'pass' } else { 'FAIL' }
    $note    = ''

    if ($code -eq 0 -and $Check) {
        $payload = $null
        if (Test-Path $json) {
            try { $payload = Get-Content $json -Raw | ConvertFrom-Json }
            catch { $note = "could not parse $id.json" }
        } else {
            $note = 'no JSON artifact written'
        }
        if ($payload) {
            try {
                if (-not (& $Check $payload)) { $verdict = 'FAIL'; $note = 'value check failed' }
            } catch {
                $verdict = 'FAIL'; $note = "value check errored: $_"
            }
        } elseif (-not $note) {
            $verdict = 'FAIL'; $note = 'no JSON to check'
        }
    }

    $colour = if ($verdict -eq 'pass') { 'Green' } else { 'Red' }
    Write-Host "  -> $verdict (exit $code)$(if ($note) { " -- $note" })" -ForegroundColor $colour
    $script:results.Add([pscustomobject]@{ Step = $Name; Result = $verdict; Exit = $code; Note = $note })

    if ($verdict -ne 'pass' -and -not $AllowFailure) {
        throw "step '$Name' failed (exit $code$(if ($note) { "; $note" })). Artifacts in $OutDir"
    }
}

Start-Transcript -Path $transcript | Out-Null
try {
    Write-Host "s32edbg bench acceptance -- artifacts in $OutDir"
    Write-Host "Preconditions: PMIC and SBC enable switches ON. A healthy 3.3 V VTREF"
    Write-Host "says nothing about the 0.8 V core rail (RM Table 295, Table 301)."
    Write-Host ""
    Write-Host "NOTE: every step is a separate s32edbg run, so every step opens a" -ForegroundColor Yellow
    Write-Host "J-Link session -- and on this board MCU_RESET_b is tied to SBC-PMIC_RSTb" -ForegroundColor Yellow
    Write-Host "through R439, so each one resets the SBC and PMIC. A failing DLL connect" -ForegroundColor Yellow
    Write-Host "additionally pulses reset itself ('Failed to identify target. Resetting" -ForegroundColor Yellow
    Write-Host "via Reset pin and trying again.'). Enough of those in a row and the TAP" -ForegroundColor Yellow
    Write-Host "stops answering entirely and needs a power cycle. If steps start failing" -ForegroundColor Yellow
    Write-Host "with CTRL/STAT=0x00000000, power-cycle before retrying rather than" -ForegroundColor Yellow
    Write-Host "re-running -- retrying makes it worse." -ForegroundColor Yellow

    # -- Tier 2a: read-only, application still running ----------------------
    Invoke-Step -Name 'installs' -Arguments @('installs') `
        -Expect 'at least one SEGGER install, with JLink.exe and JTAGLoad.exe'
    Invoke-Step -Name 'probe' -Arguments @('probe') `
        -Expect 'a serial number and VTREF around 3300 mV' `
        -Check { param($p) $p.vtref_mv -gt 2000 }
    Invoke-Step -Name 'doctor' -Arguments @('doctor') `
        -Expect 'IDCODE 0x0831F01D, JCOMP high, no blockers' `
        -Check { param($p) $p.chain.idcode -eq 0x0831F01D }
    Invoke-Step -Name 'connect-commander' -Arguments @('--transport', 'commander', 'connect') `
        -Expect 'DPIDR 0x4C013477 (DPv3 / ADIv6), or an explicit "DPIDR unavailable" note' `
        -Check {
            param($p)
            # The batch backend takes DPIDR from J-Link's connect banner, which
            # over JTAG is the only correct source: ADIv6 Figure A1-2 makes
            # DPACC address 0b00 Reserved. A null DPIDR with a live CTRL/STAT is
            # a correct answer; a wrong DPIDR is not.
            ($p.dpidr -eq '0x4C013477') -or
            (($null -eq $p.dpidr) -and ($p.ctrl_stat -ne '0x00000000'))
        }
    Invoke-Step -Name 'connect-dll' -Arguments @('connect') `
        -Expect 'KNOWN DEFECT: the dll backend reports the reserved DPACC register (0x4BA07477) as DPIDR' `
        -AllowFailure
    Invoke-Step -Name 'iface-show' -Arguments @('iface', 'show') `
        -Expect 'JTAG, proven' `
        -Check { param($p) $p.reported -eq 'jtag' }
    Invoke-Step -Name 'gates-status' -Arguments @('gates', 'status') `
        -Expect 'MDM-AP IDR 0x001DE040, SDA-AP IDR 0x001DE001' `
        -Check { param($p) ($p.mdm.idr -eq '0x001DE040') -and ($p.hse.idr -eq '0x001DE001') }

    # -- Tier 2b: opens the debug gate --------------------------------------
    Invoke-Step -Name 'gates-apply' -Arguments @('gates', 'apply') `
        -ChangesTargetState `
        -Expect 'writes four MDM-AP registers, sticky until power loss; gate reports open' `
        -Check { param($p) $p.mdm.enables_set -and ($p.mdm.registers.DBGENCNTRL -eq '0x3CF3CF00') }

    Invoke-Step -Name 'apmap' -Arguments @('apmap') `
        -Expect '9 APs, 0 missing / 0 unexpected / 0 mismatched against RM Table 957' `
        -Check {
            param($p)
            ($p.aps.Count -eq 9) -and
            -not ($p.diff_against_rm.PSObject.Properties | Where-Object { $_.Value.Count -gt 0 })
        }
    Invoke-Step -Name 'romdump' -Arguments @('romdump') `
        -Expect 'APB tree: at least 12 tables, 0 errors' `
        -Check { param($p) $p.apb.children.Count -gt 0 }
    Invoke-Step -Name 'mem-info-apb' -Arguments @('mem', 'info', '--ap', 'apb') `
        -Expect 'IDR 0x24770006, BASE 0x01BFE000' `
        -Check { param($p) $p.idr -eq '0x24770006' }
    Invoke-Step -Name 'mem-read-cidr' -Arguments @('mem', 'read', '0x00410FF0', '--ap', 'apb', '--count', '4') `
        -Expect 'CoreSight preamble 0x0D 0xE0 0x05 0xB1 -- a known answer by a new path' `
        -Check {
            param($p)
            # ADIv6 fixes CIDR0/2/3 for every CoreSight component, so this is an
            # oracle that does not depend on this silicon. CIDR1 carries the class.
            (((Hex $p.values[0]) -band 0xFF) -eq 0x0D) -and
            (((Hex $p.values[2]) -band 0xFF) -eq 0x05) -and
            (((Hex $p.values[3]) -band 0xFF) -eq 0xB1)
        }
    Invoke-Step -Name 'mem-dump-boundary' -Arguments @('mem', 'dump', '0x004103F0', '32', '--ap', 'apb') `
        -Expect 'straddles the 1 KB auto-increment boundary; cross-check against mem-read-boundary below'
    Invoke-Step -Name 'mem-read-boundary' -Arguments @('mem', 'read', '0x004103F0', '--ap', 'apb', '--count', '8') `
        -Expect 'must match mem-dump-boundary word for word (ADIv6 C2.6.4)' `
        -Check {
            param($p)
            # The failure this catches is silent. ADIv6 C2.6.4 guarantees TAR
            # auto-increment only inside a 1 KB window; without a rewrite at the
            # boundary the block read returns words from the *start* of the
            # window -- plausible values from the wrong addresses, no fault
            # raised. Only a comparison against word-at-a-time reads sees it.
            $dumpFile = Get-ChildItem $OutDir -Filter '*-mem-dump-boundary.json' | Select-Object -First 1
            if (-not $dumpFile) { return $false }
            $dump = Get-Content $dumpFile.FullName -Raw | ConvertFrom-Json
            $a = @($dump.words | ForEach-Object { Hex $_ })
            $b = @($p.values   | ForEach-Object { Hex $_ })
            ($a.Count -eq $b.Count) -and (-not (Compare-Object $a $b -SyncWindow 0))
        }

    # -- Tier 2c: stops the application -------------------------------------
    Invoke-Step -Name 'attach' -Arguments @('attach') `
        -ChangesTargetState `
        -Expect 'STOPS THE APPLICATION. Holds functional reset; the JTAGC survives (RM Table 301)'
    Invoke-Step -Name 'gates-status-held' -Arguments @('gates', 'status') `
        -Expect 'functional_reset_asserted = yes, destructive_reset_asserted = no' `
        -Check { param($p) $p.hse.reset_flags.functional_reset_asserted }
    Invoke-Step -Name 'release' -Arguments @('release') `
        -ChangesTargetState `
        -Expect 'RESTARTS THE APPLICATION; SDASYSRSTC[SYSFUNCREQ] clears'

    # -- Tier 2c2: the RAM-load flow ----------------------------------------
    #
    # After `release` (the board may boot; load resets it anyway) and before
    # bscan (ordering rule 3: nothing that needs the DAP may follow the
    # preamble). Resets the whole SoC, loads the ELF, parks rtu0.0 at its
    # entry, full-verifies every word.
    if ($ElfPath) {
        Invoke-Step -Name 'load' -Arguments @('load', $ElfPath, '--verify') `
            -ChangesTargetState `
            -Expect 'SRAMC witnesses 0; all segments written + verified; rtu0.0 halted at entry' `
            -Check {
                param($p)
                ($p.verify -eq 'full') -and
                ($p.segments.Count -gt 0) -and
                ($p.total_bytes -gt 0) -and
                ((Hex $p.pc) -eq ((Hex $p.entry) -band (-bnot [uint64]1)))
            }
    } else {
        Write-Host "  (load step skipped: pass -ElfPath <app.elf> to run it)" -ForegroundColor DarkGray
    }

    # -- Tier 2d: the whole-tool artifact -----------------------------------
    Invoke-Step -Name 'report' -Arguments @('report') `
        -Expect 'every section present: probe, tap, dap, gates, apmap, romtables' `
        -Check { param($p) $p.probe -and $p.dap -and $p.gates -and $p.apmap -and $p.romtables }

    # -- Tier 2e: boundary scan. LAST, and here is why ----------------------
    #
    # The RM Table 984 preamble writes TLM_TAP_SELECT_TDR = 0x80000011, which
    # links JTAGC_AE into the scan chain: TotalIRLen goes 8 -> 16 and stays
    # there. J-Link's DAP addressing is set up for the chain it scanned at
    # connect, so after this every DP/AP path stops working on both backends --
    # measured, CTRL/STAT 0xF0000000 -> 0x00000001. A power cycle restores it.
    # So nothing that needs the DAP may run after this point.
    Write-Host ""
    Write-Host "Everything below reconfigures the JTAG chain and will take DAP" -ForegroundColor Yellow
    Write-Host "access away until the board is power-cycled. It is last for that" -ForegroundColor Yellow
    Write-Host "reason. Do not re-run earlier steps after this without a power cycle." -ForegroundColor Yellow

    Invoke-Step -Name 'bscan-preamble' -Arguments @('bscan', 'preamble', '--run-svf') `
        -ChangesTargetState `
        -Expect 'Download successful; AFTERWARDS the chain is 16-bit and the DAP is unreachable'
    Invoke-Step -Name 'bscan-sample-1' -Arguments @('bscan', 'sample', '--bsr-length', '64') `
        -Expect 'a 64-bit capture'
    Invoke-Step -Name 'bscan-sample-2' -Arguments @('bscan', 'sample', '--bsr-length', '64') `
        -Expect 'compare against bscan-sample-1' `
        -Check {
            param($p)
            # Liveness. Equality is the wrong test: a stuck path is repeatable
            # too. What proves the scan reaches real cells is mostly-static bits
            # with one or two moving -- measured 0x0035025555A02006 then
            # 0x0015025555A02006. So: reject degenerate captures, and reject
            # noise, but accept a small difference as the good outcome.
            $prev = Get-ChildItem $OutDir -Filter '*-bscan-sample-1.json' | Select-Object -First 1
            if (-not $prev) { return $false }
            $a = Hex (Get-Content $prev.FullName -Raw | ConvertFrom-Json).first.raw_hex
            $b = Hex $p.first.raw_hex
            if (($a -eq 0) -and ($b -eq 0)) { return $false }   # never selected the BSR
            $moved = 0
            $x = $a -bxor $b
            while ($x) { $moved += $x -band 1; $x = $x -shr 1 }
            Write-Host "     $moved of 64 bits differ between captures" -ForegroundColor DarkGray
            return ($moved -le 8)
        }

    Write-Host ""
    Write-Host "POWER-CYCLE THE BOARD before running anything else: the chain is" -ForegroundColor Yellow
    Write-Host "now 16-bit and the DAP will not answer until you do." -ForegroundColor Yellow
}
finally {
    Stop-Transcript | Out-Null

    $summary = Join-Path $OutDir 'summary.json'
    $results | ConvertTo-Json -Depth 4 | Out-File -FilePath $summary -Encoding utf8

    Write-Host ""
    Write-Host ("=" * 78)
    $results | Format-Table -AutoSize
    Write-Host "artifacts: $OutDir"
    Write-Host ""
    Write-Host "Not run by this script, deliberately:" -ForegroundColor Yellow
    Write-Host "  iface swd  -- one-way; returning to JTAG needs a power cycle (RM 135.5.1)"
    Write-Host "  bscan sample --watch  -- needs a human to change the target state"
    Write-Host "  the virgin-board attach  -- needs a board with blank flash"
}

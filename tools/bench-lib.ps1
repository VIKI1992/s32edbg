<#
    Shared machinery for the bench scripts (bench-acceptance.ps1 and
    flash-state-matrix.ps1). Dot-source this file AFTER initialising:

        $OutDir             artifact folder (must exist)
        $Yes                [switch] skip state-changing confirmations
        $SkipStateChanging  [switch] skip state-changing steps entirely
        $script:results     [System.Collections.Generic.List[object]]::new()
        $script:stepNumber  0

    Dot-sourcing pastes these functions into the calling script's scope, so
    the $script: references resolve against the caller -- the same behaviour
    as when they lived inline in bench-acceptance.ps1.
#>

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

#Requires -Version 5.1
<#
.SYNOPSIS
  The per-leaf static gate: every whole-repo check that takes seconds, in one run.

.DESCRIPTION
  Promoted from the untracked scripts/_gate.sh. The knowledge in that file was rewritten from
  scratch most sessions at full cost, which is why it is tracked now.

  Two things changed in the promotion:

  * The hand-rolled substitutes are gone. `_belint.sh`, `_hyg.sh` and `_prettier.sh` each
    re-implemented a pre-commit hook by hand, and each drifted from it - findings 46 and 52 were
    both that drift. This calls pre-commit itself via scripts\pre-commit-run.ps1.

  * Nothing runs through Git Bash except the `.sh` check scripts pre-commit itself invokes.
    `MSYS2_ENV_CONV_EXCL` is therefore not needed and is deliberately absent: PowerShell starts
    `python.exe` directly, so MSYS environment conversion never happens. D-76.

  These checks are whole-repo BY DESIGN and stay in the per-leaf pass. A partial import graph or a
  partially built app would defeat them, and each costs seconds. It is the full pytest run that
  belongs at a group boundary, not these.

  Hooks covered by pre-commit here: gitleaks, ruff, ruff-format, check-test-doubles,
  check-test-credentials (FO-SEC001), check-ci-jobs, check-no-latest, check-gitleaks-config,
  check-chokepoint, gofmt, go-vet, prettier, end-of-file-fixer, trailing-whitespace,
  check-merge-conflict, check-yaml, check-added-large-files.

  Checks NOT in the hook set, run separately below: check-route-auth.py, check-hygiene.sh,
  check-structure.sh, check-makefile.sh, check-no-skips.py, and a backend import smoke.
#>
[CmdletBinding()]
param(
    [ValidateSet('range', 'staged', 'all')]
    [string] $Mode = 'range'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $RepoRoot
$PriorOutputEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
try {

$py = Join-Path $RepoRoot 'backend\.venv\Scripts\python.exe'
$gitBash = 'C:\Program Files\Git\bin\bash.exe'
$failures = @()

function Invoke-Check {
    param([string] $Name, [scriptblock] $Body)
    Write-Host ''
    Write-Host "########## $Name ##########"
    & $Body
    $rc = $LASTEXITCODE
    Write-Host "$Name exit=$rc"
    if ($rc -ne 0) { $script:failures += "$Name exit=$rc" }
}

Invoke-Check 'pre-commit' {
    & powershell -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $RepoRoot 'scripts\pre-commit-run.ps1') -Mode $Mode
}

Invoke-Check 'check-route-auth' {
    Push-Location (Join-Path $RepoRoot 'backend')
    try { & $py (Join-Path $RepoRoot 'scripts\check-route-auth.py') } finally { Pop-Location }
}

foreach ($sh in @('check-hygiene.sh', 'check-structure.sh', 'check-makefile.sh')) {
    # `-o pipefail` is load-bearing, and its absence was finding 66. Without it the exit status
    # of `bash scripts/X.sh | tail -3` is TAIL's, which is always 0 — so all three of these
    # checks could fail loudly in the output while this script reported `exit=0` and added
    # nothing to $failures. `check-hygiene.sh` did exactly that during leaf 8.5, printing two
    # violations and being recorded as clean.
    Invoke-Check $sh { & $gitBash -o pipefail -c "cd '$($RepoRoot -replace '\\','/')' && bash scripts/$sh 2>&1 | tail -3" }
}

# `check-no-skips.py` is deliberately NOT here. It consumes a pytest `--report-log` JSONL or
# `go test -json` events, so it is a property of a test RUN, not of the tree, and calling it with a
# directory just yields "no such report file". It belongs beside the `-m mandatory` run.

Invoke-Check 'backend import smoke' {
    Push-Location (Join-Path $RepoRoot 'backend')
    try {
        # Single quotes inside the Python source, not double. PowerShell's native-argument
        # marshalling strips embedded double quotes, so `print("ok", ...)` reached python as
        # `print(ok, ...)` and failed with a NameError that looked like an import problem.
        & $py -c @'
import src.auth.devices as d, src.governance.chokepoint as c, src.governance.policy as p
import src.governance.sequencing as s, src.main as m
print('ok', c.APPLY_OPERATION, c.REVERT_OPERATION, d.ENVELOPE_KEY_LABEL,
      len(p.POLICY_RESULTS), s.NONCE_HEX_LENGTH, callable(m.create_app))
'@
    } finally { Pop-Location }
}

Write-Host ''
Write-Host '========== LEAF GATE VERDICT =========='
if ($failures.Count -eq 0) { Write-Host 'all static checks clean'; exit 0 }
foreach ($f in $failures) { Write-Host "FAILED: $f" }
exit 1

} finally { [Console]::OutputEncoding = $PriorOutputEncoding; Pop-Location }

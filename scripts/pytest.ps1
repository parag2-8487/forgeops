#Requires -Version 5.1
<#
.SYNOPSIS
  Run the backend test suite from PowerShell with the local integration environment loaded.

.DESCRIPTION
  The one entry point for pytest. It exists so that pytest is never launched from Git Bash: see
  scripts/local-env.ps1 for why that matters (D-76), and docs/development.md, "Git Bash and
  native Windows executables".

  All arguments pass straight through, so the per-leaf pattern is:

      scripts\pytest.ps1 tests/unit/test_internal_ca.py
      scripts\pytest.ps1 -m mandatory
      scripts\pytest.ps1 -p no:randomly tests/property/test_q14.py

  Paths are relative to `backend/`, because that is where pytest runs.
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $PytestArgs
)

$ErrorActionPreference = 'Continue'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

. (Join-Path $PSScriptRoot 'local-env.ps1')

$py = Join-Path $RepoRoot 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) { Write-Host "no backend venv at $py"; exit 2 }

Push-Location (Join-Path $RepoRoot 'backend')
try {
    if (-not $PytestArgs) { $PytestArgs = @() }
    Write-Host ("pytest {0}" -f ($PytestArgs -join ' '))
    & $py -m pytest @PytestArgs
    $rc = $LASTEXITCODE
    Write-Host ""
    Write-Host "pytest exit=$rc"
    exit $rc
} finally { Pop-Location }

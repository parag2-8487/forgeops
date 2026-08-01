#Requires -Version 5.1
<#
  Run the backend test suite from PowerShell with the local integration environment loaded.

  The one entry point for pytest. It exists so that pytest is never launched from Git Bash: see
  scripts/local-env.ps1 for why that matters (D-76), and docs/development.md, "Git Bash and
  native Windows executables".

  NO `param()` BLOCK, DELIBERATELY. A `[CmdletBinding()] param([string[]] $PytestArgs)` looks
  tidier and silently breaks: PowerShell's parameter binder claims any argument that looks like a
  parameter name, so `scripts\pytest.ps1 -q -m mandatory` failed with
  `NamedParameterNotFound,pytest.ps1` and never reached pytest. Using the automatic `$args` gives
  verbatim pass-through, which is the only correct behaviour for a wrapper.

      scripts\pytest.ps1 -q tests/unit/test_internal_ca.py
      scripts\pytest.ps1 -q -m mandatory --report-log=../.evidence/mand.jsonl
      scripts\pytest.ps1 -q -p no:randomly tests/property

  Paths are relative to `backend/`, because that is where pytest runs.
#>

$ErrorActionPreference = 'Continue'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

. (Join-Path $PSScriptRoot 'local-env.ps1')

$py = Join-Path $RepoRoot 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) { Write-Host "no backend venv at $py"; exit 2 }

$pytestArgs = @($args)

Push-Location (Join-Path $RepoRoot 'backend')
try {
    Write-Host ("pytest {0}" -f ($pytestArgs -join ' '))
    & $py -m pytest @pytestArgs
    $rc = $LASTEXITCODE
    Write-Host ""
    Write-Host "pytest exit=$rc"
    exit $rc
} finally { Pop-Location }

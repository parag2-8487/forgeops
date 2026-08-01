#Requires -Version 5.1
<#
.SYNOPSIS
  Install pre-commit locally into .evidence/tools/py, hash-verified from
  scripts/requirements-tools.lock.

.DESCRIPTION
  Why this exists rather than the one-liner CI uses.

  CI runs `pip install --require-hashes -r scripts/requirements-tools.lock` on ubuntu-latest and
  that works. The same command on Windows fails:

      ERROR: In --require-hashes mode, all requirements must have their versions pinned
      with ==. These do not: colorama from ... (from build==1.5.0 -> requirements-tools.lock)

  `build`, a pip-tools dependency, requires `colorama; os_name == "nt"`. The lock is compiled by
  `make lock-tools` on Linux, where that marker is false, so the row was never emitted. The lock
  is therefore Linux-only in practice while presenting as universal. Recorded as finding 60 in
  docs/LEARNING-JOURNAL.md chapter 9, pattern C - a check that holds on the machine it was
  authored on and nowhere else.

  Rather than churn the lock (pip-compile would drop a hand-added row on the next regeneration)
  this installs the pre-commit subtree only. Every row still comes from the lock, and still with
  its digests, so `--require-hashes` remains in force. colorama is reached only through
  pip-tools, which is not needed to run hooks.

  Kept out of backend/.venv on purpose - scripts/requirements-tools.in states the reason:
  pre-commit is a CI tool, not a backend runtime or test dependency, and mixing them would put
  it into the graph pip-audit and the SBOM report on.
#>
[CmdletBinding()]
param(
    [string] $VenvPath = '.evidence/tools/py'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $RepoRoot
try {

# pre-commit 4.0.1's own dependency closure. virtualenv 21.x pulls distlib, filelock,
# platformdirs and python-discovery. Under --require-hashes an omitted transitive is not silently
# resolved: pip stops with "must have their versions pinned with ==", naming it. So this list is
# checked by the install itself rather than by inspection.
$Wanted = @('pre-commit', 'cfgv', 'identify', 'nodeenv', 'pyyaml', 'virtualenv',
            'distlib', 'filelock', 'platformdirs', 'python-discovery')

$lockPath = 'scripts/requirements-tools.lock'
$lines = Get-Content -LiteralPath $lockPath

# Parse the lock into name -> block. A block is `name==ver` plus its continued --hash lines.
$blocks = @{}
$current = $null
foreach ($l in $lines) {
    $m = [regex]::Match($l, '^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)')
    if ($m.Success) {
        $current = $m.Groups[1].Value.ToLowerInvariant().Replace('_', '-')
        $blocks[$current] = New-Object System.Collections.Generic.List[string]
        $blocks[$current].Add($l.TrimEnd())
        continue
    }
    if ($current -and $l -match '^\s+--hash=') { $blocks[$current].Add($l.TrimEnd()); continue }
    if ($current -and $l -match '^\s*#') { continue }
    if ($l.Trim() -eq '') { continue }
}

$out = New-Object System.Collections.Generic.List[string]
$missing = @()
foreach ($w in $Wanted) {
    $key = $w.ToLowerInvariant()
    if (-not $blocks.ContainsKey($key)) { $missing += $w; continue }
    foreach ($b in $blocks[$key]) { $out.Add($b) }
}
if ($missing.Count -gt 0) {
    throw "requirements-tools.lock has no hash-pinned row for: $($missing -join ', ')"
}

# Every emitted row must carry at least one digest, or --require-hashes is decoration.
$hashCount = ($out | Where-Object { $_ -match '--hash=sha256:' }).Count
if ($hashCount -lt $Wanted.Count) {
    throw "only $hashCount --hash lines for $($Wanted.Count) packages; refusing to install"
}
Write-Host ("subset: {0} packages, {1} digests, from {2}" -f $Wanted.Count, $hashCount, $lockPath)

$req = Join-Path $env:TEMP 'forgeops-pre-commit-subset.txt'
Set-Content -LiteralPath $req -Value $out -Encoding ASCII

$venvPy = Join-Path $RepoRoot ($VenvPath.Replace('/', '\') + '\Scripts\python.exe')
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Host "creating venv at $VenvPath"
    & py -3 -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}

& $venvPy -m pip install --require-hashes -r $req
if ($LASTEXITCODE -ne 0) { throw "pip install --require-hashes failed" }

& $venvPy -m pre_commit --version
if ($LASTEXITCODE -ne 0) { throw "pre-commit did not run after install" }

Remove-Item -LiteralPath $req -ErrorAction SilentlyContinue
Write-Host ''
Write-Host "pre-commit installed. Run the hook set with:  scripts\pre-commit-run.ps1"

} finally { Pop-Location }

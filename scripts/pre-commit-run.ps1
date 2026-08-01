#Requires -Version 5.1
<#
.SYNOPSIS
  Run the real .pre-commit-config.yaml hook set locally, from PowerShell.

.DESCRIPTION
  This replaces the hand-rolled `_belint.sh`, `_hyg.sh` and `_prettier.sh` scratch equivalents.
  Findings 46 and 52 were both the same shape: a hand-written substitute for a hook drifted from
  the hook it stood in for, so the local check passed and CI did not. `_belint.sh` checked
  `src tests` while the ruff hook matches `^backend/.*\.py$`; `_prettier.sh` had to hard-code the
  hook's `files:` glob by hand. Running pre-commit itself removes the class.

  Two PATH repairs, both load-bearing, and both the reason a plain `pre-commit run` fails here:

  1. `bash` on this machine resolves to `C:\WINDOWS\system32\bash.exe`, the WSL launcher, which
     reports `execvpe(/bin/bash) failed: No such file or directory`. Four local hooks
     (check-no-latest, check-gitleaks-config, check-chokepoint, go-vet-changed) have
     `entry: bash ...`, so every one of them fails for a reason that has nothing to do with the
     check. Git's `bin` is prepended so `bash` is Git Bash.

  2. Three local hooks have `entry: python scripts/...` with `language: system`, so `python` is
     whatever PATH gives. The backend venv is prepended so it is the interpreter the backend
     tests use, not a bare system Python that may lack a dependency.

  Note what is NOT done here: no `MSYS2_ENV_CONV_EXCL`, and no sourcing of the local integration
  environment. PowerShell launches `python.exe` directly, so MSYS variable conversion never
  happens - that is the point of the wrapper. See docs/development.md, "Git Bash and native
  Windows executables".

.PARAMETER Mode
  range   (default) the files changed against origin/<branch>, plus staged, unstaged and untracked
  staged  what `git commit` would see
  all     every tracked file; slow, use at a group close
#>
[CmdletBinding()]
param(
    [ValidateSet('range', 'staged', 'all')]
    [string] $Mode = 'range',
    [string] $Base,
    [string[]] $HookId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $RepoRoot
try {

$gitBin = 'C:\Program Files\Git\bin'
$venvBin = Join-Path $RepoRoot 'backend\.venv\Scripts'
$preCommit = Join-Path $RepoRoot '.evidence\tools\py\Scripts\pre-commit.exe'

if (-not (Test-Path -LiteralPath $preCommit)) {
    Write-Host "pre-commit not installed. Run: scripts\install-pre-commit.ps1"
    exit 2
}
if (-not (Test-Path -LiteralPath (Join-Path $gitBin 'bash.exe'))) {
    Write-Host "Git Bash not found at $gitBin -- the bash-entry hooks will fail"
    exit 2
}

$env:PATH = "$venvBin;$gitBin;$env:PATH"

$branch = (& git rev-parse --abbrev-ref HEAD).Trim()
if (-not $Base) { $Base = "origin/$branch" }

$args = @('run', '--show-diff-on-failure', '--color=never')
if ($HookId) { $args = @('run', '--show-diff-on-failure', '--color=never') + $HookId }

switch ($Mode) {
    'all' { $args += '--all-files' }
    'staged' { }
    'range' {
        $files = New-Object System.Collections.Generic.List[string]
        foreach ($src in @(
            @('diff', '--name-only', '--diff-filter=d', "$Base..HEAD"),
            @('diff', '--name-only', '--diff-filter=d'),
            @('diff', '--cached', '--name-only', '--diff-filter=d'),
            @('ls-files', '--others', '--exclude-standard')
        )) {
            $r = & git @src 2>$null
            if ($LASTEXITCODE -eq 0 -and $r) { foreach ($f in $r) { if ($f) { $files.Add($f) } } }
        }
        $uniq = @($files | Sort-Object -Unique | Where-Object { Test-Path -LiteralPath $_ })
        if ($uniq.Count -eq 0) {
            Write-Host "no changed files against $Base -- running the always_run hooks only"
            $uniq = @('README.md')
        }
        Write-Host ("pre-commit over {0} file(s) vs {1}" -f $uniq.Count, $Base)
        $args += '--files'
        $args += $uniq
    }
}

& $preCommit @args
$rc = $LASTEXITCODE
Write-Host ""
Write-Host "pre-commit exit=$rc"
exit $rc

} finally { Pop-Location }

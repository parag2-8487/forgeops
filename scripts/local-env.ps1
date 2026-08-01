#Requires -Version 5.1
<#
.SYNOPSIS
  The host-side local integration environment. Dot-source it:  . scripts\local-env.ps1

.DESCRIPTION
  Replaces the untracked scripts/_env.sh, and fixes what made that file dangerous.

  WHY POWERSHELL AND NOT BASH. Git Bash rewrites any environment value that looks like an
  absolute POSIX path when it launches a NATIVE Windows executable, so an exported
  `API_PREFIX=/api/v1` reaches `python.exe` as `C:/Program Files/Git/api/v1`.
  `backend/src/core/config.py` sets `env_file=None`, so settings come from the OS environment
  only; `create_app()` then registers a route whose path does not start with `/` and Starlette
  asserts. `scripts/check-route-auth.py` reported that as "could not build the app from
  'src.main:create_app'", which reads like a repository defect and is not one. `.env` carries
  API_PREFIX, so ANY wholesale load of `.env` inside Git Bash reproduces it. Setting the
  environment from PowerShell removes the conversion step rather than working around it, which is
  D-76. See docs/development.md, "Git Bash and native Windows executables".

  WHY `.env` IS NOT LOADED AT ALL, WHICH IS A CORRECTION. The first version of this file loaded
  `.env` in full and then overrode the endpoint variables, on the reasoning that a host-side run
  needs `.env` for INTERNAL_CA_KEY_PEM, ENVELOPE_PEPPER and LOCAL_SECRET_SEAL_KEY. That reasoning
  was wrong twice over and the mandatory selection said so: 22 tests failed.

  First, overriding the endpoints fixes finding 61 and does nothing about finding 57, which is the
  larger problem: sixty-odd tests assert on what is **ABSENT** from the environment, so any key
  from `.env` breaks them regardless of its value. `test_the_derived_radius_ignores_the_variable`
  cannot pass with MCP_AGENT_BLAST_RADIUS set, by construction.

  Second, `.env.example`'s values carry trailing inline comments -
  `MCP_AGENT_BLAST_RADIUS=read_only    # read_only | workspace | infrastructure` - so a naive
  parser exports the comment as part of the value and pydantic reports
  `Input should be 'read_only', 'workspace' or 'infrastructure'` for a variable that says
  `read_only`. Fixing the parser would have fixed the symptom and left the first problem.

  So this file sets an EXPLICIT variable set and reads no `.env`. `.env` exists for `docker
  compose` and for `make init-ca`; a host-side test run must not see it. Nothing in the test suite
  needs the development CA key - `test_internal_ca.py` builds its own - which is checked by the
  suite passing rather than asserted here.

  `-WithDotEnv` exists for the flows that genuinely want it, such as driving `make init-ca`'s
  output by hand. It is off by default and must never be on for a test run.
#>
[CmdletBinding()]
param(
    [switch] $WithDotEnv
)


$ErrorActionPreference = 'Stop'

$__leRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# ---------------------------------------------------------------------------------------------
# 1. `.evidence/ak.env` only (two Authentik bootstrap keys). `.env` is read ONLY when explicitly
#    asked for, and never for a test run - see the header.
# ---------------------------------------------------------------------------------------------
function Import-DotEnvFile {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    $n = 0
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $t = $t -replace '^export\s+', ''
        $eq = $t.IndexOf('=')
        if ($eq -lt 1) { continue }
        $k = $t.Substring(0, $eq).Trim()
        $v = $t.Substring($eq + 1).Trim()
        if ($v.Length -ge 2 -and (($v[0] -eq '"' -and $v[-1] -eq '"') -or ($v[0] -eq "'" -and $v[-1] -eq "'"))) {
            $v = $v.Substring(1, $v.Length - 2)
        } else {
            # A trailing inline comment is part of the LINE, not of the value. `.env.example`
            # documents its enums this way - `SECRET_BACKEND=infisical  # infisical | local` -
            # and exporting the comment produces
            # "Input should be 'infisical' or 'local'" for a variable that says `infisical`.
            $hash = $v.IndexOf('#')
            if ($hash -ge 0) { $v = $v.Substring(0, $hash).TrimEnd() }
        }
        if ($k -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        Set-Item -Path "env:$k" -Value $v
        $n++
    }
    return $n
}

$__leEnvCount = 0
$__leAkCount = 0

# ---------------------------------------------------------------------------------------------
# 1b. Clear every project key `.env.example` DECLARES, then set the explicit set below.
#
#     Not tidiness - correctness, and it is what makes a run reproducible in a shell somebody has
#     already polluted. Sixty-odd tests assert on what is ABSENT from the environment (finding 57),
#     so an inherited CERBOS_URL is as fatal as a loaded one, and it is harder to see: it survives
#     in the parent shell after one `. scripts\local-env.ps1` that predates this fix. The guard at
#     the bottom found exactly that - CERBOS_URL, INFISICAL_URL and OPA_URL still pointing at
#     Compose service names in a process that had never read `.env`.
#
#     The key NAMES come from `.env.example`, which is tracked and is the same file
#     `core/config.py`'s PROJECT_CONFIG_KEYS validates against, so a new setting is covered without
#     editing anything here. Only names are read; no value from that file is ever exported.
# ---------------------------------------------------------------------------------------------
$__leDeclared = @()
foreach ($line in (Get-Content -LiteralPath (Join-Path $__leRoot '.env.example') -Encoding UTF8)) {
    $m = [regex]::Match($line.Trim(), '^([A-Za-z_][A-Za-z0-9_]*)=')
    if ($m.Success) { $__leDeclared += $m.Groups[1].Value }
}
$__leDeclared = @($__leDeclared | Sort-Object -Unique)
if ($__leDeclared.Count -eq 0) {
    throw 'local-env: .env.example declared no keys; refusing to continue (the clear step would be a no-op)'
}
$__leCleared = 0
foreach ($k in $__leDeclared) {
    if (Test-Path "env:$k") { Remove-Item "env:$k" -ErrorAction SilentlyContinue; $__leCleared++ }
}

# Imports run AFTER the clear, or the clear would undo them.
if ($WithDotEnv) { $__leEnvCount = Import-DotEnvFile (Join-Path $__leRoot '.env') }
$__leAkCount = Import-DotEnvFile (Join-Path $__leRoot '.evidence\ak.env')

# ---------------------------------------------------------------------------------------------
# 2. Host-facing endpoints, overriding whatever `.env` said. These are the published ports of
#    the five `forgeops-test-*` containers; docs/development.md lists them in one table.
# ---------------------------------------------------------------------------------------------
$env:FORGEOPS_TEST_DATABASE_URL = 'postgresql+asyncpg://forgeops_migrator@127.0.0.1:55432/forgeops_test'
$env:FORGEOPS_TEST_REDIS_URL = 'redis://127.0.0.1:56379/1'
$env:FORGEOPS_TEST_OIDC_BASE_URL = 'http://localhost:9000'
$env:FORGEOPS_TEST_CERBOS_URL = 'http://127.0.0.1:53592'

$env:DATABASE_URL = 'postgresql+asyncpg://forgeops_app@127.0.0.1:55432/forgeops_test'
# Explicit and load-bearing: alembic/env.py prefers ALEMBIC_DATABASE_URL over DATABASE_URL by
# design (§6.4 - the migrator role owns the schema), so leaving `.env`'s Compose value in place
# is finding 61 exactly.
$env:ALEMBIC_DATABASE_URL = 'postgresql+asyncpg://forgeops_migrator@127.0.0.1:55432/forgeops_test'
$env:REDIS_URL = 'redis://127.0.0.1:56379/0'

$env:FORGEOPS_REQUIRE_INTEGRATION = '1'
$env:PGHOST = '127.0.0.1'
$env:PGPORT = '55432'
$env:PGUSER = 'postgres'
$env:POSTGRES_USER = 'postgres'
$env:POSTGRES_DB = 'forgeops_test'
$env:FORGEOPS_APP_DB_PASSWORD = 'local-only-not-a-real-secret'
$env:FORGEOPS_MIGRATOR_DB_PASSWORD = 'local-only-not-a-real-secret'

# NOTHING ELSE IS EXPORTED, AND THAT IS A CONSTRAINT RATHER THAN AN OVERSIGHT. This set is
# `scripts/_env.sh`'s, which the suite is known to pass under. An earlier version of this file also
# set CERBOS_URL, OPA_URL, POSTGRES_PORT and REDIS_PORT, and every one of those is a registered
# project key that some test asserts is ABSENT - finding 57. A variable is added here only with a
# test run behind it, never because it looks useful.

# ---------------------------------------------------------------------------------------------
# 3. The guard. Non-vacuous by construction: it is driven from the Compose file's own service
#    names, so it cannot go stale, and it fails if the discovered name set is empty.
# ---------------------------------------------------------------------------------------------
$__leServices = @()
$__leCompose = Join-Path $__leRoot 'docker-compose.yml'
if (Test-Path -LiteralPath $__leCompose) {
    $__leInServices = $false
    foreach ($line in (Get-Content -LiteralPath $__leCompose -Encoding UTF8)) {
        if ($line -match '^services:\s*$') { $__leInServices = $true; continue }
        if ($__leInServices -and $line -match '^[A-Za-z]') { $__leInServices = $false }
        if ($__leInServices -and $line -match '^  ([a-z][a-z0-9_-]*):\s*$') {
            $__leServices += $Matches[1]
        }
    }
}
if ($__leServices.Count -eq 0) {
    throw 'local-env: found no Compose service names to guard against; refusing to continue'
}

$__leBad = @()
foreach ($e in (Get-ChildItem env: )) {
    $v = [string] $e.Value
    if ($v -notmatch '://') { continue }
    foreach ($svc in $__leServices) {
        if ($v -match ("://(?:[^/@\s]*@)?" + [regex]::Escape($svc) + "(?::\d+)?(?:/|$)")) {
            $__leBad += ("{0} -> {1}" -f $e.Name, $svc)
        }
    }
}
if ($__leBad.Count -gt 0) {
    Write-Host 'local-env: these variables still point at a Compose service name, unreachable from the host:'
    $__leBad | Sort-Object -Unique | ForEach-Object { Write-Host "  $_" }
    throw 'local-env: add the variable above to the override block (finding 61)'
}

Write-Host ("local-env: declared keys={0} cleared={1} dotenv keys={2} ak keys={3} compose services guarded={4}" -f `
    $__leDeclared.Count, $__leCleared, $__leEnvCount, $__leAkCount, $__leServices.Count)
$env:FORGEOPS_LOCAL_ENV_READY = '1'

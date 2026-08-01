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

  WHY `.env` IS LOADED AND THEN OVERRIDDEN, NOT FILTERED. A host-side run has a real reason to
  load `.env`: it holds INTERNAL_CA_KEY_PEM / INTERNAL_CA_CERT_PEM from `make init-ca`, plus
  ENVELOPE_PEPPER and LOCAL_SECRET_SEAL_KEY. But `.env` is a copy of the COMPOSE-targeted
  `.env.example`, whose DSNs name the Compose services `postgres` and `redis` - names that do not
  resolve on the host. That is finding 61, and it presents as `socket.gaierror` inside
  `schema_at_head`'s `alembic downgrade base`, i.e. as every DB-backed test erroring at setup for
  a reason that looks nothing like its cause.

  An allow-list of "safe" keys would be pattern H: a hand-maintained list that a new key in
  `.env.example` silently escapes. So `.env` is loaded in full and the endpoint keys are then
  overridden unconditionally, and a guard afterwards fails if any exported value still points at
  a Compose service name. A new Compose endpoint appearing in `.env.example` therefore breaks the
  guard loudly instead of breaking a test suite quietly.

  NO SECRET VALUES LIVE IN THIS FILE. The two DB passwords below are for a local container
  started with POSTGRES_HOST_AUTH_METHOD=trust and are self-labelling as local-only. Everything
  genuinely sensitive comes from the untracked `.env`.
#>

$ErrorActionPreference = 'Stop'

$__leRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# ---------------------------------------------------------------------------------------------
# 1. `.env` in full, when present, so the CA key and the pepper are available.
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
        }
        if ($k -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        Set-Item -Path "env:$k" -Value $v
        $n++
    }
    return $n
}

$__leEnvCount = Import-DotEnvFile (Join-Path $__leRoot '.env')
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
$env:CERBOS_URL = 'http://127.0.0.1:53592'
# Found by the guard below on its first run, not by inspection - which is the argument for having
# the guard. `.env` ships OPA_URL=http://opa:8181. There is no local OPA container (the policy
# suite runs `opa test` as a CLI, not against a server), so this points at a loopback port that
# is simply not listening. That is the honest state: a connection refused on 127.0.0.1 names its
# own cause, where `getaddrinfo failed` on `opa` does not.
$__leOpaPort = if ($env:OPA_PORT) { $env:OPA_PORT } else { '8181' }
$env:OPA_URL = "http://127.0.0.1:$__leOpaPort"

$env:FORGEOPS_REQUIRE_INTEGRATION = '1'
$env:PGHOST = '127.0.0.1'
$env:PGPORT = '55432'
$env:PGUSER = 'postgres'
$env:POSTGRES_USER = 'postgres'
$env:POSTGRES_DB = 'forgeops_test'
$env:POSTGRES_PORT = '55432'
$env:REDIS_PORT = '56379'
$env:FORGEOPS_APP_DB_PASSWORD = 'local-only-not-a-real-secret'
$env:FORGEOPS_MIGRATOR_DB_PASSWORD = 'local-only-not-a-real-secret'

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

Write-Host ("local-env: .env keys={0} ak keys={1} compose services guarded={2}" -f `
    $__leEnvCount, $__leAkCount, $__leServices.Count)
$env:FORGEOPS_LOCAL_ENV_READY = '1'

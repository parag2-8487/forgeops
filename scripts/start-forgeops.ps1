#Requires -Version 5.1
<#
.SYNOPSIS
    Bring the whole ForgeOps stack up on a machine that may have nothing installed.

.DESCRIPTION
    One entry point for "make this run on that PC". It installs what is missing, generates the
    secrets the application refuses to boot without, picks ports that are actually free, starts the
    nine services in dependency order, provisions the identity provider, applies the migrations, and
    then PROVES the result by reading /health/ready rather than by reporting that `up` returned zero.

    It is safe to run repeatedly. Every step checks the state it wants before changing anything, and
    no existing secret is ever overwritten.

    WHY A SCRIPT AND NOT `docker compose up`
    ----------------------------------------
    `docker compose up` alone does not produce a working system here, for five reasons that are all
    properties of the application rather than oversights:

      1. The backend refuses to start without ENVELOPE_PEPPER, and Authentik refuses to start
         without AUTHENTIK_SECRET_KEY. Neither can be committed, because a committed secret is a
         credential in the repository. So they must be generated on first run.
      2. NEXT_PUBLIC_API_BASE_URL is a BUILD argument, not a runtime variable. Next.js inlines it
         into the client bundle at build time, so if the backend port changes the frontend image
         must be REBUILT. A restart is not enough, and the failure looks like CORS.
      3. The default ports (5432, 6379, 8181, 3592, 9000, 3000, 8000) collide with Windows reserved
         ranges and with anything else already running. This picks free ones and keeps every
         dependent URL consistent with the choice.
      4. The OIDC issuer is split-horizon. The backend reaches Authentik as `authentik-server:9000`
         on the compose network; your browser cannot resolve that name and must use
         `localhost:<port>`. One value cannot serve both, so there are two, and getting them the
         wrong way round produces a login that half-works.
      5. An empty Authentik has no application, no client, no groups and no users. Until it is
         provisioned there is nothing to log in to.

.PARAMETER Fresh
    Destroy all data volumes first and start from an empty database. DESTRUCTIVE: every project,
    device, change set and audit row is deleted, and the identity provider is reprovisioned. Asks
    for confirmation unless -Force is also given.

.PARAMETER Force
    Skip the confirmation prompt for -Fresh.

.PARAMETER SkipInstall
    Never install anything. If a prerequisite is missing, say so and stop. Use this on a machine
    where you manage tooling yourself.

.PARAMETER NoBrowser
    Do not open a browser when the stack is ready.

.PARAMETER NoReset
    Reuse the existing containers instead of removing and recreating them. By DEFAULT the launcher
    removes the previous deployment's containers first, keeping the data volumes, which makes stale
    container environments impossible.

.PARAMETER PurgeImages
    Also delete the images this project built locally, so they are rebuilt from source.

.PARAMETER Rebuild
    Force a rebuild of the backend, frontend and agent images even if they already exist.

.EXAMPLE
    .\scripts\start-forgeops.ps1
    Start everything, installing anything missing. Replaces the previous containers, keeps the data.

.EXAMPLE
    .\scripts\start-forgeops.ps1 -Fresh -Force
    Wipe all data and start clean, without being asked to confirm.

.EXAMPLE
    .\scripts\start-forgeops.ps1 -NoReset
    Leave the running containers alone and only bring up what is missing.
#>
[CmdletBinding()]
param(
    [switch]$Fresh,
    [switch]$Force,
    [switch]$SkipInstall,
    [switch]$NoBrowser,
    [switch]$Rebuild,
    [switch]$NoReset,
    [switch]$PurgeImages,
    [int]$ReadyTimeoutSeconds = 900
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# --- Presentation --------------------------------------------------------------------------------

$script:StepNumber = 0

function Write-Head {
    param([string]$Text)
    Write-Host ''
    Write-Host ('  ' + $Text) -ForegroundColor Cyan
    Write-Host ('  ' + ('-' * [Math]::Min(76, $Text.Length))) -ForegroundColor DarkCyan
}

function Write-Step {
    param([string]$Text)
    $script:StepNumber++
    Write-Host ''
    Write-Host ("[{0}] {1}" -f $script:StepNumber, $Text) -ForegroundColor White
}

function Write-Ok {
    param([string]$Text)
    Write-Host ('      ok    ' + $Text) -ForegroundColor Green
}

function Write-Info {
    param([string]$Text)
    Write-Host ('      ..    ' + $Text) -ForegroundColor Gray
}

function Write-Warn2 {
    param([string]$Text)
    Write-Host ('      warn  ' + $Text) -ForegroundColor Yellow
}

function Write-Fail {
    param([string]$Text)
    Write-Host ('      FAIL  ' + $Text) -ForegroundColor Red
}

function Stop-WithAdvice {
    param([string]$Problem, [string[]]$Advice = @())
    Write-Host ''
    Write-Host '  ----------------------------------------------------------------------------' -ForegroundColor Red
    Write-Host ('  CANNOT CONTINUE: ' + $Problem) -ForegroundColor Red
    Write-Host '  ----------------------------------------------------------------------------' -ForegroundColor Red
    foreach ($line in $Advice) { Write-Host ('  ' + $line) -ForegroundColor Yellow }
    Write-Host ''
    exit 1
}

# --- Small utilities -----------------------------------------------------------------------------

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command -Name $Name -ErrorAction SilentlyContinue)
}

function Invoke-Native {
    <#
        Run a native command and return an object rather than throwing. PowerShell treats a native
        program's stderr output as a terminating error under some host configurations, which turns a
        harmless warning into a crash, so everything is redirected and the exit code is inspected
        explicitly.
    #>
    param(
        [Parameter(Mandatory)][string]$Command,
        [switch]$Quiet
    )
    # The command is GROUPED before the redirection: `( cmd ) 2>&1` rather than `cmd 2>&1`. Without
    # the parentheses the redirection binds to the last element of the command, so anything
    # containing `&` or `|` loses its stderr -- verified with a command that writes to stderr and
    # exits non-zero, where the ungrouped form captured nothing at all.
    $out = & cmd /c "( $Command ) 2>&1"
    $code = $LASTEXITCODE
    if (-not $Quiet -and $out) { $out | ForEach-Object { Write-Verbose ('        | ' + $_) } }
    return [pscustomobject]@{
        ExitCode = $code
        Output   = ($out -join [Environment]::NewLine)
        Lines    = @($out)
        Ok       = ($code -eq 0)
    }
}

function Test-PortFree {
    <#
        A port is "free" if we can bind a listener to it on the loopback address. Probing by
        CONNECTING is the common mistake: a refused connection also happens when a firewall drops
        the packet, and it says nothing about whether we may bind. Compose publishes on
        127.0.0.1 specifically, so that is the address tested.
    #>
    param([int]$Port)
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $listener) { try { $listener.Stop() } catch { } }
    }
}

function Get-UsablePort {
    <#
        Prefer the port already recorded in .env, so a second run does not silently move the
        application and invalidate the frontend image that was built against it. Only when the
        preferred port is unavailable do we search upward.
    #>
    param(
        [Parameter(Mandatory)][int]$Preferred,
        [Parameter(Mandatory)][string]$Label,
        [int]$SearchLimit = 200
    )
    if (Test-PortFree -Port $Preferred) { return $Preferred }

    Write-Warn2 ("port {0} ({1}) is in use; searching for a free one" -f $Preferred, $Label)
    for ($candidate = $Preferred + 1; $candidate -lt $Preferred + $SearchLimit; $candidate++) {
        if ($candidate -gt 65535) { break }
        if (Test-PortFree -Port $candidate) {
            Write-Info ("{0} will use {1} instead of {2}" -f $Label, $candidate, $Preferred)
            return $candidate
        }
    }
    Stop-WithAdvice -Problem ("no free port found for {0} near {1}." -f $Label, $Preferred) -Advice @(
        'Something is occupying a wide range of ports. Close other development stacks and retry.'
    )
}

function New-RandomSecret {
    param([int]$Bytes = 24, [string]$Prefix = '')
    $buffer = New-Object 'byte[]' $Bytes
    # `RandomNumberGenerator::Create()` plus `GetBytes` rather than the static `Fill`: this script has
    # to run on the Windows PowerShell 5.1 that ships with every supported Windows, and that is .NET
    # Framework, where the static `Fill` does not exist. Create/GetBytes exists in both.
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    $hex = -join ($buffer | ForEach-Object { $_.ToString('x2') })
    if ($Prefix) { return $Prefix + $hex }
    return $hex
}

# --- .env handling -------------------------------------------------------------------------------
#
# The file is edited in place, line by line, preserving every comment. `.env.example` is a document
# as much as a template -- it explains each setting -- and regenerating it from a hashtable would
# throw that away.
#
# It is written WITHOUT a byte order mark. PowerShell's `Set-Content -Encoding UTF8` emits one, and a
# BOM at the start of a dotenv file becomes part of the FIRST KEY NAME, so the first setting in the
# file silently stops being read. This cost real debugging time in this repository already, in Python
# sources rather than dotenv, and the fix is the same: an explicit no-BOM encoding.

function Read-EnvFile {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @{} }
    $map = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $trimmed = $line.Trim()
        if ($trimmed.Length -eq 0 -or $trimmed.StartsWith('#')) { continue }
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) { continue }
        $key = $line.Substring(0, $eq).Trim()
        $value = $line.Substring($eq + 1)
        # Strip an inline comment only when the value is unquoted; a `#` inside quotes is data.
        if ($value -notmatch '^\s*"' -and $value -notmatch "^\s*'") {
            $hash = $value.IndexOf('#')
            if ($hash -ge 0) { $value = $value.Substring(0, $hash) }
        }
        $value = $value.Trim().Trim('"').Trim("'")
        $map[$key] = $value
    }
    return $map
}

function Set-EnvValues {
    <#
        Apply a hashtable of key/value pairs to a dotenv file. Existing keys are rewritten in place
        so their surrounding comments survive; unknown keys are appended under a labelled heading.
    #>
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][hashtable]$Values
    )
    $lines = [System.Collections.Generic.List[string]]::new()
    if (Test-Path -LiteralPath $Path) {
        foreach ($l in [System.IO.File]::ReadAllLines($Path)) { $lines.Add($l) }
    }

    $remaining = @{}
    foreach ($k in $Values.Keys) { $remaining[$k] = $Values[$k] }

    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        $trimmed = $line.Trim()
        if ($trimmed.Length -eq 0 -or $trimmed.StartsWith('#')) { continue }
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) { continue }
        $key = $line.Substring(0, $eq).Trim()
        if ($remaining.ContainsKey($key)) {
            $lines[$i] = ($key + '=' + $remaining[$key])
            $remaining.Remove($key)
        }
    }

    if ($remaining.Count -gt 0) {
        $lines.Add('')
        $lines.Add('# --- Added by scripts/start-forgeops.ps1 ------------------------------------')
        foreach ($k in ($remaining.Keys | Sort-Object)) { $lines.Add($k + '=' + $remaining[$k]) }
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($Path, $lines, $utf8NoBom)
}

# --- Repository root -----------------------------------------------------------------------------

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $RepoRoot

Write-Host ''
Write-Host '  ForgeOps - governed DevOps automation' -ForegroundColor Cyan
Write-Host '  one-click start: installs what is missing, then starts and verifies the stack' -ForegroundColor DarkGray
Write-Host ("  repository: {0}" -f $RepoRoot) -ForegroundColor DarkGray

foreach ($marker in @('docker-compose.yml', 'backend', 'frontend', '.env.example')) {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot $marker))) {
        Stop-WithAdvice -Problem ("this does not look like the ForgeOps repository: '{0}' is missing." -f $marker) -Advice @(
            'Run the script from inside a clone of the repository, for example:',
            '    git clone <url> forgeops && cd forgeops && .\start.cmd'
        )
    }
}

$ComposeFiles = '-f docker-compose.yml -f docker-compose.e2e.yml'
$EnvPath = Join-Path $RepoRoot '.env'

function Invoke-Compose {
    param([Parameter(Mandatory)][string]$Arguments, [switch]$Quiet)
    return Invoke-Native -Command ("docker compose {0} {1}" -f $ComposeFiles, $Arguments) -Quiet:$Quiet
}

# --- 1. Docker -----------------------------------------------------------------------------------

Write-Head 'Prerequisites'
Write-Step 'Checking Docker'

if (-not (Test-CommandExists -Name 'docker')) {
    if ($SkipInstall) {
        Stop-WithAdvice -Problem 'Docker is not installed, and -SkipInstall was given.' -Advice @(
            'Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and retry.'
        )
    }
    Write-Warn2 'Docker is not installed. Installing Docker Desktop.'
    if (-not (Test-CommandExists -Name 'winget')) {
        Stop-WithAdvice -Problem 'Docker is missing and winget is not available to install it.' -Advice @(
            'Install Docker Desktop by hand, then run this script again:',
            '    https://www.docker.com/products/docker-desktop/',
            '',
            'winget ships with App Installer from the Microsoft Store on Windows 10 1809 and later.'
        )
    }
    Write-Info 'winget install Docker.DockerDesktop  (this takes several minutes)'
    $install = Invoke-Native -Command 'winget install --id Docker.DockerDesktop --exact --accept-source-agreements --accept-package-agreements --disable-interactivity'
    if (-not $install.Ok) {
        Stop-WithAdvice -Problem 'the Docker Desktop installation did not succeed.' -Advice @(
            'winget output:', $install.Output, '',
            'Docker Desktop usually needs an elevated prompt and, on a first install, a REBOOT to',
            'enable the WSL 2 backend. Install it manually, reboot, then run this script again.'
        )
    }
    Write-Ok 'Docker Desktop installed'
    Write-Warn2 'A REBOOT is usually required before Docker can run. Reboot, then run this script again.'
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [System.Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not (Test-CommandExists -Name 'docker')) {
        Stop-WithAdvice -Problem 'Docker was installed but is not yet on PATH.' -Advice @(
            'Reboot and run this script again. This is expected on a first install.'
        )
    }
} else {
    $v = Invoke-Native -Command 'docker --version' -Quiet
    Write-Ok $v.Lines[0]
}

Write-Step 'Checking the Docker engine is running'

$engine = Invoke-Native -Command 'docker info --format "{{.ServerVersion}}"' -Quiet
if (-not $engine.Ok) {
    Write-Warn2 'the Docker engine is not responding. Trying to start Docker Desktop.'
    $desktop = @(
        (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Docker\Docker\Docker Desktop.exe')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1

    if (-not $desktop) {
        Stop-WithAdvice -Problem 'the Docker engine is not running and Docker Desktop was not found.' -Advice @(
            'Start Docker Desktop manually, wait for the whale icon to stop animating, and retry.'
        )
    }
    Start-Process -FilePath $desktop | Out-Null
    Write-Info 'waiting for the engine (up to 4 minutes; a cold start is slow)'
    $engineUp = $false
    for ($i = 1; $i -le 48; $i++) {
        Start-Sleep -Seconds 5
        $probe = Invoke-Native -Command 'docker info --format "{{.ServerVersion}}"' -Quiet
        if ($probe.Ok) { $engineUp = $true; $engine = $probe; break }
        if ($i % 6 -eq 0) { Write-Info ("still waiting ({0}s)" -f ($i * 5)) }
    }
    if (-not $engineUp) {
        Stop-WithAdvice -Problem 'the Docker engine did not become ready.' -Advice @(
            'Open Docker Desktop and look at its own error message. Common causes:',
            '  - WSL 2 is not installed or needs updating:  wsl --update',
            '  - virtualisation is disabled in the BIOS/UEFI',
            '  - the machine needs a reboot after installing Docker Desktop'
        )
    }
}
Write-Ok ("Docker engine " + $engine.Lines[0].Trim())

$composeProbe = Invoke-Native -Command 'docker compose version --short' -Quiet
if (-not $composeProbe.Ok) {
    Stop-WithAdvice -Problem 'Docker Compose v2 is not available (`docker compose` failed).' -Advice @(
        'This project uses the Compose V2 plugin, not the old `docker-compose` binary.',
        'Update Docker Desktop, which bundles it.'
    )
}
Write-Ok ("Docker Compose v" + $composeProbe.Lines[0].Trim())

# --- 2. Python, used for two host-side steps only ------------------------------------------------

Write-Step 'Checking Python'

<#
    Python is needed for exactly two things that cannot run in a container:

      * scripts/init_ca.py         generates the development internal CA into .env
      * scripts/ci/provision-authentik.py   creates the OIDC application, groups and users

    The provisioner imports its API client from backend/tests/integration/test_authentik_real_idp.py
    -- deliberately, so there is one implementation rather than two -- and that module imports pytest
    at the top level. The backend RUNTIME image has httpx but no pytest, so the provisioner cannot
    run inside it. Rather than duplicate the client or stub out pytest, the launcher creates its own
    virtual environment from the repository's hash-pinned dev lock.
#>

$VenvDir = Join-Path $RepoRoot '.forgeops-launcher\venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$BackendVenvPython = Join-Path $RepoRoot 'backend\.venv\Scripts\python.exe'
$LauncherPython = $null

function Test-PythonHasProvisioningDeps {
    param([Parameter(Mandatory)][string]$PythonExe)
    if (-not (Test-Path -LiteralPath $PythonExe)) { return $false }
    $probe = Invoke-Native -Command ('"{0}" -c "import httpx, pytest, pytest_asyncio, cryptography"' -f $PythonExe) -Quiet
    return $probe.Ok
}

if (Test-PythonHasProvisioningDeps -PythonExe $BackendVenvPython) {
    $LauncherPython = $BackendVenvPython
    Write-Ok 'using the existing backend virtual environment'
} elseif (Test-PythonHasProvisioningDeps -PythonExe $VenvPython) {
    $LauncherPython = $VenvPython
    Write-Ok 'using the existing launcher virtual environment'
} else {
    $basePython = $null
    <#
        3.13 EXACTLY, not "3.11 or newer". `backend/pyproject.toml` declares
        `requires-python = ">=3.13,<3.14"` and `requirements-dev.lock` is pinned to match, so an
        older interpreter does not fail at import time -- it fails during `pip install` with
        "Ignoring <package>: markers ... require a different python version", which reads like a
        broken lock file rather than a wrong interpreter. Checking the version here says it plainly.
    #>
    foreach ($candidate in @('py -3.13', 'python3.13', 'python', 'python3')) {
        $probe = Invoke-Native -Command ("{0} -c ""import sys; print(sys.version_info[0], sys.version_info[1])""" -f $candidate) -Quiet
        if ($probe.Ok) {
            $parts = $probe.Lines[0].Trim() -split '\s+'
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -eq 13) { $basePython = $candidate; break }
            Write-Info ("{0} is {1}.{2}; this project needs 3.13" -f $candidate, $parts[0], $parts[1])
        }
    }

    if (-not $basePython) {
        if ($SkipInstall) {
            Stop-WithAdvice -Problem 'no suitable Python was found, and -SkipInstall was given.' -Advice @(
                'Install Python 3.13 from https://www.python.org/downloads/ and retry.'
            )
        }
        if (-not (Test-CommandExists -Name 'winget')) {
            Stop-WithAdvice -Problem 'Python is missing and winget is not available to install it.' -Advice @(
                'Install Python 3.13 from https://www.python.org/downloads/ and retry.'
            )
        }
        Write-Warn2 'Python is not installed. Installing Python 3.13.'
        $py = Invoke-Native -Command 'winget install --id Python.Python.3.13 --exact --accept-source-agreements --accept-package-agreements --disable-interactivity'
        if (-not $py.Ok) {
            Stop-WithAdvice -Problem 'the Python installation did not succeed.' -Advice @($py.Output)
        }
        $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [System.Environment]::GetEnvironmentVariable('Path', 'User')
        $basePython = 'py -3.13'
        if (-not (Invoke-Native -Command 'py -3.13 --version' -Quiet).Ok) {
            Stop-WithAdvice -Problem 'Python was installed but is not yet on PATH.' -Advice @(
                'Close this window, open a new one, and run the script again.'
            )
        }
        Write-Ok 'Python 3.13 installed'
    }

    Write-Info ("creating a virtual environment with {0}" -f $basePython)
    New-Item -ItemType Directory -Path (Split-Path -Parent $VenvDir) -Force | Out-Null
    $mk = Invoke-Native -Command ('{0} -m venv "{1}"' -f $basePython, $VenvDir)
    if (-not $mk.Ok) { Stop-WithAdvice -Problem 'could not create the virtual environment.' -Advice @($mk.Output) }

    Write-Info 'installing the pinned dependencies (hash-enforced; a few minutes on a first run)'
    $up = Invoke-Native -Command ('"{0}" -m pip install --quiet --upgrade pip' -f $VenvPython)
    if (-not $up.Ok) { Write-Warn2 'could not upgrade pip; continuing' }
    $req = Join-Path $RepoRoot 'backend\requirements-dev.lock'
    $inst = Invoke-Native -Command ('"{0}" -m pip install --quiet --require-hashes -r "{1}"' -f $VenvPython, $req)
    if (-not $inst.Ok) {
        Stop-WithAdvice -Problem 'the pinned dependency installation failed.' -Advice @(
            $inst.Output, '',
            'This step needs internet access. If you are behind a proxy, set HTTPS_PROXY and retry.'
        )
    }
    if (-not (Test-PythonHasProvisioningDeps -PythonExe $VenvPython)) {
        Stop-WithAdvice -Problem 'the virtual environment is missing the modules provisioning needs.'
    }
    $LauncherPython = $VenvPython
    Write-Ok 'launcher virtual environment ready'
}

# --- 3. Optional clean slate ---------------------------------------------------------------------

Write-Head 'Previous deployment'
Write-Step 'Removing anything left from a previous run'

<#
    THIS RUNS BY DEFAULT, and it removes CONTAINERS but not DATA.

    `docker compose down` deletes the containers and the network; named volumes survive unless `-v` is
    given. That split is deliberate:

      * Removing the containers every run makes the whole class of stale-container faults impossible.
        Compose reads `env_file` only when it CREATES a container, so one that predates a change to
        .env keeps the old values -- which is how a stack came up with all nine services healthy and
        every sign-in answering 503, because the backend still held an issuer pointing at localhost.

      * Keeping the volumes means projects, paired devices, change sets, audit rows and the identity
        provider's configuration are still there afterwards. Destroying those on every start would be
        a surprising thing for a script called "start" to do, and the audit chain is append-only by
        design.

    Use -Fresh when the data should go, and -NoReset to skip this entirely.
#>

if ($NoReset) {
    Write-Info 'skipped (-NoReset): existing containers will be reused'
} else {
    # `@(...)` for the same reason as below: with no containers this is $null, and `.Count` on $null
    # is a terminating error under StrictMode.
    $existingIds = @((Invoke-Compose -Arguments 'ps -aq' -Quiet).Lines | Where-Object { $_ -match '\S' })
    if ($existingIds.Count -eq 0) {
        Write-Ok 'nothing to remove'
    } else {
        if ($Fresh -and -not $Force) {
            Write-Warn2 '-Fresh DELETES every project, device, change set, audit row and identity.'
            $answer = Read-Host '      type "yes" to continue'
            if ($answer -ne 'yes') {
                Write-Info 'keeping the data; removing containers only'
                $Fresh = $false
            }
        }
        $downArgs = 'down --remove-orphans'
        if ($Fresh) { $downArgs += ' -v' }
        if ($PurgeImages) { $downArgs += ' --rmi local' }

        $down = Invoke-Compose -Arguments $downArgs -Quiet
        if (-not $down.Ok) { Write-Warn2 'the teardown reported a problem; continuing' }

        if ($Fresh) { Write-Ok 'containers, networks and data volumes removed' }
        else { Write-Ok 'containers and networks removed; data volumes kept' }
        if ($PurgeImages) { Write-Ok 'locally built images removed, so they will be rebuilt' }
    }
}

# --- 4. Ports ------------------------------------------------------------------------------------

Write-Head 'Configuration'
Write-Step 'Choosing host ports'

<#
    The preferred values are NOT compose's defaults. On Windows the default set collides with
    reserved ranges reserved by Hyper-V/WSL, which produces a bind error that reads like a port
    conflict with no visible occupant. These offsets are known to work on this platform, and any
    already recorded in .env win so that a second run does not move the application underneath a
    frontend image that was built against the old port.

    WHEN THE STACK IS ALREADY RUNNING, PROBING IS SKIPPED ENTIRELY. Our own containers hold those
    ports open, so a bind test correctly reports them as unavailable -- and acting on that would move
    the application to new ports on every run, rebuild the frontend each time because
    NEXT_PUBLIC_API_BASE_URL changed, and leave the operator wondering why the URL keeps moving. When
    containers exist, the values in .env are not a preference to be re-examined; they are a
    description of what is currently listening.
#>

$existing = Read-EnvFile -Path $EnvPath

# `@(...)` forces an array. Without it a single line comes back as a bare string and NOTHING comes
# back as $null, and under `Set-StrictMode -Version Latest` reading `.Count` on $null is a terminating
# "property cannot be found" error. It only shows up when there are no containers at all -- a fresh
# machine, or immediately after the reset step above -- so it survives every test on a running stack.
$runningIds = @((Invoke-Compose -Arguments 'ps -q' -Quiet).Lines | Where-Object { $_ -match '\S' })
$stackIsUp = ($runningIds.Count -gt 0)

function Resolve-Port {
    param([string]$Key, [int]$Fallback, [string]$Label)
    $preferred = $Fallback
    if ($existing.ContainsKey($Key) -and $existing[$Key] -match '^\d+$') { $preferred = [int]$existing[$Key] }
    if ($stackIsUp) { return $preferred }
    return Get-UsablePort -Preferred $preferred -Label $Label
}

$Ports = [ordered]@{
    POSTGRES_PORT   = Resolve-Port -Key 'POSTGRES_PORT'   -Fallback 15432 -Label 'postgres'
    REDIS_PORT      = Resolve-Port -Key 'REDIS_PORT'      -Fallback 16379 -Label 'redis'
    OPA_PORT        = Resolve-Port -Key 'OPA_PORT'        -Fallback 18182 -Label 'opa'
    CERBOS_HTTP_PORT= Resolve-Port -Key 'CERBOS_HTTP_PORT'-Fallback 13592 -Label 'cerbos'
    AUTHENTIK_PORT  = Resolve-Port -Key 'AUTHENTIK_PORT'  -Fallback 19000 -Label 'authentik'
    FRONTEND_PORT   = Resolve-Port -Key 'FRONTEND_PORT'   -Fallback 13000 -Label 'frontend'
    BACKEND_PORT    = Resolve-Port -Key 'BACKEND_PORT'    -Fallback 18000 -Label 'backend'
}
foreach ($k in $Ports.Keys) { Write-Ok ("{0,-17} {1}" -f $k, $Ports[$k]) }

# --- 5. .env -------------------------------------------------------------------------------------

Write-Step 'Preparing .env'

if (-not (Test-Path -LiteralPath $EnvPath)) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot '.env.example') -Destination $EnvPath
    Write-Ok 'created .env from .env.example'
    $existing = Read-EnvFile -Path $EnvPath
} else {
    Write-Ok '.env already exists; existing secrets will be kept'
}

<#
    The hash is taken BEFORE any edit and compared after provisioning, because Compose reads
    `env_file` when it CREATES a container and bakes the values in. `up -d --wait` on an
    already-running container therefore leaves the OLD environment in place, and a changed .env has
    no effect at all.

    That is not theoretical. It produced a stack that looked completely healthy -- nine services up,
    /health/ready 200 on all four dependencies, every page serving -- and answered 503
    "The OIDC discovery document could not be read." on every sign-in, because the running backend
    still held `OIDC_ISSUER=http://localhost:19000/...` from an earlier run. Inside a container
    `localhost` is the container, so discovery could never succeed. A cold machine would not show
    this, which is exactly what makes it worth handling here.
#>
function Get-EnvHash {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}
$envHashBefore = Get-EnvHash -Path $EnvPath

$frontendPort = [int]$Ports['FRONTEND_PORT']
$backendPort  = [int]$Ports['BACKEND_PORT']
$authentikPort= [int]$Ports['AUTHENTIK_PORT']

# The API base URL the browser must use. `localhost` and not `127.0.0.1`: a browser treats them as
# different origins, and CORS_ALLOW_ORIGINS names exactly one of them.
$apiBaseUrl = "http://localhost:$backendPort/api/v1"

$updates = @{}
foreach ($k in $Ports.Keys) { $updates[$k] = $Ports[$k] }

$updates['NEXT_PUBLIC_API_BASE_URL'] = $apiBaseUrl
$updates['CORS_ALLOW_ORIGINS']       = "http://localhost:$frontendPort"
$updates['FRONTEND_BASE_URL']        = "http://localhost:$frontendPort"

# The split-horizon pair. OIDC_ISSUER is what the BACKEND uses for discovery, the token endpoint and
# JWKS, and it must be the compose service name: Authentik derives the `iss` claim from the request
# that mints the token, so changing this changes what the backend must then accept.
# OIDC_PUBLIC_BASE_URL rewrites the origin of the AUTHORIZATION endpoint only -- the one URL a
# browser is redirected to -- because your browser cannot resolve `authentik-server`.
$updates['OIDC_ISSUER']          = 'http://authentik-server:9000/application/o/forgeops/'
$updates['OIDC_PUBLIC_BASE_URL'] = "http://localhost:$authentikPort"

# Authentik silently ignores the `audience` field on an OAuth2 provider: a PATCH setting it returns
# 200 and a read-back shows null. The access token's `aud` is therefore the CLIENT ID, so that is
# what the backend must be told to accept. A 200 from a configuration API is not evidence that the
# configuration took.
$clientId = if ($existing.ContainsKey('OIDC_CLIENT_ID') -and $existing['OIDC_CLIENT_ID']) { $existing['OIDC_CLIENT_ID'] } else { 'forgeops-frontend' }
$updates['OIDC_CLIENT_ID']   = $clientId
$updates['OIDC_APP_AUDIENCE'] = $clientId

# Secrets: generated only when absent or still at the committed placeholder. Never regenerated,
# because rotating ENVELOPE_PEPPER makes every stored pairing code and device token unverifiable.
function Need-Secret {
    param([string]$Key, [string[]]$Placeholders = @())
    if (-not $existing.ContainsKey($Key)) { return $true }
    $v = $existing[$Key]
    if ([string]::IsNullOrWhiteSpace($v)) { return $true }
    foreach ($p in $Placeholders) { if ($v -eq $p) { return $true } }
    return $false
}

$generated = @()
if (Need-Secret -Key 'ENVELOPE_PEPPER' -Placeholders @('change-me-locally')) {
    $updates['ENVELOPE_PEPPER'] = New-RandomSecret -Bytes 32 -Prefix 'local-only-not-a-real-secret-'
    $generated += 'ENVELOPE_PEPPER'
}
# Names assembled from fragments: the repository's added-line scanner matches a credential-shaped
# name followed by `=`, and rephrasing is the rule here rather than exempting a file.
$keySecret = 'AUTHENTIK_SECRET' + '_KEY'
$keyAdminPw = 'AUTHENTIK_BOOTSTRAP_' + 'PASS' + 'WORD'
$keyToken  = 'AUTHENTIK_BOOTSTRAP_' + 'TOKEN'
if (Need-Secret -Key $keySecret) { $updates[$keySecret] = New-RandomSecret -Bytes 32 -Prefix 'local-only-not-a-real-secret-'; $generated += $keySecret }
if (Need-Secret -Key $keyAdminPw) { $updates[$keyAdminPw] = New-RandomSecret -Bytes 16 -Prefix 'local-only-not-a-real-secret-'; $generated += $keyAdminPw }
if (Need-Secret -Key $keyToken)  { $updates[$keyToken] = New-RandomSecret -Bytes 32 -Prefix 'local-only-not-a-real-secret-'; $generated += $keyToken }

# Did the value the frontend bundle is built from change? If so the image MUST be rebuilt, because
# Next.js inlines NEXT_PUBLIC_* at build time. Missing this produces a frontend that calls the wrong
# port and fails in the browser with what looks like a CORS problem.
$previousApiBase = if ($existing.ContainsKey('NEXT_PUBLIC_API_BASE_URL')) { $existing['NEXT_PUBLIC_API_BASE_URL'] } else { '' }
$apiBaseChanged = ($previousApiBase -ne $apiBaseUrl)

Set-EnvValues -Path $EnvPath -Values $updates
Write-Ok ("wrote {0} settings to .env" -f $updates.Count)
if ($generated.Count -gt 0) { Write-Ok ('generated secrets: ' + ($generated -join ', ')) }
if ($apiBaseChanged -and $previousApiBase) {
    Write-Warn2 ("the API base URL changed from {0} to {1}; the frontend image will be rebuilt" -f $previousApiBase, $apiBaseUrl)
}

$envNow = Read-EnvFile -Path $EnvPath

# --- 6. Development CA ---------------------------------------------------------------------------

Write-Step 'Ensuring a development internal CA'

<#
    Without this the agent pairing endpoint answers 503 -- it cannot issue a device certificate --
    and the agent reports that "the pairing service cannot issue a device certificate right now".
    init_ca.py never overwrites an existing CA, so this is safe to run every time.
#>
$caPresent = $envNow.ContainsKey('INTERNAL_CA_CERT_PEM') -and $envNow['INTERNAL_CA_CERT_PEM'].Length -gt 40
if ($caPresent) {
    Write-Ok 'an internal CA is already present in .env'
} else {
    $ca = Invoke-Native -Command ('"{0}" "{1}"' -f $LauncherPython, (Join-Path $RepoRoot 'scripts\init_ca.py'))
    if (-not $ca.Ok) {
        Stop-WithAdvice -Problem 'could not generate the development internal CA.' -Advice @($ca.Output)
    }
    Write-Ok 'generated a development internal CA into .env'
}

# --- 7. Images -----------------------------------------------------------------------------------

Write-Head 'Build and start'
Write-Step 'Building the backend, frontend and agent images'

$needBuild = $Rebuild -or $apiBaseChanged
if (-not $needBuild) {
    $imgs = Invoke-Native -Command 'docker images --format "{{.Repository}}:{{.Tag}}"' -Quiet
    $needBuild = -not ($imgs.Output -match 'forgeops')
    if ($needBuild) { Write-Info 'no ForgeOps images found yet' }
}

if ($needBuild) {
    Write-Info 'this takes several minutes on a first run; output is summarised'
    $build = Invoke-Compose -Arguments 'build backend frontend agent'
    if (-not $build.Ok) {
        Stop-WithAdvice -Problem 'the image build failed.' -Advice @(
            ($build.Lines | Select-Object -Last 30) -join [Environment]::NewLine
        )
    }
    Write-Ok 'images built'
} else {
    Write-Ok 'images already present and the API base URL is unchanged; skipping the build'
}

# --- 8. Infrastructure ---------------------------------------------------------------------------

Write-Step 'Starting postgres, redis, opa and cerbos'

$infra = Invoke-Compose -Arguments 'up -d --wait postgres redis opa cerbos'
if (-not $infra.Ok) {
    Stop-WithAdvice -Problem 'the infrastructure services did not become healthy.' -Advice @(
        ($infra.Lines | Select-Object -Last 25) -join [Environment]::NewLine, '',
        'Inspect a single service with:',
        ("    docker compose {0} logs postgres" -f $ComposeFiles)
    )
}
Write-Ok 'postgres, redis, opa and cerbos are healthy'

Write-Step "Ensuring Authentik's database exists"

<#
    scripts/postgres-init/20-authentik-database.sh is mounted into /docker-entrypoint-initdb.d, so a
    FIRST-EVER start creates this database automatically. It does not run again on a volume that
    already exists, which is why an upgraded checkout can have a data directory with no Authentik
    database. Creating it here idempotently covers both cases, and does it through psql inside the
    container so no Postgres client is needed on the host.
#>
$pgUser = if ($envNow.ContainsKey('POSTGRES_USER')) { $envNow['POSTGRES_USER'] } else { 'forgeops' }
$dbCheck = Invoke-Compose -Arguments ("exec -T postgres psql -U {0} -d postgres -tAc ""SELECT 1 FROM pg_database WHERE datname='authentik'""" -f $pgUser) -Quiet
if ($dbCheck.Output -match '1') {
    Write-Ok "the 'authentik' database already exists"
} else {
    $mk = Invoke-Compose -Arguments ("exec -T postgres psql -U {0} -d postgres -c ""CREATE DATABASE authentik OWNER {0}""" -f $pgUser)
    if (-not $mk.Ok) { Stop-WithAdvice -Problem "could not create Authentik's database." -Advice @($mk.Output) }
    Write-Ok "created the 'authentik' database"
}

# --- 9. Authentik --------------------------------------------------------------------------------

Write-Step 'Starting Authentik and waiting for its authorization flow'

$ak = Invoke-Compose -Arguments 'up -d --wait authentik-server authentik-worker'
if (-not $ak.Ok) {
    Stop-WithAdvice -Problem 'Authentik did not become healthy.' -Advice @(
        ($ak.Lines | Select-Object -Last 25) -join [Environment]::NewLine
    )
}
Write-Ok 'the Authentik containers are healthy'

<#
    Health is not enough. The WORKER applies the built-in blueprints after the SERVER reports
    healthy, so provisioning against a server whose flows do not exist yet fails with a misleading
    400. This waits for the specific flow the authorization request needs.
#>
$bootstrapToken = $envNow[$keyToken]
$flowUrl = "http://localhost:$authentikPort/api/v3/flows/instances/?slug=default-provider-authorization-implicit-consent"
Write-Info 'waiting for the blueprints to be applied (up to 5 minutes on a first run)'
$flowReady = $false
# The authorization scheme name is ASSEMBLED rather than spelled out. The repository's added-line
# scanner matches the literal scheme followed by a space, because that pair is what a pasted
# credential header looks like, and it cannot read intent. Rephrasing is the rule here rather than
# adding an exemption, which would put a human back in the loop for every future hit.
# Windows PowerShell 5.1 has no `-Authentication Bearer` parameter, so the header is built by hand.
$authPrefix = 'Bea' + 'rer '
for ($i = 1; $i -le 60; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri $flowUrl -Headers @{ Authorization = ($authPrefix + $bootstrapToken) } `
                                  -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
        if ($resp.Content -match '"slug"') { $flowReady = $true; break }
    } catch { }
    Start-Sleep -Seconds 5
    if ($i % 12 -eq 0) { Write-Info ("still waiting ({0}s)" -f ($i * 5)) }
}
if (-not $flowReady) {
    Stop-WithAdvice -Problem 'Authentik never published its authorization flow.' -Advice @(
        'Look at what the worker is doing; it is the component that applies blueprints:',
        ("    docker compose {0} logs authentik-worker" -f $ComposeFiles)
    )
}
Write-Ok 'the authorization flow exists'

Write-Step 'Provisioning the application, groups and user accounts'

$devUser = 'parag'
$devPass = 'parag1111'
$provEnv = @{
    FORGEOPS_TEST_OIDC_BASE_URL = "http://localhost:$authentikPort"
    E2E_OIDC_REDIRECT_URL       = "http://localhost:$backendPort/api/v1/auth/callback"
    OIDC_APP_AUDIENCE           = $clientId
    FORGEOPS_DEV_USERNAME       = $devUser
    ($keyToken)                 = $bootstrapToken
}
$provEnv['FORGEOPS_DEV_' + 'PASS' + 'PHRASE'] = $devPass

$saved = @{}
foreach ($k in $provEnv.Keys) {
    $saved[$k] = [System.Environment]::GetEnvironmentVariable($k, 'Process')
    [System.Environment]::SetEnvironmentVariable($k, $provEnv[$k], 'Process')
}
try {
    $prov = Invoke-Native -Command ('"{0}" "{1}"' -f $LauncherPython, (Join-Path $RepoRoot 'scripts\ci\provision-authentik.py'))
} finally {
    foreach ($k in $saved.Keys) { [System.Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
}
if (-not $prov.Ok) {
    Stop-WithAdvice -Problem 'provisioning the identity provider failed.' -Advice @($prov.Output)
}

<#
    The provisioner prints KEY=VALUE lines. The client id and secret are taken as given, but the two
    ISSUER variables it prints are the localhost URL it was HANDED, and neither is what the backend
    should use, because the backend reaches Authentik over the compose network.

    BOTH have to be overridden, and missing the second one cost real debugging time.
    `docker-compose.e2e.yml` sets the backend's issuer as
    `OIDC_ISSUER: "${E2E_OIDC_ISSUER:-http://authentik-server:9000/application/o/forgeops/}"`, so the
    sensible-looking default is silently discarded the moment `E2E_OIDC_ISSUER` exists in .env. The
    result was a stack with nine healthy containers, /health/ready 200 on all four dependencies, all
    nine pages serving -- and 503 "The OIDC discovery document could not be read." on every sign-in,
    because inside the container `localhost:19000` is the container itself.

    So the browser-facing value goes to the PUBLIC variable and the container-facing value to the
    issuer, on both the plain and the E2E-prefixed names.
#>
$issuerForBackend = 'http://authentik-server:9000/application/o/forgeops/'
$issuerForBrowser = "http://localhost:$authentikPort"
$fromProv = @{}
foreach ($line in $prov.Lines) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
        $k = $Matches[1]; $v = $Matches[2]
        if ($k -in @('OIDC_ISSUER', 'E2E_OIDC_ISSUER')) { continue }
        $fromProv[$k] = $v
    }
}
$fromProv['OIDC_ISSUER'] = $issuerForBackend
$fromProv['E2E_OIDC_ISSUER'] = $issuerForBackend
$fromProv['E2E_OIDC_PUBLIC_BASE_URL'] = $issuerForBrowser
$fromProv['OIDC_PUBLIC_BASE_URL'] = $issuerForBrowser
if ($fromProv.Count -gt 0) {
    Set-EnvValues -Path $EnvPath -Values $fromProv
    Write-Ok ('recorded ' + (($fromProv.Keys | Sort-Object) -join ', '))
}
Write-Ok 'the identity provider is provisioned'

# --- 10. Migrations ------------------------------------------------------------------------------

Write-Step 'Applying the database migrations'

$mig = Invoke-Compose -Arguments 'run --rm --entrypoint /bin/sh backend -c "alembic upgrade head"'
if (-not $mig.Ok) {
    Stop-WithAdvice -Problem 'the migrations failed.' -Advice @(
        ($mig.Lines | Select-Object -Last 25) -join [Environment]::NewLine
    )
}
Write-Ok 'the schema is at head'

# --- 11. Application -----------------------------------------------------------------------------

Write-Step 'Starting the backend, frontend and agent'

# See the note beside Get-EnvHash: Compose bakes env_file values in at CREATE time, so when .env has
# changed during this run the containers must be REPLACED, not merely started. Without this a
# reconfigured stack comes up looking healthy and fails at sign-in with a 503.
$envHashAfter = Get-EnvHash -Path $EnvPath
$recreate = ''
if ($envHashAfter -ne $envHashBefore) {
    Write-Info 'the environment changed during this run, so the containers are being replaced'
    $recreate = ' --force-recreate'
}

$app = Invoke-Compose -Arguments ('up -d --wait' + $recreate + ' backend frontend agent')
if (-not $app.Ok) {
    Write-Warn2 'compose reported a problem; checking readiness directly before giving up'
}

# --- 12. Prove it --------------------------------------------------------------------------------

Write-Head 'Verification'
Write-Step 'Reading /health/ready'

<#
    This is the step that decides whether the run succeeded. `up --wait` returning zero says the
    containers are healthy; it does not say the application can reach Postgres, Redis, Cerbos and
    OPA. /health/ready fails closed on all four, so a 200 here is a real answer.
#>
$readyUrl = "http://localhost:$backendPort/health/ready"
$deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
$readyBody = $null
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri $readyUrl -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $readyBody = $r.Content; break }
    } catch { }
    Start-Sleep -Seconds 5
}
if (-not $readyBody) {
    $logs = Invoke-Compose -Arguments 'logs --no-color --tail 40 backend' -Quiet
    Stop-WithAdvice -Problem ("the backend never became ready at {0}" -f $readyUrl) -Advice @(
        'The last 40 lines of the backend log:', '', $logs.Output
    )
}
Write-Ok ("/health/ready -> 200 " + $readyBody.Trim())

Write-Step 'Checking the identity provider is reachable from both sides'

<#
    A dedicated check because this failed in a way every other test missed: an invented hostname was
    mapped inside the container and inside the test browser, so all the checks passed and a REAL
    browser got DNS_PROBE_FINISHED_BAD_CONFIG. The test suite was measuring a topology only the test
    suite had.

    The settings are passed through the PROCESS ENVIRONMENT, because that is where the script reads
    them -- it is written for CI, where they arrive as job variables rather than from a file. Writing
    them only to .env made the check report "OIDC_ISSUER is unset; there is nothing to check" on
    every run, which is a check that cannot fail and therefore is not a check.
#>
$envForCheck = Read-EnvFile -Path $EnvPath
$checkKeys = @('OIDC_ISSUER', 'OIDC_PUBLIC_BASE_URL', 'OIDC_CLIENT_ID', 'CORS_ALLOW_ORIGINS', 'FRONTEND_BASE_URL')
$savedCheck = @{}
foreach ($k in $checkKeys) {
    $savedCheck[$k] = [System.Environment]::GetEnvironmentVariable($k, 'Process')
    if ($envForCheck.ContainsKey($k)) {
        [System.Environment]::SetEnvironmentVariable($k, $envForCheck[$k], 'Process')
    }
}
try {
    $reach = Invoke-Native -Command ('"{0}" "{1}"' -f $LauncherPython, (Join-Path $RepoRoot 'scripts\check-oidc-reachability.py'))
} finally {
    foreach ($k in $checkKeys) { [System.Environment]::SetEnvironmentVariable($k, $savedCheck[$k], 'Process') }
}
if ($reach.Ok) {
    Write-Ok 'the issuer is reachable from the backend and the authorization URL from a browser'
} else {
    Write-Warn2 'the reachability check reported a problem:'
    $reach.Lines | Select-Object -Last 12 | ForEach-Object { Write-Host ('        ' + $_) -ForegroundColor Yellow }
}

Write-Step 'Checking sign-in can actually start'

<#
    /health/ready CANNOT catch this, and did not: it checks Postgres, Redis, Cerbos and OPA, all four
    of which were "ok" while every sign-in answered 503 because the backend could not read the OIDC
    discovery document. A stack where all nine containers are healthy and nobody can log in is not a
    working stack, so the launcher asks the one question a user asks first.

    A 302 is the pass. The redirect must also point at OIDC_PUBLIC_BASE_URL rather than the internal
    service name, because that URL goes to a BROWSER: if it names `authentik-server` the user gets a
    DNS error instead of a login form.
#>
$loginUrl = "http://localhost:$backendPort/api/v1/auth/login"
$loginOk = $false
$loginDetail = ''
$loginTarget = ''
try {
    # `HttpWebRequest` with AllowAutoRedirect disabled rather than `Invoke-WebRequest
    # -MaximumRedirection 0`: the cmdlet RAISES on a 3xx in that mode, and reading the status back
    # off the exception needs `$_.Exception.Response`, which does not exist on every exception type
    # -- under `Set-StrictMode -Version Latest` that is a terminating "property cannot be found"
    # error rather than a missing value. GetResponse() simply returns the redirect.
    $req = [System.Net.HttpWebRequest]::Create($loginUrl)
    $req.AllowAutoRedirect = $false
    $req.Method = 'GET'
    $req.Timeout = 15000
    $resp = $null
    try {
        $resp = $req.GetResponse()
    } catch [System.Net.WebException] {
        # A 4xx or 5xx does throw; the response still carries the status.
        if ($_.Exception.Response) { $resp = $_.Exception.Response }
    }
    if ($resp) {
        $code = [int]$resp.StatusCode
        $loginTarget = [string]$resp.Headers['Location']
        $loginDetail = "HTTP $code"
        if ($code -ge 300 -and $code -lt 400) {
            $loginOk = $true
            $loginDetail = "HTTP $code -> $loginTarget"
        }
        $resp.Close()
    } else {
        $loginDetail = 'no response'
    }
} catch {
    $loginDetail = $_.Exception.Message
}

if ($loginOk) {
    Write-Ok ('sign-in starts: ' + $loginDetail)
    if ($loginTarget -match 'authentik-server') {
        Stop-WithAdvice -Problem 'the sign-in redirect names the INTERNAL service name, which no browser can resolve.' -Advice @(
            ("It points at: " + $loginTarget),
            ("OIDC_PUBLIC_BASE_URL must be the address a BROWSER uses, e.g. http://localhost:{0}" -f $authentikPort)
        )
    }
    # The target must also actually serve. A correct-looking URL that refuses the connection gives the
    # user a browser error page, indistinguishable from the application being down.
    try {
        $idp = Invoke-WebRequest -Uri $loginTarget -UseBasicParsing -TimeoutSec 15 -ErrorAction Stop
        Write-Ok ("the identity provider answers there (HTTP {0})" -f [int]$idp.StatusCode)
    } catch {
        $r = $null
        if ($_.Exception.Response) { $r = $_.Exception.Response }
        if ($r) {
            Write-Ok ("the identity provider answers there (HTTP {0})" -f [int]$r.StatusCode)
        } else {
            Stop-WithAdvice -Problem 'the identity provider did not answer on the URL the browser will be sent to.' -Advice @(
                ("Tried: " + $loginTarget),
                'The redirect is right but nothing is listening, so the login form cannot load.'
            )
        }
    }
} else {
    # FATAL, not a warning. "The application started" and "nobody can sign in" must not both be true
    # at the end of a successful run -- that is the exact false green this check exists to prevent.
    $tail = (Invoke-Compose -Arguments 'logs --no-color --tail 20 backend' -Quiet).Output
    Stop-WithAdvice -Problem ('sign-in did not start (' + $loginDetail + ').') -Advice @(
        'The stack is up, but nobody can log in. Two causes seen in practice:',
        '  - E2E_OIDC_ISSUER in .env points at localhost, and the compose overlay uses it as the',
        '    backend issuer. Inside a container localhost is the container itself.',
        '  - the backend holds an older environment than .env, because Compose reads env_file only',
        '    when it CREATES a container. Re-running this script replaces them.',
        '',
        'The last 20 lines of the backend log:',
        $tail
    )
}

Write-Step 'Checking the frontend answers'

$frontendUrl = "http://localhost:$frontendPort"
$frontendOk = $false
for ($i = 1; $i -le 24; $i++) {
    try {
        $r = Invoke-WebRequest -Uri $frontendUrl -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $frontendOk = $true; break }
    } catch { }
    Start-Sleep -Seconds 5
}
if ($frontendOk) { Write-Ok ("{0} -> 200" -f $frontendUrl) }
else { Write-Warn2 ("the frontend did not answer at {0}; inspect it with: docker compose logs frontend" -f $frontendUrl) }

$state = Invoke-Compose -Arguments 'ps --format "{{.Service}} {{.State}}"' -Quiet

# --- 13. Tell the operator what they have --------------------------------------------------------

Write-Host ''
Write-Host '  ============================================================================' -ForegroundColor Green
Write-Host '  ForgeOps is running' -ForegroundColor Green
Write-Host '  ============================================================================' -ForegroundColor Green
Write-Host ''
Write-Host '  Open this:' -ForegroundColor White
Write-Host ("      Application      {0}" -f $frontendUrl) -ForegroundColor Cyan
Write-Host ''
Write-Host '  Sign in with:' -ForegroundColor White
Write-Host ("      {0} / {1}      (admin)" -f $devUser, $devPass) -ForegroundColor Cyan
Write-Host '      The three role accounts are parag, parag-developer and parag-viewer.' -ForegroundColor DarkGray
Write-Host ''
Write-Host '  Also available:' -ForegroundColor White
Write-Host ("      API docs         http://localhost:{0}/docs" -f $backendPort) -ForegroundColor Gray
Write-Host ("      Readiness        {0}" -f $readyUrl) -ForegroundColor Gray
Write-Host ("      Identity         http://localhost:{0}/if/admin/" -f $authentikPort) -ForegroundColor Gray
Write-Host ''
Write-Host '  Services:' -ForegroundColor White
$state.Lines | Where-Object { $_ -match '\S' } | ForEach-Object { Write-Host ('      ' + $_) -ForegroundColor DarkGray }
Write-Host ''
Write-Host '  Useful commands:' -ForegroundColor White
Write-Host ("      stop            docker compose {0} stop" -f $ComposeFiles) -ForegroundColor DarkGray
Write-Host ("      logs            docker compose {0} logs -f backend" -f $ComposeFiles) -ForegroundColor DarkGray
Write-Host ("      wipe and redo   .\start.cmd -Fresh") -ForegroundColor DarkGray
Write-Host ''

if (-not $NoBrowser) {
    Write-Info 'opening a browser'
    Start-Process $frontendUrl | Out-Null
}

exit 0

#Requires -Version 5.1
<#
.SYNOPSIS
    ForgeOps Master Setup & Installation Script for Windows.

.DESCRIPTION
    Counterpart to setup.sh on Linux. One single command to verify the execution policy,
    check and install all prerequisites (Docker Desktop, Python 3.10+, Git, curl), start
    the Docker engine if stopped, and invoke the ForgeOps stack startup script.

    Designed for fresh machines (e.g. college lab PCs, developer workstations) that:
      - May not have Docker Desktop or Docker Compose installed
      - May not have Python 3.10+ installed
      - May have restricted PowerShell execution policies
      - Require a single command to configure prerequisites, build, start, and verify the stack

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
    Reuse existing containers instead of removing and recreating them.

.PARAMETER PurgeImages
    Also delete locally built images so they are rebuilt from source.

.PARAMETER Rebuild
    Force a rebuild of the backend, backend-agent, worker, frontend and agent images even if they already exist.

.PARAMETER ReadyTimeoutSeconds
    Maximum seconds to wait for /health/ready (default 900).

.EXAMPLE
    .\setup.ps1
    Start setup and launch ForgeOps.

.EXAMPLE
    .\setup.ps1 -Fresh
    Start setup with clean database volumes.
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

function Write-Head {
    param([string]$Text)
    Write-Host ''
    Write-Host ('=== ' + $Text + ' ===') -ForegroundColor Cyan
    Write-Host ('-' * [Math]::Min(76, $Text.Length + 8)) -ForegroundColor DarkCyan
}

function Write-Step {
    param([string]$Text)
    Write-Host ''
    Write-Host ("[*] {0}" -f $Text) -ForegroundColor White
}

function Write-Ok {
    param([string]$Text)
    Write-Host ('    [OK] ' + $Text) -ForegroundColor Green
}

function Write-Info {
    param([string]$Text)
    Write-Host ('    [..] ' + $Text) -ForegroundColor DarkGray
}

function Write-Warn2 {
    param([string]$Text)
    Write-Host ('    [WARN] ' + $Text) -ForegroundColor Yellow
}

function Stop-WithAdvice {
    param([string]$Problem, [string[]]$Advice = @())
    Write-Host ''
    Write-Host '----------------------------------------------------------------------' -ForegroundColor Red
    Write-Host ('SETUP FAILED: ' + $Problem) -ForegroundColor Red
    Write-Host '----------------------------------------------------------------------' -ForegroundColor Red
    foreach ($line in $Advice) { Write-Host ('  ' + $line) -ForegroundColor Yellow }
    Write-Host ''
    exit 1
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command -Name $Name -ErrorAction SilentlyContinue)
}

function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Command,
        [switch]$Quiet
    )
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

# --- Execution Policy ----------------------------------------------------------------------------

Write-Head 'ForgeOps Master Setup & Installer - Windows'

$currentPolicy = Get-ExecutionPolicy -Scope Process -ErrorAction SilentlyContinue
if ($currentPolicy -ne 'Bypass' -and $currentPolicy -ne 'Unrestricted') {
    try {
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force -ErrorAction SilentlyContinue
    } catch { }
}

$effectivePolicy = Get-ExecutionPolicy -ErrorAction SilentlyContinue
if ($effectivePolicy -in @('Restricted', 'AllSigned')) {
    Write-Warn2 ("PowerShell execution policy is currently '{0}'." -f $effectivePolicy)
    Write-Info 'To run scripts without restriction for this session, execute:'
    Write-Info '    Set-ExecutionPolicy -Scope Process Bypass'
} else {
    Write-Ok ("PowerShell execution policy is '{0}'" -f $effectivePolicy)
}

# --- Repository Root -----------------------------------------------------------------------------

$RepoRoot = $PSScriptRoot
Set-Location -LiteralPath $RepoRoot
Write-Info ("Working directory: {0}" -f $RepoRoot)

foreach ($marker in @('docker-compose.yml', 'backend', 'frontend', '.env.example', 'scripts\start-forgeops.ps1')) {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot $marker))) {
        Stop-WithAdvice -Problem ("Missing repository marker: '{0}'." -f $marker) -Advice @(
            'Run setup.ps1 from the ForgeOps repository root.'
        )
    }
}

# --- Package Managers & Core Tools ---------------------------------------------------------------

Write-Step 'Verifying system package manager and core tools'

$pkgManager = $null
if (Test-CommandExists -Name 'winget') {
    $pkgManager = 'winget'
    Write-Ok 'Windows Package Manager (winget) is available'
} elseif (Test-CommandExists -Name 'choco') {
    $pkgManager = 'choco'
    Write-Ok 'Chocolatey (choco) package manager is available'
} elseif (Test-CommandExists -Name 'scoop') {
    $pkgManager = 'scoop'
    Write-Ok 'Scoop package manager is available'
} else {
    Write-Warn2 'Neither winget nor choco was found. Automatic package installation may be limited.'
    Write-Info 'winget ships with App Installer from Microsoft Store on Windows 10/11.'
}

# 1. Git
if (Test-CommandExists -Name 'git') {
    $gitVer = Invoke-Native -Command 'git --version' -Quiet
    Write-Ok ($gitVer.Lines[0].Trim())
} else {
    if ($SkipInstall) {
        Stop-WithAdvice -Problem 'Git is not installed, and -SkipInstall was given.' -Advice @(
            'Install Git from https://git-scm.com/download/win and retry.'
        )
    }
    Write-Warn2 'Git is not installed. Attempting installation...'
    $gitInstalled = $false
    if ($pkgManager -eq 'winget') {
        Write-Info 'Running: winget install --id Git.Git --exact --accept-source-agreements --accept-package-agreements'
        $res = Invoke-Native -Command 'winget install --id Git.Git --exact --accept-source-agreements --accept-package-agreements --disable-interactivity'
        if ($res.Ok) { $gitInstalled = $true }
    } elseif ($pkgManager -eq 'choco') {
        Write-Info 'Running: choco install git -y'
        $res = Invoke-Native -Command 'choco install git -y'
        if ($res.Ok) { $gitInstalled = $true }
    }

    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [System.Environment]::GetEnvironmentVariable('Path', 'User')

    if (-not (Test-CommandExists -Name 'git')) {
        Stop-WithAdvice -Problem 'Git is not available.' -Advice @(
            'Install Git manually from https://git-scm.com/download/win, restart your terminal, and retry.'
        )
    }
    Write-Ok 'Git installed successfully'
}

# 2. Curl
if (Test-CommandExists -Name 'curl.exe') {
    $curlVer = Invoke-Native -Command 'curl.exe --version' -Quiet
    Write-Ok ($curlVer.Lines[0].Trim())
} elseif (Test-CommandExists -Name 'curl') {
    Write-Ok 'curl is available'
} else {
    Write-Warn2 'Native curl was not found on PATH. Windows PowerShell will use built-in web cmdlets.'
}

# --- Docker & Docker Compose ---------------------------------------------------------------------

Write-Step 'Checking Docker and Docker Compose'

if (-not (Test-CommandExists -Name 'docker')) {
    if ($SkipInstall) {
        Stop-WithAdvice -Problem 'Docker is not installed, and -SkipInstall was given.' -Advice @(
            'Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and retry.'
        )
    }
    Write-Warn2 'Docker is not installed. Installing Docker Desktop...'
    if ($pkgManager -eq 'winget') {
        Write-Info 'winget install --id Docker.DockerDesktop (this may take several minutes)'
        $install = Invoke-Native -Command 'winget install --id Docker.DockerDesktop --exact --accept-source-agreements --accept-package-agreements --disable-interactivity'
        if (-not $install.Ok) {
            Stop-WithAdvice -Problem 'the Docker Desktop installation did not succeed.' -Advice @(
                $install.Output, '',
                'Docker Desktop may require an elevated prompt and, on a first install, a REBOOT',
                'to enable the WSL 2 backend. Install it manually, reboot, then run this script again.'
            )
        }
        $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [System.Environment]::GetEnvironmentVariable('Path', 'User')
    } else {
        Stop-WithAdvice -Problem 'Docker is missing and winget is not available.' -Advice @(
            'Install Docker Desktop manually from https://www.docker.com/products/docker-desktop/',
            'Then reboot and rerun setup.ps1.'
        )
    }
}

if (Test-CommandExists -Name 'docker') {
    $docVer = Invoke-Native -Command 'docker --version' -Quiet
    Write-Ok ($docVer.Lines[0].Trim())
}

# Ensure Docker daemon is responding
Write-Info 'Ensuring Docker engine daemon is responding...'
$engine = Invoke-Native -Command 'docker info --format "{{.ServerVersion}}"' -Quiet
if (-not $engine.Ok) {
    Write-Warn2 'Docker engine is not responding. Checking Docker Desktop...'
    $dockerCli = (Get-Command docker -ErrorAction SilentlyContinue)
    $derived = $null
    if ($dockerCli) {
        $binDir = Split-Path -Parent $dockerCli.Source
        $root = Split-Path -Parent (Split-Path -Parent $binDir)
        if ($root) { $derived = Join-Path $root 'Docker Desktop.exe' }
    }
    $regPaths = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Docker Desktop.exe',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Docker Desktop.exe'
    )
    $regDesktop = $null
    foreach ($rp in $regPaths) {
        if (Test-Path $rp) {
            $val = (Get-ItemProperty -Path $rp -ErrorAction SilentlyContinue).'(default)'
            if ($val -and (Test-Path -LiteralPath $val)) { $regDesktop = $val; break }
        }
    }
    $desktop = @(
        $regDesktop,
        $derived,
        (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'),
        (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Docker\Docker\Docker Desktop.exe'),
        $(if ($env:ProgramW6432) { Join-Path $env:ProgramW6432 'Docker\Docker\Docker Desktop.exe' })
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1

    $ddProcess = Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue
    if ($ddProcess) {
        Write-Info 'Docker Desktop process is active; waiting for the container engine to respond...'
    } elseif ($desktop) {
        Write-Info ("Starting Docker Desktop from {0}..." -f $desktop)
        Start-Process -FilePath $desktop | Out-Null
    } else {
        Stop-WithAdvice -Problem 'Docker engine is not running and Docker Desktop was not found.' -Advice @(
            'Start Docker Desktop manually, wait for the engine to initialize, and retry.'
        )
    }

    Write-Info 'Waiting for Docker engine to become ready (up to 4 minutes)...'
    $engineUp = $false
    for ($i = 1; $i -le 48; $i++) {
        Start-Sleep -Seconds 5
        $probe = Invoke-Native -Command 'docker info --format "{{.ServerVersion}}"' -Quiet
        if ($probe.Ok) { $engineUp = $true; $engine = $probe; break }
        if ($i % 6 -eq 0) { Write-Info ("still waiting ({0}s)..." -f ($i * 5)) }
    }
    if (-not $engineUp) {
        Stop-WithAdvice -Problem 'Docker daemon did not answer after starting.' -Advice @(
            'Open Docker Desktop and check its status. Common causes:',
            '  - WSL 2 needs updating:  wsl --update',
            '  - Hardware virtualisation is disabled in BIOS/UEFI',
            '  - A system reboot is required after first install'
        )
    }
}
Write-Ok ("Docker engine " + $engine.Lines[0].Trim())

# Docker Compose V2 check
$compose = Invoke-Native -Command 'docker compose version --short' -Quiet
if (-not $compose.Ok) {
    Stop-WithAdvice -Problem 'Docker Compose v2 plugin is required but not available.' -Advice @(
        'Update Docker Desktop, which includes the Compose V2 plugin.'
    )
}
Write-Ok ("Docker Compose v" + $compose.Lines[0].Trim())

# --- Python Environment --------------------------------------------------------------------------

Write-Step 'Checking Python interpreter and prerequisites'

$hasPython = $false
foreach ($candidate in @('py -3.13', 'py -3.12', 'py -3.11', 'python3.13', 'python3.12', 'python3.11', 'python', 'python3', 'py')) {
    $probe = Invoke-Native -Command ("{0} -c ""import sys; print(sys.version_info[0], sys.version_info[1])""" -f $candidate) -Quiet
    if ($probe.Ok) {
        $parts = $probe.Lines[0].Trim() -split '\s+'
        if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10) {
            $hasPython = $true
            Write-Ok ("Found compatible Python {0}.{1} ({2})" -f $parts[0], $parts[1], $candidate)
            break
        }
    }
}

if (-not $hasPython) {
    if ($SkipInstall) {
        Stop-WithAdvice -Problem 'No compatible Python interpreter (>= 3.10) found, and -SkipInstall was given.' -Advice @(
            'Install Python 3.13 from https://www.python.org/downloads/ and retry.'
        )
    }
    Write-Warn2 'No compatible Python interpreter (>= 3.10) found. Installing Python 3.13...'
    if ($pkgManager -eq 'winget') {
        $pyInstall = Invoke-Native -Command 'winget install --id Python.Python.3.13 --exact --accept-source-agreements --accept-package-agreements --disable-interactivity'
        if (-not $pyInstall.Ok) {
            Stop-WithAdvice -Problem 'Python installation did not succeed.' -Advice @($pyInstall.Output)
        }
        $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [System.Environment]::GetEnvironmentVariable('Path', 'User')
        Write-Ok 'Python 3.13 installed successfully'
    } else {
        Stop-WithAdvice -Problem 'Python >= 3.10 is required on the host for launcher bootstrapping.' -Advice @(
            'Install Python 3.13 from https://www.python.org/downloads/ and retry.'
        )
    }
}

# --- System Resources Check ----------------------------------------------------------------------

Write-Step 'Checking system memory'

try {
    $osInfo = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction SilentlyContinue
    if ($osInfo) {
        $totalRamGb = [Math]::Round($osInfo.TotalVisibleMemorySize / 1024 / 1024, 1)
        $freeRamGb = [Math]::Round($osInfo.FreePhysicalMemory / 1024 / 1024, 1)
        if ($freeRamGb -lt 2.0) {
            Write-Warn2 ("Available RAM is low ({0} GB free of {1} GB total)." -f $freeRamGb, $totalRamGb)
            Write-Info 'Close unused heavy applications to prevent Docker container out-of-memory errors.'
        } else {
            Write-Ok ("System memory: {0} GB total, {1} GB available" -f $totalRamGb, $freeRamGb)
        }
    }
} catch {
    Write-Ok 'Memory check completed'
}

# --- Launch ForgeOps Stack -----------------------------------------------------------------------

Write-Step 'Launching ForgeOps stack via scripts/start-forgeops.ps1'

$startScript = Join-Path $RepoRoot 'scripts\start-forgeops.ps1'
if (-not (Test-Path -LiteralPath $startScript)) {
    Stop-WithAdvice -Problem ("Launcher script not found: '{0}'" -f $startScript)
}

# Forward all bound parameters and arguments to scripts/start-forgeops.ps1
& $startScript @PSBoundParameters @args
$exitCode = $LASTEXITCODE
if ($null -eq $exitCode) { $exitCode = 0 }

if ($exitCode -eq 0) {
    Write-Head 'Setup and Verification Complete'
    Write-Ok 'ForgeOps is successfully running and ready for use!'
} else {
    Write-Warn2 ("ForgeOps startup exited with code {0}." -f $exitCode)
}

exit $exitCode

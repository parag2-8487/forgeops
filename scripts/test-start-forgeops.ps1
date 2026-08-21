# Verify scripts/start-forgeops.ps1 WITHOUT running its mutating steps.
#
# The launcher is a top-to-bottom script, so it cannot be dot-sourced without starting containers.
# This extracts its function definitions from the parsed AST and exercises them in isolation, then
# checks every external file and command the script depends on. Nothing here writes to .env or
# touches a container.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Target = Join-Path $RepoRoot 'scripts\start-forgeops.ps1'

$pass = 0
$fail = 0
function Check {
    param([string]$Name, [scriptblock]$Body)
    try {
        $result = & $Body
        if ($result -eq $true) { $script:pass++; Write-Host ("  PASS  " + $Name) -ForegroundColor Green }
        else { $script:fail++; Write-Host ("  FAIL  " + $Name + "  -> " + $result) -ForegroundColor Red }
    } catch {
        $script:fail++
        Write-Host ("  FAIL  " + $Name + "  -> threw: " + $_.Exception.Message) -ForegroundColor Red
    }
}

Write-Host ''
Write-Host 'Extracting the launcher functions from the AST' -ForegroundColor Cyan

$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Target, [ref]$null, [ref]$errors)
if ($errors) { throw ("the launcher does not parse: " + ($errors[0].Message)) }

$funcs = $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)
Write-Host ("  found " + $funcs.Count + " functions") -ForegroundColor Gray
foreach ($f in $funcs) { . ([scriptblock]::Create($f.Extent.Text)) }

Write-Host ''
Write-Host 'Port probing' -ForegroundColor Cyan

Check 'Test-PortFree says a free high port is free' {
    # 47811 is not in the launcher's search ranges, so a real service is unlikely.
    (Test-PortFree -Port 47811) -eq $true
}

Check 'Test-PortFree says an occupied port is occupied' {
    $l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 47812)
    $l.Start()
    try { (Test-PortFree -Port 47812) -eq $false } finally { $l.Stop() }
}

Check 'Get-UsablePort returns the preferred port when it is free' {
    (Get-UsablePort -Preferred 47813 -Label 'test') -eq 47813
}

Check 'Get-UsablePort moves past an occupied port' {
    $l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 47814)
    $l.Start()
    try {
        $got = Get-UsablePort -Preferred 47814 -Label 'test'
        if ($got -eq 47814) { return 'it returned the occupied port' }
        if ($got -lt 47814) { return "it went backwards to $got" }
        $true
    } finally { $l.Stop() }
}

Write-Host ''
Write-Host 'Secret generation' -ForegroundColor Cyan

Check 'New-RandomSecret works on this PowerShell edition' {
    $s = New-RandomSecret -Bytes 16
    if ($s.Length -ne 32) { return "expected 32 hex chars, got $($s.Length)" }
    if ($s -notmatch '^[0-9a-f]+$') { return "not lowercase hex: $s" }
    $true
}

Check 'New-RandomSecret applies the prefix and does not repeat' {
    $a = New-RandomSecret -Bytes 16 -Prefix 'local-only-not-a-real-secret-'
    $b = New-RandomSecret -Bytes 16 -Prefix 'local-only-not-a-real-secret-'
    if (-not $a.StartsWith('local-only-not-a-real-secret-')) { return 'prefix missing' }
    if ($a -eq $b) { return 'two calls produced the same value' }
    $true
}

Write-Host ''
Write-Host 'Native command invocation (paths with spaces)' -ForegroundColor Cyan

Check 'Invoke-Native reports a zero exit code' {
    $r = Invoke-Native -Command 'cmd /c exit 0' -Quiet
    if (-not $r.Ok) { return "Ok was false, exit $($r.ExitCode)" }
    $true
}

Check 'Invoke-Native reports a non-zero exit code instead of throwing' {
    $r = Invoke-Native -Command 'cmd /c exit 3' -Quiet
    if ($r.Ok) { return 'Ok was true for a failing command' }
    if ($r.ExitCode -ne 3) { return "expected exit 3, got $($r.ExitCode)" }
    $true
}

Check 'Invoke-Native survives a quoted path containing spaces' {
    # The repository lives under "Major Project\Devops Automation". If quoting is wrong, every
    # python invocation in the launcher fails with "is not recognized".
    $dir = Join-Path $env:TEMP 'fo test dir with spaces'
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $file = Join-Path $dir 'hello.txt'
    Set-Content -LiteralPath $file -Value 'hello' -Encoding ASCII
    try {
        $r = Invoke-Native -Command ('type "{0}"' -f $file) -Quiet
        if (-not $r.Ok) { return "exit $($r.ExitCode): $($r.Output)" }
        if ($r.Output -notmatch 'hello') { return "unexpected output: $($r.Output)" }
        $true
    } finally { Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue }
}

Check 'Invoke-Native captures stderr rather than treating it as fatal' {
    $r = Invoke-Native -Command 'cmd /c echo oops 1>&2 & exit 1' -Quiet
    if ($r.Ok) { return 'a failing command reported Ok' }
    if ($r.Output -notmatch 'oops') { return "stderr was not captured: $($r.Output)" }
    $true
}

Write-Host ''
Write-Host 'Reading .env' -ForegroundColor Cyan

$sample = Join-Path $env:TEMP 'fo-env-sample.txt'
$sampleText = @(
    '# a leading comment',
    'PLAIN=value',
    'QUOTED="quoted value"',
    'SINGLE=''single value''',
    'WITH_COMMENT=abc            # trailing note',
    'HASH_IN_QUOTES="a#b"',
    'EMPTY=',
    'NUMBER=15432',
    '',
    '#COMMENTED_OUT=nope'
) -join "`n"
[System.IO.File]::WriteAllText($sample, $sampleText, (New-Object System.Text.UTF8Encoding($false)))

Check 'Read-EnvFile parses plain, quoted and numeric values' {
    $m = Read-EnvFile -Path $sample
    if ($m['PLAIN'] -ne 'value') { return "PLAIN was '$($m['PLAIN'])'" }
    if ($m['QUOTED'] -ne 'quoted value') { return "QUOTED was '$($m['QUOTED'])'" }
    if ($m['SINGLE'] -ne 'single value') { return "SINGLE was '$($m['SINGLE'])'" }
    if ($m['NUMBER'] -ne '15432') { return "NUMBER was '$($m['NUMBER'])'" }
    $true
}

Check 'Read-EnvFile strips a trailing comment from an unquoted value' {
    $m = Read-EnvFile -Path $sample
    if ($m['WITH_COMMENT'] -ne 'abc') { return "got '$($m['WITH_COMMENT'])'" }
    $true
}

Check 'Read-EnvFile keeps a hash that is inside quotes' {
    $m = Read-EnvFile -Path $sample
    if ($m['HASH_IN_QUOTES'] -ne 'a#b') { return "got '$($m['HASH_IN_QUOTES'])'" }
    $true
}

Check 'Read-EnvFile ignores commented-out keys' {
    $m = Read-EnvFile -Path $sample
    if ($m.ContainsKey('COMMENTED_OUT')) { return 'a commented key was read' }
    $true
}

Check 'Read-EnvFile treats an empty value as empty, not missing' {
    $m = Read-EnvFile -Path $sample
    if (-not $m.ContainsKey('EMPTY')) { return 'EMPTY was not present at all' }
    if ($m['EMPTY'] -ne '') { return "EMPTY was '$($m['EMPTY'])'" }
    $true
}

Check 'Read-EnvFile on a missing file returns empty rather than throwing' {
    $m = Read-EnvFile -Path (Join-Path $env:TEMP 'fo-definitely-not-here.txt')
    $m.Count -eq 0
}

Write-Host ''
Write-Host 'Writing .env' -ForegroundColor Cyan

Check 'Set-EnvValues rewrites an existing key in place and preserves comments' {
    $tmp = Join-Path $env:TEMP 'fo-env-write1.txt'
    Copy-Item -LiteralPath $sample -Destination $tmp -Force
    Set-EnvValues -Path $tmp -Values @{ PLAIN = 'changed' }
    $text = [System.IO.File]::ReadAllText($tmp)
    # `\r?` before the anchor: the launcher writes with WriteAllLines, which uses CRLF on Windows, and
    # in .NET regex multiline `$` matches before the \n but AFTER the \r. Widening the anchor keeps
    # the assertion exact; dropping it would let a partial line match.
    if ($text -notmatch '(?m)^PLAIN=changed\r?$') { return 'the key was not rewritten' }
    if ($text -notmatch '# a leading comment') { return 'the leading comment was lost' }
    if (([regex]::Matches($text, '(?m)^PLAIN=')).Count -ne 1) { return 'the key was duplicated' }
    $true
}

Check 'Set-EnvValues appends a key that does not exist yet' {
    $tmp = Join-Path $env:TEMP 'fo-env-write2.txt'
    Copy-Item -LiteralPath $sample -Destination $tmp -Force
    Set-EnvValues -Path $tmp -Values @{ BRAND_NEW = 'yes' }
    $text = [System.IO.File]::ReadAllText($tmp)
    if ($text -notmatch '(?m)^BRAND_NEW=yes\r?$') { return 'the key was not appended' }
    if ($text -notmatch '(?m)^PLAIN=value\r?$') { return 'an existing key was disturbed' }
    $true
}

Check 'Set-EnvValues does NOT write a byte order mark' {
    # A BOM at the start of a dotenv file becomes part of the first key name, so the first setting
    # silently stops being read. This is the specific failure the no-BOM encoding exists to prevent.
    $tmp = Join-Path $env:TEMP 'fo-env-write3.txt'
    Copy-Item -LiteralPath $sample -Destination $tmp -Force
    Set-EnvValues -Path $tmp -Values @{ PLAIN = 'x' }
    $bytes = [System.IO.File]::ReadAllBytes($tmp)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        return 'a UTF-8 BOM was written'
    }
    $true
}

Check 'Set-EnvValues does not touch a commented-out key of the same name' {
    $tmp = Join-Path $env:TEMP 'fo-env-write4.txt'
    Copy-Item -LiteralPath $sample -Destination $tmp -Force
    Set-EnvValues -Path $tmp -Values @{ COMMENTED_OUT = 'forced' }
    $text = [System.IO.File]::ReadAllText($tmp)
    if ($text -notmatch '(?m)^#COMMENTED_OUT=nope\r?$') { return 'the commented line was rewritten' }
    if ($text -notmatch '(?m)^COMMENTED_OUT=forced\r?$') { return 'the key was not appended separately' }
    $true
}

Check 'Set-EnvValues round-trips through Read-EnvFile' {
    $tmp = Join-Path $env:TEMP 'fo-env-write5.txt'
    Copy-Item -LiteralPath $sample -Destination $tmp -Force
    $written = @{ BACKEND_PORT = '18000'; OIDC_ISSUER = 'http://authentik-server:9000/application/o/forgeops/' }
    Set-EnvValues -Path $tmp -Values $written
    $back = Read-EnvFile -Path $tmp
    foreach ($k in $written.Keys) {
        if ($back[$k] -ne $written[$k]) { return "$k came back as '$($back[$k])'" }
    }
    $true
}

Write-Host ''
Write-Host 'Files and commands the launcher depends on' -ForegroundColor Cyan

foreach ($rel in @(
    'docker-compose.yml',
    'docker-compose.e2e.yml',
    '.env.example',
    'backend\requirements-dev.lock',
    'scripts\init_ca.py',
    'scripts\ci\provision-authentik.py',
    'scripts\check-oidc-reachability.py',
    'start.cmd'
)) {
    Check ("depends on " + $rel) ([scriptblock]::Create(
        "if (Test-Path -LiteralPath (Join-Path '$RepoRoot' '$rel')) { `$true } else { 'not found' }"
    ))
}

Check 'the compose files are valid together' {
    $r = Invoke-Native -Command 'docker compose -f docker-compose.yml -f docker-compose.e2e.yml config --services' -Quiet
    if (-not $r.Ok) { return "compose config failed: $($r.Output)" }
    foreach ($svc in @('postgres','redis','opa','cerbos','authentik-server','authentik-worker','backend','frontend','agent')) {
        if ($r.Output -notmatch ("(?m)^" + [regex]::Escape($svc) + "\r?$")) { return "service '$svc' is not defined" }
    }
    $true
}

Check 'every service the launcher starts by name exists in the compose files' {
    $r = Invoke-Native -Command 'docker compose -f docker-compose.yml -f docker-compose.e2e.yml config --services' -Quiet
    $declared = @($r.Lines | Where-Object { $_ -match '\S' } | ForEach-Object { $_.Trim() })
    $launcherText = [System.IO.File]::ReadAllText($Target)
    $started = [regex]::Matches($launcherText, "up -d --wait ([a-z0-9 \-]+)'") |
               ForEach-Object { $_.Groups[1].Value.Trim() -split '\s+' } | Select-Object -Unique
    if ($started.Count -eq 0) { return 'could not find any up commands to check' }
    foreach ($s in $started) {
        if ($declared -notcontains $s) { return "the launcher starts '$s', which is not a declared service" }
    }
    $true
}

Check 'the python the launcher would use can import what provisioning needs' {
    $candidates = @(
        (Join-Path $RepoRoot 'backend\.venv\Scripts\python.exe'),
        (Join-Path $RepoRoot '.forgeops-launcher\venv\Scripts\python.exe')
    )
    foreach ($py in $candidates) {
        if (Test-PythonHasProvisioningDeps -PythonExe $py) { return $true }
    }
    'no interpreter has httpx, pytest, pytest_asyncio and cryptography'
}

Check 'provision-authentik.py at least parses under that interpreter' {
    $py = Join-Path $RepoRoot 'backend\.venv\Scripts\python.exe'
    $script = Join-Path $RepoRoot 'scripts\ci\provision-authentik.py'
    $r = Invoke-Native -Command ('"{0}" -m py_compile "{1}"' -f $py, $script) -Quiet
    if (-not $r.Ok) { return $r.Output }
    $true
}

Check 'init_ca.py at least parses under that interpreter' {
    $py = Join-Path $RepoRoot 'backend\.venv\Scripts\python.exe'
    $r = Invoke-Native -Command ('"{0}" -m py_compile "{1}"' -f $py, (Join-Path $RepoRoot 'scripts\init_ca.py')) -Quiet
    if (-not $r.Ok) { return $r.Output }
    $true
}

Check 'the launcher file is pure ASCII' {
    # A non-ASCII glyph renders as mojibake on a console whose code page is not UTF-8, which is the
    # default on a machine we have never seen.
    $bytes = [System.IO.File]::ReadAllBytes($Target)
    $bad = @($bytes | Where-Object { $_ -gt 126 })
    if ($bad.Count -gt 0) { return "$($bad.Count) non-ASCII bytes" }
    $true
}

Check 'start.cmd points at the launcher that exists' {
    $cmdText = [System.IO.File]::ReadAllText((Join-Path $RepoRoot 'start.cmd'))
    if ($cmdText -notmatch 'scripts\\start-forgeops\.ps1') { return 'start.cmd does not reference the ps1' }
    if ($cmdText -notmatch 'ExecutionPolicy Bypass') { return 'start.cmd would be blocked by the execution policy' }
    $true
}

Write-Host ''
Write-Host ("  passed " + $pass + ", failed " + $fail) -ForegroundColor $(if ($fail -eq 0) { 'Green' } else { 'Red' })
Write-Host ''
if ($fail -gt 0) { exit 1 }
exit 0

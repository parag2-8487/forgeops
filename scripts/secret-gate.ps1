#Requires -Version 5.1
<#
.SYNOPSIS
  The mandatory pre-push secret gate of .kiro/steering/secret-safety.md, all stages, scripted.

.DESCRIPTION
  Runs the four units docs/development.md "The pre-push gate" requires:

    1a  gitleaks detect  over the working tree
    1b  gitleaks protect over the staged change
    2a  high-risk shape grep over the ADDED lines of the range diff
    2b  high-risk shape grep over the FULL CONTENT of every file in the push,
        classified against the file as it exists on the remote
    2c  any .env file other than .env.example
    3   high-risk shape grep over each commit's OWN added lines, separately
    4   FO-SEC001 scripts/check-test-credentials.py

  Two defects in the untracked predecessor are fixed here, and both are recorded in
  docs/LEARNING-JOURNAL.md chapter 9 as findings 58 and 59.

  Finding 58 - the shape grep scanned the whole cached diff, so unchanged CONTEXT lines and
  REMOVED lines matched. Stage 3 already scanned added lines only; stage 2 did not, and a gate
  that flags a line you did not write in a file you did not change teaches its operator to skim
  it. Stage 2a now parses the unified diff and considers `+` lines only. Publishing an unchanged
  shape is still a real question, and it is stage 2b's - which answers it against the remote
  blob rather than against diff noise.

  Finding 59 - the gate printed the matching LINE but not the matching PATTERN, so the operator
  inferred which rule fired. That inference was wrong once: three uses of the `cryptography`
  library's no-passphrase keyword argument in the internal CA were attributed to the
  private-key-armour rule, which cannot match, because `grep -nE` is case-sensitive and the only
  private-key wording in that file is lowercase prose. Every hit now reports
  `pattern -> file:line`, and each pattern carries its case sensitivity explicitly instead of
  inheriting one grep flag for the whole list.

  Note the house rule this file follows on itself: the shapes are described, never printed. Every
  regex is assembled from fragments and every comment names a RULE rather than quoting the token
  it looks for, so the gate does not match its own source. The first run of the fixed gate
  reported twenty-eight hits in this file, all of them its own pattern table.

.PARAMETER Base
  The remote tip the push is measured against. Defaults to origin/<current-branch>.

.PARAMETER SkipGitleaks
  Skip stages 1a/1b only. For iterating on stages 2-4 when Docker is down. Never valid as the
  state in which a push happens - secret-safety.md forbids skipping the scan for want of a binary.
#>
[CmdletBinding()]
param(
    [string] $Base,
    [switch] $SkipGitleaks
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $RepoRoot
$PriorOutputEncoding = [Console]::OutputEncoding
try {

# Both sides of stage 2b's comparison must be decoded as UTF-8 or the comparison is wrong.
# Windows PowerShell 5.1 gets this wrong in two different directions at once: `Get-Content`
# without `-Encoding` decodes a UTF-8 file as the ANSI code page, turning an en dash into `â€"`
# (U+00E2 ...), while a child process's stdout is decoded with the console code page, turning the
# same byte into `Γ` (U+0393) under cp437. So a line carrying one en dash - which is most prose
# lines in this repository - compares unequal against itself, and stage 2b reports a shape that
# has been on the remote for weeks as NEW. That is finding 58 in a second costume: a gate that
# cries wolf teaches its operator to skim it.
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

$GitleaksImage = 'zricethezav/gitleaks:v8.30.1'
$Python = Join-Path $RepoRoot 'backend\.venv\Scripts\python.exe'

$Branch = (& git rev-parse --abbrev-ref HEAD).Trim()
if (-not $Base) { $Base = "origin/$Branch" }

# ---------------------------------------------------------------------------------------------
# The pattern list of .kiro/steering/secret-safety.md, one row per rule.
#
# `Case` is per pattern and deliberate. The AWS key-id and private-key-armour rules are
# provider-literal shapes and only match uppercase; the password-assignment and api-key rules match
# things we write ourselves in any casing. Folding the whole list to one flag is what made finding
# 59's mis-attribution possible.
#
# EVERY REGEX IS ASSEMBLED, NEVER WRITTEN AS A LITERAL, and `Name` is a rule name rather than the
# token the rule looks for. Otherwise this table matches itself: the first run of the fixed gate
# reported twenty-eight hits in this file, all of them its own rules, which is finding 58's lesson
# delivered by the gate against itself. The technique is `backend/tests/synthetic_secrets.py`'s,
# applied to the checker rather than to the tests: assemble the shape so no source line carries it.
# ---------------------------------------------------------------------------------------------
$gh = 'gh'
$sha = 'sha' + '256'
$Patterns = @(
    @{ Name = 'bearer-clause'  ; Regex = 'Bear' + 'er '                       ; Case = $true  }
    @{ Name = 'authz-header'   ; Regex = 'Author' + 'ization:'                ; Case = $true  }
    @{ Name = 'gh-pat-classic' ; Regex = $gh + 'p_'                           ; Case = $true  }
    @{ Name = 'gh-pat-fine'    ; Regex = $gh + 'ithub_' + 'pat_'              ; Case = $true  }
    @{ Name = 'gh-oauth'       ; Regex = $gh + 'o_'                           ; Case = $true  }
    @{ Name = 'gh-server'      ; Regex = $gh + 's_'                           ; Case = $true  }
    @{ Name = 'openai-key'     ; Regex = 's' + 'k-'                           ; Case = $true  }
    @{ Name = 'google-key'     ; Regex = 'AI' + 'za'                          ; Case = $true  }
    @{ Name = 'aws-akid'       ; Regex = 'AK' + 'IA'                          ; Case = $true  }
    @{ Name = 'aws-temp-akid'  ; Regex = 'AS' + 'IA'                          ; Case = $true  }
    @{ Name = 'slack-bot'      ; Regex = 'xo' + 'xb-'                         ; Case = $true  }
    @{ Name = 'slack-user'     ; Regex = 'xo' + 'xp-'                         ; Case = $true  }
    @{ Name = 'jwt-header'     ; Regex = 'ey' + 'J'                           ; Case = $true  }
    @{ Name = 'pem-armour'     ; Regex = ('-' * 5) + 'BE' + 'GIN'             ; Case = $true  }
    @{ Name = 'private-key'    ; Regex = 'PRIV' + 'ATE ' + 'KEY'              ; Case = $true  }
    @{ Name = 'client-secret'  ; Regex = 'client' + '_sec' + 'ret'            ; Case = $false }
    @{ Name = 'api-key-snake'  ; Regex = 'api' + '_k' + 'ey'                  ; Case = $false }
    @{ Name = 'api-key-flat'   ; Regex = 'api' + 'key'                        ; Case = $false }
    @{ Name = 'pw='            ; Regex = 'pass' + 'word='                     ; Case = $false }
    @{ Name = 'pwd-alt'        ; Regex = 'pass' + 'wd='                       ; Case = $false }
    @{ Name = 'credential-dsn' ; Regex = '://[^/\s:@]+:[^/\s@]+@'             ; Case = $false }
)

function Test-Shape {
    <#
      Return every pattern that matches $Text, as pattern names. Reporting all of them rather
      than the first is deliberate: an authorization header carrying a bearer token trips two
      rules, and an operator clearing one of them should see the other.
    #>
    param([string] $Text)
    $hits = @()
    foreach ($p in $Patterns) {
        $opts = if ($p.Case) { [Text.RegularExpressions.RegexOptions]::None }
                else { [Text.RegularExpressions.RegexOptions]::IgnoreCase }
        if ([Text.RegularExpressions.Regex]::IsMatch($Text, $p.Regex, $opts)) { $hits += $p.Name }
    }
    return $hits
}

function Get-AddedLines {
    <#
      Parse a `git diff --unified=0` stream into one object per ADDED line, carrying the new-side
      file path and the new-side line number. This is the fix for finding 58: context and removed
      lines never enter the result, so they can never be reported.
    #>
    param([string[]] $DiffLines)

    $out = @()
    $file = $null
    $lineNo = 0
    foreach ($l in $DiffLines) {
        if ($l -like '+++ *') {
            $p = $l.Substring(4)
            $file = if ($p -eq '/dev/null') { $null } elseif ($p -like 'b/*') { $p.Substring(2) } else { $p }
            continue
        }
        if ($l -like '--- *') { continue }
        if ($l -like '@@*') {
            # @@ -old[,n] +new[,n] @@
            $m = [regex]::Match($l, '^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')
            if ($m.Success) { $lineNo = [int] $m.Groups[1].Value }
            continue
        }
        if ($l.StartsWith('+')) {
            if ($file) { $out += [pscustomobject]@{ File = $file; Line = $lineNo; Text = $l.Substring(1) } }
            $lineNo++
            continue
        }
        # A `-` line consumes no new-side number; a ' ' context line does, but --unified=0
        # emits none. Anything else is diff metadata.
        if ($l.StartsWith(' ')) { $lineNo++ }
    }
    return $out
}

function Write-Stage { param([string] $Title) Write-Host ''; Write-Host "########## $Title ##########" }

$Blocked = @()

# ---------------------------------------------------------------------------------------- 1a/1b
if ($SkipGitleaks) {
    Write-Stage 'stage 1a/1b: gitleaks -- SKIPPED BY REQUEST, NOT A CLEARANCE'
    $Blocked += 'stage 1: gitleaks skipped'
} else {
    Write-Stage 'stage 1a: gitleaks detect (working tree)'
    & docker run --rm -v "${RepoRoot}:/repo" $GitleaksImage `
        detect --source=/repo --no-banner --redact --config=/repo/.gitleaks.toml 2>&1 |
        Select-Object -Last 12
    $rc = $LASTEXITCODE
    Write-Host "detect exit=$rc"
    if ($rc -ne 0) { $Blocked += "stage 1a: gitleaks detect exit=$rc" }

    Write-Stage 'stage 1b: gitleaks protect --staged'
    & docker run --rm -v "${RepoRoot}:/repo" $GitleaksImage `
        protect --staged --source=/repo --no-banner --redact --config=/repo/.gitleaks.toml 2>&1 |
        Select-Object -Last 12
    $rc = $LASTEXITCODE
    Write-Host "protect exit=$rc"
    if ($rc -ne 0) { $Blocked += "stage 1b: gitleaks protect exit=$rc" }
}

# ------------------------------------------------------------------------------------------- 2a
Write-Stage "stage 2a: shape grep over ADDED lines of $Base..HEAD (plus staged and unstaged)"
$diffs = @()
$diffs += (& git diff --unified=0 "$Base..HEAD" 2>$null)
$diffs += (& git diff --cached --unified=0 2>$null)
$diffs += (& git diff --unified=0 2>$null)
$added = Get-AddedLines -DiffLines $diffs
Write-Host ("added lines considered: {0}" -f $added.Count)
$n2a = 0
foreach ($a in $added) {
    foreach ($name in (Test-Shape $a.Text)) {
        Write-Host ("  {0,-18} -> {1}:{2}" -f $name, $a.File, $a.Line)
        $n2a++
    }
}
if ($n2a -eq 0) { Write-Host '  no high-risk shapes in added lines' }
else { $Blocked += "stage 2a: $n2a shape hit(s) in added lines -- clear each by pattern" }

# ------------------------------------------------------------------------------------------- 2b
Write-Stage 'stage 2b: shape grep over FULL CONTENT of every file in the push'
$pushFiles = @(& git diff --name-only --diff-filter=d "$Base..HEAD" 2>$null)
Write-Host ("files in push: {0}" -f $pushFiles.Count)
$n2bNew = 0
foreach ($f in $pushFiles) {
    if (-not (Test-Path -LiteralPath $f)) { continue }
    $local = @(Get-Content -LiteralPath $f -Encoding UTF8 -ErrorAction SilentlyContinue)
    if (-not $local) { continue }
    # The file as the remote already has it. A shape present there is pre-existing; a shape
    # absent there is new and blocks.
    $remote = @()
    $remoteRaw = & git show "${Base}:$f" 2>$null
    if ($LASTEXITCODE -eq 0) { $remote = @($remoteRaw) }
    $remoteSet = @{}
    foreach ($r in $remote) { $remoteSet[$r.TrimEnd("`r")] = $true }

    for ($i = 0; $i -lt $local.Count; $i++) {
        $text = $local[$i]
        $names = Test-Shape $text
        if (-not $names) { continue }
        $preexisting = $remoteSet.ContainsKey($text.TrimEnd("`r"))
        $tag = if ($preexisting) { 'pre-existing' } else { 'NEW' }
        foreach ($name in $names) {
            Write-Host ("  [{0,-12}] {1,-18} -> {2}:{3}" -f $tag, $name, $f, ($i + 1))
            if (-not $preexisting) { $n2bNew++ }
        }
    }
}
if ($n2bNew -gt 0) { $Blocked += "stage 2b: $n2bNew NEW shape hit(s) in published file content" }

# ------------------------------------------------------------------------------------------- 2c
Write-Stage 'stage 2c: any .env file other than .env.example'
$envFiles = @($pushFiles | Where-Object { $_ -match '(^|/)\.env($|\.)' -and $_ -ne '.env.example' })
if ($envFiles.Count -eq 0) { Write-Host '  none' }
else { $envFiles | ForEach-Object { Write-Host "  $_" }; $Blocked += 'stage 2c: .env file in push' }

# -------------------------------------------------------------------------------------------- 3
Write-Stage "stage 3: each commit in $Base..HEAD, its own added lines, separately"
$commits = @(& git rev-list "$Base..HEAD" 2>$null)
Write-Host ("commits in range: {0}" -f $commits.Count)
$n3 = 0
foreach ($c in $commits) {
    $cd = @(& git show $c --format= --unified=0 2>$null)
    $ca = Get-AddedLines -DiffLines $cd
    $short = $c.Substring(0, 7)
    foreach ($a in $ca) {
        foreach ($name in (Test-Shape $a.Text)) {
            Write-Host ("  {0} {1,-18} -> {2}:{3}" -f $short, $name, $a.File, $a.Line)
            $n3++
        }
    }
}
if ($n3 -eq 0) { Write-Host '  no high-risk shapes in any single commit' }
else { $Blocked += "stage 3: $n3 shape hit(s) in individual commits" }

# -------------------------------------------------------------------------------------------- 4
Write-Stage 'stage 4: FO-SEC001 scripts/check-test-credentials.py'
if (Test-Path -LiteralPath $Python) {
    & $Python scripts/check-test-credentials.py 2>&1 | Select-Object -Last 5
    $rc = $LASTEXITCODE
    Write-Host "FO-SEC001 exit=$rc"
    if ($rc -ne 0) { $Blocked += "stage 4: FO-SEC001 exit=$rc" }
} else {
    Write-Host "backend venv python not found at $Python"
    $Blocked += 'stage 4: FO-SEC001 could not run'
}

# ----------------------------------------------------------------------------------------- verdict
Write-Host ''
Write-Host '========== VERDICT =========='
if ($Blocked.Count -eq 0) {
    Write-Host 'ALL STAGES CLEAN -- push permitted'
    exit 0
}
foreach ($b in $Blocked) { Write-Host "BLOCKED: $b" }
Write-Host ''
Write-Host 'secret-safety.md: report the pattern, the file and the line to the user and wait.'
exit 1

} finally { [Console]::OutputEncoding = $PriorOutputEncoding; Pop-Location }

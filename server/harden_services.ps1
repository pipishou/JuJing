# Harden WinSW service recovery policy (DryRun by default).
#
# Policy written into each <service> XML:
#   failures 1..3  -> restart after 30 s (no immediate restart)
#   failure 4+     -> action="none": circuit breaker, the service stays stopped
#                     until an operator intervenes (no infinite restart storm)
#   resetfailure   -> 1 hour of healthy uptime clears the failure counter
#
# SCM cannot express "never restart for the first 3 failures": action="none"
# leaves the service stopped, so a literal [none, none, none, restart] list
# would stop after failure 1 and never reach failure 4. The list above is the
# safe equivalent: three spaced attempts, then stop.
#
# WinSW 2.12 has no per-failure hook. Failure evidence is recorded by:
#   <logpath>\<id>.wrapper.log   restart attempts with timestamps
#   <logpath>\<id>.err.log       child stderr (bind errors, tracebacks)
#   Windows System event 7034    "service terminated unexpectedly" + count
#   <ServicesPath>\harden.log    audit line written by this script on -Apply
#
# The script edits XML as text so encoding/newlines/attributes stay intact.
# It backs up every changed XML to <ServicesPath>\_backup-<yyyyMMdd-HHmmss>\.
# It never starts, stops, or restarts a service.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File server\harden_services.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File server\harden_services.ps1 -Apply
#
# ASCII only: Windows PowerShell 5.1 decodes BOM-less scripts as ANSI.
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Apply,
    [string]$ServicesPath,
    [string[]]$Service = @('jujing-octop', 'jujing-botteam-ui')
)

$ErrorActionPreference = 'Stop'
$script:Errors = 0
$script:Changed = 0

function Write-Info {
    param([string]$Message)
    Write-Host $Message
}

function Write-Failure {
    param([string]$Message)
    $script:Errors++
    Write-Host ("ERROR: " + $Message) -ForegroundColor Red
}

function Get-LineDiff {
    param(
        [string[]]$Before,
        [string[]]$After
    )
    $n = $Before.Count
    $m = $After.Count
    $lcs = New-Object 'int[,]' ($n + 1), ($m + 1)
    for ($i = $n - 1; $i -ge 0; $i--) {
        for ($j = $m - 1; $j -ge 0; $j--) {
            if ($Before[$i] -ceq $After[$j]) {
                $lcs[$i, $j] = $lcs[($i + 1), ($j + 1)] + 1
            } else {
                $down = $lcs[($i + 1), $j]
                $right = $lcs[$i, ($j + 1)]
                if ($down -ge $right) { $lcs[$i, $j] = $down }
                else { $lcs[$i, $j] = $right }
            }
        }
    }

    $ops = New-Object System.Collections.Generic.List[string]
    $x = 0
    $y = 0
    while ($x -lt $n -and $y -lt $m) {
        if ($Before[$x] -ceq $After[$y]) {
            $ops.Add("  " + $Before[$x])
            $x++
            $y++
        } elseif ($lcs[($x + 1), $y] -ge $lcs[$x, ($y + 1)]) {
            $ops.Add("- " + $Before[$x])
            $x++
        } else {
            $ops.Add("+ " + $After[$y])
            $y++
        }
    }
    while ($x -lt $n) { $ops.Add("- " + $Before[$x]); $x++ }
    while ($y -lt $m) { $ops.Add("+ " + $After[$y]); $y++ }
    return $ops.ToArray()
}

function New-PolicyBlock {
    param(
        [string]$Indent,
        [string]$Eol
    )
    $body = @(
        '<onfailure action="restart" delay="30 sec"/>',
        '<onfailure action="restart" delay="30 sec"/>',
        '<onfailure action="restart" delay="30 sec"/>',
        '<onfailure action="none"/>',
        '<resetfailure>1 hour</resetfailure>'
    )
    $lines = @()
    foreach ($line in $body) { $lines += ($Indent + $line) }
    return (($lines -join $Eol) + $Eol)
}

function Get-HardenedText {
    param([string]$Text)

    $eol = "`n"
    if ($Text.Contains("`r`n")) { $eol = "`r`n" }

    $restartMatches = [regex]::Matches($Text, '<onfailure action="restart" delay="30 sec"/>')
    $noneMatches = [regex]::Matches($Text, '<onfailure action="none"/>')
    $resetMatches = [regex]::Matches($Text, '<resetfailure>1 hour</resetfailure>')
    $already = (
        $restartMatches.Count -eq 3 -and
        $noneMatches.Count -eq 1 -and
        $resetMatches.Count -eq 1
    )

    $stripped = [regex]::Replace(
        $Text,
        '(?m)^[ \t]*<onfailure\b[^>]*/>[ \t]*\r?\n?',
        ''
    )
    $stripped = [regex]::Replace(
        $stripped,
        '(?m)^[ \t]*<resetfailure>.*?</resetfailure>[ \t]*\r?\n?',
        ''
    )

    $anchor = '<stoptimeout'
    $index = $stripped.IndexOf($anchor)
    if ($index -lt 0) {
        $anchor = '</service>'
        $index = $stripped.IndexOf($anchor)
    }
    if ($index -lt 0) {
        throw "cannot find <stoptimeout> or </service> insertion point"
    }

    $lineStart = $stripped.LastIndexOf("`n", $index)
    if ($lineStart -lt 0) { $lineStart = 0 } else { $lineStart = $lineStart + 1 }
    $indent = $stripped.Substring($lineStart, $index - $lineStart)
    if ($indent -notmatch '^[ \t]*$') { $indent = '  ' }

    $block = New-PolicyBlock -Indent $indent -Eol $eol
    $hardened = $stripped.Insert($lineStart, $block)

    return @{
        Text = $hardened
        Eol = $eol
        Changed = (-not $already)
        AlreadyHardened = $already
    }
}

function Test-ServiceXml {
    param(
        [string]$Path,
        [string]$Text,
        [string]$ExpectedId
    )
    $doc = New-Object System.Xml.XmlDocument
    $doc.LoadXml($Text)
    $root = $doc.DocumentElement
    # PowerShell's XML adapter resolves $root.Name to the child <name> element,
    # so compare the tag name through LocalName.
    if ($null -eq $root -or $root.LocalName -ne 'service') {
        throw "root element is not <service>"
    }
    $idNode = $root.SelectSingleNode('id')
    if ($null -eq $idNode -or $idNode.InnerText -ne $ExpectedId) {
        throw "service id mismatch in $Path"
    }
    $onfail = $root.SelectNodes('onfailure')
    $reset = $root.SelectNodes('resetfailure')
    if ($onfail.Count -ne 4) { throw "expected 4 onfailure elements, got $($onfail.Count)" }
    if ($reset.Count -ne 1) { throw "expected 1 resetfailure element, got $($reset.Count)" }
    $last = $onfail[$onfail.Count - 1]
    if ($last.GetAttribute('action') -ne 'none') {
        throw "last onfailure action must be none"
    }
    for ($i = 0; $i -lt 3; $i++) {
        if ($onfail[$i].GetAttribute('action') -ne 'restart') {
            throw "onfailure[$i] must be restart"
        }
        if ($onfail[$i].GetAttribute('delay') -ne '30 sec') {
            throw "onfailure[$i] delay must be 30 sec"
        }
    }
}

function Write-Utf8NoBom {
    param(
        [string]$Path,
        [string]$Text
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

# ---------------------------------------------------------------------------
if ($DryRun -and $Apply) {
    Write-Host "ERROR: choose either -DryRun (default) or -Apply, not both."
    exit 2
}
$isDryRun = -not $Apply

# Accept both "-Service a,b" (string from -File) and "-Service a,b" (array).
$Service = @(
    $Service |
        ForEach-Object { $_ -split ',' } |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -ne '' }
)

if (-not $ServicesPath) {
    $ServicesPath = Join-Path $PSScriptRoot 'services'
}
if (-not (Test-Path -LiteralPath $ServicesPath)) {
    Write-Host ("ERROR: services path not found: " + $ServicesPath)
    exit 2
}

Write-Info "== WinSW recovery hardening =="
Write-Info ("mode:   " + $(if ($isDryRun) { "DRY-RUN (no writes; pass -Apply to write)" } else { "APPLY" }))
Write-Info ("path:   " + $ServicesPath)
Write-Info ("policy: restart after 30 sec for failures 1-3; failure 4+ -> none (stop)")
Write-Info ""

$backupDir = $null
if (-not $isDryRun) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupDir = Join-Path $ServicesPath ("_backup-" + $stamp)
}

foreach ($name in $Service) {
    $xmlPath = Join-Path $ServicesPath ($name + '.xml')
    Write-Info ("-- " + $name + " --")
    if (-not (Test-Path -LiteralPath $xmlPath)) {
        Write-Failure ("missing service config: " + $xmlPath)
        Write-Info ""
        continue
    }

    try {
        $original = [System.IO.File]::ReadAllText($xmlPath, [System.Text.Encoding]::UTF8)
        $docCheck = New-Object System.Xml.XmlDocument
        $docCheck.LoadXml($original)
    } catch {
        Write-Failure ("cannot parse " + $xmlPath + ": " + $_.Exception.Message)
        Write-Info ""
        continue
    }

    try {
        $result = Get-HardenedText -Text $original
        if (-not $result.Changed) {
            Write-Info "already hardened; no change"
            Write-Info ""
            continue
        }
        Test-ServiceXml -Path $xmlPath -Text $result.Text -ExpectedId $name
    } catch {
        Write-Failure ($name + ": " + $_.Exception.Message)
        Write-Info ""
        continue
    }

    $beforeLines = @($original -split "\r?\n")
    $afterLines = @($result.Text -split "\r?\n")
    $diff = @(Get-LineDiff -Before $beforeLines -After $afterLines)

    Write-Info ("diff: " + $xmlPath)
    foreach ($line in $diff) { Write-Host $line }

    if ($isDryRun) {
        Write-Info ("would back up to: " + (Join-Path $ServicesPath ("_backup-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))))
        Write-Info "no files written (dry-run)"
        Write-Info ""
        continue
    }

    try {
        if (-not (Test-Path -LiteralPath $backupDir)) {
            New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
            $policyNote = @(
                'WinSW recovery policy applied by server/harden_services.ps1',
                '',
                'failures 1-3 : restart after 30 sec',
                'failure 4+   : action=none -> service stays stopped (circuit breaker)',
                'resetfailure : 1 hour of healthy uptime clears the counter',
                '',
                'Failure evidence:',
                '  .octop\logs\service\<id>.wrapper.log  (restart attempts)',
                '  .octop\logs\service\<id>.err.log      (child stderr)',
                '  Get-WinEvent -FilterHashtable @{LogName="System"; Id=7034}',
                '  <ServicesPath>\harden.log             (this script audit)'
            )
            Write-Utf8NoBom -Path (Join-Path $backupDir 'POLICY.txt') -Text (($policyNote -join "`r`n") + "`r`n")
        }
        $backupFile = Join-Path $backupDir ($name + '.xml')
        Copy-Item -LiteralPath $xmlPath -Destination $backupFile -Force
        Write-Utf8NoBom -Path $xmlPath -Text $result.Text
        $script:Changed++

        $audit = Join-Path $ServicesPath 'harden.log'
        $auditLine = "[{0}] service={1} backup={2} policy=restart30s x3 + none" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $name, $backupDir
        Add-Content -Path $audit -Value $auditLine -Encoding UTF8

        Write-Info ("wrote: " + $xmlPath)
        Write-Info ("backup: " + $backupFile)
        Write-Info ("next: restart the service so WinSW reloads the XML, e.g. services\" + $name + ".exe restart")
    } catch {
        Write-Failure ($name + ": write failed: " + $_.Exception.Message)
    }
    Write-Info ""
}

if ($isDryRun) {
    Write-Info "Dry-run complete: 0 files written."
} else {
    Write-Info ("Apply complete: " + $script:Changed + " service config(s) changed.")
    if ($backupDir -and (Test-Path -LiteralPath $backupDir)) {
        Write-Info ("Backups: " + $backupDir)
    }
    Write-Info "Services were NOT restarted by this script."
}

if ($script:Errors -gt 0) { exit 1 }
exit 0

#Requires -RunAsAdministrator
# Moves bot-team out of the JuJing repo folder into bot-2\bot-team (its own repo),
# then re-creates a junction at the old path so old docs/scripts still resolve.
# ASCII only: PowerShell 5.1 decodes BOM-less scripts as ANSI.
param([switch]$DryRun)

$ErrorActionPreference = 'Stop'
$root = 'D:\work\产品类\个人产品\bot-2'
$src  = Join-Path $root 'JuJing\bot-team'   # current physical location
$dst  = Join-Path $root 'bot-team'          # desired physical location
$log  = Join-Path $root 'migrate_out.log'
$svcs = @('jujing-botteam-ui', 'jujing-octop')

function W([string]$m) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m"
    Write-Host $line
    $line | Out-File -FilePath $log -Append -Encoding utf8
}

function Fail([string]$m) { W "FAIL: $m"; exit 1 }

W "=== migrate start (dry=$DryRun) ==="

# ---- prechecks -------------------------------------------------------------
if (-not (Test-Path $src)) { Fail "source missing: $src" }
$dstItem = Get-Item $dst -Force -ErrorAction SilentlyContinue
if ($dstItem) {
    if ($dstItem.LinkType -ne 'Junction') { Fail "dst exists and is not a junction: $dst" }
}
if (-not (Test-Path (Join-Path $src '.git'))) { Fail "source is not a git repo: $src\.git" }

if ($DryRun) {
    W "dry run: would stop [$($svcs -join ', ')], rmdir $dst, move $src -> $dst, junction back, patch xml, reinstall"
    exit 0
}

# ---- backup service xml ----------------------------------------------------
$xmlBak = Join-Path $root '_archive\services-xml-20261028'
New-Item -ItemType Directory -Force -Path $xmlBak | Out-Null
Copy-Item (Join-Path $src 'server\services\*.xml') $xmlBak -Force
W "xml backed up -> $xmlBak"

# ---- stop services ---------------------------------------------------------
foreach ($n in $svcs) {
    $s = Get-Service -Name $n -ErrorAction SilentlyContinue
    if ($s) {
        W "stopping $n (was $($s.Status))"
        Stop-Service -Name $n -Force -ErrorAction SilentlyContinue
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Service -Name $n).Status -ne 'Stopped' -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
        W "stopped $n = $((Get-Service -Name $n).Status)"
    } else { W "service $n not registered (skip)" }
}

# ---- swap ------------------------------------------------------------------
$rollback = {
    param($reason)
    W "ROLLBACK: $reason"
    if (Test-Path $dst) { Move-Item $dst $src -Force }
    cmd /c rmdir $dst 2>$null
    if (-not (Test-Path $src)) { cmd /c mklink /J $src $dst | Out-Null }
    Copy-Item (Join-Path $xmlBak '*.xml') (Join-Path $src 'server\services') -Force
    & (Join-Path $src 'server\services\install_services.ps1') | Out-Null
    W "ROLLBACK done"
    exit 2
}

try {
    if (Test-Path $dst) { cmd /c rmdir $dst; W "junction removed: $dst" }
    Move-Item $src $dst -Force
    W "moved: $src -> $dst"
    cmd /c mklink /J $src $dst | Out-Null
    W "junction recreated: $src -> $dst"

    # patch working directory in service xml
    foreach ($f in Get-ChildItem (Join-Path $dst 'server\services\*.xml')) {
        $c = Get-Content $f.FullName -Raw
        $c = $c -replace [regex]::Escape('D:\work\产品类\个人产品\bot-2\JuJing\bot-team'), 'D:\work\产品类\个人产品\bot-2\bot-team'
        Set-Content $f.FullName $c -Encoding utf8NoBOM
    }
    W "xml workingdirectory patched"

    & (Join-Path $dst 'server\services\install_services.ps1') | Out-Null
    W "services reinstalled"
    Start-Sleep -Seconds 8
} catch {
    & $rollback $_.Exception.Message
}

# ---- verify ----------------------------------------------------------------
$ok = $false
for ($i = 0; $i -lt 10; $i++) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8787/api/health' -TimeoutSec 5
        if ($r.StatusCode -eq 200 -and $r.Content -like '*"pack"*') { $ok = $true; W "health: $($r.Content.Substring(0,[Math]::Min(160,$r.Content.Length)))"; break }
    } catch { Start-Sleep -Seconds 3 }
}
if (-not $ok) { & $rollback 'health check failed' }

W "=== migrate done; verify with: cd $dst; python -B tests/validate_structure.py ==="
exit 0

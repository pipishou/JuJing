#Requires -Version 5.1
# Restart the two WinSW services that host the bot (needs administrator).
# Why a script: a code change only lands after the service process restarts, and
# Stop-Service from a normal shell fails with CouldNotStopService (no privilege).
# ASCII only: PowerShell 5.1 decodes BOM-less scripts as ANSI.
$ErrorActionPreference = 'Continue'
$log = Join-Path $PSScriptRoot 'services\restart.log'

function W([string]$m) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m"
    Write-Host $line
    if (Test-Path (Split-Path -Parent $log)) { $line | Out-File -FilePath $log -Append -Encoding utf8 }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    W "not elevated; relaunching with UAC prompt"
    try {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`""
        )
        exit 0
    } catch {
        W "elevation refused: $($_.Exception.Message)"
        W "run this as administrator, or: sc.exe stop jujing-botteam-ui"
        exit 71
    }
}

$names = @('jujing-botteam-ui', 'jujing-octop')
foreach ($n in $names) {
    $svc = Get-Service -Name $n -ErrorAction SilentlyContinue
    if (-not $svc) { W "$n MISSING (run server\services\install_services.ps1 first)"; continue }
    W "$n before = $($svc.Status)"
    try {
        Restart-Service -Name $n -Force -ErrorAction Stop
        W "$n restart ok"
    } catch {
        W "$n restart failed: $($_.Exception.Message)"
    }
}

Start-Sleep -Seconds 6
foreach ($n in $names) {
    $svc = Get-Service -Name $n -ErrorAction SilentlyContinue
    if ($svc) { W "status $n = $($svc.Status)" } else { W "status $n = MISSING" }
}

# health probe: a restarted service must answer before we call it done
try {
    $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8787/api/health' -TimeoutSec 8
    W "health 8787 ok=$($h.ok) pack=$($h.pack) version=$($h.version)"
} catch {
    W "health 8787 unreachable: $($_.Exception.Message)"
}
exit 0

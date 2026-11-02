<#
  recover_ports.ps1 - recover 8088 / 8787 from orphan processes and restart both services.

  Why this exists (evidence, 2026-11-02):
    * 8088 was held by pid 9000 (python.exe, LocalSystem) while service jujing-octop
      was "Running" and restarting every ~30s: err.log had 35x [Errno 10048] bind 8088.
      Each boot connected the Feishu WebSocket first, then failed to bind -> card
      callbacks dropped ("目标回调服务当前未在线").
    * 8787 was held by pid 4176 (python.exe, orphan of a stopped service), so the live
      server kept serving OLD code (new endpoints answered 404) while
      jujing-botteam-ui showed "Stopped".
    * Restart-Service alone cannot fix either case: the port owner is not the service,
      so stop/start leaves the orphan holding the port.

  What it does:
    1. finds the pid listening on each port
    2. verifies the pid is python.exe (refuses to kill anything else)
    3. with -Apply: stops both services, kills those pids, starts both services,
       waits for /api/health, and reports verdict
    4. without -Apply: prints the exact plan and changes nothing

  Usage:
    powershell -NoProfile -ExecutionPolicy Bypass -File server\recover_ports.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File server\recover_ports.ps1 -Apply
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [int[]]$Ports = @(8787, 8088)
)

$ErrorActionPreference = 'Stop'

# Dry-run (inspection only) must work for a normal user; -Apply needs elevation
# because it stops/starts Windows services owned by LocalSystem.
if ($Apply.IsPresent) {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Write-Host 'ERROR -Apply requires an elevated prompt (services run as LocalSystem).'
        Write-Host 'Inspect first:  powershell -NoProfile -File server\recover_ports.ps1'
        exit 5
    }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logPath = Join-Path $scriptDir 'services\recover.log'

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $msg
    Write-Host $line
    $dir = Split-Path -Parent $logPath
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Add-Content -Path $logPath -Value $line -Encoding ASCII
}

function Get-PortOwner([int]$port) {
    $row = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $row) { return $null }
    $proc = Get-Process -Id $row.OwningProcess -ErrorAction SilentlyContinue
    if (-not $proc) { return [pscustomobject]@{ Port = $port; Pid = $row.OwningProcess; Name = '?'; Safe = $false } }
    $safe = ($proc.ProcessName -eq 'python' -or $proc.ProcessName -eq 'pythonw')
    return [pscustomobject]@{ Port = $port; Pid = $proc.Id; Name = $proc.ProcessName; Safe = $safe }
}

function Test-Health([int]$port, [int]$tries = 20) {
    for ($i = 0; $i -lt $tries; $i++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri ("http://127.0.0.1:{0}/api/health" -f $port)
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

$services = @('jujing-octop', 'jujing-botteam-ui')
$owners = @()
foreach ($p in $Ports) { $owners += Get-PortOwner $p }

Write-Log ("plan: ports={0} apply={1}" -f ($Ports -join ','), $Apply.IsPresent)
foreach ($o in $owners) {
    if ($null -eq $o) {
        Write-Log ("port {0}: no listener (service not up or already healthy via other path)" -f $Ports[0])
        continue
    }
    Write-Log ("port {0}: held by pid {1} ({2}) safeToKill={3}" -f $o.Port, $o.Pid, $o.Name, $o.Safe)
}

$blocked = @($owners | Where-Object { $_ -and -not $_.Safe })
if ($blocked.Count -gt 0) {
    foreach ($b in $blocked) {
        Write-Log ("REFUSING: port {0} held by non-python process '{1}' (pid {2}); resolve manually" -f $b.Port, $b.Name, $b.Pid)
    }
    exit 4
}

if (-not $Apply) {
    Write-Log 'dry-run: no service stopped, no process killed. Re-run with -Apply to execute.'
    exit 0
}

foreach ($svc in $services) {
    try {
        $s = Get-Service -Name $svc -ErrorAction Stop
        if ($s.Status -ne 'Stopped') {
            Write-Log ("stopping service {0} (status={1})" -f $svc, $s.Status)
            Stop-Service -Name $svc -Force -ErrorAction Stop
        } else {
            Write-Log ("service {0} already stopped" -f $svc)
        }
    } catch {
        Write-Log ("WARN stop {0}: {1}" -f $svc, $_.Exception.Message)
    }
}

Start-Sleep -Seconds 2

foreach ($o in $owners) {
    if ($null -eq $o) { continue }
    $still = Get-Process -Id $o.Pid -ErrorAction SilentlyContinue
    if ($still) {
        Write-Log ("killing orphan pid {0} on port {1}" -f $o.Pid, $o.Port)
        try { Stop-Process -Id $o.Pid -Force -ErrorAction Stop }
        catch { Write-Log ("WARN kill {0}: {1}" -f $o.Pid, $_.Exception.Message) }
    } else {
        Write-Log ("pid {0} already exited" -f $o.Pid)
    }
}

Start-Sleep -Seconds 2

foreach ($svc in $services) {
    try {
        Write-Log ("starting service {0}" -f $svc)
        Start-Service -Name $svc -ErrorAction Stop
    } catch {
        Write-Log ("ERROR start {0}: {1}" -f $svc, $_.Exception.Message)
    }
}

$health = @{}
# 8088 要等 octop 起 agent（实测 ~30-60s），8787 也要 ~50s；窗口给足，避免误报 FAIL
$triesByPort = @{ 8787 = 150; 8088 = 200 }
foreach ($p in $Ports) { $t = if ($triesByPort.ContainsKey($p)) { $triesByPort[$p] } else { 20 }; $health[$p] = Test-Health $p $t }

$ok = -not ($health.Values -contains $false)
foreach ($p in $Ports) {
    Write-Log ("health {0} = {1}" -f $p, $(if ($health[$p]) { 'ok' } else { 'FAIL' }))
}

foreach ($svc in $services) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    Write-Log ("service {0} status={1}" -f $svc, $(if ($s) { $s.Status } else { '?' }))
}

if ($ok) {
    Write-Log 'RESULT: PASS (both ports healthy, single owner)'
    exit 0
}
Write-Log 'RESULT: FAIL (see per-port health above; check server\services\*.err.log)'
exit 1

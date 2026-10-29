#Requires -Version 5.1
<#
  Register the weekly digest task (every Monday 15:00) on this machine.

  Usage (normal user is enough for a self-owned task):
      powershell -NoProfile -ExecutionPolicy Bypass -File server\install_weekly_task.ps1
      powershell ... -File server\install_weekly_task.ps1 -Uninstall
      powershell ... -File server\install_weekly_task.ps1 -Time 15:00 -Server http://127.0.0.1:8787

  Notes:
    - ASCII only on purpose: Windows PowerShell 5.1 decodes BOM-less files as ANSI.
    - The task runs tools\weekly_run.py, which needs the 8787 service to be up.
    - Weekly card is created as card_kind=weekly and left UNSIGNED; a human signs it.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "Jujing-Weekly-Digest",
    [string]$Time = "15:00",
    [string]$Server = "http://127.0.0.1:8787",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path (Split-Path -Parent $root) "venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = Join-Path (Split-Path -Parent (Split-Path -Parent $root)) "venv\Scripts\python.exe"
}
$runner = Join-Path $root "tools\weekly_run.py"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[ok] removed task $TaskName"
    } else {
        Write-Host "[skip] task $TaskName not found"
    }
    return
}

foreach ($p in @($python, $runner)) {
    if (-not (Test-Path $p)) { throw "missing: $p" }
}

try {
    $health = Invoke-RestMethod -Uri "$Server/api/health" -TimeoutSec 5
    Write-Host "[ok] service up: pack=$($health.pack) version=$($health.version)"
} catch {
    Write-Warning "service $Server not reachable right now; the task will fail until it is up"
}

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-B `"$runner`" --server $Server" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $Time
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "bot-team weekly digest: build weekly card, leave it for human signature" -Force | Out-Null

Write-Host "[ok] registered $TaskName every Monday $Time"
Write-Host "     action : $python -B `"$runner`" --server $Server"
Write-Host "     verify : Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "     run now: Start-ScheduledTask -TaskName $TaskName"

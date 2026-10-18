# Register a weekly local review-due task (Windows Task Scheduler).
# Usage: powershell -ExecutionPolicy Bypass -File tools/install_review_task.ps1
param(
  [string]$Root = "runs",
  [string]$Out = "runs/review-due.md",
  [string]$TaskName = "Jujing-Review-Due",
  [string]$Time = "09:00"
)
$ErrorActionPreference = "Stop"
$python = (Get-Command python).Source
$script = Join-Path $PSScriptRoot "review_due.py"
$args = "`"$script`" --root `"$Root`" --out `"$Out`""
$action = "`"$python`" $args"
schtasks /Create /SC WEEKLY /D MON /TN $TaskName /TR $action /ST $Time /F
Write-Output "registered: $TaskName (Mon $Time) -> $action"

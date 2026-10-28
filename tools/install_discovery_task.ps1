# Register the daily anomaly recheck task (Windows Task Scheduler).
# 主动发现：每日 08:30 扫 runs/*/data 存量数据 → 越过行业包阈值则建单并推发现卡。
# 路径由 discover_recheck.py 按脚本位置解析（对计划任务 cwd=system32 免疫）。
# 与 Jujing-Review-Nudge（09:00 催办）错开半小时。
# Usage: powershell -ExecutionPolicy Bypass -File tools/install_discovery_task.ps1
param(
  [string]$Root = "",
  [string]$TaskName = "Jujing-Discover-Recheck",
  [string]$Time = "08:30"
)
$ErrorActionPreference = "Stop"
$python = (Get-Command python).Source
$script = Join-Path $PSScriptRoot "discover_recheck.py"
$taskArgs = "`"$script`" --apply"
if ($Root) { $taskArgs += " --root `"$Root`"" }
$action = "`"$python`" $taskArgs"
schtasks /Create /SC DAILY /TN $TaskName /TR $action /ST $Time /F
Write-Output "registered: $TaskName (daily $Time) -> $action"

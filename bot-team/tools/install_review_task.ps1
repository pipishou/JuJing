# Register the daily T+7 review-nudge task (Windows Task Scheduler).
# G4：每日 09:00 扫描 runs/ 过期未回填任务 → 4 合 1 网关推飞书催办。
# runs 路径由 review_nudge.py 按脚本位置自行解析（对计划任务 cwd=system32 免疫）。
# Usage: powershell -ExecutionPolicy Bypass -File tools/install_review_task.ps1
param(
  [string]$Root = "",
  [string]$TaskName = "Jujing-Review-Nudge",
  [string]$Time = "09:00"
)
$ErrorActionPreference = "Stop"
$python = (Get-Command python).Source
$script = Join-Path $PSScriptRoot "review_nudge.py"
$taskArgs = "`"$script`""
if ($Root) { $taskArgs += " --root `"$Root`"" }
$action = "`"$python`" $taskArgs"
schtasks /Create /SC DAILY /TN $TaskName /TR $action /ST $Time /F
Write-Output "registered: $TaskName (daily $Time) -> $action"

# Register the G2 reply-consume scheduled task (Windows Task Scheduler).
# 批次 B（乙·调度拉）：每 10 分钟拉取 Mo 的 a/b/c 回信 → 机械留痕/转交 → 网关回执。
# 路径由 reply_consume.py 按脚本位置自行解析（对计划任务 cwd=system32 免疫）。
# Usage: powershell -ExecutionPolicy Bypass -File tools/install_reply_consume_task.ps1
# 撤销: schtasks /Delete /TN Jujing-Reply-Consume /F
param(
  [string]$TaskName = "Jujing-Reply-Consume",
  [int]$Interval = 10
)
$ErrorActionPreference = "Stop"
$python = (Get-Command python).Source
$script = Join-Path $PSScriptRoot "reply_consume.py"
$taskArgs = "`"$script`""
$action = "`"$python`" $taskArgs"
schtasks /Create /SC MINUTE /MO $Interval /TN $TaskName /TR $action /F
Write-Output "registered: $TaskName (every $Interval min) -> $action"

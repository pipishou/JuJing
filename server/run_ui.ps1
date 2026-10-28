# Launcher for scheduled task "Jujing-BotTeam-UI".
# Same constraint as run_octop.ps1: processes spawned by the IDE/tool host cannot write into
# JuJing\bot-team (PermissionError 13), so the local web server is started by Task Scheduler.
# Kept strictly ASCII: PowerShell 5.1 decodes BOM-less scripts as ANSI.
$root = (Get-Location).Path
$log = Join-Path $root '.octop\logs\ui-via-task.log'
$py = Join-Path $root 'venv\Scripts\python.exe'
$bt = Join-Path (Join-Path $root 'JuJing') 'bot-team'

"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] launcher start pid=$PID root=$root" | Out-File -FilePath $log -Append -Encoding utf8

if (-not (Test-Path $py)) {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] python not found: $py" | Out-File -FilePath $log -Append -Encoding utf8
    exit 2
}

Set-Location $bt
$env:BOT_ROOT = $bt
$env:BOT_PORT = '8787'
try {
    & $py (Join-Path $bt 'server\app.py') --port 8787 2>&1 | Add-Content -Path $log -Encoding UTF8
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ui exited code=$LASTEXITCODE" | Out-File -FilePath $log -Append -Encoding utf8
} catch {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] launcher exception: $_" | Out-File -FilePath $log -Append -Encoding utf8
}

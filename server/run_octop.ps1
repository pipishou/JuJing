# Launcher for scheduled task "Jujing-Octop-Run".
# Runs octop OUTSIDE the IDE/tool process tree: writes into JuJing\bot-team are blocked for
# processes spawned by the tool host, and this launcher must not inherit that restriction.
# Kept strictly ASCII: PowerShell 5.1 decodes BOM-less scripts as ANSI, so non-ASCII
# literals would be mangled. Project root is taken from the task working directory.
$root = (Get-Location).Path
$log = Join-Path $root '.octop\logs\run-via-task.log'
$py = Join-Path $root 'venv\Scripts\python.exe'

"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] launcher start pid=$PID root=$root" | Out-File -FilePath $log -Append -Encoding utf8

if (-not (Test-Path $py)) {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] python not found: $py" | Out-File -FilePath $log -Append -Encoding utf8
    exit 2
}

$env:OCTOP_HOME = Join-Path $root '.octop'
$env:OCTOP_CARD_HOOK = (Join-Path (Join-Path (Join-Path (Join-Path $root 'JuJing') 'bot-team') 'tools') 'card_consumer.py') + '::on_card_action'
Set-Location $root
try {
    & $py -m octop run *>> $log
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] octop exited code=$LASTEXITCODE" | Out-File -FilePath $log -Append -Encoding utf8
} catch {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] launcher exception: $_" | Out-File -FilePath $log -Append -Encoding utf8
}

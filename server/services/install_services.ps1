#Requires -RunAsAdministrator
# Installs the two WinSW-managed Windows services for the Jujing bot.
# ASCII only: PowerShell 5.1 decodes BOM-less scripts as ANSI.
$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src  = Join-Path $here 'WinSW-x64.exe'
$log  = Join-Path $here 'install.log'

function W([string]$m) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Out-File -FilePath $log -Append -Encoding utf8
}

W "=== install start (admin=$([bool](([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)))) pid=$PID ==="

if (-not (Test-Path $src)) { W "missing wrapper: $src"; exit 70 }

foreach ($n in @('jujing-octop','jujing-botteam-ui')) {
    $exe = Join-Path $here ($n + '.exe')
    Copy-Item $src $exe -Force
    W "wrapper copied -> $exe"

    $svc = Get-Service -Name $n -ErrorAction SilentlyContinue
    if ($svc) {
        W "$n already registered (state=$($svc.Status)); reinstalling"
        & $exe stop    *>&1 | ForEach-Object { W "  stop: $_" }
        Start-Sleep -Seconds 3
        & $exe uninstall *>&1 | ForEach-Object { W "  uninstall: $_" }
        Start-Sleep -Seconds 2
    }

    & $exe install *>&1 | ForEach-Object { W "  install: $_" }
    W "$n install exit=$LASTEXITCODE"
}

Start-Sleep -Seconds 2
foreach ($n in @('jujing-octop','jujing-botteam-ui')) {
    $exe = Join-Path $here ($n + '.exe')
    & $exe start *>&1 | ForEach-Object { W "  start($n): $_" }
    W "$n start exit=$LASTEXITCODE"
}

Start-Sleep -Seconds 8
foreach ($n in @('jujing-octop','jujing-botteam-ui')) {
    $svc = Get-Service -Name $n -ErrorAction SilentlyContinue
    if ($svc) { W "status $n = $($svc.Status)" } else { W "status $n = MISSING" }
}
W "=== install done ==="
exit 0

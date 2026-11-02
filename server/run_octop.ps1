# Launcher for scheduled task "Jujing-Octop-Run" (single-instance guarded).
#
# Runs octop OUTSIDE the IDE/tool process tree: writes into JuJing\bot-team are
# blocked for processes spawned by the tool host, and this launcher must not
# inherit that restriction.
#
# Guard order (fail closed, never start a second gateway silently):
#   1. named mutex Global\JujingOctopGateway (Local\ fallback on ACL denial)
#   2. TCP probe 127.0.0.1:8088
#        port closed        -> start
#        port open + /api/health == 200 -> REFUSE, exit 3
#        port open + anything else      -> REFUSE, exit 4 (unknown listener)
#
# Exit codes: 0 ok / octop exit code, 2 python or .octop missing,
#             3 another gateway is already running, 4 port 8088 held by an
#             unknown process, 5 launcher exception.
#
# Kept strictly ASCII: PowerShell 5.1 decodes BOM-less scripts as ANSI, so
# non-ASCII literals would be mangled. Project root is taken from the task
# working directory, with a script-relative fallback.
param(
    [int]$Port = 8088
)
$ErrorActionPreference = 'Stop'
$root = (Get-Location).Path
if (-not (Test-Path (Join-Path $root '.octop'))) {
    $fallback = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    if (Test-Path (Join-Path $fallback '.octop')) { $root = $fallback }
}
if (-not (Test-Path (Join-Path $root '.octop'))) {
    Write-Host "octop launcher: .octop not found under $root"
    exit 2
}

$logDir = Join-Path $root '.octop\logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$log = Join-Path $logDir 'run-via-task.log'
$py = Join-Path $root 'venv\Scripts\python.exe'
$port = $Port
$healthUrl = "http://127.0.0.1:$port/api/health"
$mutexName = 'Global\JujingOctopGateway'
$script:mutex = $null
$script:ownsMutex = $false

function Write-LauncherLog {
    param([string]$Message)
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Add-Content -Path $log -Value $line -Encoding UTF8
}

function Test-PortOpen {
    param(
        [string]$TargetHost,
        [int]$TargetPort,
        [int]$TimeoutMs = 1500
    )
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($TargetHost, $TargetPort, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $client) { $client.Close() }
    }
}

function Get-HealthStatusCode {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 5
        return [int]$response.StatusCode
    } catch {
        $webResponse = $_.Exception.Response
        if ($null -ne $webResponse) {
            try { return [int]$webResponse.StatusCode } catch { return -1 }
        }
        return -1
    }
}

Write-LauncherLog "launcher start pid=$PID root=$root"

if (-not (Test-Path $py)) {
    Write-LauncherLog "python not found: $py"
    exit 2
}

try {
    # 1) single-instance mutex, held for the whole lifetime of the child.
    $createdNew = $false
    try {
        $script:mutex = [System.Threading.Mutex]::new($false, $mutexName, [ref]$createdNew)
    } catch [System.UnauthorizedAccessException] {
        Write-LauncherLog "WARN global mutex denied; falling back to Local\JujingOctopGateway"
        $mutexName = 'Local\JujingOctopGateway'
        $script:mutex = [System.Threading.Mutex]::new($false, $mutexName, [ref]$createdNew)
    }

    $acquired = $false
    try {
        $acquired = $script:mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $acquired = $true
        Write-LauncherLog "WARN previous gateway mutex was abandoned; taking ownership"
    }
    if (-not $acquired) {
        Write-LauncherLog "REFUSE another launcher holds $mutexName; not starting a second instance"
        exit 3
    }
    $script:ownsMutex = $true
    Write-LauncherLog "mutex acquired: $mutexName (created=$createdNew)"

    # 2) port probe: an orphan (or service-managed) gateway owns 8088.
    if (Test-PortOpen -TargetHost '127.0.0.1' -TargetPort $port) {
        $code = Get-HealthStatusCode -Url $healthUrl
        if ($code -eq 200) {
            Write-LauncherLog "REFUSE 127.0.0.1:$port already serves $healthUrl (HTTP 200); gateway is alive"
            exit 3
        }
        Write-LauncherLog "REFUSE 127.0.0.1:$port is open but /api/health returned $code; unknown listener, fail closed"
        exit 4
    }
    Write-LauncherLog "port $port is free; starting octop"

    $env:OCTOP_HOME = Join-Path $root '.octop'
    $env:OCTOP_CARD_HOOK = (Join-Path (Join-Path (Join-Path (Join-Path $root 'JuJing') 'bot-team') 'tools') 'card_consumer.py') + '::on_card_action'
    # Keep child output UTF-8 and byte-for-byte in the log. PowerShell's *>>
    # would encode native output as UTF-16 on PS 5.1 and corrupt the log.
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONUNBUFFERED = '1'
    Set-Location $root
    $cmdLine = '"' + $py + '" -m octop run >> "' + $log + '" 2>&1'
    & cmd.exe /d /c $cmdLine
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    Write-LauncherLog "octop exited code=$code"
    exit $code
} catch {
    Write-LauncherLog "launcher exception: $_"
    exit 5
} finally {
    if ($script:ownsMutex -and $null -ne $script:mutex) {
        try { $script:mutex.ReleaseMutex() } catch { }
    }
    if ($null -ne $script:mutex) {
        try { $script:mutex.Dispose() } catch { }
    }
}

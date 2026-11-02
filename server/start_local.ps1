# 本机临时入口：不需要管理员，在 8790 跑当前代码（8787 被 SYSTEM 孤儿进程占用时的替代）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root "..\..\venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$env:BOT_PACK = Join-Path $root "packs\workops\pack.json"
$env:PYTHONIOENCODING = "utf-8"
Write-Host ("[start] root={0}" -f $root)
Write-Host ("[start] python={0}" -f $py)
& $py -B (Join-Path $root "server\app.py") --port 8790 --root $root

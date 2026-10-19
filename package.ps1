# Package bot-team-dsh -> dist/bot-team-dsh-<date>.zip + manifest (sha256)
# Usage: powershell -ExecutionPolicy Bypass -File package.ps1 [-Date 2026-10-18]
# NOTE: ASCII-only comments on purpose (Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI).
param(
  [string]$Date = (Get-Date -Format 'yyyy-MM-dd')
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$dist = Join-Path $root 'dist'
New-Item -ItemType Directory -Force -Path $dist | Out-Null

# 1) staging copy (exclude dist so old zips are not packed)
$staging = Join-Path $env:TEMP ("bot-team-dsh-staging-" + $Date)
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Force -Path $staging | Out-Null
Get-ChildItem $root -Force | Where-Object { $_.Name -ne 'dist' } | ForEach-Object {
  Copy-Item $_.FullName -Destination $staging -Recurse -Force
}
# never package caches or real task data
Get-ChildItem $staging -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
$runs = Join-Path $staging 'runs'
if (Test-Path $runs) {
  Get-ChildItem $runs -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
}

# 2) manifest: every file + sha256, relative to package root
$manifest = Join-Path $dist ("manifest-" + $Date + ".txt")
$files = Get-ChildItem $staging -Recurse -File
$lines = foreach ($f in $files) {
  $rel = $f.FullName.Substring($staging.Length + 1)
  $hash = (Get-FileHash $f.FullName -Algorithm SHA256).Hash.ToLower()
  "$hash  $rel"
}
$lines | Set-Content -Path $manifest -Encoding UTF8

# 3) zip
$zip = Join-Path $dist ("bot-team-dsh-" + $Date + ".zip")
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $zip -CompressionLevel Optimal
$zipInfo = Get-Item $zip
$zipHash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
Remove-Item $staging -Recurse -Force

Write-Output "zip      : $($zipInfo.FullName)"
Write-Output ("size     : " + [math]::Round($zipInfo.Length / 1KB, 1) + " KB")
Write-Output "sha256   : $zipHash"
Write-Output "manifest : $manifest"
Write-Output ("files    : " + $files.Count)

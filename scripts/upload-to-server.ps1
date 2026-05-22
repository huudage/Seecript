#requires -Version 5.1
<#
.SYNOPSIS
  Upload Seecript to the chronic-medication server (47.239.58.145) via tar + scp.

.DESCRIPTION
  Why this exists:
  - We don't want to require git or rsync on Windows
  - PowerShell 5.1 mangles binary streams in pipelines, so we cannot do
    `tar -czf - . | ssh ... "tar -xzf -"`. Instead we go via a temp tarball.

  Steps:
    1. Tar the project locally (excluding venv / caches / logs)
    2. scp the tarball to /tmp on the server
    3. SSH in and extract to /opt/seecript (creating the dir if needed)
    4. Delete the temp tarball on both ends

.PARAMETER Server
  Server SSH target. Default: root@47.239.58.145

.PARAMETER RemoteDir
  Where to put the code on the server. Default: /opt/seecript

.EXAMPLE
  .\scripts\upload-to-server.ps1
#>
param(
  [string]$Server    = "root@47.239.58.145",
  [string]$RemoteDir = "/opt/seecript"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$tarballName = "seecript-upload-$(Get-Date -Format 'yyyyMMdd-HHmmss').tar.gz"
$tarballPath = Join-Path $env:TEMP $tarballName

Write-Host "==== Seecript upload ====" -ForegroundColor Cyan
Write-Host "Source : $projectRoot"
Write-Host "Target : ${Server}:${RemoteDir}"
Write-Host "Tarball: $tarballPath"
Write-Host ""

# Step 1: Tar the project, excluding things we don't want on the server.
# IMPORTANT: tar needs forward slashes for excludes on Windows; -C handles cwd.
$excludes = @(
  "--exclude=server/venv",
  "--exclude=server/.venv",
  "--exclude=__pycache__",
  "--exclude=.pytest_cache",
  "--exclude=.coverage",
  "--exclude=htmlcov",
  "--exclude=logs",
  "--exclude=*.log",
  "--exclude=.server.pid",
  "--exclude=*.bak",
  "--exclude=node_modules",
  "--exclude=.git"
)

Write-Host "[1/4] Creating tarball (excluding venv/caches)..." -ForegroundColor Yellow
& tar.exe -czf $tarballPath @excludes -C $projectRoot .
if ($LASTEXITCODE -ne 0) { throw "tar failed (exit $LASTEXITCODE)" }
$sizeBytes = (Get-Item $tarballPath).Length
$sizeMB = [math]::Round($sizeBytes / 1MB, 2)
Write-Host "       size: $sizeMB MB" -ForegroundColor Green

# Step 2: scp to /tmp on the server.
Write-Host "[2/4] scp to ${Server}:/tmp/..." -ForegroundColor Yellow
& scp.exe $tarballPath "${Server}:/tmp/$tarballName"
if ($LASTEXITCODE -ne 0) { throw "scp failed (exit $LASTEXITCODE)" }
Write-Host "       scp ok" -ForegroundColor Green

# Step 3: extract on the server. We use a single ssh invocation to keep auth count low.
Write-Host "[3/4] extract on server -> $RemoteDir ..." -ForegroundColor Yellow
$remoteScript = @"
set -e
mkdir -p $RemoteDir
cd $RemoteDir
tar -xzf /tmp/$tarballName
chown -R root:root $RemoteDir
ls -la $RemoteDir | head -25
"@
& ssh.exe $Server $remoteScript
if ($LASTEXITCODE -ne 0) { throw "remote extract failed (exit $LASTEXITCODE)" }
Write-Host "       extract ok" -ForegroundColor Green

# Step 4: cleanup temp tarballs (best-effort).
Write-Host "[4/4] cleanup..." -ForegroundColor Yellow
Remove-Item $tarballPath -ErrorAction SilentlyContinue
& ssh.exe $Server "rm -f /tmp/$tarballName" | Out-Null
Write-Host "       done" -ForegroundColor Green

Write-Host ""
Write-Host "==== UPLOAD COMPLETE ====" -ForegroundColor Cyan
Write-Host "Next step: SSH in and run the installer:"
Write-Host "  ssh $Server"
Write-Host "  sudo DOMAIN=seecript.zlhu.asia bash $RemoteDir/scripts/install-on-medi-server.sh"

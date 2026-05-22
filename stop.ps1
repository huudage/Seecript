#Requires -Version 5.1
# Stop the http.server started by run.ps1 (graceful, then force after 3s)
$ErrorActionPreference = 'Stop'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $ScriptRoot '.server.pid'
if (-not (Test-Path -LiteralPath $PidFile)) {
  throw "PID file $PidFile not found - server probably not started by run.ps1"
}
$line = (Get-Content -LiteralPath $PidFile -Raw) -as [string]
$targetPid = [int]($line.Trim())
$proc = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
if ($null -eq $proc) {
  Remove-Item -LiteralPath $PidFile -Force
  Write-Host "Process $targetPid is already gone, cleaned $PidFile"
  exit 0
}
$proc | Stop-Process -ErrorAction SilentlyContinue
$deadline = (Get-Date).AddSeconds(3)
while ((Get-Date) -lt $deadline) {
  $p2 = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
  if ($null -eq $p2) { break }
  Start-Sleep -Milliseconds 200
}
$p3 = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
if ($null -ne $p3) {
  Write-Warning "Force killing PID $targetPid"
  $p3 | Stop-Process -Force
}
Remove-Item -LiteralPath $PidFile -Force
Write-Host "Stopped (PID: $targetPid)"
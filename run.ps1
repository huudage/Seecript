#Requires -Version 5.1
# Seecript launcher (FastAPI + uvicorn). Serves the static frontend AND /api/* in one process.
#
# Usage:
#   .\run.ps1                  # bootstrap venv if missing, install deps, start server
#   $env:PORT = 8091; .\run.ps1
#
# Env overrides:
#   PORT             default 8090
#   HOST             default 127.0.0.1
#   SKIP_INSTALL     1 = skip pip install (faster restart, only safe if requirements unchanged)
#   PYTHON           override interpreter; default tries `python` then `py -3`

$ErrorActionPreference = 'Stop'

# --- Paths ---
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir  = Join-Path $ScriptRoot 'server'
$VenvDir    = Join-Path $ServerDir 'venv'
$LogDir     = Join-Path $ScriptRoot 'logs'
$PidFile    = Join-Path $ScriptRoot '.server.pid'
$OutLog     = Join-Path $LogDir 'uvicorn.log'
$ErrLog     = Join-Path $LogDir 'uvicorn.err.log'

$DefaultPort = 8090
$DefaultHost = '127.0.0.1'
$Port = if ($env:PORT) { [int]($env:PORT) } else { $DefaultPort }
$HostBind = if ($env:HOST) { $env:HOST } else { $DefaultHost }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $ServerDir | Out-Null

# --- Pre-flight: refuse to double-start ---
if (Test-Path -LiteralPath $PidFile) {
  $oldPidRaw = (Get-Content -LiteralPath $PidFile -Raw -ErrorAction SilentlyContinue) -as [string]
  $oldPid = if ($oldPidRaw) { $oldPidRaw.Trim() } else { $null }
  if ($oldPid) {
    $p = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($null -ne $p) {
      throw "Seecript already running (PID $oldPid). Run .\stop.ps1 first."
    }
  }
  Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

# Warn (don't fail) if port already taken — uvicorn will throw a clear error itself.
$listening = & netstat -an 2>$null | Select-String -Pattern ":$Port\s+.*LISTEN" -ErrorAction SilentlyContinue
if ($listening) {
  Write-Warning "Port $Port appears to be in use. Set `$env:PORT` to another value if uvicorn fails to bind."
}

# --- Resolve Python interpreter (system python; venv is created via this) ---
function Resolve-Python {
  if ($env:PYTHON) {
    $cmd = Get-Command $env:PYTHON -ErrorAction SilentlyContinue
    if ($cmd) { return @{ Path = $cmd.Source; Args = @() } }
    throw "PYTHON env points to '$env:PYTHON' but it was not found."
  }
  $py = Get-Command 'python' -ErrorAction SilentlyContinue
  if ($py) { return @{ Path = $py.Source; Args = @() } }
  $py = Get-Command 'py' -ErrorAction SilentlyContinue
  if ($py) { return @{ Path = $py.Source; Args = @('-3') } }
  throw "Neither 'python' nor 'py' found. Install Python 3.10+ first."
}

# --- Bootstrap venv if missing ---
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPython)) {
  Write-Host "venv missing — bootstrapping at $VenvDir ..."
  $sysPython = Resolve-Python
  & $sysPython.Path @($sysPython.Args + @('-m', 'venv', $VenvDir))
  if ($LASTEXITCODE -ne 0) { throw "Failed to create venv (exit $LASTEXITCODE)." }
}

# --- Install / refresh dependencies ---
if ($env:SKIP_INSTALL -ne '1') {
  Write-Host "pip install -r server/requirements.txt ..."
  & $VenvPython -m pip install --upgrade pip --quiet
  if ($LASTEXITCODE -ne 0) { throw "pip self-upgrade failed." }
  & $VenvPython -m pip install -r (Join-Path $ServerDir 'requirements.txt') --quiet
  if ($LASTEXITCODE -ne 0) { throw "pip install failed; check $ErrLog" }
} else {
  Write-Host "SKIP_INSTALL=1 — skipping pip install."
}

# --- .env hint ---
$EnvFile = Join-Path $ServerDir '.env'
$EnvExample = Join-Path $ServerDir '.env.example'
if (-not (Test-Path -LiteralPath $EnvFile)) {
  if (Test-Path -LiteralPath $EnvExample) {
    Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
    Write-Host "Created server/.env from .env.example. LLM_PROVIDER=mock by default — edit to add your DeepSeek key."
  } else {
    Write-Warning "server/.env not found and no .env.example to seed it."
  }
}

# --- Start uvicorn ---
$UvicornArgs = @(
  '-m', 'uvicorn',
  'app.main:app',
  '--host', $HostBind,
  '--port', "$Port",
  '--log-level', 'info'
)

Write-Host "Working dir : $ServerDir"
Write-Host "Frontend    : http://$HostBind`:$Port/"
Write-Host "API base    : http://$HostBind`:$Port/api/"
Write-Host "Docs (dev)  : http://$HostBind`:$Port/docs"
Write-Host "Logs        : $OutLog / $ErrLog"

$proc = Start-Process -FilePath $VenvPython -ArgumentList $UvicornArgs `
  -WorkingDirectory $ServerDir -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
$proc.Id | Out-File -FilePath $PidFile -Encoding ascii -NoNewline

# Sanity: process should still be alive after a moment
Start-Sleep -Milliseconds 800
$alive = $null
try { $alive = Get-Process -Id $proc.Id -ErrorAction Stop } catch { }
if ($null -eq $alive) {
  Write-Warning "Process exited immediately. Last 40 lines of $ErrLog :"
  if (Test-Path -LiteralPath $ErrLog) { Get-Content -LiteralPath $ErrLog -Tail 40 }
  exit 1
}

Write-Host "Started PID: $($proc.Id). Stop with .\stop.ps1"

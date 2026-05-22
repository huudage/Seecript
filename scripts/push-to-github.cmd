@echo off
REM Wrapper for push-to-github.ps1 with -ExecutionPolicy Bypass.
REM
REM Usage (from anywhere — this wrapper auto-cd's to the Seecript project root):
REM   path\to\scripts\push-to-github.cmd https://github.com/yourname/seecript.git
REM
REM Optional extra args pass through (e.g. -CommitAuthorName "Your Name" -CommitAuthorEmail you@x.com)

REM %~dp0 = directory of this .cmd (...\scripts\). Project root is its parent.
REM Force cwd to the project root so the .ps1 cannot accidentally operate on
REM whatever git repo the caller happens to be in (this was a real near-miss).
pushd "%~dp0.." >nul
if errorlevel 1 (
  echo FAIL : cannot cd into project root from %~dp0
  exit /b 1
)
echo [wrapper] cwd switched to %CD%

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0push-to-github.ps1" -RepoUrl %1 %2 %3 %4 %5 %6 %7 %8 %9
set RC=%ERRORLEVEL%
popd >nul
exit /b %RC%

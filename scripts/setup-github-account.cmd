@echo off
REM Wrapper for setup-github-account.ps1 with -ExecutionPolicy Bypass.
REM
REM Usage:
REM   scripts\setup-github-account.cmd
REM   scripts\setup-github-account.cmd -Username yourname -Email you@example.com
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-github-account.ps1" %*
exit /b %ERRORLEVEL%

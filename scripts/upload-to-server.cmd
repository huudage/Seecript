@echo off
REM ----------------------------------------------------------------------------
REM Wrapper that invokes upload-to-server.ps1 with -ExecutionPolicy Bypass.
REM Useful when the user has not run `Set-ExecutionPolicy RemoteSigned`.
REM
REM Usage:
REM   scripts\upload-to-server.cmd
REM   scripts\upload-to-server.cmd -Server root@1.2.3.4 -RemoteDir /opt/koc
REM ----------------------------------------------------------------------------
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0upload-to-server.ps1" %*
exit /b %ERRORLEVEL%

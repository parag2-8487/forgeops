@echo off
setlocal

rem ============================================================================
rem  ForgeOps - Master Setup & Installation Script (Windows)
rem
rem  Counterpart to setup.sh on Linux. Double-click or run from cmd/powershell:
rem      setup.cmd
rem      setup.cmd -Fresh
rem      setup.cmd -Fresh -Force
rem      setup.cmd -SkipInstall -NoBrowser
rem ============================================================================

cd /d "%~dp0"

set "PS_SCRIPT=%~dp0setup.ps1"

if not exist "%PS_SCRIPT%" (
    echo.
    echo   CANNOT CONTINUE: setup.ps1 was not found.
    echo   Expected it at: %PS_SCRIPT%
    echo.
    pause
    exit /b 1
)

where pwsh.exe >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PS_EXE=pwsh.exe"
) else (
    set "PS_EXE=powershell.exe"
)

"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*

set "RC=%ERRORLEVEL%"

echo %CMDCMDLINE% | find /i "/c" >nul 2>&1
if %ERRORLEVEL% equ 0 pause

exit /b %RC%

@echo off
setlocal

rem ============================================================================
rem  ForgeOps - one-click start (Windows)
rem
rem  DOUBLE-CLICK THIS FILE. It starts the whole application, installing whatever
rem  is missing first.
rem
rem  This wrapper exists because a .ps1 file is not double-clickable on Windows:
rem  the default action for it is "open in Notepad", and even when invoked
rem  directly the default execution policy refuses to run an unsigned script.
rem  A .cmd file has neither problem, so this is the entry point and
rem  scripts\start-forgeops.ps1 holds the actual work.
rem
rem  Arguments are passed straight through, so all of these work:
rem      start.cmd
rem      start.cmd -Fresh
rem      start.cmd -Fresh -Force
rem      start.cmd -SkipInstall -NoBrowser
rem      start.cmd -Rebuild
rem ============================================================================

cd /d "%~dp0"

set "PS_SCRIPT=%~dp0scripts\start-forgeops.ps1"

if not exist "%PS_SCRIPT%" (
    echo.
    echo   CANNOT CONTINUE: scripts\start-forgeops.ps1 was not found.
    echo   Expected it at: %PS_SCRIPT%
    echo.
    echo   This file must stay in the repository root, next to the scripts folder.
    echo.
    pause
    exit /b 1
)

rem Prefer PowerShell 7 when present; fall back to the Windows PowerShell that
rem ships with every supported version of Windows. The script targets 5.1 so
rem either is fine.
where pwsh.exe >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PS_EXE=pwsh.exe"
) else (
    set "PS_EXE=powershell.exe"
)

rem -NoProfile          a user profile that writes to stdout or sets StrictMode
rem                     differently would change how the script behaves
rem -ExecutionPolicy Bypass   for this process only; nothing machine-wide is changed
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*

set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo   Finished. The application is running; this window can be closed.
) else (
    echo   The launcher stopped with exit code %RC%. The reason is above.
)
echo.

rem Only pause when double-clicked. When run from an existing console the caller
rem does not want to press a key, and CI would hang forever. The heuristic: a
rem double-clicked .cmd gets cmd.exe's /c form as its command line.
echo %CMDCMDLINE% | find /i "/c" >nul 2>&1
if %ERRORLEVEL% equ 0 pause

exit /b %RC%

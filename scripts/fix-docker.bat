@echo off
REM =============================================================================
REM scripts\fix-docker.bat — double-clickable wrapper for fix-docker.ps1
REM
REM Double-click this file (or invoke from cmd) when Docker Desktop won't start.
REM The .ps1 it calls is idempotent: safe to run even when Docker is healthy.
REM
REM What it does: kills lingering Docker processes, stashes zombie AF_UNIX
REM socket files aside, re-registers the docker-desktop WSL distro if missing,
REM launches Docker Desktop, waits up to ~4 minutes for the engine to respond.
REM See fix-docker.ps1 for full detail.
REM =============================================================================

setlocal
set SCRIPT_DIR=%~dp0
set PS1=%SCRIPT_DIR%fix-docker.ps1

echo Running Docker Desktop recovery (fix-docker.ps1)...
echo.

REM Prefer pwsh (PowerShell 7+) when available — Windows PowerShell 5.1 has
REM tokenizer quirks around `&&` in string literals that we don't want to fight.
REM Falls back to powershell.exe if pwsh isn't installed.
where pwsh.exe >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
)
set RC=%ERRORLEVEL%

echo.
if %RC% EQU 0 (
    echo [SUCCESS] Docker Desktop is up.
) else (
    echo [FAILED] Recovery script exited with code %RC%. Review output above.
)
echo.
pause
endlocal
exit /b %RC%

@echo off
setlocal
set "ROOT=%~dp0.."

where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 "%ROOT%\obs_sync.py" %*
) else (
  python "%ROOT%\obs_sync.py" %*
)

if not %errorlevel% equ 0 pause
exit /b %errorlevel%

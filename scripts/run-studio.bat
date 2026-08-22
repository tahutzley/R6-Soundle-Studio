@echo off
setlocal
cd /d "%~dp0.."
py -3 studio_server.py %*
if errorlevel 1 pause

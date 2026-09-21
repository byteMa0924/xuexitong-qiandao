@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title xuexitong-signin-monitor

rem ============================================================
rem  Double-click this file to open the control panel.
rem
rem  IMPORTANT: keep this .bat file ASCII-ONLY.
rem  cmd.exe reads .bat files using the system ANSI codepage,
rem  so UTF-8 Chinese text here gets mis-parsed and shreds the
rem  commands (e.g. "errorlevel" becomes "orlevel").
rem  This was verified -- it is why double-clicking did nothing.
rem  Chinese user-facing text is printed by Python instead.
rem ============================================================

set "PY="
set "PYW="
where py >nul 2>nul && set "PY=py -3"
where pyw >nul 2>nul && set "PYW=pyw -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
    where pythonw >nul 2>nul && set "PYW=pythonw"
)
if not defined PY (
    echo.
    echo   [ERROR] Python not found.
    echo.
    echo   Please install Python 3.8 or newer:
    echo       https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)
if not defined PYW set "PYW=%PY%"

rem The control panel needs tkinter (bundled with the official Python).
%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo.
    echo   [WARN] This Python has no tkinter, so the panel cannot be shown.
    echo          Starting console mode instead - press Ctrl+C to stop.
    echo.
    %PY% "%~dp0cxmon.py" monitor
    echo.
    pause
    exit /b 0
)

rem Launch the panel without a console window.
start "" %PYW% "%~dp0cxmon.py" panel
exit /b 0

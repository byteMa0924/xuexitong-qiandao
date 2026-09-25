@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title cxmon-build-exe

rem ============================================================
rem  Build cxmon.exe -- for classmates who don't want to
rem  install Python.
rem
rem  IMPORTANT: KEEP THIS FILE ASCII-ONLY.
rem  cmd.exe parses .bat files using the system ANSI codepage, so
rem  UTF-8 Chinese text here gets mis-parsed and shreds the
rem  commands (verified: "errorlevel" became "ofile", and the
rem  build silently did nothing). Chinese paths and filenames are
rem  handled by make_release.py, which Python reads as UTF-8.
rem ============================================================

echo.
echo ============================================
echo   Building cxmon.exe
echo ============================================
echo.

set "PY="
if exist "..\.cxmon-build\Scripts\python.exe" set "PY=..\.cxmon-build\Scripts\python.exe"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo   [ERROR] Python not found.
    echo   Install Python 3.8+ and run:
    echo       python -m pip install pyinstaller
    echo.
    pause
    exit /b 1
)

%PY% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo   [ERROR] This Python has no PyInstaller. Run:
    echo       %PY% -m pip install pyinstaller
    echo.
    pause
    exit /b 1
)

echo [1/3] Cleaning old output ...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo [2/3] Running PyInstaller (about 30-60 seconds) ...
rem All build settings live in cxmon.spec (icon, bundled files, version info,
rem upx=False, console=False). Do NOT pass them here as CLI flags: that
rem regenerates the spec and the two copies drift apart.
%PY% -m PyInstaller --noconfirm --clean cxmon.spec
if errorlevel 1 (
    echo.
    echo   [ERROR] PyInstaller failed. See the messages above.
    pause
    exit /b 1
)

echo [3/3] Making the release zip ...
%PY% "%~dp0make_release.py"
if errorlevel 1 (
    echo.
    echo   [ERROR] Packaging failed.
    pause
    exit /b 1
)

echo ============================================
echo   Done
echo ============================================
echo   Test    : dist\cxmon\cxmon.exe   (double-click)
echo   Upload  : dist\cxmon-windows-x64.zip
echo             -^> GitHub Releases, NOT into the repo
echo.
pause

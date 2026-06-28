@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo    AI Usage Widget - Build EXE
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 goto NOPYTHON

echo [1/3] Installing required packages (requests, browser_cookie3, pyinstaller)...
python -m pip install --upgrade pip >nul 2>nul
python -m pip install requests browser_cookie3 pyinstaller
if errorlevel 1 goto PIPFAIL
echo       (optional) installing curl_cffi / rookiepy for better Claude support...
python -m pip install curl_cffi rookiepy >nul 2>nul

set "ICON_OPT="
set "ICON_DATA="
if exist icon.ico set "ICON_OPT=--icon icon.ico"
if exist icon.ico set "ICON_DATA=--add-data icon.ico;."

rem Bundle optional libs into the EXE only if they are installed
set "COLLECT="
python -c "import browser_cookie3" >nul 2>nul && set "COLLECT=%COLLECT% --collect-all browser_cookie3"
python -c "import curl_cffi" >nul 2>nul && set "COLLECT=%COLLECT% --collect-all curl_cffi"
python -c "import rookiepy" >nul 2>nul && set "COLLECT=%COLLECT% --collect-all rookiepy"

echo.
echo [2/3] Building EXE. First run takes a few minutes...
python -m PyInstaller --noconfirm --onefile --windowed --name AIUsageWidget %ICON_OPT% %ICON_DATA%%COLLECT% ai_usage_widget.py
if errorlevel 1 goto BUILDFAIL

echo.
echo ==========================================
echo  [3/3] Done.
echo   Output file: dist\AIUsageWidget.exe
echo.
echo   Run it, then click the gear icon on the widget
echo   and paste your Claude sessionKey to start.
echo ==========================================
echo.
pause
exit /b 0

:NOPYTHON
echo [ERROR] Python was not found on PATH.
echo   Install Python from https://www.python.org/downloads/
echo   During setup, check "Add Python to PATH", then re-run this file.
echo.
pause
exit /b 1

:PIPFAIL
echo.
echo [ERROR] Failed to install packages with pip.
echo   Check your internet connection and that pip works:
echo       python -m pip --version
echo.
pause
exit /b 1

:BUILDFAIL
echo.
echo [ERROR] PyInstaller build failed. See the log above for details.
echo.
pause
exit /b 1

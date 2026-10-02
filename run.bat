@echo off
setlocal
cd /d "%~dp0"

rem Find a real Python 3.10 or newer. A bare "python" can be the Microsoft Store stub,
rem which prints nothing and only opens the Store, so each candidate must answer.
set "PYEXE="
for %%L in ("python" "py -3" "python3") do (
  if not defined PYEXE (
    for /f "delims=" %%I in ('%%~L -c "import sys; print(sys.executable) if sys.version_info >= (3, 10) else None" 2^>nul') do set "PYEXE=%%I"
  )
)

if not defined PYEXE (
  echo PC Cleanup needs Python 3.10 or newer.
  echo.
  echo Install it from https://www.python.org/downloads/
  echo During setup, tick "Add python.exe to PATH", then run this file again.
  echo.
  pause
  exit /b 1
)

set "PYW=%PYEXE:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PYEXE%"

"%PYEXE%" -c "import customtkinter, psutil, cryptography" >nul 2>&1
if errorlevel 1 (
  echo Installing the pieces this app needs. This happens once.
  "%PYEXE%" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo.
    echo The install did not finish. Check the internet connection and try again.
    pause
    exit /b 1
  )
)

start "" "%PYW%" "%~dp0main.py"
endlocal

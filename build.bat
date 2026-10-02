@echo off
setlocal
cd /d "%~dp0"

rem Builds dist\PC Cleanup.exe: one file that runs on any Windows 10 or 11 PC
rem without Python. Only building needs Python. If it is missing, this
rem downloads the official installer from python.org and installs it for this
rem user. The build tools go in .build-venv here, so other Python setups are left alone.

echo === PC Cleanup build ===
echo.
call :find_python
if not defined PYEXE (
  echo Python 3.10 or newer was not found on this PC.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\get-python.ps1"
  if errorlevel 1 goto failed
  call :find_python
)
if not defined PYEXE (
  echo Python was installed, but this window cannot see it yet.
  echo Close this window and run build.bat again.
  goto failed
)
"%PYEXE%" -c "import sys; print('Using Python', sys.version.split()[0], 'at', sys.executable)"

if not exist ".build-venv\Scripts\python.exe" (
  echo Setting up the build tools. This happens once.
  "%PYEXE%" -m venv .build-venv
  if errorlevel 1 goto failed
)

echo Checking the app's requirements and the packager...
".build-venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller
if errorlevel 1 goto failed

rem Windows will not replace an exe that is running, so check before the slow part.
if exist "dist\PC Cleanup.exe" (
  2>nul (>>"dist\PC Cleanup.exe" (call )) || (
    echo PC Cleanup.exe is open. Close it, then run build.bat again.
    goto failed
  )
)

echo Building PC Cleanup.exe. This takes a minute or two...
".build-venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --log-level WARN "PC Cleanup.spec"
if errorlevel 1 goto failed
if not exist "dist\PC Cleanup.exe" goto failed

echo.
echo Done: %~dp0dist\PC Cleanup.exe
echo Copy that one file to any PC. It needs nothing else installed.
pause
exit /b 0

:find_python
rem A bare "python" can be the Microsoft Store stub, which prints nothing and only
rem opens the Store, so each candidate has to answer before it counts.
set "PYEXE="
for %%L in ("python" "py -3") do (
  if not defined PYEXE (
    for /f "delims=" %%I in ('%%~L -c "import sys; print(sys.executable) if sys.version_info >= (3, 10) else None" 2^>nul') do set "PYEXE=%%I"
  )
)
rem Just after get-python.ps1 runs, the new Python is not on this window's PATH yet.
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
exit /b 0

:failed
echo.
echo The build did not finish. The messages above say why.
pause
exit /b 1

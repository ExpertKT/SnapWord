@echo off
rem SnapWord launcher.
rem Keep this file PURE ASCII: cmd.exe reads .cmd bytes in the OEM codepage (936/GBK here),
rem so any UTF-8 Chinese in here turns into garbage and gets executed as commands.
rem Mount src into the venv, then start the GUI with pythonw (no console window).
setlocal
set HERE=%~dp0
if "%HERE:~-1%"=="\" set HERE=%HERE:~0,-1%
set VENV=%HERE%\.venv
set PY=%VENV%\Scripts\pythonw.exe
if not exist "%PY%" (
  echo [SnapWord] not found: %PY%
  echo Create the venv first:  python -m venv .venv
  pause
  exit /b 1
)
echo %HERE%\src> "%VENV%\Lib\site-packages\snapword.pth"
start "" "%PY%" -m snapword.gui
endlocal

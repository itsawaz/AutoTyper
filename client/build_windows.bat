@echo off
REM Build the standalone Windows executable for AutoTyper.
REM Run from the client\ directory in a Developer/Command Prompt.

setlocal
cd /d "%~dp0"

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

echo ==^> Installing build deps
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

echo ==^> Cleaning previous build
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo ==^> Building AutoTyper.exe
"%PY%" -m PyInstaller autotyper.spec --noconfirm
if errorlevel 1 goto :error

echo ==^> Done. Executable is at: dist\AutoTyper\AutoTyper.exe
goto :eof

:error
echo Build failed.
exit /b 1

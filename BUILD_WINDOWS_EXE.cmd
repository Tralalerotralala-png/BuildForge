@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
where py.exe >nul 2>&1
if errorlevel 1 goto :python
set "PY=py -3"
goto :build
:python
set "PY=python"
:build
%PY% -m PyInstaller --version
if errorlevel 1 (
 echo PyInstaller is not installed. Install it in your selected Python environment first.
 pause
 exit /b 2
)
%PY% -m PyInstaller --noconfirm --clean --onedir --console --name BuildForge --hidden-import engine --hidden-import resource_control app.py
if errorlevel 1 (
 echo Packaging failed.
 pause
 exit /b 1
)
echo Build created in dist\BuildForge. Test on Windows before distributing.
pause

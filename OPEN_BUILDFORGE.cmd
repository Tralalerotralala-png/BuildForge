@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
where py.exe >nul 2>&1
if not errorlevel 1 (
  py -3 "%~dp0app.py"
  goto :done
)
where python.exe >nul 2>&1
if not errorlevel 1 (
  python "%~dp0app.py"
  goto :done
)
echo Python 3.9+ with Tkinter is required for the source edition.
pause
exit /b 2
:done
if errorlevel 1 pause

@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

set "PY="
where py.exe >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import tkinter" >nul 2>&1
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  where python.exe >nul 2>&1
  if not errorlevel 1 (
    python -c "import tkinter" >nul 2>&1
    if not errorlevel 1 set "PY=python"
  )
)
if not defined PY (
  where pythonw.exe >nul 2>&1
  if not errorlevel 1 (
    pythonw -c "import tkinter" >nul 2>&1
    if not errorlevel 1 set "PY=pythonw"
  )
)
if not defined PY (
  echo Python 3.9+ with Tkinter is required for BuildForge.
  pause
  exit /b 2
)

%PY% "%~dp0app.py"
if errorlevel 1 (
  echo BuildForge exited with an error.
  pause
  exit /b 1
)

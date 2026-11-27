@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
set "BUILD_FORGE_LAUNCHER="
py -3 -I -B -c "import sys; sys.exit(sys.version_info < (3,9))" >nul 2>&1
if not errorlevel 1 set "BUILD_FORGE_LAUNCHER=py"
if not defined BUILD_FORGE_LAUNCHER (
    python -I -B -c "import sys; sys.exit(sys.version_info < (3,9))" >nul 2>&1
    if not errorlevel 1 set "BUILD_FORGE_LAUNCHER=python"
)
if not defined BUILD_FORGE_LAUNCHER (
    echo Python 3.9 or later is required. Use an existing Python installation.
    popd
    pause
    exit /b 2
)
if "%BUILD_FORGE_LAUNCHER%"=="py" (
    py -3 -I -B "%~dp0validate.py"
) else (
    python -I -B "%~dp0validate.py"
)
set "BUILD_FORGE_RESULT=%ERRORLEVEL%"
popd
pause
exit /b %BUILD_FORGE_RESULT%

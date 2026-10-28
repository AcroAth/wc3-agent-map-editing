@echo off
rem ---------------------------------------------------------------------------
rem WC3 Tester MCP launcher.
rem IMPORTANT: stdout is the MCP stdio channel. Nothing except the server may
rem write to stdout. All bootstrap chatter goes to stderr (1>&2).
rem The venv lives in %LOCALAPPDATA%\wc3-tester\venv (override: WC3_TESTER_VENV)
rem because plugin cache paths are deep enough to break pip on MAX_PATH systems.
rem ---------------------------------------------------------------------------
setlocal enableextensions
set "HERE=%~dp0"
pushd "%HERE%.." || exit /b 1
set "PLUGIN_ROOT=%CD%"

if not defined WC3_TESTER_VENV set "WC3_TESTER_VENV=%LOCALAPPDATA%\wc3-tester\venv"
set "VENV=%WC3_TESTER_VENV%"
set "VPY=%VENV%\Scripts\python.exe"
rem Marker is written only after a successful install, so a failed install retries.
set "MARKER=%VENV%\.wc3-tester-deps-v2"

if not exist "%VPY%" (
    echo [wc3-tester] Creating virtual environment in "%VENV%" ... 1>&2
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv "%VENV%" 1>&2
    ) else (
        python -m venv "%VENV%" 1>&2
    )
    if not exist "%VPY%" (
        echo [wc3-tester] ERROR: failed to create virtual environment. 1>&2
        popd
        exit /b 1
    )
)
if not exist "%MARKER%" (
    echo [wc3-tester] Installing requirements ... 1>&2
    "%VPY%" -m pip install --disable-pip-version-check --quiet -r "%PLUGIN_ROOT%\requirements.txt" 1>&2
    if errorlevel 1 (
        echo [wc3-tester] ERROR: pip install failed. 1>&2
        popd
        exit /b 1
    )
    echo ok> "%MARKER%"
)

set "PYTHONPATH=%PLUGIN_ROOT%\src"
set "PYTHONUNBUFFERED=1"
"%VPY%" -m wc3_tester.server
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%


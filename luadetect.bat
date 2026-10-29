@echo off
setlocal
rem ASCII only - see install.py for why (cmd.exe re-reads batch files by byte offset).
rem Put this folder on PATH and "luadetect <args>" works from any project directory.
rem setlocal keeps PY out of the caller's environment.

set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"

if not defined PY (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)

if not defined PY (
    echo [x] Python not found. Install Python 3.9+ and tick "Add Python to PATH".
    exit /b 2
)

%PY% "%~dp0run_cli.py" %*
exit /b %ERRORLEVEL%

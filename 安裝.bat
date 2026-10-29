@echo off
setlocal
rem ASCII only on purpose - see install.py for why.
rem cmd.exe re-reads this file by byte offset while running it, so non-ASCII
rem text (or a mid-file chcp) desynchronises the parser and turns echo lines
rem into commands. All messages are printed from Python instead.

cd /d "%~dp0"

set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"

if not defined PY (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)

if not defined PY (
    echo.
    echo   [x] Python not found. Install Python 3.9+ and tick
    echo       "Add Python to PATH", or install the py launcher.
    echo.
    pause
    exit /b 1
)

rem No args -> the settings menu (double-clicked). Any args are passed
rem straight through and we do NOT pause: pause blocks forever when stdin
rem is not a console, so running this file with args from a script would hang.
rem
rem Do NOT capture %ERRORLEVEL% inside an if/else block: cmd.exe expands it when
rem it parses the whole block, i.e. before anything in the block has run, so it
rem would hold the errorlevel from BEFORE the block (verified: an inner command
rem exiting 3 left RC=7 and the file exited 7). Call python outside any block and
rem let its errorlevel stand.
if not "%~1"=="" goto :withargs

%PY% "%~dp0install.py" --menu
exit /b %ERRORLEVEL%

:withargs
%PY% "%~dp0install.py" %*
exit /b %ERRORLEVEL%

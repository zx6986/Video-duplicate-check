@echo off
rem ============================================================
rem  Launcher with a visible log window (for troubleshooting)
rem  ASCII only, CRLF. Keep this window open to read errors.
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"
set "SCRIPT=%~dp0duplicate_finder.py"
set "PY="

rem 1) python.exe on PATH
for %%I in (python.exe) do set "PY=%%~$PATH:I"
if defined PY goto launch

rem 2) py launcher
for %%I in (py.exe) do set "PY=%%~$PATH:I"
if defined PY goto launch

rem 3) per-user Python installs
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
if defined PY goto launch

rem 4) known machine-wide install
if exist "D:\APP\Python\Python313\python.exe" set "PY=D:\APP\Python\Python313\python.exe"
if defined PY goto launch

rem 5) last resort: the runtime bundled with the DeepSeek Harness
if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if defined PY goto launch

echo Python was not found. Install Python 3.8+ with "Add Python to PATH".
pause
exit /b 1

:launch
echo Using Python: %PY%
echo Script      : %SCRIPT%
echo.
"%PY%" "%SCRIPT%"
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%
pause
exit /b %RC%

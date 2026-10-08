@echo off
rem ============================================================
rem  Launcher for duplicate_finder.py  (ASCII only, CRLF)
rem  Double click me. No console window is kept open.
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"
set "SCRIPT=%~dp0duplicate_finder.py"
set "PYW="

rem 1) pythonw.exe on PATH
for %%I in (pythonw.exe) do set "PYW=%%~$PATH:I"
if defined PYW goto launch

rem 2) py launcher (pyw.exe)
for %%I in (pyw.exe) do set "PYW=%%~$PATH:I"
if defined PYW goto launch

rem 3) per-user Python installs
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%~fD\pythonw.exe" set "PYW=%%~fD\pythonw.exe"
if defined PYW goto launch

rem 4) known machine-wide install
if exist "D:\APP\Python\Python313\pythonw.exe" set "PYW=D:\APP\Python\Python313\pythonw.exe"
if defined PYW goto launch

rem 5) last resort: the runtime bundled with the DeepSeek Harness
if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PYW=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if defined PYW goto launch

goto nopython

:launch
start "" "%PYW%" "%SCRIPT%"
exit /b 0

:nopython
echo.
echo   Python was not found on this computer.
echo   Install Python 3.8 or newer and tick "Add Python to PATH",
echo   then run this file again.
echo.
echo   (Or open "launch_with_log.bat" to see the detailed error.)
echo.
pause
exit /b 1

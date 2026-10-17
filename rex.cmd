@echo off
rem rextools entry point for Windows (PowerShell, cmd): runs py\rex.py with
rem Python 3.11+. Put this folder on PATH and type `rex help`.
setlocal
set "REX_MAIN=%~dp0py\rex.py"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -I -B "%REX_MAIN%" %*
) else (
  python -I -B "%REX_MAIN%" %*
)
exit /b %errorlevel%

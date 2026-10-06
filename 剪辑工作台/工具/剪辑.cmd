@echo off
rem ============================================================
rem  Xu Gongzi video editor - one click launcher (Windows)
rem  Double click  = run (intake + advance)
rem  With args     = 剪辑.cmd status | deliver | doctor | inputs
rem
rem  NOTE: this folder must contain exactly ONE .py file
rem        (the main script). The launcher finds it by wildcard
rem        so that no non-ASCII literal is written in this file.
rem ============================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
set "HERE=%~dp0"

set "ENTRY="
for %%F in ("%HERE%*.py") do if not defined ENTRY set "ENTRY=%%~fF"
if not defined ENTRY (
  echo [!] Cannot find the main script next to this launcher.
  echo     Expected exactly one .py file in:
  echo         %HERE%
  pause
  exit /b 1
)

set "PYEXE="
py -3 -c "import sys" >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  python -c "import sys" >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo [!] No usable Python 3 found.
  echo     Install Python 3.11+ and make sure it is on PATH.
  pause
  exit /b 1
)

%PYEXE% -X utf8 "%ENTRY%" %*
set "RC=%ERRORLEVEL%"

echo.
echo ---------------------------------------------------------------
if "%RC%"=="0" (echo   Finished.) else (echo   Stopped with code %RC%.)
echo ---------------------------------------------------------------
pause
exit /b %RC%

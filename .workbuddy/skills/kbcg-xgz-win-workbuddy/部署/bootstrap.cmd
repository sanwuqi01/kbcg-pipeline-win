@echo off
rem ===================================================================
rem  kbcg-xgz-win-workbuddy  ·  bootstrap launcher
rem  NOTE: this file is intentionally ASCII-only, so cmd.exe never
rem        garbles it. All Chinese output comes from bootstrap.py.
rem ===================================================================
setlocal
chcp 65001 >nul 2>nul

set "PYEXE="

rem Prefer the Python Launcher (py -3): it is reliable and never hits the
rem Microsoft Store "app execution alias" stub that bare `python` can hit.
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)

if not defined PYEXE (
  echo.
  echo [ERROR] No Python found in PATH.
  echo         Install Python 3.11+ from https://www.python.org/downloads/
  echo         ^(tick "Add python.exe to PATH" during setup^), then rerun this file.
  echo         Or run it manually with an explicit interpreter:
  echo             python "%~dp0bootstrap.py" --python "C:\path\to\python.exe"
  echo.
  echo         See 部署\安装说明.md  section 4.1
  echo.
  pause
  exit /b 1
)

rem Version gate: bootstrap needs 3.11+.
%PYEXE% -c "import sys;raise SystemExit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Found a Python, but it is older than 3.11.
  echo         Install Python 3.11+ and rerun.
  echo.
  pause
  exit /b 1
)

%PYEXE% "%~dp0bootstrap.py" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo [bootstrap exited with code %RC%]
  pause
)

endlocal & exit /b %RC%

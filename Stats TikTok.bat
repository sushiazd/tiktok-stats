@echo off
rem Double-clic : met a jour l'outil, puis ouvre la page des stats TikTok.
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Il faut Python 3 ^(gratuit^). Installe-le depuis la page qui s'ouvre, en cochant "Add python.exe to PATH", puis relance.
  start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)
%PY% lanceur.py %*
pause

@echo off
REM Abre o sistema de prospeccao no navegador (Windows)
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -m prospector servidor
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python -m prospector servidor
  ) else (
    echo Python nao encontrado. Instale em https://www.python.org/downloads/ marcando "Add python.exe to PATH".
  )
)
pause

@echo off
REM Abre o sistema de prospeccao no navegador (Windows)
cd /d "%~dp0"
where py >nul 2>nul && goto usar_py
where python >nul 2>nul && goto usar_python
echo Python nao encontrado. Instale em https://www.python.org/downloads/ marcando "Add python.exe to PATH".
goto fim
:usar_py
py -m prospector servidor
goto fim
:usar_python
python -m prospector servidor
:fim
pause

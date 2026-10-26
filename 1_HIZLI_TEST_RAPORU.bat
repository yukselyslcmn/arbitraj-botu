@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ===============================================================================
echo   E-TICARET ARBITRAJ RADARI - HIZLI KONSOL TESTI (TELEGRAM BILDIRIMSIZ)
echo ===============================================================================
echo.
.\venv\Scripts\python.exe test_run.py
echo.
pause

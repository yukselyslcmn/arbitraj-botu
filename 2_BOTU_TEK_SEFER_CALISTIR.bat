@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ===============================================================================
echo   E-TICARET ARBITRAJ RADARI - TEK SEFERLIK TAM TARAMA (--once)
echo ===============================================================================
echo.
.\venv\Scripts\python.exe main.py --once
echo.
pause

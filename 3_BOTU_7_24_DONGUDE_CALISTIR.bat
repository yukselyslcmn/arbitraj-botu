@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ===============================================================================
echo   E-TICARET ARBITRAJ RADARI - 7/24 KESINTISIZ DONGU MODU (--loop)
echo   Durdurmak icin CTRL+C tuslarina basabilirsiniz.
echo ===============================================================================
echo.
.\venv\Scripts\python.exe main.py --loop
echo.
pause

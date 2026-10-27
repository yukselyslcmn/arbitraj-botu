@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================================
echo   ARBITRAJ WEB KONTROL PANELI (http://127.0.0.1:8080) ACILIYOR...
echo ============================================================================
.\venv\Scripts\python.exe dashboard.py
pause

@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================================
echo   TELEGRAM INTERAKTIF KOMUT BOTU (/durum, /firsatlar, /ara, /filtre, /takip)
echo ============================================================================
.\venv\Scripts\python.exe main.py --listen-commands
pause

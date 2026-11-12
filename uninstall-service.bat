@echo off
REM ===== ArtechCMS - go dich vu Windows Service =====
REM Chuot phai -> "Run as administrator"
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
  echo [!] Can chay bang quyen Administrator.
  pause
  exit /b 1
)

if exist ".venv\Scripts\activate.bat" call .venv\Scripts\activate.bat

echo [*] Dung va go dich vu ArtechCMS ...
python service.py stop
python service.py remove
echo [*] Da go dich vu. Du lieu trong data\ va recordings\ van duoc giu.
pause

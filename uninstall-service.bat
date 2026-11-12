@echo off
REM ===== ArtechCMS - go chay nen tu dong =====
REM Chuot phai -> "Run as administrator"
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
  echo [!] Can chay bang quyen Administrator.
  pause
  exit /b 1
)

echo [*] Dung va go tac vu ArtechCMS ...
schtasks /End /TN ArtechCMS >nul 2>&1
schtasks /Delete /TN ArtechCMS /F >nul 2>&1
REM don luon dich vu pywin32 cu neu con
sc stop ArtechCMS >nul 2>&1
if exist ".venv\Scripts\activate.bat" ( call .venv\Scripts\activate.bat & python service.py remove >nul 2>&1 )
sc delete ArtechCMS >nul 2>&1
REM dung tien trinh python con dang chay (neu con)
taskkill /F /IM pythonw.exe >nul 2>&1
echo [*] Da go. Du lieu trong data\ va recordings\ van duoc giu.
pause

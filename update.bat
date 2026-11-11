@echo off
REM ===== ArtechCMS - cap nhat thu cong tu GitHub =====
cd /d "%~dp0"
where git >nul 2>&1
if errorlevel 1 (
  echo [!] Chua cai Git. Tai tu https://git-scm.com/download/win
  pause & exit /b 1
)
if not exist ".git" (
  echo [!] Thu muc chua phai git repo. Lan dau chay:
  echo     git clone https://github.com/virutkod97/ArtechCMS
  pause & exit /b 1
)
echo [*] Keo ban moi nhat tu GitHub ...
git pull --ff-only
echo [*] Xong. Chay lai deploy_windows.bat de khoi dong.
pause

@echo off
REM ===== ArtechCMS - cai dat chay nen tu dong (1-click) =====
REM Chuot phai -> "Run as administrator"
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
  echo [!] Can chay bang quyen Administrator.
  echo     Chuot phai vao install-service.bat -^> Run as administrator
  pause
  exit /b 1
)

echo ============================================
echo   ArtechCMS - cai dat chay nen tu dong
echo ============================================

REM 1) Python launcher
set "PYEXE="
where py >nul 2>&1 && set "PYEXE=py"
if not defined PYEXE ( where python >nul 2>&1 && set "PYEXE=python" )
if not defined PYEXE (
  echo [!] Khong tim thay Python. Cai Python 3.9+ tu https://www.python.org/downloads/
  pause
  exit /b 1
)

REM 2) venv + thu vien
if not exist ".venv" (
  echo [*] Tao moi truong ao .venv ...
  %PYEXE% -m venv .venv
)
call .venv\Scripts\activate.bat
echo [*] Cai thu vien ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt

REM 3) don dep dich vu pywin32 cu (neu da cai ban truoc)
echo [*] Don dep ban cu (neu co) ...
sc stop ArtechCMS >nul 2>&1
python service.py remove >nul 2>&1
sc delete ArtechCMS >nul 2>&1
schtasks /Delete /TN ArtechCMS /F >nul 2>&1

REM 4) tao tac vu chay luc khoi dong (tai khoan SYSTEM, chay an) + chay ngay
echo [*] Tao tac vu tu dong chay ...
schtasks /Create /TN ArtechCMS /TR "\"%CD%\run-background.bat\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 (
  echo [!] Tao tac vu that bai. Hay chac chan dang chay bang quyen Administrator.
  pause
  exit /b 1
)
schtasks /Run /TN ArtechCMS

echo.
echo [*] Xong. ArtechCMS dang chay nen va tu bat khi khoi dong Windows.
echo     Mo trinh duyet: http://localhost:8787  (doi vai giay cho lan dau)
echo     Quan ly: Task Scheduler -^> ten "ArtechCMS"
echo     Go cai: uninstall-service.bat
pause

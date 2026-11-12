@echo off
REM ===== ArtechCMS - cai dat chay nen dang Windows Service (1-click) =====
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
echo   ArtechCMS - cai dat Windows Service
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

REM 2) venv + thu vien (bao gom pywin32)
if not exist ".venv" (
  echo [*] Tao moi truong ao .venv ...
  %PYEXE% -m venv .venv
)
call .venv\Scripts\activate.bat
echo [*] Cai thu vien ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt pywin32
REM dang ky DLL cua pywin32 (can cho service)
python .venv\Scripts\pywin32_postinstall.py -install >nul 2>&1

REM 3) cai + chay service (tu khoi dong cung Windows)
echo [*] Cai dat dich vu ...
python service.py --startup auto install
python service.py restart

echo.
echo [*] Xong. Dich vu "ArtechCMS" dang chay nen va tu bat khi khoi dong Windows.
echo     Mo trinh duyet: http://localhost:8787
echo     Quan ly: services.msc  (ten: ArtechCMS)
echo     Go cai: uninstall-service.bat
pause

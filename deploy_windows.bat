@echo off
REM ===== ArtechCMS - 1-click deploy for Windows =====
REM Installs Python venv + deps and starts the web app with auto-restart (for online update).
setlocal
cd /d "%~dp0"

echo ============================================
echo   ArtechCMS - cai dat va khoi dong
echo ============================================

REM 1) Find Python launcher (prefer "py", fallback "python")
set "PYEXE="
where py >nul 2>&1 && set "PYEXE=py"
if not defined PYEXE ( where python >nul 2>&1 && set "PYEXE=python" )
if not defined PYEXE (
  echo [!] Khong tim thay Python. Cai Python 3.9+ tu https://www.python.org/downloads/
  echo     Nho tick "Add python.exe to PATH" khi cai.
  pause
  exit /b 1
)
echo [*] Dung Python: %PYEXE%

REM 2) Create venv
if not exist ".venv" (
  echo [*] Tao moi truong ao .venv ...
  %PYEXE% -m venv .venv
)
call .venv\Scripts\activate.bat

REM 3) Install deps (python/pip now point to the venv)
echo [*] Cai thu vien ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt

REM 4) Run with auto-restart loop (exit code 3 = update/restart request)
echo [*] Khoi dong ArtechCMS ...
echo     Mo trinh duyet: http://localhost:8787
:runloop
python app.py
if "%ERRORLEVEL%"=="3" (
  echo [*] Cap nhat xong - khoi dong lai ...
  goto runloop
)
echo [*] ArtechCMS da dung (ma loi %ERRORLEVEL%).
pause

@echo off
REM Launcher chay nen cho ArtechCMS (duoc Task Scheduler goi luc khoi dong).
REM Tu chay lai khi app thoat voi ma 3 (sau khi Cap nhat).
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call .venv\Scripts\activate.bat
:loop
if exist ".venv\Scripts\pythonw.exe" (
  ".venv\Scripts\pythonw.exe" app.py
) else (
  pythonw app.py
)
if "%ERRORLEVEL%"=="3" goto loop

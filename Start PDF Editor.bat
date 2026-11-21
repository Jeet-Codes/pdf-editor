@echo off
title PDF Editor
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Install it from https://www.python.org/downloads/ and try again.
  pause
  exit /b 1
)
python -c "import fitz, flask" >nul 2>nul
if errorlevel 1 (
  echo First run: installing what the editor needs...
  python -m pip install -r requirements.txt
)
python app.py
pause

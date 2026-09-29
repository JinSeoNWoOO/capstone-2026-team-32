@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv-new\Scripts\python.exe" (
  echo Python environment missing. See README.md.
  exit /b 1
)
".venv-new\Scripts\python.exe" -m uvicorn backend.app.main:app --reload --reload-dir backend

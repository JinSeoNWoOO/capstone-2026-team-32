@echo off
setlocal
cd /d "%~dp0frontend"
if not exist "node_modules\vite\bin\vite.js" (
  echo Frontend dependencies missing. Run npm ci in frontend.
  exit /b 1
)
where node >nul 2>nul
if not errorlevel 1 (
  node node_modules\vite\bin\vite.js --host 127.0.0.1 --port 5173 --strictPort
  exit /b
)
set "TRADER_NODE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
if not exist "%TRADER_NODE%" (
  echo Node.js missing. Install Node.js and reopen the terminal.
  exit /b 1
)
"%TRADER_NODE%" node_modules\vite\bin\vite.js --host 127.0.0.1 --port 5173 --strictPort

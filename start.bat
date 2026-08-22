@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=C:\Users\Administrator\Desktop\turb-gpt-free-register\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo Python runtime not found: %PYTHON%
  pause
  exit /b 1
)
netstat -ano | findstr ":5091" | findstr "LISTENING" >nul
if errorlevel 1 start "Email Rebind Console" /min "%PYTHON%" "%~dp0app.py"
ping 127.0.0.1 -n 3 >nul
start "" "http://127.0.0.1:5091/"
endlocal
exit /b 0

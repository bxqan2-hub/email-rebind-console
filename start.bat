@echo off
setlocal
cd /d "%~dp0"
set "EMAIL_REBIND_PORT=5092"
set "PYTHONW=C:\Users\Administrator\Desktop\turb-gpt-free-register\.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" (
  echo Python windowless runtime not found: %PYTHONW%
  pause
  exit /b 1
)
netstat -ano | findstr ":5092" | findstr "LISTENING" >nul
if errorlevel 1 start "" "%PYTHONW%" "%~dp0app.py"
ping 127.0.0.1 -n 3 >nul
start "" "http://127.0.0.1:5092/"
endlocal
exit /b 0

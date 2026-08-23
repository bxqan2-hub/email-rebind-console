@echo off
setlocal
cd /d "%~dp0"
set "EMAIL_REBIND_PORT=5092"
set "APP_URL=http://127.0.0.1:5092/"
set "HEALTH_URL=http://127.0.0.1:5092/health"
set "PYTHONW=C:\Users\Administrator\Desktop\turb-gpt-free-register\.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" (
  echo Python windowless runtime not found: %PYTHONW%
  pause
  exit /b 1
)
call :healthcheck
if not errorlevel 1 goto :open

start "" "%PYTHONW%" "%~dp0app.py"
for /l %%i in (1,1,30) do (
  call :healthcheck
  if not errorlevel 1 goto :open
  powershell.exe -NoProfile -Command "Start-Sleep -Seconds 1" >nul 2>&1
)
echo Email rebind console did not become ready at %HEALTH_URL%.
pause
endlocal
exit /b 1

:open
start "" "%APP_URL%"
endlocal
exit /b 0

:healthcheck
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri '%HEALTH_URL%' -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
exit /b %errorlevel%

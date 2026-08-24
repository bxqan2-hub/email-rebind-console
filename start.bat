@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "EMAIL_REBIND_PORT=5092"
set "EMAIL_REBIND_GCASH_PORT=8931"
rem Use a fixed clean loopback address; 127.0.0.1 may retain a dead 5092 socket after a forced stop.
set "EMAIL_REBIND_HOST=127.0.0.3"
set "APP_HOST=127.0.0.3"
set "APP_URL=http://%APP_HOST%:%EMAIL_REBIND_PORT%/"
set "HEALTH_URL=http://%APP_HOST%:%EMAIL_REBIND_PORT%/health"
set "GCASH_HEALTH_URL=http://%APP_HOST%:%EMAIL_REBIND_GCASH_PORT%/api/health"
set "PYTHONW=C:\Users\Administrator\Desktop\turb-gpt-free-register\.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" (
  echo Python windowless runtime not found: %PYTHONW%
  pause
  exit /b 1
)

echo Stopping existing Email Rebind Console listeners on %APP_HOST%:%EMAIL_REBIND_PORT%...
call :stop_existing
if errorlevel 1 (
  echo Existing listener could not be stopped; see the port owner above.
  pause
  endlocal
  exit /b 1
)

echo Starting Email Rebind Console on %APP_URL% ...
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
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try { $main = Invoke-WebRequest -UseBasicParsing -Uri '%HEALTH_URL%' -TimeoutSec 2; $gcash = Invoke-WebRequest -UseBasicParsing -Uri '%GCASH_HEALTH_URL%' -TimeoutSec 2; if (($main.StatusCode -eq 200) -and ($gcash.StatusCode -eq 200)) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
exit /b %errorlevel%

:stop_existing
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $ports=@(%EMAIL_REBIND_PORT%,%EMAIL_REBIND_GCASH_PORT%); $root=[IO.Path]::GetFullPath('%~dp0'); $ids=@(Get-NetTCPConnection -LocalPort $ports -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); $ids += @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { ($_.Name -in @('python.exe','pythonw.exe')) -and ($_.CommandLine -like ('*' + $root + 'app.py*')) } | Select-Object -ExpandProperty ProcessId); foreach($id in ($ids | Where-Object { $_ -and ($_ -ne 0) } | Sort-Object -Unique)) { & taskkill.exe /PID ([int]$id) /T /F *> $null; Stop-Process -Id ([int]$id) -Force -ErrorAction SilentlyContinue }; $deadline=(Get-Date).AddSeconds(10); do { $entries=@(Get-NetTCPConnection -State Listen -LocalPort $ports -ErrorAction SilentlyContinue); $live=@($entries | Where-Object { Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue }); if($live.Count -eq 0) { exit 0 }; foreach($entry in $live) { & taskkill.exe /PID ([int]$entry.OwningProcess) /T /F *> $null; Stop-Process -Id $entry.OwningProcess -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 250 } while((Get-Date) -lt $deadline); $remaining=@(Get-NetTCPConnection -State Listen -LocalPort $ports -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,OwningProcess); if($remaining.Count -gt 0) { $remaining | ConvertTo-Json -Compress; exit 1 }; exit 0"
exit /b %errorlevel%

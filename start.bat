@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=C:\Users\Administrator\Desktop\turb-gpt-free-register\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=py"
start "邮箱换绑分站" /min "%PYTHON%" app.py
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:5091/


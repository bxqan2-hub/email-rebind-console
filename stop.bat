@echo off
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5092" ^| findstr "LISTENING"') do taskkill /PID %%p /F >nul 2>nul
echo 邮箱换绑分站已停止。

@echo off
for %%r in (5092 8931) do (
  for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%%r" ^| findstr "LISTENING"') do taskkill /PID %%p /T /F >nul 2>nul
)
echo 邮箱换绑分站和 GCash 提链服务已停止。

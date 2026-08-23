@echo off
setlocal
cd /d "%~dp0"

git config --local core.hooksPath .githooks
if errorlevel 1 exit /b %errorlevel%

for /f "delims=" %%R in ('git remote get-url rebind-origin') do set "ACTUAL_REMOTE=%%R"
if not "%ACTUAL_REMOTE%"=="https://github.com/bxqan2-hub/email-rebind-console.git" (
  echo HOOK_SETUP_ERROR=unexpected rebind-origin: %ACTUAL_REMOTE%
  exit /b 2
)

echo HOOK_SETUP_RESULT=success
echo AUTO_PUSH_TARGET=rebind-origin/codex/email-rebind-console
exit /b 0

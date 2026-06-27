@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0"

set "MINICODEX2_MODEL_PROFILE=deepseek_flash"
set "MINICODEX2_SKIP_LOCAL=1"

if not "%~1"=="" (
  set "MINICODEX2_MODEL_PROFILE=%~1"
)

if exist "%~dp0run_tui_deepseek.local.bat" (
  call "%~dp0run_tui_deepseek.local.bat"
)

if "%DEEPSEEK_API_KEY%"=="" (
  echo DEEPSEEK_API_KEY is not set.
  set /p DEEPSEEK_API_KEY=Enter DeepSeek API key:
)

if "%DEEPSEEK_API_KEY%"=="" (
  echo DeepSeek API key is required.
  exit /b 1
)

call "%~dp0run_tui.bat"

endlocal

@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0"

set "MINICODEX2_MODEL_PROFILE=aihub2apiOpenAI"
set "MINICODEX2_SKIP_LOCAL=1"

if not "%~1"=="" (
  set "MINICODEX2_MODEL_PROFILE=%~1"
)

if exist "%~dp0run_tui_aihub2api_openai.local.bat" (
  call "%~dp0run_tui_aihub2api_openai.local.bat"
)

if "%aihub2apiOpenAI_API_KEY%"=="" (
  echo aihub2apiOpenAI_API_KEY is not set.
  set /p aihub2apiOpenAI_API_KEY=Enter aihub2apiOpenAI API key:
)

if "%aihub2apiOpenAI_API_KEY%"=="" (
  echo aihub2apiOpenAI API key is required.
  exit /b 1
)

call "%~dp0run_tui.bat"

endlocal

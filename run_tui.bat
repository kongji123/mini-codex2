@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0"

if "%MINICODEX2_WORKSPACE%"=="" set "MINICODEX2_WORKSPACE=%~dp0playground"

if not "%MINICODEX2_SKIP_LOCAL%"=="1" if exist "%~dp0run_tui.local.bat" (
  call "%~dp0run_tui.local.bat"
)

if "%MINICODEX2_MODEL_PROFILE%"=="" (
  if "%MINICODEX2_BASE_URL%"=="" set "MINICODEX2_BASE_URL=https://api.openai.com/v1"
  if "%MINICODEX2_MODEL%"=="" set "MINICODEX2_MODEL=gpt-4.1-mini"
  if not "%~1"=="" (
    set "MINICODEX2_MODEL=%~1"
  )
  if "%OPENAI_API_KEY%"=="" (
    echo OPENAI_API_KEY is not set.
    set /p OPENAI_API_KEY=Enter OpenAI-compatible API key:
  )
  if "%OPENAI_API_KEY%"=="" (
    echo API key is required to run MiniCodex2 with a real model.
    exit /b 1
  )
) else (
  if not "%~1"=="" (
    set "MINICODEX2_MODEL_PROFILE=%~1"
  )
)

if not exist ".venv\Scripts\minicodex2.exe" (
  echo .venv is missing. Installing project dependencies...
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -e ".[dev,api,cli,tui]"
)

if not exist "%MINICODEX2_WORKSPACE%" (
  mkdir "%MINICODEX2_WORKSPACE%"
)

if not "%MINICODEX2_MODEL_PROFILE%"=="" (
  echo MiniCodex2 model profile: %MINICODEX2_MODEL_PROFILE%
  .\.venv\Scripts\python.exe -m minicodex2.cli.main tui ^
    --workspace "%MINICODEX2_WORKSPACE%" ^
    --model-profile "%MINICODEX2_MODEL_PROFILE%"
) else (
  echo MiniCodex2 model: %MINICODEX2_MODEL%
  echo MiniCodex2 base URL: %MINICODEX2_BASE_URL%
  .\.venv\Scripts\python.exe -m minicodex2.cli.main tui ^
    --workspace "%MINICODEX2_WORKSPACE%" ^
    --base-url "%MINICODEX2_BASE_URL%" ^
    --model "%MINICODEX2_MODEL%" ^
    --api-key "%OPENAI_API_KEY%"
)

endlocal

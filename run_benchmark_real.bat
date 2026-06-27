@echo off
setlocal

cd /d "%~dp0"

set "MINICODEX2_BASE_URL=https://api.openai.com/v1"
set "MINICODEX2_MODEL=gpt-4.1-mini"
set "MINICODEX2_BENCHMARK_SUITE=smoke"
set "MINICODEX2_BENCHMARK_OUTPUT="

if exist "%~dp0run_tui.local.bat" (
  call "%~dp0run_tui.local.bat"
)

if exist "%~dp0run_benchmark_real.local.bat" (
  call "%~dp0run_benchmark_real.local.bat"
)

if "%OPENAI_API_KEY%"=="" (
  echo OPENAI_API_KEY is not set.
  set /p OPENAI_API_KEY=Enter OpenAI-compatible API key:
)

if "%OPENAI_API_KEY%"=="" (
  echo API key is required to run the real-model benchmark.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo .venv is missing. Installing project dependencies...
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -e ".[dev,api,cli,tui]"
)

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "MINICODEX2_BENCHMARK_RUN_ID=%%i"
if "%MINICODEX2_BENCHMARK_OUTPUT%"=="" (
  set "MINICODEX2_BENCHMARK_OUTPUT=.minicodex2\benchmarks\%MINICODEX2_BENCHMARK_SUITE%-real-%MINICODEX2_BENCHMARK_RUN_ID%"
)

echo Running MiniCodex2 real-model benchmark...
echo Suite: %MINICODEX2_BENCHMARK_SUITE%
echo Model: %MINICODEX2_MODEL%
echo Base URL: %MINICODEX2_BASE_URL%
echo Run ID: %MINICODEX2_BENCHMARK_RUN_ID%
echo Output root: %~dp0%MINICODEX2_BENCHMARK_OUTPUT%
echo Case workspaces: %~dp0%MINICODEX2_BENCHMARK_OUTPUT%\workspaces
echo.
.\.venv\Scripts\python.exe -m minicodex2.cli.main benchmark run ^
  --suite "%MINICODEX2_BENCHMARK_SUITE%" ^
  --model-mode real ^
  --api-key "%OPENAI_API_KEY%" ^
  --base-url "%MINICODEX2_BASE_URL%" ^
  --model "%MINICODEX2_MODEL%" ^
  --output "%MINICODEX2_BENCHMARK_OUTPUT%"

echo.
echo Report:
echo   %~dp0%MINICODEX2_BENCHMARK_OUTPUT%\benchmark-report.md
echo   %~dp0%MINICODEX2_BENCHMARK_OUTPUT%\benchmark-report.json
echo Case workspaces:
echo   %~dp0%MINICODEX2_BENCHMARK_OUTPUT%\workspaces
echo.
pause

endlocal

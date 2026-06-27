@echo off
setlocal

cd /d "%~dp0"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "MINICODEX2_BENCHMARK_RUN_ID=%%i"
set "MINICODEX2_BENCHMARK_OUTPUT=.minicodex2\benchmarks\extended-fake-%MINICODEX2_BENCHMARK_RUN_ID%"

if not exist ".venv\Scripts\python.exe" (
  echo .venv is missing. Installing project dependencies...
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -e ".[dev,api,cli,tui]"
)

echo Running MiniCodex2 fake-model extended benchmark...
echo Suite: extended
echo Run ID: %MINICODEX2_BENCHMARK_RUN_ID%
echo Output root: %~dp0%MINICODEX2_BENCHMARK_OUTPUT%
echo Case workspaces: %~dp0%MINICODEX2_BENCHMARK_OUTPUT%\workspaces
echo.
.\.venv\Scripts\python.exe -m minicodex2.cli.main benchmark run ^
  --suite extended ^
  --model-mode fake ^
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

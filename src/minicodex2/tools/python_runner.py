from __future__ import annotations

import subprocess
import sys
import time
import os
from dataclasses import dataclass
from pathlib import Path

from minicodex2.tools.command_runner import truncate_output
from minicodex2.tools.results import CommandResult


@dataclass(slots=True)
class PythonScriptResult:
    command_result: CommandResult
    script_path: Path


class PythonScriptRunner:
    def run(
        self,
        *,
        code: str,
        cwd: Path,
        artifact_root: Path,
        timeout_seconds: int,
    ) -> PythonScriptResult:
        script_dir = artifact_root / "logs" / "python_tools"
        script_dir.mkdir(parents=True, exist_ok=True)
        script_path = script_dir / f"tool_{int(time.time() * 1000)}.py"
        script_path.write_text(code, encoding="utf-8")
        python_executable = _python_executable_for_cwd(cwd)
        command = [str(python_executable), str(script_path)]
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(cwd) if not existing_pythonpath else f"{cwd}{os.pathsep}{existing_pythonpath}"
        start = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
            duration = time.monotonic() - start
            result = CommandResult(
                command=" ".join(command),
                cwd=str(cwd),
                exit_code=completed.returncode,
                stdout=truncate_output(completed.stdout),
                stderr=truncate_output(completed.stderr),
                duration_seconds=duration,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            result = CommandResult(
                command=" ".join(command),
                cwd=str(cwd),
                exit_code=None,
                stdout=truncate_output(exc.stdout or ""),
                stderr=truncate_output(exc.stderr or ""),
                timed_out=True,
                duration_seconds=duration,
            )
        return PythonScriptResult(command_result=result, script_path=script_path)


def _python_executable_for_cwd(cwd: Path) -> Path:
    for base in (cwd, *cwd.parents):
        for name in (".venv", "venv"):
            candidate = _venv_python(base / name)
            if candidate.exists():
                return candidate
    return Path(sys.executable)


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"

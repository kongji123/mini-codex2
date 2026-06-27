from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from minicodex2.config.settings import ConfigLoader
from minicodex2.project.detector import ProjectDetector
from minicodex2.tools.command_runner import find_free_port, is_port_open
from minicodex2.tools.runtime_tools import RuntimeTools
from minicodex2.verification.plan import VerificationPlan, VerificationStep
from minicodex2.verification.plan_builder import VerificationPlanBuilder
from minicodex2.verification.runner import VerificationRunner


def test_c_project_compile_and_run_when_gcc_available(tmp_path: Path) -> None:
    if shutil.which("gcc") is None:
        pytest.skip("gcc is not available in this environment")
    (tmp_path / "main.c").write_text(
        '#include <stdio.h>\nint main(){ printf("ok\\n"); return 0; }\n',
        encoding="utf-8",
    )
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, [])
    result = VerificationRunner(settings).run(plan)
    assert result.passed


def test_verification_runner_cleans_background_server(tmp_path: Path) -> None:
    port = find_free_port()
    settings = ConfigLoader().load(tmp_path)
    plan = VerificationPlan(
        steps=[
            VerificationStep(
                name="server",
                step_type="background_server",
                command=f"python -m http.server {port}",
                port=port,
                timeout_seconds=5,
            ),
            VerificationStep(name="smoke", step_type="http_smoke", port=port, timeout_seconds=5),
        ],
        reason="test server cleanup",
    )
    result = VerificationRunner(settings).run(plan)
    assert result.passed
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and is_port_open(port):
        time.sleep(0.1)
    assert not is_port_open(port)


def test_start_background_command_can_restart_occupied_port(tmp_path: Path) -> None:
    port = find_free_port()
    runtime = None
    old = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port)],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not is_port_open(port):
            time.sleep(0.1)
        settings = ConfigLoader().load(tmp_path)
        runtime = RuntimeTools(settings)
        result = runtime.start_background_command(
            f"python -m http.server {port}",
            port=port,
            restart_port=True,
        )
        assert result.ok
        assert is_port_open(port)
    finally:
        if runtime is not None:
            runtime.background.terminate_all()
        if old.poll() is None:
            old.kill()

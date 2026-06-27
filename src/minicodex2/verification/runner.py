from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from minicodex2.config.settings import AppSettings
from minicodex2.tools.command_runner import BackgroundCommandManager, CommandRunner
from minicodex2.tools.results import CommandResult
from minicodex2.verification.plan import VerificationPlan, VerificationStep
from minicodex2.verification.result import VerificationResult, VerificationStepResult

if TYPE_CHECKING:
    from minicodex2.agent.events import AgentEventBus


class VerificationRunner:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.command_runner = CommandRunner()
        self.background = BackgroundCommandManager()
        (self.settings.artifact_root / "build").mkdir(parents=True, exist_ok=True)
        (self.settings.artifact_root / "logs").mkdir(parents=True, exist_ok=True)

    def run(
        self,
        plan: VerificationPlan,
        *,
        event_bus: AgentEventBus | None = None,
        turn_id: str | None = None,
    ) -> VerificationResult:
        try:
            if plan.requires_model_decision:
                return VerificationResult(
                    plan=plan,
                    passed=False,
                    blocked=True,
                    blocked_reason=plan.reason,
                )
            step_results: list[VerificationStepResult] = []
            for step in plan.steps:
                if event_bus:
                    event_bus.emit(
                        "verification_step_started",
                        {
                            "name": step.name,
                            "type": step.step_type,
                            "command": step.command,
                            "cwd": step.cwd,
                        },
                        turn_id,
                    )
                    if step.step_type == "background_server":
                        event_bus.emit(
                            "background_command_started",
                            {
                                "name": step.name,
                                "command": step.command,
                                "cwd": step.cwd,
                                "port": step.port,
                            },
                            turn_id,
                        )
                result = self._run_step(step)
                step_results.append(result)
                if event_bus:
                    payload = {
                        "name": step.name,
                        "type": step.step_type,
                        "ok": result.ok,
                        "command": step.command,
                    }
                    if result.command_result:
                        payload["exit_code"] = result.command_result.exit_code
                        payload["duration_seconds"] = result.command_result.duration_seconds
                        payload["stdout_excerpt"] = result.command_result.stdout[:1000]
                        payload["stderr_excerpt"] = result.command_result.stderr[:1000]
                    if result.failure_summary:
                        payload["failure_summary"] = result.failure_summary[:1000]
                    event_bus.emit("verification_step_finished", payload, turn_id)
                    if step.step_type == "background_server":
                        event_bus.emit(
                            "background_command_ready" if result.ok else "background_command_failed",
                            payload,
                            turn_id,
                        )
                if not result.ok and step.stop_on_failure:
                    return VerificationResult(plan=plan, step_results=step_results, passed=False)
            return VerificationResult(
                plan=plan,
                step_results=step_results,
                passed=all(result.ok for result in step_results),
            )
        finally:
            self.background.terminate_all()

    def _run_step(self, step: VerificationStep) -> VerificationStepResult:
        if step.step_type == "http_smoke":
            return self._run_http_smoke(step)
        if step.step_type == "background_server":
            return self._run_background(step)
        if step.step_type == "static_file_check":
            return self._run_static_file_check(step)
        if step.step_type == "document_check":
            return self._run_document_check(step)
        if not step.command:
            return VerificationStepResult(step=step, ok=False, failure_summary="step has no command")
        command_result = self.command_runner.run(
            step.command,
            self.settings.workspace_root / step.cwd,
            timeout_seconds=step.timeout_seconds
            or self.settings.timeouts.verification_command_seconds,
        )
        return VerificationStepResult(
            step=step,
            command_result=command_result,
            ok=command_result.ok,
            failure_summary=None if command_result.ok else command_result.stderr or command_result.stdout,
        )

    def _run_static_file_check(self, step: VerificationStep) -> VerificationStepResult:
        if not step.command:
            return VerificationStepResult(step=step, ok=False, failure_summary="static check missing path")
        path = (self.settings.workspace_root / step.cwd / step.command).resolve()
        root = self.settings.workspace_root.resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return self._static_result(
                step,
                ok=False,
                stderr=f"static check path escapes workspace: {step.command}",
            )
        if not path.exists() or not path.is_file():
            return self._static_result(
                step,
                ok=False,
                stderr=f"static check file not found: {step.command}",
            )
        text = path.read_text(encoding="utf-8", errors="replace")
        issue = self._script_interactive_issue(path, text)
        if issue:
            return self._static_result(step, ok=False, stderr=issue)
        return self._static_result(step, ok=True, stdout=f"static check passed: {step.command}")

    def _run_document_check(self, step: VerificationStep) -> VerificationStepResult:
        if not step.command:
            return VerificationStepResult(step=step, ok=False, failure_summary="document check missing path")
        path = (self.settings.workspace_root / step.cwd / step.command).resolve()
        root = self.settings.workspace_root.resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return self._static_result(
                step,
                ok=False,
                stderr=f"document check path escapes workspace: {step.command}",
            )
        if not path.exists() or not path.is_file():
            return self._static_result(
                step,
                ok=False,
                stderr=f"document file not found: {step.command}",
            )
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return self._static_result(step, ok=False, stderr=f"document is empty: {step.command}")
        return self._static_result(step, ok=True, stdout=f"document check passed: {step.command}")

    def _static_result(
        self,
        step: VerificationStep,
        *,
        ok: bool,
        stdout: str = "",
        stderr: str = "",
    ) -> VerificationStepResult:
        command_result = CommandResult(
            command=f"static check {step.command}",
            cwd=str(self.settings.workspace_root / step.cwd),
            exit_code=0 if ok else 1,
            stdout=stdout,
            stderr=stderr,
        )
        return VerificationStepResult(
            step=step,
            command_result=command_result,
            ok=ok,
            failure_summary=None if ok else stderr or stdout,
        )

    @staticmethod
    def _script_interactive_issue(path: Path, text: str) -> str | None:
        suffix = path.suffix.lower()
        patterns: tuple[tuple[str, str], ...]
        if suffix in {".bat", ".cmd"}:
            patterns = (
                (r"^\s*pause\b", "interactive command 'pause'"),
                (r"^\s*choice\b", "interactive command 'choice'"),
                (r"^\s*set\s+/p\b", "interactive input command 'set /p'"),
            )
        elif suffix == ".ps1":
            patterns = (
                (r"\bRead-Host\b", "interactive command 'Read-Host'"),
                (r"\bPause\b", "interactive command 'Pause'"),
            )
        elif suffix == ".sh":
            patterns = (
                (r"^\s*read\b", "interactive shell command 'read'"),
            )
        else:
            return None

        for pattern, label in patterns:
            if re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
                return (
                    f"{label} found in {path.name}; remove it because verification and "
                    "agent-run scripts must not wait for user input."
                )
        return None

    def _run_http_smoke(self, step: VerificationStep) -> VerificationStepResult:
        url = step.url
        if not url and step.port:
            url = f"http://127.0.0.1:{step.port}/"
        if not url:
            return VerificationStepResult(step=step, ok=False, failure_summary="http smoke missing url")
        try:
            with urllib.request.urlopen(url, timeout=step.timeout_seconds) as response:
                ok = 200 <= response.status < 500
                command_result = CommandResult(
                    command=f"HTTP GET {url}",
                    cwd=str(Path.cwd()),
                    exit_code=0 if ok else response.status,
                    stdout=response.read(1000).decode("utf-8", errors="replace"),
                    stderr="",
                )
                return VerificationStepResult(step=step, command_result=command_result, ok=ok)
        except urllib.error.URLError as exc:
            command_result = CommandResult(
                command=f"HTTP GET {url}",
                cwd=str(Path.cwd()),
                exit_code=1,
                stdout="",
                stderr=str(exc),
            )
            return VerificationStepResult(step=step, command_result=command_result, ok=False)

    def _run_background(self, step: VerificationStep) -> VerificationStepResult:
        if not step.command:
            return VerificationStepResult(step=step, ok=False, failure_summary="background step missing command")
        log_path = self.settings.artifact_root / "logs" / "background" / "verification_server.log"
        info = self.background.start(
            step.command,
            self.settings.workspace_root / step.cwd,
            log_path,
            port=step.port,
            ready_timeout_seconds=step.timeout_seconds,
        )
        ok = bool(info.get("ready"))
        command_result = CommandResult(
            command=step.command,
            cwd=str(self.settings.workspace_root / step.cwd),
            exit_code=0 if ok else 1,
            stdout=str(info),
            stderr="" if ok else "background service did not become ready",
        )
        return VerificationStepResult(step=step, command_result=command_result, ok=ok)

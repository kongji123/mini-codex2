from __future__ import annotations

import hashlib
import re

from minicodex2.decision.types import FailureClassification
from minicodex2.tools.results import CommandResult
from minicodex2.verification.plan import VerificationStep


class FailureClassifier:
    def classify(
        self,
        *,
        step: VerificationStep,
        command_result: CommandResult | None,
        user_goal: str | None = None,
    ) -> FailureClassification:
        if command_result is None:
            return self._make("unknown", "task_critical", "no command result", step.name)
        if command_result.blocked:
            return self._make(
                "command_blocked",
                "global",
                command_result.block_reason or "command blocked",
                command_result.command,
            )
        output = f"{command_result.stderr}\n{command_result.stdout}"
        first = self._first_meaningful_line(output) or "command failed"
        if command_result.timed_out:
            return self._make("timeout", "task_critical", first, command_result.command)
        missing_toolchain = self._missing_toolchain(command_result.command, output)
        if missing_toolchain:
            return self._make(
                "missing_toolchain",
                "global",
                (
                    f"Required toolchain command not found: {missing_toolchain}. "
                    f"Install {missing_toolchain} and ensure it is on PATH, then rerun verification."
                ),
                command_result.command,
            )
        if "ModuleNotFoundError" in output or "No module named" in output:
            return self._make("missing_dependency", "task_critical", first, command_result.command)
        if step.step_type == "compile" or re.search(r"\berror:", output):
            return self._make("compile_error", "task_critical", first, command_result.command)
        if "FAILED " in output or "AssertionError" in output:
            return self._make("test_failure", "task_critical", first, command_result.command)
        if command_result.exit_code not in (0, None):
            return self._make("runtime_error", "task_critical", first, command_result.command)
        return self._make("unknown", "task_critical", first, command_result.command)

    def _make(
        self, failure_type: str, scope: str, first_line: str, command: str
    ) -> FailureClassification:
        normalized = f"{failure_type}:{_normalize_failure_command(command)}:{first_line}".strip()
        signature = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
        return FailureClassification(
            failure_type=failure_type,
            scope=scope,  # type: ignore[arg-type]
            signature=signature,
            first_blocking_line=first_line,
            can_retry=failure_type not in {"command_blocked", "missing_toolchain"},
        )

    @staticmethod
    def _first_meaningful_line(output: str) -> str | None:
        for line in output.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:1000]
        return None

    @staticmethod
    def _missing_toolchain(command: str, output: str) -> str | None:
        tool = command.strip().split(maxsplit=1)[0] if command.strip() else ""
        tool = tool.strip("'\"")
        known_toolchains = {
            "go",
            "cargo",
            "rustc",
            "node",
            "npm",
            "python",
            "py",
            "cmake",
            "make",
            "gcc",
            "clang",
        }
        if tool.lower() not in known_toolchains:
            return None

        lower_output = output.lower()
        quoted_tool = re.escape(tool.lower())
        windows_patterns = (
            rf"'{quoted_tool}' is not recognized as an internal or external command",
            rf'"{quoted_tool}" is not recognized as an internal or external command',
            rf"['\"]?{quoted_tool}['\"]?\s+\u4e0d\u662f\u5185\u90e8\u6216\u5916\u90e8\u547d\u4ee4",
            rf"the term '{quoted_tool}' is not recognized as a name of a cmdlet",
        )
        posix_patterns = (
            rf"\b{quoted_tool}: command not found\b",
            rf"\b{quoted_tool}: not found\b",
        )
        return tool if any(
            re.search(pattern, lower_output) for pattern in (*windows_patterns, *posix_patterns)
        ) else None


def _normalize_failure_command(command: str) -> str:
    normalized = command.strip()
    normalized = re.sub(r"(https?://(?:127\.0\.0\.1|localhost|\[::1\])):\d+", r"\1:<port>", normalized)
    normalized = re.sub(r"--port\s+\d+", "--port <port>", normalized)
    normalized = re.sub(r":\d{4,5}\b", ":<port>", normalized)
    normalized = re.sub(r"pytest-\d+", "pytest-<n>", normalized)
    normalized = re.sub(r"run_\d{8}_\d{6}_\d+", "run_<timestamp>", normalized)
    return normalized

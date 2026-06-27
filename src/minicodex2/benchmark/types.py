from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from minicodex2.model.fake_adapter import FakeStep

BenchmarkStatus = Literal["passed", "failed", "blocked", "skipped"]
BenchmarkModelMode = Literal["fake", "real"]


@dataclass(slots=True)
class BenchmarkCase:
    name: str
    description: str
    prompt: str
    setup: Callable[[Path], None]
    steps: list[FakeStep]
    hidden_assert: Callable[[Path], tuple[bool, str]]
    seed_broken: Callable[[Path], None] | None = None
    expected_status: str = "passed"
    requires_executable: str | None = None
    requires_any_executable: list[str] = field(default_factory=list)
    max_repair_rounds: int = 3


@dataclass(slots=True)
class BenchmarkCaseResult:
    name: str
    status: BenchmarkStatus
    one_turn_success: bool
    agent_status: str
    workspace: str
    summary: str
    model_mode: BenchmarkModelMode = "fake"
    repair_rounds: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    total_tokens: int = 0
    verification_started: bool = False
    verification_passed: bool = False
    failure_pack_created: bool = False
    false_completed: bool = False
    verification_skipped: bool = False
    blocked_without_evidence: bool = False
    hidden_assertion: str = ""
    log_path: str | None = None
    event_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "one_turn_success": self.one_turn_success,
            "agent_status": self.agent_status,
            "model_mode": self.model_mode,
            "workspace": self.workspace,
            "summary": self.summary,
            "repair_rounds": self.repair_rounds,
            "tool_calls": self.tool_calls,
            "model_calls": self.model_calls,
            "total_tokens": self.total_tokens,
            "verification_started": self.verification_started,
            "verification_passed": self.verification_passed,
            "failure_pack_created": self.failure_pack_created,
            "false_completed": self.false_completed,
            "verification_skipped": self.verification_skipped,
            "blocked_without_evidence": self.blocked_without_evidence,
            "hidden_assertion": self.hidden_assertion,
            "log_path": self.log_path,
            "event_counts": self.event_counts,
        }


@dataclass(slots=True)
class BenchmarkSuiteResult:
    suite: str
    model_mode: BenchmarkModelMode
    results: list[BenchmarkCaseResult]

    @property
    def runnable_count(self) -> int:
        return sum(1 for result in self.results if result.status != "skipped")

    @property
    def success_count(self) -> int:
        return sum(1 for result in self.results if result.one_turn_success)

    @property
    def one_turn_success_rate(self) -> float:
        if self.runnable_count == 0:
            return 0.0
        return self.success_count / self.runnable_count

    def to_dict(self) -> dict[str, object]:
        return {
            "suite": self.suite,
            "model_mode": self.model_mode,
            "runnable_count": self.runnable_count,
            "success_count": self.success_count,
            "one_turn_success_rate": self.one_turn_success_rate,
            "results": [result.to_dict() for result in self.results],
        }

    def to_markdown(self) -> str:
        lines = [
            f"# MiniCodex2 Benchmark: {self.suite}",
            "",
            f"- model_mode: {self.model_mode}",
            f"- runnable: {self.runnable_count}",
            f"- passed: {self.success_count}",
            f"- one_turn_success_rate: {self.one_turn_success_rate:.2%}",
            "",
            "| case | status | agent | repair | tools | tokens | workspace | issues |",
            "| --- | --- | --- | ---: | ---: | ---: | --- | --- |",
        ]
        for result in self.results:
            issues = []
            if result.false_completed:
                issues.append("false_completed")
            if result.verification_skipped:
                issues.append("verification_skipped")
            if result.blocked_without_evidence:
                issues.append("blocked_without_evidence")
            if not issues and result.hidden_assertion:
                issues.append(result.hidden_assertion)
            lines.append(
                "| "
                + " | ".join(
                    [
                        result.name,
                        result.status,
                        result.agent_status,
                        str(result.repair_rounds),
                        str(result.tool_calls),
                        str(result.total_tokens),
                        result.workspace.replace("\\", "/"),
                        ", ".join(issues) or "-",
                    ]
                )
                + " |"
            )
        return "\n".join(lines) + "\n"

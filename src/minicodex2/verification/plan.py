from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

StepType = Literal[
    "command",
    "compile",
    "run",
    "test",
    "background_server",
    "http_smoke",
    "static_file_check",
    "document_check",
]


@dataclass(slots=True)
class VerificationStep:
    name: str
    step_type: StepType
    command: str | None = None
    timeout_seconds: int = 60
    stop_on_failure: bool = True
    cwd: str = "."
    port: int | None = None
    url: str | None = None


@dataclass(slots=True)
class VerificationPlan:
    steps: list[VerificationStep]
    reason: str
    confidence: float = 1.0
    requires_model_decision: bool = False

    def summary(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "confidence": self.confidence,
            "steps": [
                {
                    "name": step.name,
                    "type": step.step_type,
                    "command": step.command,
                    "timeout_seconds": step.timeout_seconds,
                }
                for step in self.steps
            ],
        }

from __future__ import annotations

from dataclasses import dataclass, field

from minicodex2.tools.results import CommandResult
from minicodex2.verification.plan import VerificationPlan, VerificationStep


@dataclass(slots=True)
class VerificationStepResult:
    step: VerificationStep
    command_result: CommandResult | None = None
    ok: bool = False
    failure_summary: str | None = None


@dataclass(slots=True)
class VerificationResult:
    plan: VerificationPlan
    step_results: list[VerificationStepResult] = field(default_factory=list)
    passed: bool = False
    blocked: bool = False
    blocked_reason: str | None = None

    def first_failed_step(self) -> VerificationStepResult | None:
        for result in self.step_results:
            if not result.ok:
                return result
        return None


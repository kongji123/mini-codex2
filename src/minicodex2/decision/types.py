from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PolicyAction = Literal["allow", "ask", "deny", "blocked"]
FailureScope = Literal["global", "task_critical", "local", "optional"]
ContinuationAction = Literal[
    "repair_now",
    "continue_independent_work",
    "ask_user",
    "blocked",
    "partial",
    "passed",
]


@dataclass(slots=True)
class PolicyDecision:
    action: PolicyAction
    reason: str
    confidence: float = 1.0

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


@dataclass(slots=True)
class FailureClassification:
    failure_type: str
    scope: FailureScope
    signature: str
    first_blocking_line: str
    can_retry: bool = True


@dataclass(slots=True)
class ContinuationDecision:
    action: ContinuationAction
    reason: str


@dataclass(slots=True)
class BlockedItem:
    id: str
    scope: FailureScope
    summary: str
    affected_files: list[str]
    affected_verification: str | None = None
    can_continue_independent_work: bool = False


from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from minicodex2.agent.failure_pack import FailurePack
from minicodex2.model.messages import TokenUsage

TurnStatus = Literal["passed", "partial", "blocked", "completed"]


@dataclass(slots=True)
class AgentTurnResult:
    status: TurnStatus
    response: str
    changed_files: list[str] = field(default_factory=list)
    failure_pack: FailurePack | None = None
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    metrics: dict[str, object] = field(default_factory=dict)

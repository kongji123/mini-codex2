from __future__ import annotations

from minicodex2.agent.failure_pack import FailurePack
from minicodex2.decision.types import ContinuationDecision


class ContinuationPlanner:
    def decide(
        self,
        *,
        failure_pack: FailurePack | None,
        repair_round: int,
        max_repair_rounds: int,
        previous_signature: str | None,
    ) -> ContinuationDecision:
        if failure_pack is None:
            return ContinuationDecision("passed", "no failure")
        if failure_pack.scope == "global":
            return ContinuationDecision("blocked", "global failure blocks progress")
        if repair_round >= max_repair_rounds:
            return ContinuationDecision("blocked", "max repair rounds reached")
        if failure_pack.scope == "local":
            return ContinuationDecision("continue_independent_work", "local blocker can be recorded")
        return ContinuationDecision("repair_now", "task-critical failure should be repaired")

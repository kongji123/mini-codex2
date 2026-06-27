from __future__ import annotations

from pathlib import Path

from minicodex2.config.settings import AppSettings
from minicodex2.project.detector import ProjectDetector
from minicodex2.verification.plan import VerificationPlan
from minicodex2.verification.plan_builder import VerificationPlanBuilder
from minicodex2.verification.result import VerificationResult
from minicodex2.verification.runner import VerificationRunner


class CodeRuntimeServices:
    """Runtime-owned code project services, not a model-facing Skill.

    Skills are model-visible instruction/tool packages. These services are
    ordinary runtime organs: project detection, verification plan construction,
    and verification execution. Keeping the name explicit prevents this layer
    from being confused with external CodeSkill/EngineeringSkill hooks.
    """

    def __init__(self, settings: AppSettings) -> None:
        self.project_detector = ProjectDetector()
        self.plan_builder = VerificationPlanBuilder(settings)
        self.verification_runner = VerificationRunner(settings)

    def detect_project(self, root: Path):
        return self.project_detector.detect(root)

    def build_verification_plan(
        self,
        root: Path,
        changed_files: list[str],
    ) -> VerificationPlan:
        profile = self.project_detector.detect(root)
        return self.plan_builder.build(root, profile, changed_files)

    def run_verification(self, plan: VerificationPlan, *, event_bus, turn_id: str) -> VerificationResult:
        return self.verification_runner.run(plan, event_bus=event_bus, turn_id=turn_id)

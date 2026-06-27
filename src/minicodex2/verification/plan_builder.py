from __future__ import annotations

from pathlib import Path

from minicodex2.config.settings import AppSettings
from minicodex2.project.profile import ProjectProfile
from minicodex2.verification.plan import VerificationPlan
from minicodex2.verification.strategies import (
    ConfiguredCommandsStrategy,
    DocumentationVerificationStrategy,
    ScriptStaticVerificationStrategy,
    VerificationStrategy,
)


class VerificationPlanBuilder:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def build(
        self,
        root: str | Path,
        profile: ProjectProfile,
        changed_files: list[str],
    ) -> VerificationPlan:
        # Keep runtime-owned automatic verification deliberately narrow.
        #
        # MiniCodex2's main path is model-selected verification: the model sees
        # project facts, changed files, and available tools, then chooses the
        # appropriate command or browser/API check. The runtime should not guess
        # engineering intent from language markers such as "package.json" or
        # "manage.py"; those are hints, not decisions. We only auto-run:
        #   * explicit configured commands (user/runtime configuration),
        #   * static script safety checks,
        #   * documentation-only checks.
        strategies: list[VerificationStrategy] = []
        if self.settings.verification.commands:
            strategies.append(ConfiguredCommandsStrategy(self.settings.verification.commands))
        strategies.extend(
            [
                ScriptStaticVerificationStrategy(),
                DocumentationVerificationStrategy(),
            ]
        )
        root_path = Path(root)
        matches = [
            (strategy, strategy.matches(root_path, profile, changed_files)) for strategy in strategies
        ]
        matches = [(strategy, match) for strategy, match in matches if match.matched]
        if not matches:
            return VerificationPlan(
                steps=[],
                reason=_model_decision_reason(root_path, profile, changed_files),
                confidence=0.0,
                requires_model_decision=True,
            )
        strategy, match = max(matches, key=lambda item: item[1].confidence)
        plan = strategy.build_plan(root_path, profile, changed_files)
        plan.confidence = match.confidence
        return plan


def _model_decision_reason(root: Path, profile: ProjectProfile, changed_files: list[str]) -> str:
    parts: list[str] = ["model should choose project-specific verification"]
    if changed_files:
        parts.append("changed_files=" + ", ".join(changed_files[-12:]))
    if profile.detected_types:
        parts.append("detected_types=" + ", ".join(profile.detected_types))
    if profile.key_files:
        parts.append("key_files=" + ", ".join(profile.key_files[:12]))
    if profile.test_signals:
        parts.append("test_signals=" + ", ".join(profile.test_signals))
    if profile.web_signals:
        parts.append("web_signals=" + ", ".join(profile.web_signals))
    if profile.likely_entrypoints:
        parts.append("entrypoints=" + ", ".join(profile.likely_entrypoints[:8]))
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        parts.append("tests_dir=tests")
        if _tests_appear_service_dependent(tests_dir):
            parts.append("tests_hint=service-dependent or integration-like tests detected")
    parts.append(
        "runtime will not auto-select language strategy; inspect scripts and run the narrowest "
        "appropriate command/API/browser verification"
    )
    return "; ".join(parts)


def _tests_appear_service_dependent(path: Path) -> bool:
    markers = (
        "requests",
        "http://",
        "https://",
        "localhost",
        "127.0.0.1",
        "base_url",
        "api/",
    )
    for candidate in list(path.rglob("test_*.py"))[:20] + list(path.rglob("*_test.py"))[:20]:
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        if any(marker in text for marker in markers):
            return True
    return False

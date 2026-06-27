from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class CollaborationAdvice:
    should_advise: bool
    project_size: str
    suggested_mode: str
    reason: str
    score: int = 0
    signals: list[str] = field(default_factory=list)


class CollaborationAdvisor:
    SIGNALS = {
        "new_or_from_scratch": ("新项目", "从零", "new project", "from scratch", "创建项目", "create"),
        "multiple_interfaces": ("cli", "api", "tui", "desktop", "桌面", "local api"),
        "multi_language": ("多语言", "python", "node", "c ", "rust", "go"),
        "tests_or_ci": ("测试", "pytest", "ci", "验证", "test suite", "tests", "acceptance"),
        "permissions_or_security": ("权限", "安全", "permission", "security"),
        "config_history_or_storage": ("配置", "历史", "存储", "config", "history"),
        "autonomous_delivery": ("最后验收", "不用管", "自主", "autonomous", "deliver", "final acceptance"),
    }
    WEIGHTS = {
        "new_or_from_scratch": 2,
        "multiple_interfaces": 2,
        "multi_language": 2,
        "tests_or_ci": 1,
        "permissions_or_security": 1,
        "config_history_or_storage": 1,
        "autonomous_delivery": 2,
    }

    def evaluate(
        self,
        user_message: str,
        *,
        workspace_root: str | Path | None = None,
        already_advised: bool = False,
        design_docs_exist: bool = False,
    ) -> CollaborationAdvice:
        if already_advised or design_docs_exist:
            return CollaborationAdvice(False, "small", "direct", "already advised or docs exist")
        lower = user_message.lower()
        score = 0
        signals: list[str] = []
        for name, words in self.SIGNALS.items():
            if any(word.lower() in lower for word in words):
                score += self.WEIGHTS[name]
                signals.append(name)
        if workspace_root:
            root = Path(workspace_root)
            if root.exists() and not any(root.iterdir()):
                score += 1
                signals.append("empty_workspace")
        if score >= 6:
            return CollaborationAdvice(
                True,
                "large",
                "design_first",
                f"request has complex signals: {', '.join(signals)}",
                score,
                signals,
            )
        if score >= 3:
            return CollaborationAdvice(
                False,
                "medium",
                "brief_clarification",
                f"request has medium complexity signals: {', '.join(signals)}",
                score,
                signals,
            )
        return CollaborationAdvice(False, "small", "direct", "low complexity", score, signals)

    def format_advice(self, advice: CollaborationAdvice) -> str:
        return (
            "This request looks like a larger project. I recommend a design-first pass before "
            "coding: PRD, non-goals, architecture, module boundaries, milestones, acceptance "
            "criteria, and test plan.\n\n"
            f"Reason: {advice.reason}.\n\n"
            "If you want direct implementation, say so and I will proceed with MVP assumptions."
        )

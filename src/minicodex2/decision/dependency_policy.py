from __future__ import annotations

import re
from pathlib import Path

from minicodex2.decision.types import PolicyDecision


class DependencyInstallPolicy:
    def __init__(self, *, allow_project_install: bool = False) -> None:
        self.allow_project_install = allow_project_install

    def decide_missing_python_dependency(self, workspace_root: Path, output: str) -> PolicyDecision:
        module = self._extract_missing_module(output)
        if not module:
            return PolicyDecision("blocked", "missing dependency could not be identified")
        declared = self._declared_in_python_files(workspace_root, module)
        if declared and self.allow_project_install:
            return PolicyDecision("allow", f"project-declared dependency can be installed once: {module}")
        if declared:
            return PolicyDecision("ask", f"project-declared dependency requires install approval: {module}")
        return PolicyDecision("blocked", f"missing dependency is not declared in project files: {module}")

    @staticmethod
    def _extract_missing_module(output: str) -> str | None:
        patterns = [
            r"No module named ['\"]([^'\"]+)['\"]",
            r"ModuleNotFoundError:.*?['\"]([^'\"]+)['\"]",
        ]
        for pattern in patterns:
            match = re.search(pattern, output)
            if match:
                return match.group(1).split(".")[0].lower()
        return None

    @staticmethod
    def _declared_in_python_files(workspace_root: Path, module: str) -> bool:
        candidates = [workspace_root / "requirements.txt", workspace_root / "pyproject.toml"]
        for path in candidates:
            if path.exists() and module in path.read_text(encoding="utf-8", errors="ignore").lower():
                return True
        return False


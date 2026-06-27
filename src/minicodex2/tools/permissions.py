from __future__ import annotations

from minicodex2.decision.types import PolicyDecision


class PermissionPolicy:
    def __init__(self, mode: str = "auto") -> None:
        self.mode = mode

    def check_write(self, path: str) -> PolicyDecision:
        if self.mode == "read_only":
            return PolicyDecision("deny", f"write denied in read_only mode: {path}")
        if self.mode == "guarded":
            return PolicyDecision("ask", f"write requires approval in guarded mode: {path}")
        return PolicyDecision("allow", f"workspace write allowed in auto mode: {path}")

    def check_delete(self, path: str) -> PolicyDecision:
        if self.mode == "read_only":
            return PolicyDecision("deny", f"delete denied in read_only mode: {path}")
        if self.mode == "guarded":
            return PolicyDecision("ask", f"delete requires approval in guarded mode: {path}")
        return PolicyDecision("allow", f"workspace delete allowed in auto mode: {path}")

    def check_command(self, command: str, trusted: bool = False) -> PolicyDecision:
        lower = command.lower()
        dangerous = ["rm -rf", "del /s", "rmdir /s", "format ", "git reset --hard", "git clean -fd"]
        if any(token in lower for token in dangerous):
            return PolicyDecision("blocked", f"dangerous command blocked: {command}")
        if self._is_remote_git(lower):
            return PolicyDecision("blocked", f"remote git command blocked in v0.1: {command}")
        if trusted and self.mode == "auto":
            return PolicyDecision("allow", f"trusted command allowed: {command}")
        if self.mode == "auto":
            return PolicyDecision("allow", f"command allowed in auto mode: {command}")
        return PolicyDecision("ask", f"command requires approval: {command}")

    @staticmethod
    def _is_remote_git(lower_command: str) -> bool:
        return any(
            lower_command.startswith(prefix)
            for prefix in ("git push", "git pull", "git fetch", "git remote")
        )


class GitPolicy:
    def decide(self, command: str) -> PolicyDecision:
        lower = command.lower().strip()
        if lower.startswith(("git status", "git diff", "git log")):
            return PolicyDecision("allow", "read-only git command allowed")
        if lower.startswith(("git init", "git add", "git commit")):
            return PolicyDecision("ask", "local git write requires explicit request")
        if lower.startswith(("git push", "git pull", "git fetch", "git remote")):
            return PolicyDecision("blocked", "remote git is out of scope for v0.1")
        if lower.startswith(("git reset --hard", "git clean", "git checkout --", "git restore")):
            return PolicyDecision("blocked", "destructive git command blocked")
        return PolicyDecision("ask", "git command requires review")

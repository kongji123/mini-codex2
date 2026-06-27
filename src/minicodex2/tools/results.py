from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolResult:
    ok: bool
    content: str
    did_write: bool = False
    changed_files: list[str] = field(default_factory=list)
    blocked: bool = False
    block_reason: str | None = None
    permission_request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CommandResult:
    command: str
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_seconds: float = 0.0
    blocked: bool = False
    block_reason: str | None = None

    @property
    def ok(self) -> bool:
        return not self.blocked and not self.timed_out and self.exit_code == 0

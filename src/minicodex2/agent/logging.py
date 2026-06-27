from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = ("api_key", "authorization", "token", "key")
DEFAULT_MAX_LOG_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5


def redact_text(text: str) -> str:
    redacted = text
    marker = "sk-"
    while marker in redacted:
        start = redacted.find(marker)
        end = start
        while end < len(redacted) and not redacted[end].isspace():
            end += 1
        redacted = redacted[:start] + "<redacted-secret>" + redacted[end:]
    return redacted


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if any(sensitive in key.lower() for sensitive in SENSITIVE_KEYS)
            else redact_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class RuntimeLogger:
    def __init__(
        self,
        artifact_root: str | Path,
        name: str = "session",
        *,
        max_bytes: int = DEFAULT_MAX_LOG_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
    ) -> None:
        root = Path(artifact_root)
        self.log_dir = root / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / f"{name}.log"
        self.max_bytes = max(0, max_bytes)
        self.backup_count = max(0, backup_count)

    def write(self, message: str) -> None:
        timestamp = datetime.now(UTC).isoformat()
        self._rotate_if_needed()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"{timestamp} {redact_text(message)}\n")

    def event(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        self.write(f"{event_type}: {redact_payload(payload or {})}")

    def _rotate_if_needed(self) -> None:
        if self.max_bytes <= 0 or self.backup_count <= 0:
            return
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        if oldest.exists():
            try:
                oldest.unlink()
            except OSError:
                pass
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            target = self.path.with_name(f"{self.path.name}.{index + 1}")
            if source.exists():
                try:
                    source.replace(target)
                except OSError:
                    pass
        try:
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        except OSError:
            pass

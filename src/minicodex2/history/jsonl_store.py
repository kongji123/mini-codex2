from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from minicodex2.agent.chat_history import ChatHistory
from minicodex2.model.messages import ChatMessage


SENSITIVE_KEYS = {"api_key", "authorization", "token"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("<redacted>" if key.lower() in SENSITIVE_KEYS else redact(val)) for key, val in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class JsonlHistoryStore:
    def __init__(self, workspace_root: str | Path) -> None:
        self.root = Path(workspace_root)
        self.sessions_dir = self.root / ".minicodex2" / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def save(self, session_id: str, history: ChatHistory, metadata: dict[str, Any]) -> tuple[Path, Path]:
        jsonl_path = self.sessions_dir / f"{session_id}.jsonl"
        meta_path = self.sessions_dir / f"{session_id}.meta.json"
        with jsonl_path.open("w", encoding="utf-8") as fh:
            for message in history.messages():
                data = redact(asdict(message))
                fh.write(json.dumps(data, ensure_ascii=False) + "\n")
        meta = redact(
            {"session_id": session_id, "saved_at": datetime.now(UTC).isoformat(), **metadata}
        )
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return jsonl_path, meta_path

    def list_sessions(self) -> list[str]:
        paths = sorted(self.sessions_dir.glob("*.meta.json"), key=lambda path: path.stat().st_mtime)
        return [path.name.removesuffix(".meta.json") for path in paths]

    def latest_session_id(self) -> str | None:
        sessions = self.list_sessions()
        return sessions[-1] if sessions else None

    def load_history(self, session_id: str) -> ChatHistory:
        history = ChatHistory()
        for message in self.load_messages(session_id):
            history.add(message)
        return history

    def load_metadata(self, session_id: str) -> dict[str, Any]:
        meta_path = self.sessions_dir / f"{session_id}.meta.json"
        return json.loads(meta_path.read_text(encoding="utf-8"))

    def load_messages(self, session_id: str) -> list[ChatMessage]:
        jsonl_path = self.sessions_dir / f"{session_id}.jsonl"
        messages = []
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                data = json.loads(line)
                messages.append(ChatMessage(**data))
        return messages

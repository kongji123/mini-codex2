from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from minicodex2.model.messages import ChatMessage


class ContextBufferStore:
    """Persist the exact model-visible context for a session/channel.

    The buffer has two representations:

    - a pretty JSON snapshot for humans and debugging;
    - a canonical byte-buffer JSONL snapshot for prefix-cache stability.

    Provider prompt caches are byte-prefix caches, not semantic caches. Rebuilding
    a Python object graph into JSON before every call can change bytes through
    timestamps, whitespace, field ordering, or optional metadata. The sidecar is
    treated like a C/C++ byte buffer: initialize once, then append bytes. Loading
    for the Python adapter parses that stable byte buffer back into ChatMessage
    objects, but the persisted prefix itself is never reserialized during normal
    growth.
    """

    def __init__(self, artifact_root: Path) -> None:
        self.root = artifact_root / "context_buffers"

    def path_for(self, session_id: str, *, channel: str = "main") -> Path:
        safe_session = _safe_name(session_id)
        safe_channel = _safe_name(channel)
        return self.root / safe_session / f"{safe_channel}.json"

    def wire_path_for(self, session_id: str, *, channel: str = "main") -> Path:
        safe_session = _safe_name(session_id)
        safe_channel = _safe_name(channel)
        return self.root / safe_session / f"{safe_channel}.wire.jsonl"

    def exists(self, session_id: str, *, channel: str = "main") -> bool:
        return self.wire_path_for(session_id, channel=channel).exists() or self.path_for(
            session_id, channel=channel
        ).exists()

    def save(
        self,
        session_id: str,
        messages: list[ChatMessage],
        *,
        channel: str = "main",
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        path = self.path_for(session_id, channel=channel)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_snapshot(
            path,
            session_id,
            messages,
            channel=channel,
            metadata=metadata,
        )
        self._write_wire_snapshot(
            self.wire_path_for(session_id, channel=channel),
            messages,
        )
        return path

    def _write_json_snapshot(
        self,
        path: Path,
        session_id: str,
        messages: list[ChatMessage],
        *,
        channel: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "version": 1,
            "session_id": session_id,
            "channel": channel,
            "updated_at": datetime.now(UTC).isoformat(),
            "metadata": metadata or {},
            "messages": [asdict(message) for message in messages],
        }
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _write_wire_snapshot(self, path: Path, messages: list[ChatMessage]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("wb") as handle:
            for message in messages:
                handle.write(_wire_line(message))
        tmp_path.replace(path)

    def load(self, session_id: str, *, channel: str = "main") -> list[ChatMessage]:
        wire_messages = self._load_wire(session_id, channel=channel)
        if wire_messages:
            return wire_messages
        path = self.path_for(session_id, channel=channel)
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list):
            return []
        messages: list[ChatMessage] = []
        for raw in raw_messages:
            if not isinstance(raw, dict):
                continue
            try:
                messages.append(ChatMessage(**raw))
            except TypeError:
                continue
        return messages

    def repair_tool_call_sequence(self, session_id: str, *, channel: str = "main") -> int:
        """Repair persisted Chat API tool-call ordering for this buffer.

        OpenAI-compatible chat APIs require every assistant message that contains
        tool_calls to be followed immediately by one tool message for each
        tool_call_id. MiniCodex2 appends history incrementally; if the process is
        cancelled between tool calls, or an older bug skipped one tool result,
        the persisted buffer can become invalid and every later request fails
        before the model sees it. This repair is deliberately protocol-level: it
        inserts an "unknown/interrupted" synthetic tool result, not a semantic
        judgement about the project.
        """
        messages = self.load(session_id, channel=channel)
        if not messages:
            return 0
        repaired_messages, repair_count = _repair_tool_call_sequence(messages)
        if repair_count:
            self.save(
                session_id,
                repaired_messages,
                channel=channel,
                metadata={"reason": "repair_tool_call_sequence", "repairs": repair_count},
            )
        return repair_count

    def _load_wire(self, session_id: str, *, channel: str = "main") -> list[ChatMessage]:
        path = self.wire_path_for(session_id, channel=channel)
        if not path.exists():
            return []
        messages: list[ChatMessage] = []
        try:
            with path.open("rb") as handle:
                for raw_line in handle:
                    if not raw_line.strip():
                        continue
                    raw = json.loads(raw_line.decode("utf-8"))
                    if isinstance(raw, dict):
                        messages.append(ChatMessage(**raw))
        except (OSError, json.JSONDecodeError, TypeError):
            return []
        return messages

    def append(
        self,
        session_id: str,
        message: ChatMessage,
        *,
        channel: str = "main",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        messages = self.load(session_id, channel=channel)
        if not messages:
            return False
        messages.append(message)
        json_path = self.path_for(session_id, channel=channel)
        self._write_json_snapshot(
            json_path,
            session_id,
            messages,
            channel=channel,
            metadata=metadata,
        )
        wire_path = self.wire_path_for(session_id, channel=channel)
        wire_path.parent.mkdir(parents=True, exist_ok=True)
        with wire_path.open("ab") as handle:
            handle.write(_wire_line(message))
        return True


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
    return cleaned or "default"


def _repair_tool_call_sequence(messages: list[ChatMessage]) -> tuple[list[ChatMessage], int]:
    repaired: list[ChatMessage] = []
    repair_count = 0
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "tool":
            repair_count += 1
            repaired.append(
                ChatMessage(
                    role="runtime",
                    content=(
                        "Recovered context buffer: dropped an orphan tool result "
                        f"with tool_call_id={message.tool_call_id or 'unknown'}."
                    ),
                    metadata={"context_buffer_repair": "orphan_tool_result"},
                )
            )
            index += 1
            continue

        repaired.append(message)
        tool_calls = _tool_calls_for_message(message)
        if not tool_calls:
            index += 1
            continue

        expected_ids = [_tool_call_id(call) for call in tool_calls]
        expected_ids = [tool_call_id for tool_call_id in expected_ids if tool_call_id]
        expected = set(expected_ids)
        seen: set[str] = set()
        delayed_notes: list[ChatMessage] = []
        index += 1

        while index < len(messages) and messages[index].role == "tool":
            tool_message = messages[index]
            tool_call_id = str(tool_message.tool_call_id or "")
            if tool_call_id in expected and tool_call_id not in seen:
                repaired.append(tool_message)
                seen.add(tool_call_id)
            else:
                repair_count += 1
                delayed_notes.append(
                    ChatMessage(
                        role="runtime",
                        content=(
                            "Recovered context buffer: dropped an unexpected tool result "
                            f"with tool_call_id={tool_call_id or 'unknown'} after assistant tool_calls."
                        ),
                        metadata={"context_buffer_repair": "unexpected_tool_result"},
                    )
                )
            index += 1

        for missing_id in expected_ids:
            if missing_id in seen:
                continue
            repair_count += 1
            repaired.append(
                ChatMessage(
                    role="tool",
                    content=(
                        "Recovered context buffer: this tool call result was missing before the "
                        "next model request. Treat the operation as interrupted/unknown; inspect "
                        "current project state before relying on it."
                    ),
                    name=_tool_call_name(tool_calls, missing_id),
                    tool_call_id=missing_id,
                    metadata={
                        "context_buffer_repair": "missing_tool_result",
                        "synthetic": True,
                    },
                )
            )
        repaired.extend(delayed_notes)
    return repaired, repair_count


def _tool_calls_for_message(message: ChatMessage) -> list[dict[str, Any]]:
    raw = message.metadata.get("tool_calls") if isinstance(message.metadata, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _tool_call_id(tool_call: dict[str, Any]) -> str | None:
    value = tool_call.get("id")
    return str(value) if value else None


def _tool_call_name(tool_calls: list[dict[str, Any]], tool_call_id: str) -> str | None:
    for tool_call in tool_calls:
        if _tool_call_id(tool_call) != tool_call_id:
            continue
        function = tool_call.get("function")
        if isinstance(function, dict) and function.get("name"):
            return str(function["name"])
    return None


def _wire_line(message: ChatMessage) -> bytes:
    # Canonical enough for our boundary: fixed key ordering, no whitespace, no
    # volatile snapshot metadata. This is not the final HTTP payload, but it keeps
    # MiniCodex2's persisted message prefix byte-stable before the adapter turns
    # it into provider-specific JSON.
    payload = asdict(message)
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")

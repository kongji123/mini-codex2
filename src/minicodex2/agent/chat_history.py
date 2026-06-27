from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from minicodex2.model.messages import ChatMessage

MAX_TOOL_RESULT_CHARS = 4_000
CHECKPOINT_KIND = "compaction_checkpoint"


@dataclass
class ChatHistory:
    _messages: list[ChatMessage] = field(default_factory=list)
    on_message_added: Callable[[ChatMessage], None] | None = None

    def add(self, message: ChatMessage) -> None:
        stored = _with_history_metadata(message)
        self._messages.append(stored)
        if self.on_message_added is not None:
            self.on_message_added(stored)

    def add_user(self, content: str) -> None:
        self.add(ChatMessage(role="user", content=content))

    def add_user_image(
        self,
        content: str,
        *,
        path: str,
        mime_type: str,
        detail: str = "low",
    ) -> None:
        self.add(
            ChatMessage(
                role="user",
                content=content,
                metadata={
                    "image": {
                        "path": path,
                        "mime_type": mime_type,
                        "detail": detail,
                    }
                },
            )
        )

    def add_assistant(self, content: str) -> None:
        self.add(ChatMessage(role="assistant", content=content))

    def add_runtime(self, content: str) -> None:
        self.add(ChatMessage(role="runtime", content=content))

    def add_compaction_checkpoint(
        self,
        *,
        summary: str,
        replacement_history: list[ChatMessage],
        source_messages: list[ChatMessage],
    ) -> ChatMessage:
        parent_checkpoint_id = _latest_checkpoint_id(self._messages)
        source_message_ids = [
            _message_id(message)
            for message in source_messages
            if _message_id(message) is not None
        ]
        checkpoint_id = f"cp_{uuid4().hex[:12]}"
        checkpoint = ChatMessage(
            role="runtime",
            content=summary,
            metadata={
                "kind": CHECKPOINT_KIND,
                "checkpoint_id": checkpoint_id,
                "parent_checkpoint_id": parent_checkpoint_id,
                "source_message_ids": source_message_ids,
                "replacement_history": [
                    asdict(_with_history_metadata(message))
                    for message in replacement_history
                    if not _is_compaction_checkpoint(message)
                ],
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        self.add(checkpoint)
        return checkpoint

    def add_tool_result(self, tool_call_id: str, content: str, name: str | None = None) -> None:
        self.add(
            ChatMessage(
                role="tool",
                content=_compact_tool_result(content),
                name=name,
                tool_call_id=tool_call_id,
            )
        )

    def clear(self) -> int:
        count = len(self._messages)
        self._messages.clear()
        return count

    def drop_first(self, count: int) -> int:
        count = max(0, min(count, len(self._messages)))
        del self._messages[:count]
        return count

    def drop_last(self, count: int) -> int:
        count = max(0, min(count, len(self._messages)))
        if count:
            del self._messages[-count:]
        return count

    def drop_images(self, count: int | None = None) -> int:
        image_indexes = [
            index
            for index, message in enumerate(self._messages)
            if isinstance(message.metadata.get("image"), dict)
        ]
        if count is not None:
            image_indexes = image_indexes[-max(0, count) :]
        for index in reversed(image_indexes):
            message = self._messages[index]
            metadata = dict(message.metadata)
            metadata.pop("image", None)
            self._messages[index] = ChatMessage(
                role=message.role,
                content=f"{message.content}\n[image dropped from history]",
                name=message.name,
                tool_call_id=message.tool_call_id,
                metadata=metadata,
            )
        return len(image_indexes)

    def repair_tool_call_sequence(self) -> int:
        """Keep persisted history compatible with OpenAI tool-call sequencing."""
        repaired: list[ChatMessage] = []
        repair_count = 0
        index = 0
        while index < len(self._messages):
            message = self._messages[index]
            if message.role == "tool":
                repair_count += 1
                repaired.append(
                    ChatMessage(
                        role="runtime",
                        content=(
                            "Recovered history: dropped an orphan tool result because no preceding "
                            f"assistant tool_call was available. tool_call_id={message.tool_call_id or 'unknown'}"
                        ),
                    )
                )
                index += 1
                continue

            repaired.append(message)
            tool_call_ids = _assistant_tool_call_ids(message)
            if not tool_call_ids:
                index += 1
                continue

            expected = set(tool_call_ids)
            seen: set[str] = set()
            index += 1
            while index < len(self._messages) and self._messages[index].role == "tool":
                tool_message = self._messages[index]
                if tool_message.tool_call_id in expected:
                    seen.add(str(tool_message.tool_call_id))
                    repaired.append(tool_message)
                else:
                    repair_count += 1
                    repaired.append(
                        ChatMessage(
                            role="runtime",
                            content=(
                                "Recovered history: dropped a tool result with an unexpected "
                                f"tool_call_id={tool_message.tool_call_id or 'unknown'}."
                            ),
                        )
                    )
                index += 1

            missing = [tool_call_id for tool_call_id in tool_call_ids if tool_call_id not in seen]
            for tool_call_id in missing:
                repair_count += 1
                repaired.append(
                    ChatMessage(
                        role="tool",
                        content=(
                            "Recovered history: this tool call did not finish before the session "
                            "was interrupted or restarted. Treat the previous tool operation as "
                            "unknown and inspect current project state before continuing."
                        ),
                        name=_assistant_tool_call_name(message, tool_call_id),
                        tool_call_id=tool_call_id,
                    )
                )

        if repair_count:
            self._messages = repaired
        return repair_count

    def messages(self) -> list[ChatMessage]:
        return list(self._messages)

    def active_messages(self) -> list[ChatMessage]:
        checkpoint_index = _last_checkpoint_index(self._messages)
        if checkpoint_index is None:
            return self.messages()
        checkpoint = self._messages[checkpoint_index]
        replacement_history = checkpoint.metadata.get("replacement_history")
        active: list[ChatMessage] = []
        if isinstance(replacement_history, list):
            for raw_message in replacement_history:
                if isinstance(raw_message, dict):
                    try:
                        active.append(_with_history_metadata(ChatMessage(**raw_message)))
                    except TypeError:
                        continue
        active.extend(self._messages[checkpoint_index + 1 :])
        return [message for message in active if not _is_compaction_checkpoint(message)]

    def latest_user_text(self) -> str | None:
        for message in reversed(self._messages):
            if message.role == "user":
                return message.content
        return None


def _assistant_tool_call_ids(message: ChatMessage) -> list[str]:
    if message.role != "assistant":
        return []
    tool_calls = message.metadata.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    ids: list[str] = []
    for call in tool_calls:
        if isinstance(call, dict) and isinstance(call.get("id"), str):
            ids.append(call["id"])
    return ids


def _assistant_tool_call_name(message: ChatMessage, tool_call_id: str) -> str | None:
    tool_calls = message.metadata.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None
    for call in tool_calls:
        if not isinstance(call, dict) or call.get("id") != tool_call_id:
            continue
        function = call.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return function["name"]
    return None


def _compact_tool_result(content: str, *, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(content) <= limit:
        return content
    head_limit = max(1000, int(limit * 0.70))
    tail_limit = max(500, limit - head_limit - 220)
    omitted = len(content) - head_limit - tail_limit
    return (
        content[:head_limit]
        + f"\n\n[tool output truncated: {omitted} characters omitted from model history]\n\n"
        + content[-tail_limit:]
    )


def _with_history_metadata(message: ChatMessage) -> ChatMessage:
    metadata = dict(message.metadata)
    metadata.setdefault("message_id", f"msg_{uuid4().hex[:12]}")
    metadata.setdefault("created_at", datetime.now(UTC).isoformat())
    if metadata is message.metadata:
        return message
    return ChatMessage(
        role=message.role,
        content=message.content,
        name=message.name,
        tool_call_id=message.tool_call_id,
        metadata=metadata,
    )


def _message_id(message: ChatMessage) -> str | None:
    value = message.metadata.get("message_id")
    return value if isinstance(value, str) else None


def _is_compaction_checkpoint(message: ChatMessage) -> bool:
    return message.role == "runtime" and message.metadata.get("kind") == CHECKPOINT_KIND


def _last_checkpoint_index(messages: list[ChatMessage]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if _is_compaction_checkpoint(messages[index]):
            return index
    return None


def _latest_checkpoint_id(messages: list[ChatMessage]) -> str | None:
    index = _last_checkpoint_index(messages)
    if index is None:
        return None
    checkpoint_id = messages[index].metadata.get("checkpoint_id")
    return checkpoint_id if isinstance(checkpoint_id, str) else None

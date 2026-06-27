from __future__ import annotations

import base64
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool", "runtime"]


@dataclass(slots=True)
class ChatMessage:
    role: Role
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_model_dict(
        self,
        *,
        support_images: bool = True,
        include_reasoning_content: bool = False,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role if self.role != "runtime" else "system"}
        model_content = self._model_content(support_images=support_images)
        data["content"] = model_content if model_content is not None else self.content
        if self.role == "assistant" and "tool_calls" in self.metadata:
            data["tool_calls"] = self.metadata["tool_calls"]
        if self.role == "assistant" and include_reasoning_content:
            reasoning_content = self.metadata.get("reasoning_content")
            if isinstance(reasoning_content, str) and reasoning_content:
                data["reasoning_content"] = reasoning_content
        if self.name:
            data["name"] = self.name
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        return data

    def _model_content(self, *, support_images: bool = True) -> str | list[dict[str, Any]] | None:
        image = self.metadata.get("image")
        if self.role != "user" or not isinstance(image, dict):
            return None
        path = Path(str(image.get("path") or ""))
        if not support_images:
            return f"{self.content}\n\n[image attached: {path}]"
        mime_type = str(image.get("mime_type") or "application/octet-stream")
        detail = str(image.get("detail") or "low")
        if not path.exists() or not path.is_file():
            return f"{self.content}\n\n[image unavailable: {path}]"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return [
            {"type": "text", "text": self.content},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{encoded}",
                    "detail": detail,
                },
            },
        ]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_model_dict(self) -> dict[str, Any]:
        import json

        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_prompt_tokens: int = 0
    cache_hit_prompt_tokens: int = 0
    cache_miss_prompt_tokens: int = 0
    estimated: bool = True

    def add(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cached_prompt_tokens=self.cached_prompt_tokens + other.cached_prompt_tokens,
            cache_hit_prompt_tokens=self.cache_hit_prompt_tokens + other.cache_hit_prompt_tokens,
            cache_miss_prompt_tokens=self.cache_miss_prompt_tokens + other.cache_miss_prompt_tokens,
            estimated=self.estimated or other.estimated,
        )

    @property
    def cache_observed_prompt_tokens(self) -> int:
        return self.cache_hit_prompt_tokens + self.cache_miss_prompt_tokens

    @property
    def cache_hit_ratio(self) -> float | None:
        observed = self.cache_observed_prompt_tokens
        if observed <= 0:
            return None
        return self.cache_hit_prompt_tokens / observed


@dataclass(slots=True)
class ModelRequest:
    messages: list[ChatMessage]
    tools: list[dict[str, Any]]
    model: str = "default"
    runtime_context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelResponse:
    message: ChatMessage
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)

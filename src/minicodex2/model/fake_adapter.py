from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from minicodex2.model.adapter import ModelAdapter
from minicodex2.model.messages import ChatMessage, ModelRequest, ModelResponse, TokenUsage, ToolCall


@dataclass(slots=True)
class FakeStep:
    content: str
    tool_calls: list[ToolCall] | None = None
    finish_reason: str = "stop"


class FakeModelAdapter(ModelAdapter):
    def __init__(self, steps: list[FakeStep] | None = None) -> None:
        self.steps: deque[FakeStep] = deque(steps or [])
        self.call_count = 0
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self.requests.append(request)
        if self.steps:
            step = self.steps.popleft()
            content = step.content
            tool_calls = step.tool_calls or []
            finish_reason = step.finish_reason
        else:
            last_user = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
            content = f"Fake response: {last_user}"
            tool_calls = []
            finish_reason = "stop"
        estimated = sum(len(m.content) for m in request.messages) // 4 + 1
        return ModelResponse(
            message=ChatMessage(role="assistant", content=content),
            tool_calls=tool_calls,
            usage=TokenUsage(
                prompt_tokens=estimated,
                completion_tokens=max(1, len(content) // 4),
                total_tokens=estimated + max(1, len(content) // 4),
                estimated=True,
            ),
            finish_reason=finish_reason,
        )


from __future__ import annotations

from dataclasses import dataclass, field

from minicodex2.model.messages import ChatMessage, TokenUsage

IMAGE_TOKEN_ESTIMATE = 1000


def estimate_message_tokens(messages: list[ChatMessage]) -> int:
    chars = sum(len(m.content or "") for m in messages)
    image_count = sum(1 for message in messages if isinstance(message.metadata.get("image"), dict))
    return max(1, chars // 4 + image_count * IMAGE_TOKEN_ESTIMATE)


@dataclass
class TokenUsageTracker:
    calls: list[TokenUsage] = field(default_factory=list)

    def record(self, usage: TokenUsage) -> None:
        self.calls.append(usage)

    def total(self) -> TokenUsage:
        total = TokenUsage(estimated=False)
        for usage in self.calls:
            total = total.add(usage)
        return total

    def estimate_before_call(self, messages: list[ChatMessage]) -> TokenUsage:
        tokens = estimate_message_tokens(messages)
        return TokenUsage(prompt_tokens=tokens, total_tokens=tokens, estimated=True)

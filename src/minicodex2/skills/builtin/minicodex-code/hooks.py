from __future__ import annotations

from pathlib import Path
from string import Formatter
from typing import Any


PROMPT_BY_HOOK = {
    "project_preflight": "prompts/project_preflight.md",
    "diagnostic_first": "prompts/diagnostic_first.md",
    "tool_failure_repair": "prompts/tool_failure_repair.md",
    "verification_guidance": "prompts/verification_guidance.md",
}


def project_preflight(ctx: Any) -> str:
    return _render_prompt_hook(ctx, "project_preflight")


def diagnostic_first(ctx: Any) -> str:
    return _render_prompt_hook(ctx, "diagnostic_first")


def tool_failure_repair(ctx: Any) -> str:
    return _render_prompt_hook(ctx, "tool_failure_repair")


def verification_guidance(ctx: Any) -> str:
    return _render_prompt_hook(ctx, "verification_guidance")


def _render_prompt_hook(ctx: Any, hook_name: str) -> str:
    prompt_path = PROMPT_BY_HOOK[hook_name]
    template = (Path(__file__).resolve().parent / prompt_path).read_text(encoding="utf-8")
    values = {
        "skill_name": ctx.skill["name"],
        **ctx.variables,
    }
    return _SafeFormatter().format(template, **{
        key: _compact(value)
        for key, value in values.items()
    })


class _SafeFormatter(Formatter):
    def get_value(self, key: object, args: tuple[object, ...], kwargs: dict[str, object]) -> object:
        if isinstance(key, str):
            return kwargs.get(key, "")
        return super().get_value(key, args, kwargs)


def _compact(value: object, limit: int = 900) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."

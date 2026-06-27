from __future__ import annotations

from pathlib import Path
from string import Formatter
from typing import Any


PROMPT_BY_HOOK = {
    "visual_task_preflight": "prompts/visual_task_preflight.md",
    "action_repair": "prompts/action_repair.md",
}


def visual_task_preflight(ctx: Any) -> str:
    return _render_prompt_hook(ctx, "visual_task_preflight")


def action_repair(ctx: Any) -> str:
    return _render_prompt_hook(ctx, "action_repair")


def _render_prompt_hook(ctx: Any, hook_name: str) -> str:
    template = (Path(__file__).resolve().parent / PROMPT_BY_HOOK[hook_name]).read_text(
        encoding="utf-8"
    )
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

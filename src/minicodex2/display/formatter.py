from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from minicodex2.agent.events import AgentEvent
from minicodex2.display.catalog_en_us import CATALOG as EN_US
from minicodex2.display.catalog_zh_cn import CATALOG as ZH_CN


@dataclass(slots=True)
class DisplayMessage:
    level: str
    title: str
    message: str
    phase: str = "system"
    status: str = "info"
    details: dict[str, Any] = field(default_factory=dict)


class UiMessageFormatter:
    def __init__(self, locale: str = "zh-CN") -> None:
        self.locale = normalize_locale(locale)
        self.catalog = ZH_CN if self.locale == "zh-CN" else EN_US

    def status_label(self, status: str) -> str:
        return self._t(f"status.{status}", status=status)

    def ui_text(self, key: str, **values: object) -> str:
        return self._t(f"ui.{key}", **values)

    def format_event(self, event: AgentEvent) -> DisplayMessage | None:
        payload = event.payload
        if event.type in {"session_created", "token_usage_recorded"}:
            return None
        key, level, phase, status, values = self._event_template(event.type, payload)
        title = self._t(f"{key}.title", **values)
        message = self._t(f"{key}.message", **values)
        return DisplayMessage(level, title, message, phase, status, payload)

    def _event_template(
        self, event_type: str, payload: dict[str, Any]
    ) -> tuple[str, str, str, str, dict[str, object]]:
        values: dict[str, object] = dict(payload)
        if "name" in payload:
            values["tool"] = self.tool_label(str(payload.get("name") or "tool"))
        if "status" in payload:
            values["status"] = self.status_label(str(payload.get("status")))
        details = _details(str(payload.get("content_excerpt") or payload.get("failure_summary") or ""))
        values["details"] = details
        mapping = {
            "turn_started": ("event.turn_started", "info", "turn", "running"),
            "context_built": ("event.context_built", "info", "model", "running"),
            "model_call_started": ("event.model_call_started", "info", "model", "running"),
            "model_call_finished": ("event.model_call_finished", "info", "model", "done"),
            "tool_call_started": ("event.tool_call_started", "info", "tool", "running"),
            "command_progress": ("event.command_progress", "info", "tool", str(payload.get("stage") or "running")),
            "file_changed": ("event.file_changed", "warning", "file", "changed"),
            "verification_started": ("event.verification_started", "info", "verify", "running"),
            "verification_plan_created": ("event.verification_plan_created", "info", "verify", "running"),
            "verification_needs_model_decision": ("event.verification_needs_model_decision", "warning", "verify", "paused"),
            "verification_step_started": ("event.verification_step_started", "info", "verify", "running"),
            "background_command_started": ("event.background_command_started", "info", "server", "running"),
            "background_command_ready": ("event.background_command_ready", "success", "server", "ready"),
            "background_command_failed": ("event.background_command_failed", "error", "server", "failed"),
            "failure_pack_created": ("event.failure_pack_created", "error", "failure", "failed"),
            "repair_round_started": ("event.repair_round_started", "warning", "repair", "running"),
            "recoverable_tool_error": ("event.recoverable_tool_error", "warning", "tool", "retry"),
            "required_tool_retry": ("event.required_tool_retry", "warning", "tool", "retry"),
            "permission_requested": ("event.permission_requested", "warning", "permission", "waiting"),
            "blocked": ("event.blocked", "error", "turn", "blocked"),
            "turn_finished": ("event.turn_finished", "success", "turn", str(payload.get("status"))),
        }
        if event_type == "tool_call_finished":
            if payload.get("ok"):
                return ("event.tool_call_finished.ok", "success", "tool", "done", values)
            return ("event.tool_call_finished.failed", "error", "tool", "failed", values)
        if event_type == "verification_step_finished":
            if payload.get("ok"):
                return ("event.verification_step_finished.ok", "success", "verify", "passed", values)
            return ("event.verification_step_finished.failed", "error", "verify", "failed", values)
        if event_type in mapping:
            key, level, phase, status = mapping[event_type]
            return key, level, phase, status, values
        values.update({"event_type": event_type, "payload": payload})
        return "event.fallback", "info", "system", "info", values

    def tool_label(self, name: str) -> str:
        return self._t(f"tool.{name}", name=name)

    def _t(self, key: str, **values: object) -> str:
        template = self.catalog.get(key) or EN_US.get(key) or key
        return template.format(**values)


def normalize_locale(locale: str | None) -> str:
    normalized = (locale or "zh-CN").replace("_", "-").lower()
    if normalized.startswith("zh"):
        return "zh-CN"
    return "en-US"


def _details(text: str) -> str:
    compact = " ".join(text.split())
    if not compact:
        return ""
    if len(compact) > 220:
        compact = compact[:217] + "..."
    return " " + compact

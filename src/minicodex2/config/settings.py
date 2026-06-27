from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import os


@dataclass(slots=True)
class ModelSettings:
    provider: str = "openai_compatible"
    profile: str | None = None
    name: str | None = None
    base_url: str = "https://api.openai.com/v1"
    model: str = "default"
    wire_api: str = "chat"
    api_key_env: str | None = None
    api_key: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int = 120


@dataclass(slots=True)
class TimeoutSettings:
    run_command_seconds: int = 30
    verification_command_seconds: int = 60
    background_ready_seconds: int = 15
    full_turn_seconds: int = 300


@dataclass(slots=True)
class ContextSettings:
    trigger_auto_compact_limit_tokens: int = 50_000
    post_compact_ratio: float = 0.60
    baseline_ratio: float = 0.25
    model_context_limit_tokens: int = 128_000
    tool_result_raw_keep: int = 8
    tool_result_summary_chars: int = 700
    warning_threshold: float = 0.80
    hard_limit_threshold: float = 0.95
    runtime_summary_token_budget: int | None = None
    runtime_section_token_budget: int | None = None

    @property
    def budget_tokens(self) -> int:
        return self.trigger_auto_compact_limit_tokens

    @budget_tokens.setter
    def budget_tokens(self, value: int) -> None:
        self.trigger_auto_compact_limit_tokens = value

    @property
    def request_soft_limit_tokens(self) -> int:
        return self.trigger_auto_compact_limit_tokens

    @request_soft_limit_tokens.setter
    def request_soft_limit_tokens(self, value: int) -> None:
        self.trigger_auto_compact_limit_tokens = value

    @property
    def compression_threshold(self) -> float:
        return self.post_compact_ratio

    @compression_threshold.setter
    def compression_threshold(self, value: float) -> None:
        self.post_compact_ratio = value


@dataclass(slots=True)
class VerificationSettings:
    commands: list[str] = field(default_factory=list)
    prefer_project_scripts: bool = True
    allow_dependency_install: bool = False


@dataclass(slots=True)
class WebSearchSettings:
    enabled: bool = False
    provider: str = "duckduckgo_html"
    base_url: str = ""
    api_key_env: str | None = "BRAVE_SEARCH_API_KEY"
    api_key: str | None = None
    timeout_seconds: int = 15
    max_results: int = 8
    cache_ttl_seconds: int = 86_400
    user_agent: str = "MiniCodex2/0.1"


@dataclass(slots=True)
class SkillSettings:
    code_guidance_mode: str = "external"
    active_code_skill: str = "minicodex-code"
    external_dirs: list[Path] = field(default_factory=list)
    allow_third_party_primary: bool = False


@dataclass(slots=True)
class AgentSettings:
    max_verified_steps_per_turn: int = 8
    idle_tick_enabled: bool = True
    idle_tick_interval_seconds: int = 20
    idle_tick_max: int = 2
    memory_extraction_enabled: bool = False
    memory_extraction_max_events: int = 80
    context_dump_enabled: bool = False
    context_dump_max_chars_per_message: int = 20_000


@dataclass(slots=True)
class PathSettings:
    projects_root: Path | None = None
    artifact_dir: str = ".minicodex2"
    ignore: list[str] = field(
        default_factory=lambda: [".git", "node_modules", ".venv", "dist", "build", ".minicodex2"]
    )


@dataclass(slots=True)
class UiSettings:
    locale: str = "zh-CN"


@dataclass(slots=True)
class AppSettings:
    workspace_root: Path
    permission_mode: str = "auto"
    max_repair_rounds: int = 3
    model: ModelSettings = field(default_factory=ModelSettings)
    timeouts: TimeoutSettings = field(default_factory=TimeoutSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    verification: VerificationSettings = field(default_factory=VerificationSettings)
    web_search: WebSearchSettings = field(default_factory=WebSearchSettings)
    skills: SkillSettings = field(default_factory=SkillSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    paths: PathSettings = field(default_factory=PathSettings)
    ui: UiSettings = field(default_factory=UiSettings)

    @property
    def artifact_root(self) -> Path:
        return self.workspace_root / self.paths.artifact_dir


def _toml_load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    import tomllib

    with path.open("rb") as fh:
        return tomllib.load(fh)


class ConfigLoader:
    def load(
        self,
        workspace_root: str | Path,
        *,
        api_key: str | None = None,
        model_profile: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        wire_api: str | None = None,
        permission_mode: str | None = None,
        projects_root: str | Path | None = None,
    ) -> AppSettings:
        root = Path(workspace_root).resolve()
        data = _toml_load(root / "minicodex2.toml")
        settings = AppSettings(workspace_root=root)

        model_data = data.get("model", {})
        selected_profile = (
            model_profile
            or os.environ.get("MINICODEX2_MODEL_PROFILE")
            or data.get("model_profile")
            or data.get("default_model_profile")
            or model_data.get("profile")
        )
        profile_data = _selected_model_profile(data, selected_profile)
        if profile_data:
            settings.model.profile = str(selected_profile) if selected_profile else None
        _apply_model_data(settings.model, model_data)
        if profile_data:
            _apply_model_data(settings.model, profile_data)

        permissions = data.get("permissions", {})
        settings.permission_mode = permissions.get("mode", settings.permission_mode)

        repair = data.get("repair", {})
        settings.max_repair_rounds = int(repair.get("max_rounds", settings.max_repair_rounds))

        context = data.get("context", {})
        if "trigger_auto_compact_limit_tokens" in context:
            settings.context.trigger_auto_compact_limit_tokens = int(
                context["trigger_auto_compact_limit_tokens"]
            )
        if "request_soft_limit_tokens" in context:
            settings.context.trigger_auto_compact_limit_tokens = int(
                context["request_soft_limit_tokens"]
            )
        if "budget_tokens" in context:
            settings.context.trigger_auto_compact_limit_tokens = int(context["budget_tokens"])
        if "post_compact_ratio" in context:
            settings.context.post_compact_ratio = float(context["post_compact_ratio"])
        if "compression_threshold" in context:
            settings.context.post_compact_ratio = float(context["compression_threshold"])
        if "baseline_ratio" in context:
            settings.context.baseline_ratio = float(context["baseline_ratio"])
        if "model_context_limit_tokens" in context:
            settings.context.model_context_limit_tokens = int(context["model_context_limit_tokens"])
        if "tool_result_raw_keep" in context:
            settings.context.tool_result_raw_keep = max(0, int(context["tool_result_raw_keep"]))
        if "tool_result_summary_chars" in context:
            settings.context.tool_result_summary_chars = max(
                160,
                int(context["tool_result_summary_chars"]),
            )
        if "warning_threshold" in context:
            settings.context.warning_threshold = float(context["warning_threshold"])
        if "hard_limit_threshold" in context:
            settings.context.hard_limit_threshold = float(context["hard_limit_threshold"])
        if "runtime_summary_token_budget" in context:
            settings.context.runtime_summary_token_budget = max(
                500,
                int(context["runtime_summary_token_budget"]),
            )
        if "runtime_section_token_budget" in context:
            settings.context.runtime_section_token_budget = max(
                200,
                int(context["runtime_section_token_budget"]),
            )

        verification = data.get("verification", {})
        settings.verification.commands = list(verification.get("commands", []))
        settings.verification.prefer_project_scripts = bool(
            verification.get("prefer_project_scripts", settings.verification.prefer_project_scripts)
        )
        settings.verification.allow_dependency_install = bool(
            verification.get("allow_dependency_install", False)
        )

        web_search = data.get("web_search", {})
        if isinstance(web_search, dict):
            if "enabled" in web_search:
                settings.web_search.enabled = bool(web_search["enabled"])
            if "provider" in web_search:
                settings.web_search.provider = str(web_search["provider"])
            if "base_url" in web_search:
                settings.web_search.base_url = str(web_search["base_url"])
            if "api_key_env" in web_search:
                settings.web_search.api_key_env = str(web_search["api_key_env"])
            if "api_key" in web_search:
                settings.web_search.api_key = str(web_search["api_key"])
            if "timeout_seconds" in web_search:
                settings.web_search.timeout_seconds = max(1, int(web_search["timeout_seconds"]))
            if "max_results" in web_search:
                settings.web_search.max_results = max(1, int(web_search["max_results"]))
            if "cache_ttl_seconds" in web_search:
                settings.web_search.cache_ttl_seconds = max(
                    0,
                    int(web_search["cache_ttl_seconds"]),
                )
            if "user_agent" in web_search:
                settings.web_search.user_agent = str(web_search["user_agent"])

        skills = data.get("skills", {})
        if isinstance(skills, dict):
            if "code_guidance_mode" in skills:
                mode = str(skills["code_guidance_mode"]).strip().lower()
                if mode in {"legacy", "overlay", "external"}:
                    settings.skills.code_guidance_mode = mode
            if "active_code_skill" in skills:
                settings.skills.active_code_skill = str(skills["active_code_skill"]).strip()
            if "allow_third_party_primary" in skills:
                settings.skills.allow_third_party_primary = bool(skills["allow_third_party_primary"])
            raw_dirs = skills.get("external_dirs", [])
            if isinstance(raw_dirs, list):
                external_dirs: list[Path] = []
                for item in raw_dirs:
                    raw_path = str(item).strip()
                    if not raw_path:
                        continue
                    candidate = Path(raw_path).expanduser()
                    if not candidate.is_absolute():
                        candidate = root / candidate
                    external_dirs.append(candidate.resolve())
                settings.skills.external_dirs = external_dirs

        agent = data.get("agent", {})
        if "max_verified_steps_per_turn" in agent:
            settings.agent.max_verified_steps_per_turn = max(
                1,
                int(agent["max_verified_steps_per_turn"]),
            )
        if "idle_tick_enabled" in agent:
            settings.agent.idle_tick_enabled = bool(agent["idle_tick_enabled"])
        if "idle_tick_interval_seconds" in agent:
            settings.agent.idle_tick_interval_seconds = max(
                1,
                int(agent["idle_tick_interval_seconds"]),
            )
        if "idle_tick_max" in agent:
            settings.agent.idle_tick_max = max(0, int(agent["idle_tick_max"]))
        if "memory_extraction_enabled" in agent:
            settings.agent.memory_extraction_enabled = bool(agent["memory_extraction_enabled"])
        if "memory_extraction_max_events" in agent:
            settings.agent.memory_extraction_max_events = max(
                10,
                int(agent["memory_extraction_max_events"]),
            )
        if "context_dump_enabled" in agent:
            settings.agent.context_dump_enabled = bool(agent["context_dump_enabled"])
        if "context_dump_max_chars_per_message" in agent:
            settings.agent.context_dump_max_chars_per_message = max(
                1_000,
                int(agent["context_dump_max_chars_per_message"]),
            )

        paths = data.get("paths", {})
        configured_projects_root = projects_root or paths.get("projects_root") or None
        if configured_projects_root:
            settings.paths.projects_root = Path(configured_projects_root).resolve()
        settings.paths.artifact_dir = paths.get("artifact_dir", settings.paths.artifact_dir)

        ui = data.get("ui", {})
        settings.ui.locale = ui.get("locale", settings.ui.locale)

        timeouts = data.get("timeouts", {})
        for field_name in (
            "run_command_seconds",
            "verification_command_seconds",
            "background_ready_seconds",
            "full_turn_seconds",
        ):
            if field_name in timeouts:
                setattr(settings.timeouts, field_name, int(timeouts[field_name]))

        if model:
            settings.model.model = model
        if base_url:
            settings.model.base_url = base_url
        if wire_api:
            settings.model.wire_api = wire_api
        if permission_mode:
            settings.permission_mode = permission_mode
        settings.model.api_key = _resolve_api_key(settings.model, api_key=api_key)
        settings.web_search.api_key = _resolve_web_search_api_key(settings.web_search)
        return settings


def _selected_model_profile(
    data: dict[str, Any],
    selected_profile: object,
) -> dict[str, Any]:
    """Return one complete model endpoint profile from configuration.

    MiniCodex2 treats a profile as the user-facing unit of choice: model name,
    base URL, wire protocol, and key source live together.  This keeps frequent
    experiments like `deepseek_flash` vs `aihub_gpt55` out of fragile
    provider/model cross-references while still allowing a legacy [model] block.
    """

    if not selected_profile:
        return {}
    profiles = data.get("model_profiles", {})
    if not isinstance(profiles, dict):
        return {}
    profile = profiles.get(str(selected_profile), {})
    return profile if isinstance(profile, dict) else {}


def _apply_model_data(settings: ModelSettings, data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        return
    if "provider" in data:
        settings.provider = str(data["provider"])
    if "name" in data:
        settings.name = str(data["name"])
    if "base_url" in data:
        settings.base_url = str(data["base_url"])
    if "model" in data:
        settings.model = str(data["model"])
    if "wire_api" in data:
        settings.wire_api = str(data["wire_api"])
    if "api_key_env" in data:
        settings.api_key_env = str(data["api_key_env"])
    if "api_key" in data:
        settings.api_key = str(data["api_key"])
    if "reasoning_effort" in data:
        settings.reasoning_effort = str(data["reasoning_effort"])
    if "timeout_seconds" in data:
        settings.timeout_seconds = max(1, int(data["timeout_seconds"]))


def _resolve_api_key(settings: ModelSettings, *, api_key: str | None) -> str | None:
    if api_key:
        return api_key
    if settings.api_key:
        return settings.api_key
    if settings.api_key_env:
        return os.environ.get(settings.api_key_env)
    return None


def _resolve_web_search_api_key(settings: WebSearchSettings) -> str | None:
    if settings.api_key:
        return settings.api_key
    if settings.api_key_env:
        return os.environ.get(settings.api_key_env)
    return None

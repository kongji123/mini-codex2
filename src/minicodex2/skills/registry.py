from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
from pathlib import Path
from typing import Any
import copy
import json
import tomllib

from minicodex2.config.settings import AppSettings
from minicodex2.tools.results import ToolResult


CONTROLLED_DOMAINS = {
    "browser",
    "code",
    "data",
    "document",
    "finance",
    "general",
    "git",
    "memory",
    "planning",
    "web_search",
}

VALID_ROLES = {"primary", "reference", "candidate"}


@dataclass(frozen=True, slots=True)
class SkillHook:
    name: str
    description: str = ""
    reference: str | None = None
    python: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "reference": self.reference,
            "python": self.python,
        }


@dataclass(frozen=True, slots=True)
class SkillHookContext:
    domain: str
    hook: str
    skill: dict[str, Any]
    variables: dict[str, Any]
    api: "SkillPluginApi"


@dataclass(frozen=True, slots=True)
class RenderedSkillHook:
    content: str
    metadata: dict[str, Any]


class SkillPluginApi:
    """Small Agent OS facade exposed to skill hook code."""

    def __init__(self, registry: "SkillRegistry") -> None:
        self._registry = registry

    def list_skills(self, domain: str = "", role: str = "") -> dict[str, Any]:
        result = self._registry.list_skills(domain=domain, role=role)
        try:
            return json.loads(result.content)
        except json.JSONDecodeError:
            return {"ok": result.ok, "content": result.content}

    def load_skill(self, name: str, reference: str = "") -> str:
        result = self._registry.load_skill(name, reference)
        if not result.ok:
            raise RuntimeError(result.content)
        return result.content


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    name: str
    domain: str
    role: str
    description: str
    capabilities: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    hooks: tuple[SkillHook, ...] = ()
    priority: int = 0
    source: str = "external"
    trust_level: str = "untrusted"
    path: Path | None = None
    package: str | None = None
    status: str = "available"
    manifest_warnings: tuple[str, ...] = ()

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": self.domain,
            "role": self.role,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "references": list(self.references),
            "hooks": [hook.metadata() for hook in self.hooks],
            "priority": self.priority,
            "source": self.source,
            "trust_level": self.trust_level,
            "status": self.status,
            "path": str(self.path) if self.path else None,
            "package": self.package,
            "warnings": list(self.manifest_warnings),
        }


@dataclass(frozen=True, slots=True)
class SkillSelection:
    active_by_domain: dict[str, str] = field(default_factory=dict)
    references_by_domain: dict[str, tuple[str, ...]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_by_domain": dict(self.active_by_domain),
            "references_by_domain": {
                key: list(value) for key, value in self.references_by_domain.items()
            },
            "warnings": list(self.warnings),
        }


class SkillRegistry:
    """Manifest-driven skill catalog for Agent OS.

    This registry intentionally does not read skill bodies to decide conflicts.
    Skill bodies are natural-language instructions for the model; manifests are
    the machine-readable contract Agent OS uses for domain/role selection.
    """

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._skills = _load_all_skills(settings)
        self._selection = _select_active_skills(self._skills, settings)
        self._legacy_code_guidance_enabled = _legacy_code_guidance_enabled(
            settings,
            self._selection,
        )
        self._summary = {
            "code_guidance_mode": self.settings.skills.code_guidance_mode,
            "active_code_skill": self.settings.skills.active_code_skill,
            "legacy_code_guidance_enabled": self._legacy_code_guidance_enabled,
            "selection": self._selection.to_dict(),
            "skills": self.list_metadata(),
        }

    @property
    def selection(self) -> SkillSelection:
        return self._selection

    @property
    def legacy_code_guidance_enabled(self) -> bool:
        return self._legacy_code_guidance_enabled

    def list_metadata(self) -> list[dict[str, Any]]:
        return [skill.metadata() for skill in self._skills]

    def summary(self) -> dict[str, Any]:
        # This summary is injected into the stable prefix of model context.
        # Freeze it at registry construction time so repeated turns do not
        # accidentally perturb provider prompt-cache prefixes.
        return copy.deepcopy(self._summary)

    def list_skills(
        self,
        domain: str = "",
        role: str = "",
        include_references: bool = True,
    ) -> ToolResult:
        domain = domain.strip()
        role = role.strip()
        filtered = []
        for skill in self._skills:
            if domain and skill.domain != domain:
                continue
            if role and skill.role != role:
                continue
            if not include_references and skill.role == "reference":
                continue
            filtered.append(skill.metadata())
        payload = {
            "selection": self._selection.to_dict(),
            "skills": filtered,
        }
        return ToolResult(ok=True, content=json.dumps(payload, ensure_ascii=False, indent=2))

    def load_skill(self, name: str, reference: str = "") -> ToolResult:
        normalized = name.strip()
        skill = next((item for item in self._skills if item.name == normalized), None)
        if skill is None:
            return ToolResult(
                ok=False,
                content=f"unknown skill: {name}",
                blocked=True,
                block_reason="unknown skill",
                metadata={
                    "failure_kind": "unknown_skill",
                    "available_skills": [item.name for item in self._skills],
                },
            )
        if skill.name == "legacy-builtin-code":
            content = _legacy_builtin_skill_body()
            return ToolResult(
                ok=True,
                content=content,
                metadata={"skill": skill.metadata(), "reference": None},
            )
        if skill.path is None:
            return ToolResult(
                ok=False,
                content=f"skill has no readable path: {skill.name}",
                blocked=True,
                block_reason="skill has no readable path",
            )
        try:
            target = _resolve_skill_resource(skill.path, reference)
            content = target.read_text(encoding="utf-8")
        except Exception as exc:
            metadata = {
                "skill": skill.metadata(),
                "reference": reference or None,
                "failure_kind": "skill_load_failed",
            }
            if skill.references:
                metadata["available_references"] = list(skill.references)
            return ToolResult(
                ok=False,
                content=f"failed to load skill {skill.name}: {exc}",
                blocked=True,
                block_reason="failed to load skill",
                metadata=metadata,
            )
        return ToolResult(
            ok=True,
            content=content,
            metadata={"skill": skill.metadata(), "reference": reference or None},
        )

    def render_hook(
        self,
        *,
        domain: str,
        hook: str,
        legacy_content: str,
        variables: dict[str, Any] | None = None,
    ) -> str:
        return self.render_hook_with_metadata(
            domain=domain,
            hook=hook,
            legacy_content=legacy_content,
            variables=variables,
        ).content

    def render_hook_with_metadata(
        self,
        *,
        domain: str,
        hook: str,
        legacy_content: str,
        variables: dict[str, Any] | None = None,
    ) -> RenderedSkillHook:
        active_name = self._selection.active_by_domain.get(domain, "")
        active_skill = next((item for item in self._skills if item.name == active_name), None)
        if active_skill is None or active_skill.name == "legacy-builtin-code":
            return RenderedSkillHook(
                content=legacy_content,
                metadata={
                    "domain": domain,
                    "hook": hook,
                    "active_skill": active_name or "none",
                    "mode": "legacy",
                    "python_hook": None,
                    "used_python_hook": False,
                },
            )
        skill_hook = next((item for item in active_skill.hooks if item.name == hook), None)
        if skill_hook is None:
            return RenderedSkillHook(
                content=legacy_content,
                metadata={
                    "domain": domain,
                    "hook": hook,
                    "active_skill": active_skill.name,
                    "mode": "legacy_fallback_missing_hook",
                    "python_hook": None,
                    "used_python_hook": False,
                },
            )
        code_result = self._run_python_hook(
            active_skill,
            skill_hook,
            domain=domain,
            hook_name=hook,
            variables=variables or {},
        )
        if code_result:
            return RenderedSkillHook(
                content=code_result,
                metadata={
                    "domain": domain,
                    "hook": hook,
                    "active_skill": active_skill.name,
                    "mode": "python_hook",
                    "reference": skill_hook.reference,
                    "python_hook": skill_hook.python,
                    "used_python_hook": True,
                },
            )
        lines = [
            f"[SKILL HOOK: {domain}.{hook}]",
            f"- ActiveSkill: {active_skill.name}",
            "- Runtime supplies facts, tools, safety, logs, evidence, memory, and persistence.",
            "- The active skill supplies workflow guidance for this hook.",
        ]
        if skill_hook.description:
            lines.append(f"- HookDescription: {skill_hook.description}")
        if skill_hook.reference:
            lines.append(
                f"- LoadGuidance: call load_skill(name='{active_skill.name}', reference='{skill_hook.reference}') when this hook needs detailed workflow guidance."
            )
        if variables:
            compact_variables = {
                key: value
                for key, value in variables.items()
                if value not in (None, "", [], {})
            }
            if compact_variables:
                lines.append("- HookVariables: " + json.dumps(
                    compact_variables,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )[:1200])
        return RenderedSkillHook(
            content="\n".join(lines),
            metadata={
                "domain": domain,
                "hook": hook,
                "active_skill": active_skill.name,
                "mode": "prompt_hook",
                "reference": skill_hook.reference,
                "python_hook": skill_hook.python,
                "used_python_hook": False,
            },
        )

    def _run_python_hook(
        self,
        skill: SkillDefinition,
        hook: SkillHook,
        *,
        domain: str,
        hook_name: str,
        variables: dict[str, Any],
    ) -> str | None:
        if not hook.python or skill.path is None:
            return None
        if not _python_hook_execution_allowed(skill, self.settings):
            return None
        try:
            function = _load_python_hook_function(skill.path, hook.python)
            context = SkillHookContext(
                domain=domain,
                hook=hook_name,
                skill=skill.metadata(),
                variables=copy.deepcopy(variables),
                api=SkillPluginApi(self),
            )
            result = function(context)
        except Exception as exc:
            return (
                f"[SKILL HOOK ERROR: {domain}.{hook_name}]\n"
                f"- ActiveSkill: {skill.name}\n"
                f"- PythonHook: {hook.python}\n"
                f"- Error: {type(exc).__name__}: {exc}\n"
                "- Runtime fell back to model-driven recovery. Use load_skill for guidance or switch to legacy mode if needed."
            )
        if result is None:
            return None
        if isinstance(result, str):
            return result
        if isinstance(result, dict) and isinstance(result.get("content"), str):
            return str(result["content"])
        return str(result)


def _load_all_skills(settings: AppSettings) -> list[SkillDefinition]:
    skills = [_legacy_builtin_code_skill(settings)]
    builtin_root = Path(__file__).resolve().parent / "builtin"
    skills.extend(_load_skill_dirs([builtin_root], source="builtin", trust_level="trusted"))
    skills.extend(
        _load_skill_dirs(
            settings.skills.external_dirs,
            source="external",
            trust_level="untrusted",
        )
    )
    return _dedupe_skills(skills)


def _legacy_builtin_code_skill(settings: AppSettings) -> SkillDefinition:
    role = "primary" if settings.skills.code_guidance_mode in {"legacy", "overlay"} else "reference"
    status = "active" if role == "primary" else "available"
    return SkillDefinition(
        name="legacy-builtin-code",
        domain="code",
        role=role,
        priority=100,
        source="legacy_runtime",
        trust_level="trusted",
        status=status,
        description=(
            "Current MiniCodex2 built-in coding workflow implemented inside runtime/context "
            "guidance. It remains the active code primary in legacy and overlay modes."
        ),
        capabilities=(
            "code_edit_loop",
            "verification_after_write",
            "failure_repair",
            "work_plan",
            "evidence_tracking",
            "integration_debug_guidance",
            "workflow_memory_guidance",
        ),
        package="minicodex2",
    )


def _load_skill_dirs(
    roots: list[Path],
    *,
    source: str,
    trust_level: str,
) -> list[SkillDefinition]:
    loaded: list[SkillDefinition] = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        candidates: list[Path] = []
        if (root / "SKILL.md").is_file():
            candidates.append(root)
        candidates.extend(
            path for path in sorted(root.iterdir(), key=lambda item: item.name.lower())
            if (path / "SKILL.md").is_file()
        )
        for candidate in candidates:
            loaded.append(_load_skill_dir(candidate, source=source, trust_level=trust_level))
    return loaded


def _load_skill_dir(path: Path, *, source: str, trust_level: str) -> SkillDefinition:
    manifest = _load_manifest(path)
    warnings: list[str] = []
    name = str(manifest.get("name") or path.name).strip()
    domain = str(manifest.get("domain") or "general").strip()
    role = str(manifest.get("role") or "reference").strip()
    description = str(manifest.get("description") or _frontmatter_description(path) or "").strip()
    capabilities = _string_list(manifest.get("capabilities"))
    references = _skill_references(path, manifest, warnings)
    hooks = _skill_hooks(manifest, warnings)
    priority = _safe_int(manifest.get("priority"), 0)
    if not name:
        name = path.name
    if not _valid_domain(domain):
        warnings.append(f"unknown domain '{domain}'; treating as reference-only")
        role = "reference"
    if role not in VALID_ROLES:
        warnings.append(f"invalid role '{role}'; using reference")
        role = "reference"
    if not description:
        warnings.append("missing description; model selection may be unreliable")
        description = f"Skill package {name}."
    return SkillDefinition(
        name=name,
        domain=domain,
        role=role,
        description=description,
        capabilities=tuple(capabilities),
        references=tuple(references),
        hooks=tuple(hooks),
        priority=priority,
        source=source,
        trust_level=str(manifest.get("trust_level") or trust_level),
        path=path,
        package=str(manifest.get("package") or name),
        manifest_warnings=tuple(warnings),
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    for filename in ("skill.toml", "manifest.toml"):
        candidate = path / filename
        if candidate.is_file():
            with candidate.open("rb") as fh:
                data = tomllib.load(fh)
            return data.get("skill", data) if isinstance(data, dict) else {}
    frontmatter = _read_frontmatter(path / "SKILL.md")
    return frontmatter


def _read_frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data: dict[str, Any] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if value.startswith("[") and value.endswith("]"):
            data[key] = [item.strip().strip("'\"") for item in value.strip("[]").split(",") if item.strip()]
        else:
            data[key] = value.strip("'\"")
    return data


def _frontmatter_description(path: Path) -> str:
    return str(_read_frontmatter(path / "SKILL.md").get("description") or "")


def _skill_references(path: Path, manifest: dict[str, Any], warnings: list[str]) -> list[str]:
    explicit = _string_list(manifest.get("references"))
    if explicit:
        references: list[str] = []
        for item in explicit:
            try:
                references.append(_safe_relative_reference(item))
            except ValueError as exc:
                warnings.append(str(exc))
        return sorted(references)
    reference_root = path / "references"
    if not reference_root.is_dir():
        return []
    references: list[str] = []
    for candidate in sorted(reference_root.rglob("*.md"), key=lambda item: item.as_posix().lower()):
        try:
            relative = candidate.relative_to(path).as_posix()
        except ValueError:
            continue
        references.append(relative)
    return references


def _safe_relative_reference(reference: str) -> str:
    normalized = reference.strip().replace("\\", "/")
    if not normalized or normalized.startswith("/") or ".." in Path(normalized).parts:
        raise ValueError(f"invalid skill reference path: {reference}")
    return normalized


def _skill_hooks(manifest: dict[str, Any], warnings: list[str]) -> list[SkillHook]:
    raw_hooks = manifest.get("hooks")
    if not isinstance(raw_hooks, dict):
        return []
    hooks: list[SkillHook] = []
    for name, raw_hook in sorted(raw_hooks.items()):
        hook_name = str(name).strip()
        if not hook_name:
            warnings.append("empty hook name ignored")
            continue
        description = ""
        reference: str | None = None
        python: str | None = None
        if isinstance(raw_hook, dict):
            description = str(raw_hook.get("description") or "").strip()
            raw_reference = raw_hook.get("reference")
            if raw_reference:
                try:
                    reference = _safe_relative_reference(str(raw_reference))
                except ValueError as exc:
                    warnings.append(str(exc))
            raw_python = raw_hook.get("python")
            if raw_python:
                try:
                    python = _safe_python_hook_spec(str(raw_python))
                except ValueError as exc:
                    warnings.append(str(exc))
        elif isinstance(raw_hook, str):
            description = raw_hook.strip()
        else:
            warnings.append(f"invalid hook '{hook_name}' ignored")
            continue
        hooks.append(SkillHook(
            name=hook_name,
            description=description,
            reference=reference,
            python=python,
        ))
    return hooks


def _safe_python_hook_spec(spec: str) -> str:
    normalized = spec.strip().replace("\\", "/")
    if ":" not in normalized:
        raise ValueError(f"invalid python hook spec: {spec}")
    module_path, function_name = normalized.split(":", 1)
    _safe_relative_reference(module_path)
    if not module_path.endswith(".py"):
        raise ValueError(f"python hook must point to a .py file: {spec}")
    if not function_name.isidentifier():
        raise ValueError(f"invalid python hook function name: {spec}")
    return f"{module_path}:{function_name}"


def _python_hook_execution_allowed(skill: SkillDefinition, settings: AppSettings) -> bool:
    if skill.source == "builtin" and skill.trust_level == "trusted":
        return True
    if skill.trust_level == "trusted" and settings.skills.allow_third_party_primary:
        return True
    return False


def _load_python_hook_function(skill_root: Path, spec: str):
    module_path, function_name = spec.split(":", 1)
    target = _resolve_skill_resource(skill_root, module_path)
    module_name = f"_minicodex2_skill_hook_{abs(hash((str(target), function_name)))}"
    module_spec = importlib.util.spec_from_file_location(module_name, target)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"cannot load hook module: {module_path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    function = getattr(module, function_name, None)
    if not callable(function):
        raise AttributeError(f"hook function not found: {function_name}")
    return function


def _select_active_skills(skills: list[SkillDefinition], settings: AppSettings) -> SkillSelection:
    warnings: list[str] = []
    active_by_domain: dict[str, str] = {}
    references_by_domain: dict[str, list[str]] = {}
    by_domain: dict[str, list[SkillDefinition]] = {}
    for skill in skills:
        by_domain.setdefault(skill.domain, []).append(skill)
        if skill.role != "primary":
            references_by_domain.setdefault(skill.domain, []).append(skill.name)
    for domain, domain_skills in by_domain.items():
        primary_candidates = [skill for skill in domain_skills if skill.role == "primary"]
        if domain == "code":
            desired = _desired_code_primary(settings)
            selected = next((skill for skill in domain_skills if skill.name == desired), None)
            if selected is None:
                selected = _highest_priority(primary_candidates)
                warnings.append(
                    f"configured active code skill '{desired}' was not found; "
                    f"using '{selected.name if selected else 'none'}'"
                )
            if selected:
                if (
                    selected.source == "external"
                    and selected.name == settings.skills.active_code_skill
                    and not settings.skills.allow_third_party_primary
                ):
                    warnings.append(
                        f"external code skill '{selected.name}' is explicitly configured as primary; "
                        "Agent OS will allow it, but third-party primary skills are otherwise disabled"
                    )
                active_by_domain[domain] = selected.name
                references_by_domain[domain] = [
                    skill.name for skill in domain_skills if skill.name != selected.name
                ]
            if len(primary_candidates) > 1:
                warnings.append(
                    f"multiple primary skills for domain '{domain}': "
                    + ", ".join(skill.name for skill in primary_candidates)
                )
            continue
        allowed_primary_candidates = [
            skill
            for skill in primary_candidates
            if _primary_skill_allowed(skill, settings=settings)
        ]
        blocked_primary_candidates = [
            skill
            for skill in primary_candidates
            if skill not in allowed_primary_candidates
        ]
        for skill in blocked_primary_candidates:
            references_by_domain.setdefault(domain, []).append(skill.name)
            warnings.append(
                f"external primary skill '{skill.name}' for domain '{domain}' was not activated; "
                "set allow_third_party_primary=true only after explicitly trusting the package"
            )
        selected = _highest_priority(allowed_primary_candidates)
        if selected:
            active_by_domain[domain] = selected.name
            references_by_domain[domain] = [
                skill.name for skill in domain_skills if skill.name != selected.name
            ]
        if len(primary_candidates) > 1:
            warnings.append(
                f"multiple primary skills for domain '{domain}': "
                + ", ".join(skill.name for skill in primary_candidates)
            )
    return SkillSelection(
        active_by_domain=active_by_domain,
        references_by_domain={key: tuple(value) for key, value in references_by_domain.items()},
        warnings=tuple(warnings),
    )


def _primary_skill_allowed(skill: SkillDefinition, *, settings: AppSettings) -> bool:
    if skill.source != "external":
        return True
    return settings.skills.allow_third_party_primary


def _desired_code_primary(settings: AppSettings) -> str:
    if settings.skills.code_guidance_mode in {"legacy", "overlay"}:
        return "legacy-builtin-code"
    configured = settings.skills.active_code_skill.strip()
    return configured if configured and configured != "legacy-builtin-code" else "minicodex-code"


def _legacy_code_guidance_enabled(settings: AppSettings, selection: SkillSelection) -> bool:
    active_code_skill = selection.active_by_domain.get(
        "code",
        settings.skills.active_code_skill or "legacy-builtin-code",
    )
    return (
        settings.skills.code_guidance_mode != "external"
        or active_code_skill == "legacy-builtin-code"
    )


def _highest_priority(skills: list[SkillDefinition]) -> SkillDefinition | None:
    if not skills:
        return None
    return sorted(skills, key=lambda skill: (skill.priority, skill.source == "builtin"), reverse=True)[0]


def _dedupe_skills(skills: list[SkillDefinition]) -> list[SkillDefinition]:
    result: dict[str, SkillDefinition] = {}
    for skill in skills:
        if skill.name not in result or skill.priority >= result[skill.name].priority:
            result[skill.name] = skill
    return [result[name] for name in sorted(result)]


def _resolve_skill_resource(skill_root: Path, reference: str) -> Path:
    if not reference:
        return skill_root / "SKILL.md"
    candidate = (skill_root / reference).resolve()
    root = skill_root.resolve()
    if not (candidate == root or root in candidate.parents):
        raise ValueError("skill reference escapes skill root")
    if not candidate.is_file():
        raise FileNotFoundError(reference)
    return candidate


def _valid_domain(domain: str) -> bool:
    if domain in CONTROLLED_DOMAINS:
        return True
    return "." in domain and all(part for part in domain.split("."))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _legacy_builtin_skill_body() -> str:
    return """# legacy-builtin-code

This is not an external SKILL.md package. It represents the current MiniCodex2
code guidance that still lives inside runtime/context Python code.

It is registered so Agent OS can reason about code-domain conflicts while the
implementation is gradually migrated into external Code Skill packages.

Runtime hard policy remains outside this skill: path safety, permissions,
timeouts, background process control, tool schema validation, event logs,
context buffer, memory store, write-after-verify gates, and failure feedback.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from pathlib import Path
from typing import Any
import tomllib


@dataclass(frozen=True, slots=True)
class PluginDefinition:
    name: str
    domain: str
    description: str
    tools: tuple[str, ...] = ()
    tool_registrar: str | None = None
    source: str = "external"
    trust_level: str = "untrusted"
    path: Path | None = None
    manifest_warnings: tuple[str, ...] = ()

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": self.domain,
            "description": self.description,
            "tools": list(self.tools),
            "tool_registrar": self.tool_registrar,
            "source": self.source,
            "trust_level": self.trust_level,
            "path": str(self.path) if self.path else None,
            "warnings": list(self.manifest_warnings),
        }


def load_builtin_plugins() -> list[PluginDefinition]:
    return _load_plugin_dirs([_builtin_plugins_root()], source="builtin", trust_level="trusted")


def register_builtin_plugin_tools(registry: Any, runtime_tools: Any) -> list[dict[str, Any]]:
    """Register tool calls declared by trusted built-in plugin manifests."""
    registered: list[dict[str, Any]] = []
    for plugin in load_builtin_plugins():
        if not _tool_registration_allowed(plugin):
            continue
        if not plugin.tool_registrar:
            continue
        registrar = _load_tool_registrar(plugin)
        before = set(getattr(registry, "_tools", {}).keys())
        registrar(registry, runtime_tools)
        after = set(getattr(registry, "_tools", {}).keys())
        registered.append({
            "plugin": plugin.name,
            "domain": plugin.domain,
            "tools": sorted(after - before) or list(plugin.tools),
            "tool_registrar": plugin.tool_registrar,
        })
    return registered


def _builtin_plugins_root() -> Path:
    return Path(__file__).resolve().parent / "builtin"


def _load_plugin_dirs(
    roots: list[Path],
    *,
    source: str,
    trust_level: str,
) -> list[PluginDefinition]:
    plugins: list[PluginDefinition] = []
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if not candidate.is_dir():
                continue
            plugin = _load_plugin(candidate, source=source, trust_level=trust_level)
            if plugin:
                plugins.append(plugin)
    return plugins


def _load_plugin(path: Path, *, source: str, trust_level: str) -> PluginDefinition | None:
    manifest_path = path / "plugin.toml"
    if not manifest_path.is_file():
        return None
    warnings: list[str] = []
    with manifest_path.open("rb") as fh:
        data = tomllib.load(fh)
    manifest = data.get("plugin", data) if isinstance(data, dict) else {}
    if not isinstance(manifest, dict):
        return None
    name = str(manifest.get("name") or path.name).strip()
    domain = str(manifest.get("domain") or "general").strip()
    description = str(manifest.get("description") or f"Plugin package {name}.").strip()
    tools = tuple(_string_list(manifest.get("tools")))
    registrar: str | None = None
    raw_registrar = manifest.get("tool_registrar")
    if raw_registrar:
        try:
            registrar = _safe_registrar_spec(str(raw_registrar))
        except ValueError as exc:
            warnings.append(str(exc))
    return PluginDefinition(
        name=name,
        domain=domain,
        description=description,
        tools=tools,
        tool_registrar=registrar,
        source=str(manifest.get("source") or source),
        trust_level=str(manifest.get("trust_level") or trust_level),
        path=path,
        manifest_warnings=tuple(warnings),
    )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _safe_registrar_spec(spec: str) -> str:
    normalized = spec.strip().replace("\\", "/")
    if ":" not in normalized:
        raise ValueError(f"invalid plugin tool registrar spec: {spec}")
    module_path, function_name = normalized.split(":", 1)
    _safe_relative_path(module_path)
    if not module_path.endswith(".py"):
        raise ValueError(f"plugin tool registrar must point to a .py file: {spec}")
    if not function_name.isidentifier():
        raise ValueError(f"invalid plugin tool registrar function name: {spec}")
    return f"{module_path}:{function_name}"


def _safe_relative_path(reference: str) -> str:
    normalized = reference.strip().replace("\\", "/")
    if not normalized or normalized.startswith("/") or ".." in Path(normalized).parts:
        raise ValueError(f"invalid plugin path: {reference}")
    return normalized


def _tool_registration_allowed(plugin: PluginDefinition) -> bool:
    return plugin.source == "builtin" and plugin.trust_level == "trusted"


def _load_tool_registrar(plugin: PluginDefinition):
    if plugin.path is None or plugin.tool_registrar is None:
        raise ImportError(f"plugin {plugin.name} has no tool registrar")
    module_path, function_name = plugin.tool_registrar.split(":", 1)
    target = (plugin.path / module_path).resolve()
    try:
        target.relative_to(plugin.path.resolve())
    except ValueError as exc:
        raise ImportError(f"plugin registrar escapes plugin root: {module_path}") from exc
    module_name = f"_minicodex2_plugin_tools_{abs(hash((str(target), function_name)))}"
    module_spec = importlib.util.spec_from_file_location(module_name, target)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"cannot load plugin tool registrar: {module_path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    function = getattr(module, function_name, None)
    if not callable(function):
        raise AttributeError(f"plugin tool registrar not found: {function_name}")
    return function

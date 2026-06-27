from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from minicodex2.tools.results import ToolResult


@dataclass(slots=True)
class RegisteredTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., ToolResult]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name not in self._tools:
            available = ", ".join(sorted(self._tools))
            return ToolResult(
                ok=False,
                content=(
                    f"unknown tool: {name}. Use one of the available tools instead: {available}. "
                    "For grep-like code search, use search_files; for exact code context, use read_file."
                ),
                blocked=False,
                block_reason="unknown tool",
                metadata={
                    "failure_kind": "unknown_tool",
                    "tool": name,
                    "available_tools": sorted(self._tools),
                    "recoverable": True,
                },
            )
        tool = self._tools[name]
        if not isinstance(args, dict):
            return ToolResult(
                ok=False,
                content=f"invalid tool arguments for {name}: expected object",
                blocked=True,
                block_reason="invalid tool arguments",
                metadata={"failure_kind": "invalid_tool_arguments", "tool": name},
            )
        missing = [
            str(key)
            for key in tool.parameters.get("required", [])
            if isinstance(key, str) and key not in args
        ]
        if missing:
            missing_text = ", ".join(missing)
            return ToolResult(
                ok=False,
                content=f"missing required tool arguments for {name}: {missing_text}",
                blocked=True,
                block_reason=f"missing required tool arguments: {missing_text}",
                metadata={
                    "failure_kind": "invalid_tool_arguments",
                    "tool": name,
                    "missing_arguments": missing,
                },
            )
        try:
            return tool.handler(**args)
        except TypeError as exc:
            return ToolResult(
                ok=False,
                content=f"invalid tool arguments for {name}: {exc}",
                blocked=True,
                block_reason="invalid tool arguments",
                metadata={"failure_kind": "invalid_tool_arguments", "tool": name},
            )


def build_runtime_tool_registry(runtime_tools: Any) -> ToolRegistry:
    from minicodex2.plugins.registry import register_builtin_plugin_tools

    registry = ToolRegistry()
    string = {"type": "string"}
    integer = {"type": "integer"}
    boolean = {"type": "boolean"}
    registry.register(
        RegisteredTool(
            "list_directory",
            (
                "List files under a workspace directory. Defaults to one level to save tokens. "
                "Use recursive=true with a small max_depth when you need a compact project tree."
            ),
            {
                "type": "object",
                "properties": {
                    "path": string,
                    "recursive": boolean,
                    "max_depth": integer,
                    "max_entries": integer,
                    "include_hidden": boolean,
                    "include_metadata": boolean,
                    "glob": string,
                },
                "required": [],
            },
            runtime_tools.list_directory,
        )
    )
    registry.register(
        RegisteredTool(
            "read_file",
            "Read a UTF-8 text file from the workspace. Use start_line/end_line for code snippets, or offset/limit byte ranges for continuing a partial read.",
            {
                "type": "object",
                "properties": {
                    "path": string,
                    "max_bytes": integer,
                    "offset": integer,
                    "limit": integer,
                    "start_line": integer,
                    "end_line": integer,
                },
                "required": ["path"],
            },
            runtime_tools.read_file,
        )
    )
    registry.register(
        RegisteredTool(
            "search_files",
            (
                "Search workspace text files for a symbol, route, class, config key, or error string. "
                "Use this instead of raw grep/findstr when locating project code. It skips common "
                "dependency/build/cache directories such as node_modules, venv, .git, target, build, and .minicodex2."
            ),
            {
                "type": "object",
                "properties": {
                    "query": string,
                    "root": string,
                    "path": string,
                    "glob": string,
                    "case_sensitive": boolean,
                    "max_results": integer,
                    "recursive": boolean,
                    "recurse": boolean,
                },
                "required": ["query"],
            },
            runtime_tools.search_files,
        )
    )
    registry.register(
        RegisteredTool(
            "find_images",
            "Find image files in the workspace before calling view_image. Use this when the user refers to a screenshot/image by natural language, partial name, or recency.",
            {
                "type": "object",
                "properties": {
                    "query": string,
                    "root": string,
                    "latest_only": boolean,
                    "max_results": integer,
                },
                "required": [],
            },
            runtime_tools.find_images,
        )
    )
    registry.register(
        RegisteredTool(
            "view_image",
            "Attach a local workspace image so the model can visually inspect it. Use a path returned by find_images or list_directory.",
            {
                "type": "object",
                "properties": {
                    "path": string,
                    "detail": {
                        "type": "string",
                        "enum": ["low", "high", "original"],
                    },
                },
                "required": ["path"],
            },
            runtime_tools.view_image,
        )
    )
    registry.register(
        RegisteredTool(
            "write_file",
            "Create or fully replace a file in the workspace.",
            {
                "type": "object",
                "properties": {"path": string, "content": string},
                "required": ["path", "content"],
            },
            runtime_tools.write_file,
        )
    )
    registry.register(
        RegisteredTool(
            "edit_file",
            (
                "Edit a workspace file. Prefer start_line/end_line/content for large files or when you "
                "just read the target range. Use old_text/new_text only for small exact replacements."
            ),
            {
                "type": "object",
                "properties": {
                    "path": string,
                    "old_text": string,
                    "new_text": string,
                    "start_line": integer,
                    "end_line": integer,
                    "content": string,
                },
                "required": ["path"],
            },
            runtime_tools.edit_file,
        )
    )
    registry.register(
        RegisteredTool(
            "delete_file",
            "Delete one file in the workspace.",
            {"type": "object", "properties": {"path": string}, "required": ["path"]},
            runtime_tools.delete_file,
        )
    )
    registry.register(
        RegisteredTool(
            "run_command",
            "Run a local workspace command expected to exit, including .bat/.cmd/.ps1 helpers that finish. Use this when the user asks to run, test, install project dependencies, or execute a script. Set purpose to verify/build/test/smoke/check/lint when the command is intended as acceptance evidence; use inspect/install/generic otherwise. For dependency installs or slow builds, set timeout_seconds explicitly. Do not use this for long-running servers or port/process inspection/cleanup; use start_background_command, inspect_port, and release_port.",
            {
                "type": "object",
                "properties": {
                    "command": string,
                    "cwd": string,
                    "timeout_seconds": integer,
                    "purpose": {
                        "type": "string",
                        "enum": [
                            "generic",
                            "inspect",
                            "install",
                            "verify",
                            "build",
                            "test",
                            "smoke",
                            "check",
                            "lint",
                        ],
                    },
                },
                "required": ["command"],
            },
            runtime_tools.run_command,
        )
    )
    registry.register(
        RegisteredTool(
            "run_python",
            "Run a short Python helper script in the workspace for structured file analysis, data conversion, or report generation. Do not use this for long-running servers, package/toolchain installation, port cleanup, or bypassing dedicated tools. For HTTP/API smoke checks prefer http_request; do not import third-party modules such as requests unless the selected project interpreter already has them installed. For Django helpers, prefer manage.py commands or set cwd to the directory containing manage.py.",
            {
                "type": "object",
                "properties": {"code": string, "cwd": string, "timeout_seconds": integer},
                "required": ["code"],
            },
            runtime_tools.run_python,
        )
    )
    registry.register(
        RegisteredTool(
            "http_request",
            "Send one HTTP request and return structured status, headers, body excerpt, and connection errors. Use this instead of curl for HTTP smoke checks and HTTP failure targets.",
            {
                "type": "object",
                "properties": {
                    "url": string,
                    "method": string,
                    "headers": {"type": "object", "additionalProperties": string},
                    "body": string,
                    "timeout_seconds": integer,
                    "purpose": {
                        "type": "string",
                        "enum": ["generic", "inspect", "verify", "smoke"],
                    },
                },
                "required": ["url"],
            },
            runtime_tools.http_request,
        )
    )
    registry.register(
        RegisteredTool(
            "web_search",
            (
                "Search the public web for current or external information. Use this only when local "
                "project files are insufficient or the user asks to look something up. This returns "
                "candidate URLs and snippets; call fetch_web_page on selected public URLs for page text."
            ),
            {
                "type": "object",
                "properties": {
                    "query": string,
                    "max_results": integer,
                    "freshness": string,
                    "domains": {"type": "array", "items": string},
                },
                "required": ["query"],
            },
            runtime_tools.web_search,
        )
    )
    registry.register(
        RegisteredTool(
            "fetch_web_page",
            (
                "Fetch and extract text from a public HTTP/HTTPS web page. This blocks localhost and "
                "private network targets; use http_request or browser_test for local services."
            ),
            {
                "type": "object",
                "properties": {
                    "url": string,
                    "max_chars": integer,
                },
                "required": ["url"],
            },
            runtime_tools.fetch_web_page,
        )
    )
    register_builtin_plugin_tools(registry, runtime_tools)
    registry.register(
        RegisteredTool(
            "start_background_command",
            "Start a long-running server command in the background, such as Django runserver, Flask, FastAPI/uvicorn, Streamlit, Vite, or a dev server. Use this instead of run_command for commands that keep serving. Set restart_port=true to release an occupied target port before starting.",
            {
                "type": "object",
                "properties": {
                    "command": string,
                    "cwd": string,
                    "port": integer,
                    "restart_port": boolean,
                },
                "required": ["command"],
            },
            runtime_tools.start_background_command,
        )
    )
    registry.register(
        RegisteredTool(
            "cleanup_background_processes",
            "Terminate background processes started by this MiniCodex2 session. Use this to release dev servers or test services when they are no longer needed.",
            {"type": "object", "properties": {}, "required": []},
            runtime_tools.cleanup_background_processes,
        )
    )
    registry.register(
        RegisteredTool(
            "inspect_port",
            "Inspect which local process IDs are listening on a TCP port. Use this instead of netstat/Get-NetTCPConnection/lsof.",
            {"type": "object", "properties": {"port": integer}, "required": ["port"]},
            runtime_tools.inspect_port,
        )
    )
    registry.register(
        RegisteredTool(
            "release_port",
            "Release a local TCP port by killing the listening process tree. Use this instead of raw taskkill/kill/Stop-Process.",
            {"type": "object", "properties": {"port": integer}, "required": ["port"]},
            runtime_tools.release_port,
        )
    )
    registry.register(
        RegisteredTool(
            "inspect_toolchain",
            "Inspect whether a supported language toolchain is installed and visible on PATH. Supported first version: go.",
            {"type": "object", "properties": {"name": string}, "required": ["name"]},
            runtime_tools.inspect_toolchain,
        )
    )
    registry.register(
        RegisteredTool(
            "install_toolchain",
            "Install a supported language toolchain only when the user explicitly requested installation. On Windows, Go uses winget.",
            {
                "type": "object",
                "properties": {"name": string, "installer": string},
                "required": ["name"],
            },
            runtime_tools.install_toolchain,
        )
    )
    return registry

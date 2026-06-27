from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from minicodex2.tools.browser_automation import (
    BrowserAutomationManager,
    _browser_failure_details,
    _browser_semantic_failure,
)
from minicodex2.config.settings import AppSettings
from minicodex2.decision.types import PolicyDecision
from minicodex2.tools.command_runner import BackgroundCommandManager, CommandRunner, PortManager
from minicodex2.tools.path_safety import PathSafety
from minicodex2.tools.permission_store import PermissionStore
from minicodex2.tools.permissions import PermissionPolicy
from minicodex2.tools.python_runner import PythonScriptRunner
from minicodex2.tools.results import ToolResult
from minicodex2.tools.toolchain import ToolchainManager
from minicodex2.web.tools import WebTools


class RuntimeTools:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.progress_sink: Callable[[str, dict[str, object]], None] | None = None
        self.path_safety = PathSafety(settings.workspace_root, settings.paths.projects_root)
        self.permission_policy = PermissionPolicy(settings.permission_mode)
        self.permission_store = PermissionStore()
        self.command_runner = CommandRunner()
        self.background = BackgroundCommandManager()
        self.ports = PortManager()
        self.toolchains = ToolchainManager()
        self.python_runner = PythonScriptRunner()
        self.web = WebTools(settings)
        self.browser = BrowserAutomationManager(
            artifact_root=settings.artifact_root,
            workspace_root=settings.workspace_root,
            progress_callback=self._emit_browser_progress,
        )

    def web_search(
        self,
        query: str,
        max_results: int | None = None,
        freshness: str | None = None,
        domains: list[str] | None = None,
    ) -> ToolResult:
        return self.web.web_search(
            query=query,
            max_results=max_results,
            freshness=freshness,
            domains=domains,
        )

    def fetch_web_page(self, url: str, max_chars: int = 12_000) -> ToolResult:
        return self.web.fetch_web_page(url=url, max_chars=max_chars)

    def _permission_gate(
        self,
        action: str,
        decision: PolicyDecision,
        payload: dict[str, object],
    ) -> ToolResult | None:
        if decision.action == "allow":
            return None
        if decision.action == "ask" and self.permission_store.consume_approval(action, payload):
            return None
        permission_request_id = None
        if decision.action == "ask":
            request = self.permission_store.create(action, decision.reason, payload)
            permission_request_id = request.id
        return ToolResult(
            ok=False,
            content=decision.reason,
            blocked=True,
            block_reason=decision.reason,
            permission_request_id=permission_request_id,
        )

    def list_directory(
        self,
        path: str = ".",
        recursive: bool = False,
        max_depth: int = 1,
        max_entries: int = 200,
        include_hidden: bool = False,
        include_metadata: bool = True,
        glob: str | None = None,
    ) -> ToolResult:
        try:
            safe = self.path_safety.resolve_workspace_path(path)
            if not safe.path.exists():
                raise FileNotFoundError(safe.relative)
            if not safe.path.is_dir():
                raise NotADirectoryError(safe.relative)
            max_depth = max(1, min(int(max_depth), 8))
            max_entries = max(1, min(int(max_entries), 1000))
            entries = _list_directory_entries(
                safe.path,
                self.settings.workspace_root,
                recursive=bool(recursive),
                max_depth=max_depth,
                max_entries=max_entries,
                include_hidden=bool(include_hidden),
                include_metadata=bool(include_metadata),
                glob_pattern=_normalize_optional_glob(glob),
            )
            truncated = len(entries) >= max_entries
            return ToolResult(
                ok=True,
                content=json.dumps(entries, ensure_ascii=False, indent=2),
                metadata={
                    "path": safe.relative,
                    "entries": entries,
                    "recursive": bool(recursive),
                    "max_depth": max_depth,
                    "max_entries": max_entries,
                    "truncated": truncated,
                    "glob": _normalize_optional_glob(glob),
                },
            )
        except Exception as exc:
            return self._path_failure_result("list_directory", path, exc)

    def read_file(
        self,
        path: str,
        max_bytes: int = 200_000,
        offset: int = 0,
        limit: int | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ToolResult:
        try:
            safe = self.path_safety.resolve_workspace_path(path)
            raw_data = safe.path.read_bytes()
            total_size = len(raw_data)
            max_bytes = max(1, min(int(max_bytes), 1_000_000))
            stat = safe.path.stat()
            metadata = {
                "path": safe.relative,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "sha256": hashlib.sha256(raw_data).hexdigest(),
            }
            if start_line is not None or end_line is not None:
                text = raw_data.decode("utf-8", errors="replace")
                lines = text.splitlines(keepends=True)
                line_count = len(lines)
                start = 1 if start_line is None else max(1, int(start_line))
                end = line_count if end_line is None else max(start, int(end_line))
                selected = "".join(lines[start - 1 : end])
                encoded = selected.encode("utf-8")
                truncated = len(encoded) > max_bytes or end < line_count
                if len(encoded) > max_bytes:
                    selected = encoded[:max_bytes].decode("utf-8", errors="replace")
                metadata.update(
                    {
                        "truncated": truncated,
                        "start_line": start,
                        "end_line": min(end, line_count),
                        "line_count": line_count,
                        "returned_lines": max(0, min(end, line_count) - start + 1),
                        "returned_bytes": len(selected.encode("utf-8")),
                        "next_start_line": min(end, line_count) + 1 if end < line_count else None,
                    }
                )
                return ToolResult(ok=True, content=selected, metadata=metadata)

            offset = max(0, int(offset))
            effective_limit = max_bytes if limit is None else max(1, min(int(limit), max_bytes))
            data = raw_data[offset : offset + effective_limit]
            returned_end = min(total_size, offset + len(data))
            truncated = returned_end < total_size
            metadata.update(
                {
                    "truncated": truncated,
                    "offset": offset,
                    "limit": effective_limit,
                    "returned_bytes": len(data),
                    "next_offset": returned_end if truncated else None,
                }
            )
            return ToolResult(
                ok=True,
                content=data.decode("utf-8", errors="replace"),
                metadata=metadata,
            )
        except Exception as exc:
            return self._path_failure_result("read_file", path, exc)

    def search_files(
        self,
        query: str,
        root: str = ".",
        path: str | None = None,
        glob: str = "*",
        case_sensitive: bool = False,
        max_results: int = 50,
        recurse: bool | None = None,
        recursive: bool | None = None,
    ) -> ToolResult:
        try:
            if not isinstance(query, str) or not query:
                return _invalid_tool_arguments("search_files", "query must be a non-empty string")
            original_root = root
            original_path = path
            if path is not None and root == ".":
                root = path
            safe_root = self.path_safety.resolve_workspace_path(root)
            max_results = max(1, min(int(max_results), 200))
            should_recurse = True
            if recursive is not None:
                should_recurse = bool(recursive)
            elif recurse is not None:
                should_recurse = bool(recurse)
            needle = query if case_sensitive else query.lower()
            results: list[dict[str, object]] = []
            searchable_files = (
                [safe_root.path]
                if safe_root.path.is_file()
                else list(_iter_searchable_files(safe_root.path, glob, recursive=should_recurse))
            )
            candidate_count = len(searchable_files)
            if self.progress_sink is not None:
                self.progress_sink(
                    "search_files_started",
                    {
                        "query": query,
                        "root_arg": original_root,
                        "path_arg": original_path,
                        "resolved_root": safe_root.relative,
                        "root_kind": "file" if safe_root.path.is_file() else "directory",
                        "glob": glob,
                        "case_sensitive": case_sensitive,
                        "recursive": should_recurse,
                        "candidate_files": candidate_count,
                        "max_results": max_results,
                    },
                )
            for path in searchable_files:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                haystack = text if case_sensitive else text.lower()
                if needle not in haystack:
                    continue
                for line_number, line in enumerate(text.splitlines(), start=1):
                    comparison_line = line if case_sensitive else line.lower()
                    if needle in comparison_line:
                        results.append(
                            {
                                "path": path.relative_to(self.settings.workspace_root).as_posix(),
                                "line": line_number,
                                "text": line.strip()[:300],
                            }
                        )
                        break
                if len(results) >= max_results:
                    break
            payload = {
                "query": query,
                "root": safe_root.relative,
                "glob": glob,
                "case_sensitive": case_sensitive,
                "recursive": should_recurse,
                "results": results,
                "truncated": len(results) >= max_results,
                "candidate_files": candidate_count,
                "match_count": len(results),
                "root_kind": "file" if safe_root.path.is_file() else "directory",
            }
            if self.progress_sink is not None:
                self.progress_sink(
                    "search_files_finished",
                    {
                        "query": query,
                        "resolved_root": safe_root.relative,
                        "root_kind": payload["root_kind"],
                        "candidate_files": candidate_count,
                        "match_count": len(results),
                        "matched_lines": [
                            {
                                "path": item.get("path"),
                                "line": item.get("line"),
                                "text": item.get("text"),
                            }
                            for item in results[:10]
                        ],
                        "truncated": payload["truncated"],
                    },
                )
            return ToolResult(
                ok=True,
                content=json.dumps(payload, ensure_ascii=False, indent=2),
                metadata=payload,
            )
        except Exception as exc:
            if self.progress_sink is not None:
                self.progress_sink(
                    "search_files_failed",
                    {
                        "query": query if isinstance(query, str) else repr(query),
                        "root_arg": root,
                        "path_arg": path,
                        "error": str(exc),
                    },
                )
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def _path_failure_result(self, tool_name: str, path: str, exc: Exception) -> ToolResult:
        payload = {
            "error": str(exc),
            "tool": tool_name,
            "requested_path": path,
            "hint": (
                "The requested path does not exist or cannot be read. Use existing_siblings "
                "or call list_directory on an existing parent before guessing another path."
            ),
        }
        try:
            safe = self.path_safety.resolve_workspace_path(path)
            parent = safe.path.parent
            payload["resolved_path"] = str(safe.path)
            payload["parent"] = parent.relative_to(self.settings.workspace_root).as_posix()
        except Exception:
            parent = self.settings.workspace_root
            payload["parent"] = "."
        if parent.exists() and parent.is_dir():
            siblings = []
            for child in sorted(parent.iterdir(), key=lambda p: p.name.lower())[:80]:
                siblings.append({"name": child.name, "type": "dir" if child.is_dir() else "file"})
            payload["parent_exists"] = True
            payload["existing_siblings"] = siblings
        else:
            payload["parent_exists"] = False
            payload["existing_siblings"] = []
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        return ToolResult(ok=False, content=content, blocked=True, block_reason=str(exc), metadata=payload)

    def find_images(
        self,
        query: str = "",
        root: str = ".",
        latest_only: bool = False,
        max_results: int = 10,
    ) -> ToolResult:
        try:
            safe_root = self.path_safety.resolve_workspace_path(root)
            max_results = max(1, min(int(max_results), 50))
            images = _find_image_candidates(safe_root.path, query)
            if latest_only and not images and query:
                images = _find_image_candidates(safe_root.path, "")
            if latest_only:
                images = sorted(images, key=lambda item: item["mtime"], reverse=True)[:1]
            else:
                images = sorted(images, key=lambda item: (-item["score"], -item["mtime"], item["path"]))
                images = images[:max_results]
            for image in images:
                path = Path(str(image["path"]))
                image["path"] = path.relative_to(self.settings.workspace_root).as_posix()
            return ToolResult(ok=True, content=json.dumps(images, ensure_ascii=False, indent=2))
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def view_image(self, path: str, detail: str = "low") -> ToolResult:
        try:
            safe = self.path_safety.resolve_workspace_path(path)
            suffix = safe.path.suffix.lower()
            mime_types = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }
            if suffix not in mime_types:
                return ToolResult(
                    ok=False,
                    content=f"unsupported image type: {suffix}",
                    blocked=True,
                    block_reason="unsupported image type",
                )
            if detail not in {"low", "high", "original"}:
                return ToolResult(
                    ok=False,
                    content="view_image.detail must be one of: low, high, original",
                    blocked=True,
                    block_reason="invalid image detail",
                )
            if not safe.path.is_file():
                return ToolResult(
                    ok=False,
                    content=f"image path is not a file: {safe.relative}",
                    blocked=True,
                    block_reason="image path is not a file",
                )
            return ToolResult(
                ok=True,
                content=f"attached image: {safe.relative}",
                metadata={
                    "image": {
                        "path": str(safe.path),
                        "mime_type": mime_types[suffix],
                        "detail": detail,
                        "relative_path": safe.relative,
                    }
                },
            )
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def write_file(self, path: str, content: str) -> ToolResult:
        try:
            safe = self.path_safety.resolve_workspace_path(path)
            decision = self.permission_policy.check_write(safe.relative)
            blocked = self._permission_gate("write_file", decision, {"path": safe.relative})
            if blocked:
                return blocked
            safe.path.parent.mkdir(parents=True, exist_ok=True)
            before = safe.path.read_text(encoding="utf-8") if safe.path.exists() else ""
            safe.path.write_text(content, encoding="utf-8")
            _invalidate_python_bytecode(safe.path)
            return ToolResult(
                ok=True,
                content=f"wrote {safe.relative}",
                did_write=True,
                changed_files=[safe.relative],
                metadata={
                    "path": safe.relative,
                    "diff": _build_diff_summary(safe.relative, before, content),
                },
            )
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def edit_file(
        self,
        path: str,
        old_text: str | None = None,
        new_text: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        content: str | None = None,
    ) -> ToolResult:
        try:
            safe = self.path_safety.resolve_workspace_path(path)
            decision = self.permission_policy.check_write(safe.relative)
            blocked = self._permission_gate("edit_file", decision, {"path": safe.relative})
            if blocked:
                return blocked
            text = safe.path.read_text(encoding="utf-8")
            line_count = len(text.splitlines())
            requested_line_edit = start_line is not None or end_line is not None or content is not None
            line_edit_is_valid = (
                content is not None
                and start_line is not None
                and end_line is not None
                and start_line >= 1
                and end_line >= start_line
                and end_line <= line_count
            )
            has_text_replacement = old_text is not None and new_text is not None

            # Models sometimes mix edit styles, e.g. passing start_line=0/end_line=0
            # together with a valid old_text/new_text replacement. Treat invalid
            # line coordinates as a recoverable argument slip instead of blocking a
            # perfectly usable exact-text edit.
            if requested_line_edit and line_edit_is_valid:
                updated = _replace_line_range(
                    text,
                    start_line=start_line,
                    end_line=end_line,
                    content=content if content is not None else new_text,
                )
            elif requested_line_edit and not has_text_replacement:
                return ToolResult(
                    ok=False,
                    content=(
                        "invalid line range; use 1-based inclusive start_line/end_line "
                        "inside the file, or call read_file for the target range first"
                    ),
                    blocked=True,
                    block_reason="invalid line range",
                    metadata={
                        "failure_kind": "invalid_tool_arguments",
                        "tool": "edit_file",
                        "path": safe.relative,
                        "line_count": line_count,
                        "requested_start_line": start_line,
                        "requested_end_line": end_line,
                        "has_content": content is not None,
                        "valid_line_range": f"1-{line_count}" if line_count else "empty file",
                        "recovery_hint": (
                            "Use old_text/new_text for exact replacements, or retry with "
                            "1-based inclusive line numbers from read_file metadata."
                        ),
                    },
                )
            elif old_text is None or new_text is None:
                return ToolResult(
                    ok=False,
                    content=(
                        "edit_file requires either old_text/new_text or "
                        "start_line/end_line/content"
                    ),
                    blocked=True,
                    block_reason="invalid edit_file arguments",
                    metadata={"failure_kind": "invalid_tool_arguments", "tool": "edit_file"},
                )
            elif old_text in text:
                updated = text.replace(old_text, new_text, 1)
            else:
                normalized_text = text.replace("\r\n", "\n")
                normalized_old = old_text.replace("\r\n", "\n")
                if normalized_old in normalized_text:
                    normalized_new = new_text.replace("\r\n", "\n")
                    updated = normalized_text.replace(normalized_old, normalized_new, 1)
                else:
                    collapsed_text = _collapse_blank_lines(normalized_text)
                    collapsed_old = _collapse_blank_lines(normalized_old)
                    if collapsed_old not in collapsed_text:
                        return ToolResult(
                            ok=False,
                            content="old_text not found",
                            blocked=True,
                            block_reason="old_text not found",
                        )
                    collapsed_new = _collapse_blank_lines(new_text.replace("\r\n", "\n"))
                    updated = collapsed_text.replace(collapsed_old, collapsed_new, 1)
            safe.path.write_text(updated, encoding="utf-8")
            _invalidate_python_bytecode(safe.path)
            return ToolResult(
                ok=True,
                content=f"edited {safe.relative}",
                did_write=True,
                changed_files=[safe.relative],
                metadata={
                    "path": safe.relative,
                    "diff": _build_diff_summary(safe.relative, text, updated),
                },
            )
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def delete_file(self, path: str) -> ToolResult:
        try:
            safe = self.path_safety.resolve_workspace_path(path)
            decision = self.permission_policy.check_delete(safe.relative)
            blocked = self._permission_gate("delete_file", decision, {"path": safe.relative})
            if blocked:
                return blocked
            if safe.path.is_dir():
                return ToolResult(ok=False, content="delete_file refuses directories", blocked=True, block_reason="directory delete denied")
            before = safe.path.read_text(encoding="utf-8")
            safe.path.unlink()
            _invalidate_python_bytecode(safe.path)
            return ToolResult(
                ok=True,
                content=f"deleted {safe.relative}",
                did_write=True,
                changed_files=[safe.relative],
                metadata={
                    "path": safe.relative,
                    "diff": _build_diff_summary(safe.relative, before, ""),
                },
            )
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def run_command(
        self,
        command: str,
        cwd: str = ".",
        timeout_seconds: int | None = None,
        purpose: str = "generic",
    ) -> ToolResult:
        try:
            specialized_redirect = _specialized_command_redirect(command)
            if specialized_redirect:
                return ToolResult(
                    ok=False,
                    content=specialized_redirect,
                    blocked=False,
                    metadata={"suggested_tool": "inspect_port/release_port"},
                )
            safe_cwd = self.path_safety.resolve_workspace_path(cwd)
            normalized_command, normalization_note = _normalize_command_for_cwd(
                command,
                safe_cwd.relative,
                self.settings.workspace_root,
            )
            trusted = normalized_command in self.settings.verification.commands
            decision = self.permission_policy.check_command(normalized_command, trusted=trusted)
            blocked = self._permission_gate(
                "run_command",
                decision,
                {"command": normalized_command, "cwd": cwd, "original_command": command},
            )
            if blocked:
                return blocked
            before_snapshot = _workspace_file_snapshot(self.settings.workspace_root)
            result = self.command_runner.run(
                normalized_command,
                safe_cwd.path,
                _effective_command_timeout(command, timeout_seconds, self.settings.timeouts.run_command_seconds),
                progress_callback=self._emit_command_progress,
            )
            observed_changes = _workspace_snapshot_changes(
                before_snapshot,
                _workspace_file_snapshot(self.settings.workspace_root),
            )
            changed_files, runtime_data_changed_files = _partition_command_changes(observed_changes)
            content = (
                f"command: {normalized_command}\nexit_code: {result.exit_code}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
            if normalized_command != command:
                content += f"\n\noriginal_command: {command}\ncommand_normalization: {normalization_note}"
            if result.block_reason:
                content += f"\n\nblock_reason: {result.block_reason}"
            semantic_failure = _successful_test_command_without_tests(
                command,
                result.stdout,
                result.stderr,
            )
            failure_details = _command_failure_details(
                command=normalized_command,
                stdout=result.stdout,
                stderr=result.stderr,
                cwd=safe_cwd.path,
            )
            if semantic_failure:
                content += f"\n\nsemantic_failure: {semantic_failure}"
            if failure_details:
                content += "\n\nfailure_analysis:\n" + "\n".join(
                    f"{key}: {value}" for key, value in failure_details.items()
                )
            return ToolResult(
                ok=result.ok and not semantic_failure,
                content=content,
                did_write=bool(changed_files),
                changed_files=changed_files,
                blocked=result.blocked,
                block_reason=result.block_reason,
                metadata={
                    "command_result": result,
                    "command": normalized_command,
                    "original_command": command if normalized_command != command else None,
                    "command_normalization": normalization_note,
                    "cwd": safe_cwd.relative,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "duration_seconds": result.duration_seconds,
                    "semantic_failure": semantic_failure,
                    **failure_details,
                    "purpose": purpose,
                    "changed_files": changed_files,
                    "runtime_data_changed_files": runtime_data_changed_files,
                    "observed_changed_files": observed_changes,
                },
            )
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def run_python(self, code: str, cwd: str = ".", timeout_seconds: int | None = None) -> ToolResult:
        try:
            safe_cwd = self.path_safety.resolve_workspace_path(cwd)
            decision = self.permission_policy.check_command("run_python")
            if decision.action in {"deny", "blocked", "ask"}:
                permission_request_id = None
                if decision.action == "ask":
                    request = self.permission_store.create(
                        "run_python", decision.reason, {"cwd": cwd}
                    )
                    permission_request_id = request.id
                return ToolResult(
                    ok=False,
                    content=decision.reason,
                    blocked=True,
                    block_reason=decision.reason,
                    permission_request_id=permission_request_id,
                )
            before_snapshot = _workspace_file_snapshot(self.settings.workspace_root)
            script_result = self.python_runner.run(
                code=code,
                cwd=safe_cwd.path,
                artifact_root=self.settings.artifact_root,
                timeout_seconds=timeout_seconds or self.settings.timeouts.run_command_seconds,
            )
            result = script_result.command_result
            observed_changes = _workspace_snapshot_changes(
                before_snapshot,
                _workspace_file_snapshot(self.settings.workspace_root),
            )
            changed_files, runtime_data_changed_files = _partition_command_changes(observed_changes)
            relative_script = script_result.script_path.relative_to(self.settings.workspace_root).as_posix()
            content = (
                f"script: {relative_script}\ncommand: {result.command}\nexit_code: {result.exit_code}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
            semantic_failure = _successful_test_command_without_tests(
                result.command,
                result.stdout,
                result.stderr,
            )
            failure_details = _python_script_failure_details(
                code=code,
                stdout=result.stdout,
                stderr=result.stderr,
                cwd=safe_cwd.path,
            )
            if semantic_failure:
                content += f"\n\nsemantic_failure: {semantic_failure}"
            if failure_details:
                content += "\n\nfailure_analysis:\n" + "\n".join(
                    f"{key}: {value}" for key, value in failure_details.items()
                )
            return ToolResult(
                ok=result.ok and not semantic_failure,
                content=content,
                did_write=bool(changed_files),
                changed_files=changed_files,
                blocked=result.blocked,
                block_reason=result.block_reason,
                metadata={
                    "command_result": result,
                    "command": result.command,
                    "cwd": safe_cwd.relative,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "duration_seconds": result.duration_seconds,
                    "script_path": str(script_result.script_path),
                    "relative_script_path": relative_script,
                    "semantic_failure": semantic_failure,
                    **failure_details,
                    "changed_files": changed_files,
                    "runtime_data_changed_files": runtime_data_changed_files,
                    "observed_changed_files": observed_changes,
                },
            )
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def http_request(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: object | None = None,
        timeout_seconds: int | None = None,
        purpose: str = "smoke",
    ) -> ToolResult:
        try:
            if not isinstance(url, str) or not url.strip():
                return _invalid_tool_arguments("http_request", "url must be a non-empty string")
            if not isinstance(method, str):
                return _invalid_tool_arguments("http_request", "method must be a string")
            method = method.upper().strip() or "GET"
            if headers is None:
                request_headers: dict[str, str] = {}
            elif isinstance(headers, dict):
                request_headers = {str(key): str(value) for key, value in headers.items()}
            else:
                return _invalid_tool_arguments(
                    "http_request",
                    "headers must be an object/dict of header names to values, not a string or list",
                    {"received_headers_type": type(headers).__name__},
                )
            if isinstance(body, (dict, list)):
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                request_headers.setdefault("Content-Type", "application/json")
            elif body is None:
                data = None
            else:
                data = str(body).encode("utf-8")
            request = urllib.request.Request(
                url.strip(),
                data=data,
                headers=request_headers,
                method=method,
            )
            timeout = _clamp_timeout(timeout_seconds or self.settings.timeouts.run_command_seconds)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read(1200)
                    status_code = int(response.status)
                    response_headers = dict(response.headers.items())
                    body_excerpt = raw.decode("utf-8", errors="replace")
                    error = None
            except urllib.error.HTTPError as exc:
                raw = exc.read(1200)
                status_code = int(exc.code)
                response_headers = dict(exc.headers.items()) if exc.headers else {}
                body_excerpt = raw.decode("utf-8", errors="replace")
                error = str(exc)
                failure_kind = "http_status_error"
            except urllib.error.URLError as exc:
                status_code = None
                response_headers = {}
                body_excerpt = ""
                error = str(exc.reason)
                failure_kind = _http_request_failure_kind(error)
            else:
                failure_kind = ""
            ok = status_code is not None and 200 <= status_code < 400
            payload = {
                "url": url.strip(),
                "method": method,
                "status_code": status_code,
                "ok": ok,
                "headers": response_headers,
                "body_excerpt": body_excerpt,
                "error": error,
                "purpose": purpose,
                "failure_kind": failure_kind,
                "recoverable": bool(failure_kind),
                "diagnostic_hint": _http_request_diagnostic_hint(failure_kind),
            }
            return ToolResult(
                ok=ok,
                content=json.dumps(payload, ensure_ascii=False, indent=2),
                blocked=False,
                block_reason=failure_kind or None,
                metadata=payload,
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                content=str(exc),
                blocked=True,
                block_reason=str(exc),
                metadata={"failure_kind": "http_request_runtime_error"},
            )

    def browser_test(
        self,
        url: str = "",
        port: int | str | None = None,
        path: str = "/",
        actions: list[dict[str, object]] | None = None,
        browser: str = "auto",
        headless: bool = True,
        timeout_seconds: int | None = None,
        purpose: str = "smoke",
        capture_screenshot: bool = True,
        wait_until: str = "domcontentloaded",
    ) -> ToolResult:
        try:
            url = _browser_target_url(url, port, path)
            if not url:
                return _invalid_tool_arguments(
                    "browser_test",
                    "browser_test requires url or port",
                    {
                        "missing_arguments": ["url"],
                        "hint": "Call browser_test with url='http://localhost:<port>/path' or with port=<port> and optional path.",
                    },
                )
            if shutil.which("node") is None or shutil.which("npm") is None:
                return ToolResult(
                    ok=False,
                    content="browser_test requires local Node.js and npm on PATH",
                    blocked=True,
                    block_reason="missing node or npm",
                )
            info = self.browser.run(
                url=url,
                actions=actions,
                browser=browser,
                headless=headless,
                timeout_seconds=timeout_seconds or max(self.settings.timeouts.run_command_seconds, 45),
                purpose=purpose,
                capture_screenshot=capture_screenshot,
                wait_until=wait_until,
            )
            ok = bool(info.get("ok"))
            semantic_failure = str(info.get("semantic_failure") or "")
            if not semantic_failure:
                semantic_failure = _browser_semantic_failure(info, url) or ""
            if semantic_failure:
                info["semantic_failure"] = semantic_failure
                ok = False
            failure_details = _browser_failure_details(info, url)
            if failure_details:
                info.update(failure_details)
            if not ok and _looks_like_browser_timeout(info):
                info["failure_kind"] = "browser_timeout"
                info["failure_stage"] = "browser_timeout"
                info["failure_summary"] = str(info.get("failure_summary") or info.get("error") or "browser test timed out")
                info["recovery_hint"] = (
                    "The browser automation timed out, but this is usually recoverable. "
                    "Check whether the target service is ready, inspect browser logs or "
                    "background logs, then retry a narrower browser_test or equivalent HTTP check."
                )
            # Browser failures are usually evidence for the model, not terminal runtime
            # failures: the next step may be to inspect a service log, correct a route,
            # shorten the action list, or retry after a server becomes ready. Keep hard
            # blocking for preflight failures above and unexpected Python exceptions below.
            hard_block = False
            return ToolResult(
                ok=ok,
                content=json.dumps(info, ensure_ascii=False, indent=2),
                blocked=hard_block,
                block_reason=str(
                    info.get("error")
                    or semantic_failure
                    or info.get("failure_summary")
                    or ""
                )
                or None,
                metadata=info,
            )
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def start_background_command(
        self,
        command: str,
        cwd: str = ".",
        port: int | None = None,
        restart_port: bool = False,
    ) -> ToolResult:
        try:
            safe_cwd = self.path_safety.resolve_workspace_path(cwd)
            decision = self.permission_policy.check_command(command)
            if decision.action in {"deny", "blocked", "ask"}:
                permission_request_id = None
                if decision.action == "ask":
                    request = self.permission_store.create(
                        "start_background_command",
                        decision.reason,
                        {"command": command, "cwd": cwd, "port": port, "restart_port": restart_port},
                    )
                    permission_request_id = request.id
                return ToolResult(
                    ok=False,
                    content=decision.reason,
                    blocked=True,
                    block_reason=decision.reason,
                    permission_request_id=permission_request_id,
                )
            log_path = self.settings.artifact_root / "logs" / "background" / f"bg_{len(self.background.processes)+1}.log"
            normalized_command, normalization_note = _normalize_command_for_cwd(
                command,
                safe_cwd.relative,
                self.settings.workspace_root,
            )
            info = self.background.start(
                normalized_command,
                safe_cwd.path,
                log_path,
                port=port,
                ready_timeout_seconds=self.settings.timeouts.background_ready_seconds,
                restart_port=restart_port,
            )
            if normalized_command != command:
                info["original_command"] = command
                info["command_normalization"] = normalization_note
            _enrich_background_start_failure(
                info,
                command=normalized_command,
                cwd=safe_cwd.path,
                workspace_root=self.settings.workspace_root,
            )
            diagnosis = background_service_diagnosis(info, cwd=safe_cwd.path)
            info["diagnosis"] = diagnosis
            process_running = info.get("exit_code") is None and isinstance(info.get("pid"), int)
            if info.get("ready") is True:
                info["startup_state"] = "ready"
            elif process_running:
                info["startup_state"] = "running_not_ready"
                info.setdefault(
                    "recovery_hint",
                    "The background process is still running but the requested port was not ready "
                    "within the startup wait. Inspect the background log or retry readiness checks "
                    "before treating it as a startup failure.",
                )
            else:
                info["startup_state"] = "failed"
            tool_ok = bool(info.get("ready")) or process_running
            return ToolResult(ok=tool_ok, content=json.dumps(info, ensure_ascii=False, indent=2), metadata=info)
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def cleanup_background_processes(self) -> ToolResult:
        terminated = self.background.terminate_all()
        return ToolResult(
            ok=True,
            content=json.dumps(
                {
                    "terminated_count": sum(1 for item in terminated if item.get("was_running")),
                    "processes": terminated,
                },
                ensure_ascii=False,
                indent=2,
            ),
            metadata={"terminated": terminated},
        )

    def inspect_port(self, port: int) -> ToolResult:
        try:
            info = self.ports.inspect(port)
            return ToolResult(ok=True, content=json.dumps(info, ensure_ascii=False, indent=2), metadata=info)
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def release_port(self, port: int) -> ToolResult:
        try:
            decision = self.permission_policy.check_command(f"release_port {port}")
            if decision.action in {"deny", "blocked", "ask"}:
                permission_request_id = None
                if decision.action == "ask":
                    request = self.permission_store.create(
                        "release_port",
                        decision.reason,
                        {"port": port},
                    )
                    permission_request_id = request.id
                return ToolResult(
                    ok=False,
                    content=decision.reason,
                    blocked=True,
                    block_reason=decision.reason,
                    permission_request_id=permission_request_id,
                )
            info = self.ports.release(port)
            return ToolResult(ok=bool(info["released"]), content=json.dumps(info, ensure_ascii=False, indent=2), metadata=info)
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def inspect_toolchain(self, name: str) -> ToolResult:
        try:
            info = self.toolchains.inspect(name)
            return ToolResult(ok=True, content=json.dumps(info, ensure_ascii=False, indent=2), metadata=info)
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def install_toolchain(self, name: str, installer: str = "auto") -> ToolResult:
        try:
            info = self.toolchains.install(
                name,
                installer=installer,
                artifact_root=self.settings.artifact_root,
                progress=lambda stage, payload: self._emit_toolchain_progress(stage, payload),
            )
            ok = bool(info.get("installed") or info.get("already_available"))
            blocked = not ok
            reason = None if ok else str(info.get("error") or info.get("message") or "toolchain install failed")
            return ToolResult(
                ok=ok,
                content=json.dumps(info, ensure_ascii=False, indent=2),
                blocked=blocked,
                block_reason=reason,
                metadata=info,
            )
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def _emit_toolchain_progress(self, stage: str, payload: dict[str, object]) -> None:
        if self.progress_sink:
            self.progress_sink("toolchain_install_progress", {"stage": stage, **payload})

    def _emit_command_progress(self, payload: dict[str, object]) -> None:
        if self.progress_sink:
            self.progress_sink("command_progress", payload)

    def _emit_browser_progress(self, stage: str, payload: dict[str, object]) -> None:
        if self.progress_sink:
            self.progress_sink("browser_progress", {"stage": stage, **payload})


def _collapse_blank_lines(text: str) -> str:
    lines = text.split("\n")
    collapsed: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        collapsed.append(line)
        previous_blank = blank
    return "\n".join(collapsed)


def _replace_line_range(
    text: str,
    *,
    start_line: int | None,
    end_line: int | None,
    content: str | None,
) -> str:
    if content is None:
        raise ValueError("line range edit requires content")
    if start_line is None or end_line is None:
        raise ValueError("line range edit requires start_line and end_line")
    if start_line < 1 or end_line < start_line:
        raise ValueError("invalid line range")
    lines = text.splitlines(keepends=True)
    if end_line > len(lines):
        raise ValueError(f"line range outside file: {start_line}-{end_line}, file has {len(lines)} lines")
    newline = "\r\n" if "\r\n" in text else "\n"
    replacement = content.replace("\r\n", "\n").replace("\n", newline)
    if end_line < len(lines) and replacement and not replacement.endswith(("\n", "\r")):
        replacement += newline
    replacement_lines = replacement.splitlines(keepends=True)
    return "".join([*lines[: start_line - 1], *replacement_lines, *lines[end_line:]])


def _http_request_failure_kind(error: str | None) -> str:
    text = (error or "").lower()
    if not text:
        return ""
    if "timed out" in text or "timeout" in text:
        return "http_timeout"
    if any(
        marker in text
        for marker in (
            "connection refused",
            "actively refused",
            "winerror 10061",
            "failed to establish a new connection",
        )
    ):
        return "service_unreachable"
    if any(marker in text for marker in ("name or service not known", "temporary failure", "nodename")):
        return "dns_or_host_unreachable"
    return "http_request_failed"


def _http_request_diagnostic_hint(failure_kind: str) -> str:
    if failure_kind == "http_timeout":
        return "The HTTP request timed out. Inspect whether the server is running, whether the route hangs, and relevant background logs; retry with a corrected URL or timeout only after diagnosing."
    if failure_kind == "service_unreachable":
        return "The service is unreachable. Inspect ports/background processes and start or restart the correct server before retrying."
    if failure_kind == "dns_or_host_unreachable":
        return "The host could not be resolved or reached. Check localhost/127.0.0.1/port/proxy configuration before retrying."
    if failure_kind:
        return "HTTP request failed. Treat this as diagnostic evidence and continue with a corrected tool call or concrete blocker."
    return ""


def _effective_command_timeout(
    command: str,
    requested_timeout: int | None,
    default_timeout: int,
) -> int:
    if requested_timeout is not None:
        return _clamp_timeout(requested_timeout)
    lower = f" {command.lower().strip()} "
    install_markers = (
        " npm install",
        " npm ci",
        " pnpm install",
        " yarn install",
        " bun install",
        " pip install",
        " python -m pip install",
        " py -m pip install",
        " poetry install",
        " uv sync",
        " cargo fetch",
        " go mod download",
    )
    build_markers = (
        " npm run build",
        " pnpm build",
        " yarn build",
        " cargo build",
        " go build",
        " cmake --build",
    )
    test_markers = (
        " pytest",
        " npm test",
        " npm run test",
        " pnpm test",
        " yarn test",
        " cargo test",
        " go test",
        " ctest",
    )
    if any(marker in lower for marker in install_markers):
        return max(default_timeout, 300)
    if any(marker in lower for marker in build_markers):
        return max(default_timeout, 120)
    if any(marker in lower for marker in test_markers):
        return max(default_timeout, 60)
    return _clamp_timeout(default_timeout)


def _invalid_tool_arguments(
    tool_name: str,
    message: str,
    metadata: dict[str, object] | None = None,
) -> ToolResult:
    payload = {
        "tool": tool_name,
        "failure_kind": "invalid_tool_arguments",
        "message": message,
    }
    if metadata:
        payload.update(metadata)
    return ToolResult(
        ok=False,
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        blocked=True,
        block_reason=message,
        metadata=payload,
    )


def _browser_target_url(url: object, port: object, path: object) -> str:
    if isinstance(url, str) and url.strip():
        return url.strip()
    if port is None:
        return ""
    if isinstance(port, str):
        port_text = port.strip()
        if not port_text.isdigit():
            return ""
        port_number = int(port_text)
    elif isinstance(port, int):
        port_number = port
    else:
        return ""
    if port_number <= 0 or port_number > 65535:
        return ""
    path_text = path.strip() if isinstance(path, str) and path.strip() else "/"
    if not path_text.startswith("/"):
        path_text = f"/{path_text}"
    return f"http://localhost:{port_number}{path_text}"


def _looks_like_browser_timeout(info: dict[str, object]) -> bool:
    text = " ".join(
        str(value or "")
        for value in (
            info.get("error"),
            info.get("failure_summary"),
            info.get("failure_kind"),
        )
    ).lower()
    return "timeout" in text or "timed out" in text or "page.goto" in text


def _clamp_timeout(timeout_seconds: int) -> int:
    return max(1, min(int(timeout_seconds), 900))


def _invalidate_python_bytecode(path: Path) -> None:
    if path.suffix != ".py":
        return
    cache_dir = path.parent / "__pycache__"
    if not cache_dir.is_dir():
        return
    for candidate in cache_dir.glob(f"{path.stem}.*.pyc"):
        try:
            candidate.unlink()
        except OSError:
            pass


def _workspace_file_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if _ignored_snapshot_path(path, root):
            continue
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def _workspace_snapshot_changes(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
    *,
    max_files: int = 200,
) -> list[str]:
    changed = [
        path
        for path, state in after.items()
        if before.get(path) != state
    ]
    deleted = [path for path in before if path not in after]
    return sorted([*changed, *deleted])[:max_files]


def _partition_command_changes(paths: list[str]) -> tuple[list[str], list[str]]:
    source_changes: list[str] = []
    runtime_data_changes: list[str] = []
    for path in paths:
        if _is_runtime_data_change(path):
            runtime_data_changes.append(path)
        else:
            source_changes.append(path)
    return source_changes, runtime_data_changes


def _is_runtime_data_change(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    suffix = Path(normalized).suffix
    if suffix in {".sqlite", ".sqlite3", ".db", ".db3", ".pyc", ".pyo", ".log", ".tmp"}:
        return True
    if name in {"db.sqlite3"}:
        return True
    runtime_segments = {
        ".cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "coverage",
    }
    return any(segment in normalized.split("/") for segment in runtime_segments)


def _ignored_snapshot_path(path: Path, root: Path) -> bool:
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        return True
    ignored_dirs = {
        ".git",
        ".hg",
        ".svn",
        ".minicodex2",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
    }
    return any(part in ignored_dirs for part in relative_parts)


def _list_directory_entries(
    root: Path,
    workspace_root: Path,
    *,
    recursive: bool,
    max_depth: int,
    max_entries: int,
    include_hidden: bool,
    include_metadata: bool,
    glob_pattern: str | None = None,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []

    def visit(current: Path, depth: int) -> None:
        if len(entries) >= max_entries:
            return
        children = sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        for child in children:
            if len(entries) >= max_entries:
                return
            if not include_hidden and _is_hidden_path(child, workspace_root):
                continue
            try:
                relative = child.relative_to(workspace_root).as_posix()
            except ValueError:
                relative = child.as_posix()
            is_dir = child.is_dir()
            matches_filter = _path_matches_glob(child.name, relative, glob_pattern)
            if matches_filter:
                entry: dict[str, object] = {
                    "name": child.name,
                    "path": relative,
                    "type": "dir" if is_dir else "file",
                    "depth": depth,
                }
                if include_metadata:
                    try:
                        stat = child.stat()
                        entry["mtime"] = stat.st_mtime
                        if not is_dir:
                            entry["size"] = stat.st_size
                            entry["extension"] = child.suffix.lower()
                    except OSError:
                        entry["metadata_error"] = True
                entries.append(entry)
            if recursive and is_dir and depth < max_depth:
                visit(child, depth + 1)

    visit(root, 1)
    return entries


def _normalize_optional_glob(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _path_matches_glob(name: str, relative_path: str, glob_pattern: str | None) -> bool:
    if not glob_pattern:
        return True
    return fnmatch.fnmatch(name, glob_pattern) or fnmatch.fnmatch(relative_path, glob_pattern)


def _is_hidden_path(path: Path, workspace_root: Path) -> bool:
    try:
        parts = path.relative_to(workspace_root).parts
    except ValueError:
        parts = path.parts
    return any(part.startswith(".") for part in parts if part not in {".", ".."})


def _build_diff_summary(
    path: str,
    before: str,
    after: str,
    *,
    max_lines: int = 40,
    max_line_chars: int = 220,
) -> dict[str, object]:
    raw_lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    truncated = len(raw_lines) > max_lines
    lines = [_truncate_line(line, max_line_chars) for line in raw_lines[:max_lines]]
    added = sum(
        1 for line in raw_lines if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1 for line in raw_lines if line.startswith("-") and not line.startswith("---")
    )
    return {
        "path": path,
        "added_lines": added,
        "removed_lines": removed,
        "lines": lines,
        "truncated": truncated,
    }


def _truncate_line(line: str, limit: int) -> str:
    if len(line) <= limit:
        return line
    return line[: limit - 3] + "..."


def _find_image_candidates(root: Path, query: str) -> list[dict[str, object]]:
    suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    ignored_dirs = {".git", ".venv", "node_modules", ".minicodex2", ".pytest_cache", ".ruff_cache"}
    query = " ".join(query.lower().split())
    query_tokens = [token for token in _split_query_tokens(query) if token]
    candidates: list[dict[str, object]] = []
    for path in root.rglob("*"):
        if any(part in ignored_dirs for part in path.parts):
            continue
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        score, reason = _image_match_score(path, query, query_tokens)
        if query and score <= 0:
            continue
        stat = path.stat()
        candidates.append(
            {
                "path": path,
                "name": path.name,
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "score": score,
                "reason": reason,
            }
        )
    return candidates


def _split_query_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    current = []
    for char in query:
        if char.isalnum() or char in {"_", "-"}:
            current.append(char)
        else:
            if current:
                tokens.append("".join(current))
                current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _image_match_score(path: Path, query: str, query_tokens: list[str]) -> tuple[float, str]:
    if not query:
        return 0.0, "no query; sorted by latest when requested"
    name = path.name.lower()
    stem = path.stem.lower()
    path_text = path.as_posix().lower()
    if query in name:
        return 100.0, "query appears in filename"
    if query in path_text:
        return 80.0, "query appears in path"
    score = 0.0
    matched: list[str] = []
    for token in query_tokens:
        if token in name:
            score += 20.0
            matched.append(token)
        elif token in path_text:
            score += 8.0
            matched.append(token)
    if score:
        return score, f"matched tokens: {', '.join(matched[:5])}"
    close = difflib.SequenceMatcher(None, query, stem).ratio()
    if close >= 0.55:
        return close * 20.0, "similar filename"
    return 0.0, "no match"


def _specialized_command_redirect(command: str) -> str | None:
    normalized = " ".join(command.lower().split())
    process_kill_markers = ("taskkill", "kill -9", "kill /f", "stop-process")
    port_inspect_markers = ("netstat", "get-nettcpconnection", "lsof")
    port_shape_markers = (":", "localport", "tcp:")

    if any(marker in normalized for marker in process_kill_markers):
        return (
            "This command manages/kills processes directly. Use inspect_port(port) to identify "
            "listeners and release_port(port) to free a port instead of raw taskkill/kill/Stop-Process "
            "through run_command."
        )
    if any(marker in normalized for marker in port_inspect_markers) and any(
        marker in normalized for marker in port_shape_markers
    ):
        return (
            "This command inspects local port ownership. Use inspect_port(port) instead of raw "
            "netstat/Get-NetTCPConnection/lsof through run_command."
        )
    return None


def _iter_searchable_files(root: Path, glob_pattern: str, *, recursive: bool = True) -> Iterable[Path]:
    ignored_dirs = {
        ".git",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".minicodex2",
    }
    binary_suffixes = {
        ".7z",
        ".bin",
        ".bmp",
        ".class",
        ".db",
        ".dll",
        ".exe",
        ".gif",
        ".ico",
        ".jar",
        ".jpeg",
        ".jpg",
        ".lock",
        ".mp3",
        ".mp4",
        ".pdf",
        ".png",
        ".pyc",
        ".sqlite",
        ".sqlite3",
        ".webp",
        ".zip",
    }
    pattern = glob_pattern or "*"
    paths = root.rglob(pattern) if recursive else root.glob(pattern)
    for path in paths:
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in ignored_dirs for part in relative_parts):
            continue
        if not path.is_file() or path.suffix.lower() in binary_suffixes:
            continue
        yield path


def _normalize_command_for_cwd(
    command: str,
    cwd: str,
    workspace_root: Path | None = None,
) -> tuple[str, str | None]:
    notes: list[str] = []
    normalized_cwd = cwd.replace("\\", "/").strip("/")
    if normalized_cwd and normalized_cwd != ".":
        match = re.match(r"^\s*cd\s+([\"']?)(.+?)\1\s*(?:&&|&)\s*(.+)$", command, flags=re.IGNORECASE | re.DOTALL)
        if match:
            target = match.group(2).strip().replace("\\", "/").strip("/")
            rest = match.group(3).strip()
            if target.lower() == normalized_cwd.lower():
                command = rest
                notes.append(
                    "removed leading cd because the tool already received the same cwd; "
                    "shell cwd is supplied through the tool argument"
                )
    if workspace_root is not None:
        command, executable_note = _normalize_workspace_relative_executable(command, cwd, workspace_root)
        if executable_note:
            notes.append(executable_note)
    return command, "; ".join(notes) if notes else None


def _normalize_workspace_relative_executable(
    command: str,
    cwd: str,
    workspace_root: Path,
) -> tuple[str, str | None]:
    token = _first_command_token(command)
    if not token or not any(separator in token for separator in ("/", "\\")):
        return command, None
    raw = Path(token)
    if raw.is_absolute():
        return command, None
    workspace_root = workspace_root.resolve()
    cwd_path = (workspace_root / cwd).resolve()
    workspace_candidate = (workspace_root / raw).resolve()
    cwd_candidate = (cwd_path / raw).resolve()
    try:
        workspace_candidate.relative_to(workspace_root)
    except ValueError:
        return command, None
    if not workspace_candidate.exists() or cwd_candidate.exists():
        return command, None
    replaced = _replace_first_command_token(command, str(workspace_candidate))
    return (
        replaced,
        "converted workspace-relative executable path to an absolute path because cwd is a subdirectory",
    )


def _enrich_background_start_failure(
    info: dict[str, object],
    *,
    command: str,
    cwd: Path,
    workspace_root: Path,
) -> None:
    if info.get("ready") is True:
        return
    text = str(info.get("log_tail") or info.get("error") or "")
    missing_module = _extract_missing_python_module(text)
    if not missing_module:
        return
    info["failure_kind"] = "python_dependency_missing"
    info["missing_python_module"] = missing_module
    candidates = _nearby_python_venv_executables(cwd, workspace_root)
    if candidates:
        info["nearby_python_venvs"] = [str(path) for path in candidates[:4]]
    info["recovery_hint"] = (
        f"Python module '{missing_module}' is missing in the interpreter used by the startup command. "
        "Use the project's nearby virtualenv Python if one exists, or install declared project dependencies "
        "into the intended environment before retrying the background command."
    )
    first_token = _first_command_token(command)
    if first_token and first_token.lower().endswith(("python.exe", "python")):
        info["startup_python"] = first_token


def background_service_diagnosis(info: dict[str, object], *, cwd: Path | None = None) -> dict[str, object]:
    """Return factual startup observations for the model without choosing the fix.

    The model still decides the project-specific action, but this diagnosis prevents
    repeated ad-hoc shell probing when a background server is alive, not ready, or
    clearly failing from its log tail.
    """
    ready = bool(info.get("ready"))
    exit_code = info.get("exit_code")
    port = info.get("port")
    log_tail = str(info.get("log_tail") or "")
    lower = log_tail.lower()
    command = str(info.get("command") or "")
    status = "ready" if ready else "unknown"
    signals: list[str] = []
    suggested_next_actions: list[str] = []

    if ready:
        status = "ready"
        suggested_next_actions.append("smoke the expected HTTP/UI endpoint")
    elif exit_code is None:
        status = "process_running_port_not_ready" if port else "process_running_without_port_check"
        suggested_next_actions.append("read_background_log for the latest output before retrying startup")
        if port:
            suggested_next_actions.append("inspect the expected port or smoke the exact configured URL")
    else:
        status = "process_exited_before_ready"
        suggested_next_actions.append("fix the startup error shown in log_tail before retrying")

    if "react-scripts start" in lower or "starting the development server" in lower:
        signals.append("react_scripts_start_seen")
        if not ready:
            suggested_next_actions.append("check frontend dependencies and dev-server compilation output")
    if "vite" in lower and ("ready in" in lower or "local:" in lower):
        signals.append("vite_ready_log_seen")
    if "compiled successfully" in lower or "webpack compiled successfully" in lower:
        signals.append("frontend_compiled_successfully")
    if "module not found" in lower or "modulenotfounderror" in lower:
        signals.append("missing_dependency_or_import")
    if "eaddrinuse" in lower or "address already in use" in lower or "port" in lower and "already in use" in lower:
        signals.append("port_occupied")
    if "npm err!" in lower or "error:" in lower or "traceback" in lower:
        signals.append("startup_error_output_seen")
    if not log_tail.strip():
        signals.append("no_log_output_yet")

    dependency_dirs: list[str] = []
    if cwd is not None:
        for name in ("node_modules", ".venv", "venv"):
            if (cwd / name).exists():
                dependency_dirs.append(name)

    return {
        "status": status,
        "ready": ready,
        "exit_code": exit_code,
        "port": port,
        "command": command,
        "log_signals": signals,
        "dependency_dirs_present": dependency_dirs,
        "suggested_next_actions": _dedupe_strings(suggested_next_actions),
    }


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _extract_missing_python_module(text: str) -> str | None:
    patterns = (
        r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]",
        r"ImportError:\s+No module named ['\"]([^'\"]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _python_script_failure_details(
    *,
    code: str,
    stdout: str,
    stderr: str,
    cwd: Path,
) -> dict[str, object]:
    text = f"{stdout}\n{stderr}"
    if not text.strip():
        return {}
    details: dict[str, object] = {}
    missing_module = _extract_missing_python_module(text)
    if "DJANGO_SETTINGS_MODULE" in code or "django.setup()" in code:
        settings_match = re.search(
            r"DJANGO_SETTINGS_MODULE['\"]?\]\s*=\s*['\"]([^'\"]+)['\"]", code
        ) or re.search(
            r"setdefault\(\s*['\"]DJANGO_SETTINGS_MODULE['\"]\s*,\s*['\"]([^'\"]+)['\"]",
            code,
        )
        configured = settings_match.group(1) if settings_match else None
        manage_settings = _django_manage_settings_module(cwd)
        manage_info = _django_manage_py_info(cwd)
        if (
            configured
            and manage_settings
            and configured != manage_settings
            and (
                "ModuleNotFoundError" in text
                or "ImportError" in text
                or "ImproperlyConfigured" in text
                or "Could not import" in text
            )
        ):
            details["failure_kind"] = "django_settings_mismatch"
            details["configured_settings_module"] = configured
            details["detected_manage_py_settings_module"] = manage_settings
            details["failure_summary"] = (
                "Django helper used a settings module that differs from manage.py; "
                f"use DJANGO_SETTINGS_MODULE={manage_settings!r} or run via manage.py."
            )
            return details
        if (
            configured
            and missing_module
            and (missing_module == configured or configured.startswith(f"{missing_module}."))
            and manage_info
            and manage_info[0] == configured
        ):
            details["failure_kind"] = "django_helper_wrong_cwd_or_pythonpath"
            details["configured_settings_module"] = configured
            details["detected_manage_py_settings_module"] = manage_info[0]
            details["detected_manage_py_dir"] = str(manage_info[1])
            details["failure_summary"] = (
                "Django helper used the right settings module, but the current cwd/PYTHONPATH "
                "does not expose that package; run with cwd set to the manage.py directory "
                f"({manage_info[1]}) or use manage.py shell/commands."
            )
            return details
        if "django.core.exceptions.ImproperlyConfigured" in text or "Requested setting" in text:
            details["failure_kind"] = "django_settings_unconfigured"
            if manage_settings:
                details["detected_manage_py_settings_module"] = manage_settings
            details["failure_summary"] = (
                "Django settings were not configured for this helper; use the project's manage.py "
                "settings module or run a manage.py shell/command."
            )
            return details
    if missing_module:
        details["failure_kind"] = "python_dependency_missing"
        details["missing_python_module"] = missing_module
        if missing_module == "requests":
            details["failure_summary"] = (
                "Python helper imported third-party module 'requests', but it is not installed "
                "in the selected interpreter; for HTTP/API smoke checks use the http_request "
                "tool, or use stdlib urllib if a Python helper is truly needed."
            )
            details["suggested_tool"] = "http_request"
        else:
            details["failure_summary"] = f"Python module '{missing_module}' is missing"
        return details
    if "sqlite3.OperationalError: no such table:" in text:
        table_match = re.search(r"sqlite3\.OperationalError:\s+no such table:\s+([^\s]+)", text)
        details["failure_kind"] = "sqlite_wrong_database_or_unmigrated"
        if table_match:
            details["missing_table"] = table_match.group(1)
        details["failure_summary"] = (
            "SQLite helper opened a database without the expected table; check cwd/database path "
            "or run migrations before querying."
        )
        return details
    if "SyntaxError:" in text:
        details["failure_kind"] = "python_syntax_error"
        details["failure_summary"] = "Python helper failed with SyntaxError"
        return details
    if "NameError:" in text:
        details["failure_kind"] = "python_name_error"
        details["failure_summary"] = "Python helper failed with NameError"
        return details
    return {}


def _python_command_failure_details(
    *,
    command: str,
    stdout: str,
    stderr: str,
    cwd: Path,
) -> dict[str, object]:
    if not _looks_like_python_command(command):
        return {}
    effective_cwd = _effective_command_cwd(command, cwd)
    return _python_script_failure_details(
        code=command,
        stdout=stdout,
        stderr=stderr,
        cwd=effective_cwd,
    )


def _command_failure_details(
    *,
    command: str,
    stdout: str,
    stderr: str,
    cwd: Path,
) -> dict[str, object]:
    python_details = _python_command_failure_details(
        command=command,
        stdout=stdout,
        stderr=stderr,
        cwd=cwd,
    )
    if python_details:
        return python_details
    return _shell_command_failure_details(command=command, stdout=stdout, stderr=stderr)


def _shell_command_failure_details(
    *,
    command: str,
    stdout: str,
    stderr: str,
) -> dict[str, object]:
    text = f"{stdout}\n{stderr}"
    if not text.strip():
        return {}
    missing = _extract_unrecognized_shell_command(text)
    if not missing:
        return {}
    details: dict[str, object] = {
        "failure_kind": "shell_command_not_found",
        "missing_shell_command": missing,
        "failure_summary": f"Shell command '{missing}' was not found in the current shell.",
    }
    if os.name == "nt" and missing.lower() in _POWERSHELL_CMDLETS:
        details["failure_kind"] = "powershell_cmdlet_used_in_cmd_shell"
        details["expected_shell"] = "powershell"
        details["current_shell_hint"] = os.environ.get("COMSPEC", "cmd.exe")
        details["failure_summary"] = (
            f"'{missing}' is a PowerShell cmdlet, but run_command uses the Windows platform shell "
            "by default (usually cmd.exe). Use a cmd-compatible command such as 'dir'/'if exist', "
            "or explicitly run powershell -NoProfile -Command \"...\"."
        )
    return details


_POWERSHELL_CMDLETS = {
    "test-path",
    "get-childitem",
    "select-object",
    "where-object",
    "get-content",
    "set-content",
    "new-item",
    "remove-item",
    "copy-item",
    "move-item",
}


def _extract_unrecognized_shell_command(text: str) -> str | None:
    patterns = (
        r"'([^'\r\n]+)'\s+is not recognized as an internal or external command",
        r'"([^"\r\n]+)"\s+is not recognized as an internal or external command',
        r"The term ['\"]([^'\"]+)['\"] is not recognized as a name of a cmdlet",
        r"([^:\r\n]+):\s+command not found",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _looks_like_python_command(command: str) -> bool:
    lower = command.lower()
    return (
        "python" in lower
        or "manage.py" in lower
        or "django_settings_module" in lower
        or ".py" in lower
    )


def _effective_command_cwd(command: str, cwd: Path) -> Path:
    match = re.search(r"\bcd\s+(?:/d\s+)?([^\&]+)\s*&&", command, flags=re.IGNORECASE)
    if not match:
        return cwd
    raw = match.group(1).strip().strip('"').strip("'")
    if not raw:
        return cwd
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    return path


def _django_manage_settings_module(cwd: Path) -> str | None:
    info = _django_manage_py_info(cwd)
    if info:
        return info[0]
    return None


def _django_manage_py_info(cwd: Path) -> tuple[str, Path] | None:
    for base in (cwd, *cwd.parents):
        manage_py = base / "manage.py"
        if not manage_py.exists():
            continue
        module = _read_django_manage_settings_module(manage_py)
        if module:
            return module, manage_py.parent
    for manage_py in cwd.glob("*/manage.py"):
        module = _read_django_manage_settings_module(manage_py)
        if module:
            return module, manage_py.parent
    return None


def _read_django_manage_settings_module(manage_py: Path) -> str | None:
    try:
        text = manage_py.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(
        r"setdefault\(\s*['\"]DJANGO_SETTINGS_MODULE['\"]\s*,\s*['\"]([^'\"]+)['\"]",
        text,
    )
    if match:
        return match.group(1)
    return None


def _nearby_python_venv_executables(cwd: Path, workspace_root: Path) -> list[Path]:
    candidates: list[Path] = []
    current = cwd.resolve()
    root = workspace_root.resolve()
    while True:
        for name in (".venv", "venv"):
            python_path = _venv_python_path(current / name)
            if python_path.exists():
                candidates.append(python_path)
        if current == root or current.parent == current:
            break
        current = current.parent
    return candidates


def _venv_python_path(venv_dir: Path) -> Path:
    if re.match(r"^[A-Za-z]:\\|^\\\\", str(venv_dir)):
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _replace_first_command_token(command: str, replacement: str) -> str:
    leading_length = len(command) - len(command.lstrip())
    leading = command[:leading_length]
    stripped = command[leading_length:]
    if not stripped:
        return command
    if stripped[0] in {'"', "'"}:
        quote = stripped[0]
        end = stripped.find(quote, 1)
        if end > 0:
            rest = stripped[end + 1 :]
            return f'{leading}"{replacement}"{rest}'
    parts = stripped.split(maxsplit=1)
    rest = f" {parts[1]}" if len(parts) == 2 else ""
    return f'{leading}"{replacement}"{rest}'


def _first_command_token(command: str) -> str | None:
    stripped = command.strip()
    if not stripped:
        return None
    if stripped[0] in {'"', "'"}:
        quote = stripped[0]
        end = stripped.find(quote, 1)
        if end > 0:
            return stripped[1:end]
    return stripped.split()[0]


def _successful_test_command_without_tests(command: str, stdout: str, stderr: str) -> str | None:
    lower_command = command.lower()
    if not any(
        marker in lower_command
        for marker in (
            " test",
            "pytest",
            "unittest",
            "manage.py test",
            "npm test",
            "cargo test",
            "go test",
            "ctest",
        )
    ):
        return None
    output = f"{stdout}\n{stderr}".lower()
    no_test_markers = (
        "no tests ran",
        "no tests run",
        "ran 0 tests",
        "collected 0 items",
        "found 0 test",
        "0 tests found",
        "0 passing",
    )
    if any(marker in output for marker in no_test_markers):
        return "test command completed without executing any tests"
    return None

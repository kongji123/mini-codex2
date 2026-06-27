from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable


PLAYWRIGHT_CORE_VERSION = "1.53.0"


class BrowserAutomationManager:
    def __init__(
        self,
        *,
        artifact_root: Path,
        workspace_root: Path,
        progress_callback: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self.artifact_root = artifact_root
        self.workspace_root = workspace_root
        self.progress_callback = progress_callback
        self.tool_root = self.artifact_root / "browser_tooling"
        self.runs_root = self.artifact_root / "browser_runs"

    def run(
        self,
        *,
        url: str,
        actions: list[dict[str, object]] | None = None,
        browser: str = "auto",
        headless: bool = True,
        timeout_seconds: int = 45,
        purpose: str = "smoke",
        capture_screenshot: bool = True,
        wait_until: str = "domcontentloaded",
    ) -> dict[str, object]:
        self._emit("browser_prepare", {"url": url, "browser": browser, "purpose": purpose})
        executable_path, resolved_browser = self._detect_browser(browser)
        self._ensure_playwright_core()
        run_dir = self._create_run_dir()
        spec_path = run_dir / "spec.json"
        output_path = run_dir / "result.json"
        screenshot_path = run_dir / "final.png"
        normalized_actions = self._normalize_actions(actions or [])
        spec = {
            "url": url,
            "actions": normalized_actions,
            "browser": resolved_browser,
            "browser_executable": str(executable_path),
            "headless": bool(headless),
            "timeout_ms": max(1, int(timeout_seconds)) * 1000,
            "capture_screenshot": bool(capture_screenshot),
            "screenshot_path": str(screenshot_path),
            "wait_until": wait_until,
            "purpose": purpose,
        }
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        self._emit(
            "browser_actions_planned",
            {
                "url": url,
                "action_count": len(normalized_actions),
                "actions": [_action_brief(item) for item in normalized_actions[:12]],
            },
        )
        self._emit(
            "browser_run_started",
            {
                "url": url,
                "browser": resolved_browser,
                "run_dir": str(run_dir),
                "action_count": len(normalized_actions),
            },
        )
        command = [
            self._require_executable("node"),
            str(_runner_script_path()),
            str(spec_path),
            str(output_path),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.workspace_root),
                text=True,
                capture_output=True,
                timeout=max(30, int(timeout_seconds) + 15),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "url": url,
                "browser": resolved_browser,
                "purpose": purpose,
                "error": f"browser test timed out after {timeout_seconds}s",
                "stdout": _compact_text(exc.stdout or "", 1200),
                "stderr": _compact_text(exc.stderr or "", 1200),
                "run_dir": _relative_to_workspace(run_dir, self.workspace_root),
            }
        if output_path.exists():
            result = json.loads(output_path.read_text(encoding="utf-8"))
        else:
            result = {
                "ok": False,
                "error": "browser runner did not produce a result file",
                "stdout": _compact_text(completed.stdout or "", 1200),
                "stderr": _compact_text(completed.stderr or "", 1200),
            }
        result["browser"] = resolved_browser
        result["purpose"] = purpose
        result["run_dir"] = _relative_to_workspace(run_dir, self.workspace_root)
        if screenshot_path.exists():
            result["screenshot_path"] = _relative_to_workspace(screenshot_path, self.workspace_root)
        if completed.returncode != 0 and not result.get("error"):
            result["error"] = f"browser runner exited with code {completed.returncode}"
        result["stdout"] = _compact_text(
            str(result.get("stdout") or completed.stdout or ""),
            1200,
        )
        result["stderr"] = _compact_text(
            str(result.get("stderr") or completed.stderr or ""),
            1200,
        )
        semantic_failure = _browser_semantic_failure(result, url)
        if semantic_failure:
            result["semantic_failure"] = semantic_failure
            result["ok"] = False
        self._emit(
            "browser_run_finished",
            {
                "url": url,
                "browser": resolved_browser,
                "ok": bool(result.get("ok")),
                "semantic_failure": result.get("semantic_failure") or "",
            },
        )
        return result

    def _ensure_playwright_core(self) -> None:
        package_dir = self.tool_root / "node_modules" / "playwright-core"
        if package_dir.exists():
            return
        self.tool_root.mkdir(parents=True, exist_ok=True)
        package_json = self.tool_root / "package.json"
        if not package_json.exists():
            package_json.write_text(
                json.dumps(
                    {
                        "name": "minicodex2-browser-tooling",
                        "private": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        self._emit("browser_bootstrap_started", {"tool_root": str(self.tool_root)})
        completed = subprocess.run(
            [
                self._require_executable("npm"),
                "install",
                "--no-audit",
                "--no-fund",
                f"playwright-core@{PLAYWRIGHT_CORE_VERSION}",
            ],
            cwd=str(self.tool_root),
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        if completed.returncode != 0 or not package_dir.exists():
            stderr = _compact_text(completed.stderr or "", 1200)
            stdout = _compact_text(completed.stdout or "", 1200)
            raise RuntimeError(
                "failed to install browser automation tooling with npm install playwright-core; "
                f"stdout={stdout!r}; stderr={stderr!r}"
            )
        self._emit("browser_bootstrap_finished", {"tool_root": str(self.tool_root)})

    def _detect_browser(self, requested: str) -> tuple[Path, str]:
        browser_order = {
            "auto": ("edge", "chrome"),
            "edge": ("edge",),
            "chrome": ("chrome",),
        }.get(requested, ())
        if not browser_order:
            raise RuntimeError("browser must be one of: auto, edge, chrome")
        for browser in browser_order:
            for candidate in _browser_candidates(browser):
                if candidate.exists():
                    return candidate, browser
        raise RuntimeError(
            f"no supported browser executable found for {requested}; "
            "expected Microsoft Edge or Google Chrome on this machine"
        )

    def _create_run_dir(self) -> Path:
        self.runs_root.mkdir(parents=True, exist_ok=True)
        run_dir = self.runs_root / f"run_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _normalize_actions(self, actions: list[dict[str, object]]) -> list[dict[str, object]]:
        normalized: list[dict[str, object]] = []
        for raw in actions:
            if not isinstance(raw, dict):
                continue
            action = dict(raw)
            if str(action.get("type") or "") == "upload_files":
                values = action.get("values")
                if isinstance(values, list):
                    action["values"] = [
                        str(self._resolve_upload_path(str(item)))
                        for item in values
                    ]
                elif action.get("value"):
                    action["value"] = str(self._resolve_upload_path(str(action["value"])))
            normalized.append(action)
        return normalized

    @staticmethod
    def _require_executable(name: str) -> str:
        path = shutil.which(name)
        if not path:
            raise RuntimeError(f"{name} executable was not found on PATH")
        return path

    def _resolve_upload_path(self, value: str) -> Path:
        raw = Path(value)
        if raw.is_absolute():
            return raw.resolve()
        return (self.workspace_root / raw).resolve()

    def _emit(self, stage: str, payload: dict[str, object]) -> None:
        if self.progress_callback:
            self.progress_callback(stage, payload)


def _runner_script_path() -> Path:
    return Path(__file__).with_name("browser_runner.mjs")


def _browser_candidates(browser: str) -> list[Path]:
    if browser == "edge":
        return [
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ]
    if browser == "chrome":
        return [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ]
    return []


def _relative_to_workspace(path: Path, workspace_root: Path) -> str:
    try:
        return path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _compact_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n... output truncated ...\n" + text[-half:]


def _browser_semantic_failure(result: dict[str, object], requested_url: str) -> str | None:
    page_errors = result.get("page_errors")
    if isinstance(page_errors, list) and page_errors:
        return f"browser page errors observed while testing {requested_url}"
    failed_requests = result.get("failed_requests")
    if not isinstance(failed_requests, list):
        return None
    same_origin = _origin_of(str(result.get("final_url") or requested_url))
    for item in failed_requests:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if same_origin and _origin_of(url) != same_origin:
            continue
        status_code = item.get("status")
        resource_type = str(item.get("resource_type") or "")
        if isinstance(status_code, int) and status_code >= 400 and resource_type in {
            "document",
            "script",
            "fetch",
            "xhr",
        }:
            return f"browser observed failed same-origin {resource_type} request: {url} status={status_code}"
        error_text = str(item.get("error") or "")
        if error_text and resource_type in {"document", "script", "fetch", "xhr"}:
            return f"browser observed failed same-origin {resource_type} request: {url} error={error_text}"
    return None


def _browser_failure_details(result: dict[str, object], requested_url: str) -> dict[str, str]:
    action_results = result.get("action_results")
    if isinstance(action_results, list):
        for item in action_results:
            if not isinstance(item, dict) or item.get("ok", True):
                continue
            action_type = str(item.get("type") or "action")
            selector = str(item.get("selector") or "")
            target = selector or str(item.get("value") or "")
            error_text = _compact_text(str(item.get("error") or "browser action failed"), 220)
            summary = f"browser action failed: {action_type}"
            if target:
                summary += f" {target}"
            if error_text:
                summary += f" ({error_text})"
            network_hint = _recent_browser_network_hint(result)
            if network_hint:
                summary += f"; recent_network={network_hint}"
            return {
                "failure_kind": "browser_action_failed",
                "failure_stage": "browser_action",
                "failure_summary": summary,
            }
    page_errors = result.get("page_errors")
    if isinstance(page_errors, list) and page_errors:
        first_error = _compact_text(str(page_errors[0]), 220)
        return {
            "failure_kind": "browser_page_error",
            "failure_stage": "page_runtime",
            "failure_summary": f"browser page error at {requested_url}: {first_error}",
        }
    failed_requests = result.get("failed_requests")
    if not isinstance(failed_requests, list):
        return {}
    same_origin = _origin_of(str(result.get("final_url") or requested_url))
    for item in failed_requests:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if same_origin and _origin_of(url) != same_origin:
            continue
        status_code = item.get("status")
        resource_type = str(item.get("resource_type") or "")
        if isinstance(status_code, int) and status_code >= 400 and resource_type in {
            "document",
            "script",
            "fetch",
            "xhr",
        }:
            return {
                "failure_kind": "browser_request_failed",
                "failure_stage": "app_request",
                "failure_summary": (
                    f"browser observed failed same-origin {resource_type} request: "
                    f"{url} status={status_code}"
                ),
            }
        error_text = str(item.get("error") or "")
        if error_text and resource_type in {"document", "script", "fetch", "xhr"}:
            return {
                "failure_kind": "browser_request_failed",
                "failure_stage": "app_request",
                "failure_summary": (
                    f"browser observed failed same-origin {resource_type} request: "
                    f"{url} error={_compact_text(error_text, 220)}"
                ),
            }
    error_text = str(result.get("error") or "").strip()
    if error_text:
        return {
            "failure_kind": "browser_runtime_error",
            "failure_stage": "tool_runtime",
            "failure_summary": f"browser runtime error: {_compact_text(error_text, 220)}",
        }
    return {}


def _recent_browser_network_hint(result: dict[str, object]) -> str:
    responses = result.get("network_responses")
    if not isinstance(responses, list):
        return ""
    interesting: list[dict[str, object]] = []
    for item in responses:
        if not isinstance(item, dict):
            continue
        resource_type = str(item.get("resource_type") or "")
        method = str(item.get("method") or "GET").upper()
        status = item.get("status")
        if resource_type not in {"document", "fetch", "xhr"}:
            continue
        if method != "GET" or (isinstance(status, int) and status >= 300):
            interesting.append(item)
    if not interesting:
        return ""
    latest = interesting[-1]
    status = latest.get("status")
    method = str(latest.get("method") or "")
    url = str(latest.get("url") or "")
    body = _compact_text(str(latest.get("body_excerpt") or ""), 160)
    hint = f"{method} {url} status={status}"
    if body:
        hint += f" body={body}"
    return _compact_text(hint, 320)


def _origin_of(url: str) -> str:
    if "://" not in url:
        return ""
    parts = url.split("/", 3)
    if len(parts) < 3:
        return url
    return f"{parts[0]}//{parts[2]}"


def _action_brief(action: dict[str, object]) -> str:
    action_type = str(action.get("type") or "action")
    selector = str(action.get("selector") or "")
    value = str(action.get("value") or "")
    if action_type in {"click", "fill", "hover", "wait_for", "extract_text", "check", "uncheck"} and selector:
        return f"{action_type} {selector}"
    if action_type in {
        "wait_for_text",
        "click_text",
        "assert_text",
        "wait_for_url_contains",
        "assert_url_contains",
        "wait_for_timeout",
    } and value:
        return f"{action_type} {value}"
    if action_type == "upload_files":
        if isinstance(action.get("values"), list):
            return f"upload_files {len(action['values'])} file(s)"
        if value:
            return f"upload_files {Path(value).name}"
    return action_type

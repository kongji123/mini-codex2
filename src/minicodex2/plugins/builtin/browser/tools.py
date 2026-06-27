from __future__ import annotations

from typing import Any

from minicodex2.tools.registry import RegisteredTool, ToolRegistry


def register_browser_tools(registry: ToolRegistry, runtime_tools: Any) -> None:
    string = {"type": "string"}
    integer = {"type": "integer"}
    boolean = {"type": "boolean"}
    registry.register(
        RegisteredTool(
            "browser_test",
            (
                "Open a real local browser page and exercise frontend UI with JavaScript enabled. "
                "Use this for DOM/rendering/forms/router/proxy/debugging that http_request alone cannot verify. "
                "Pass url when you know the full page URL. If you only know a local dev server port, "
                "pass port and optional path instead of omitting the target. "
                "Supported action types: wait_for, wait_for_selector, assert_selector, hover, click, fill, press, check, uncheck, select, "
                "upload_files, wait_for_url_contains, assert_url_contains, wait_for_text, click_text, "
                "assert_text, wait_for_load_state, wait_for_timeout, extract_text, screenshot, media_diagnostics. "
                "For visible text, prefer wait_for_text/click_text over selector strings like text=... "
                "For video/audio bugs, run media_diagnostics and compare browser='chrome' vs browser='edge' when "
                "a failure appears browser-specific; the result includes readyState/networkState/error/currentSrc. "
                "Each browser_test call starts a fresh browser context; include login/setup actions in the same call "
                "when testing protected routes. "
                "Provided by the built-in minicodex-browser plugin."
            ),
            {
                "type": "object",
                "properties": {
                    "url": string,
                    "port": integer,
                    "path": string,
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": string,
                                "selector": string,
                                "value": string,
                                "values": {"type": "array", "items": string},
                                "key": string,
                                "state": string,
                                "path": string,
                                "timeout_ms": integer,
                            },
                            "required": ["type"],
                        },
                    },
                    "browser": {
                        "type": "string",
                        "enum": ["auto", "edge", "chrome"],
                    },
                    "headless": boolean,
                    "timeout_seconds": integer,
                    "purpose": {
                        "type": "string",
                        "enum": ["generic", "inspect", "verify", "smoke"],
                    },
                    "capture_screenshot": boolean,
                    "wait_until": {
                        "type": "string",
                        "enum": ["domcontentloaded", "load", "networkidle", "commit"],
                    },
                },
                "required": [],
            },
            runtime_tools.browser_test,
        )
    )

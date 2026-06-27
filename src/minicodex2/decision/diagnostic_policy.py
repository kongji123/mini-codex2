from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class DiagnosticPolicyDecision:
    should_diagnose: bool
    reason: str = ""


ERROR_MARKERS = (
    "报错",
    "错误",
    "失败",
    "打不开",
    "不能",
    "注册不了",
    "登录不了",
    "网络或服务器错误",
    "not found",
    "404",
    "500",
    "uncaught",
    "syntaxerror",
    "referenceerror",
    "traceback",
    "exception",
    "failed",
    "error",
    "cannot",
    "can't",
)

ASK_USER_MARKERS = (
    "请告诉我",
    "请提供",
    "截图",
    "开发者工具",
    "network",
    "控制台",
    "你可以",
    "你需要",
    "确认一下",
    "tell me",
    "provide",
    "send me",
    "screenshot",
    "developer tools",
)


def diagnose_user_input(user_input: str) -> DiagnosticPolicyDecision:
    text = user_input.lower()
    if any(marker in text for marker in ERROR_MARKERS):
        return DiagnosticPolicyDecision(True, "user reported runtime/build/integration error")
    if re.search(r"\b(get|post|put|delete|patch)\s+https?://.+\s+(4\d\d|5\d\d)", text):
        return DiagnosticPolicyDecision(True, "user reported HTTP failure")
    return DiagnosticPolicyDecision(False)


def assistant_is_asking_user_for_diagnostics(text: str) -> bool:
    normalized = text.lower()
    return any(marker in normalized for marker in ASK_USER_MARKERS)

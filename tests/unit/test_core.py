from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from minicodex2.agent.chat_history import ChatHistory
from minicodex2.agent.collaboration_advisor import CollaborationAdvisor
from minicodex2.agent.compaction import CompactionManager
from minicodex2.agent.context import ContextManager, estimate_static_payload_tokens, plan_context_budget
from minicodex2.agent.context_buffer import ContextBufferStore
from minicodex2.agent.token_usage import TokenUsageTracker
from minicodex2.config.settings import ConfigLoader
from minicodex2.decision.continuation import ContinuationPlanner
from minicodex2.decision.failure_classifier import FailureClassifier
from minicodex2.decision.types import PolicyDecision
from minicodex2.display import UiMessageFormatter
from minicodex2.history.jsonl_store import JsonlHistoryStore
from minicodex2.model.fake_adapter import FakeModelAdapter, FakeStep
from minicodex2.model.messages import ChatMessage, ModelRequest, TokenUsage, ToolCall
from minicodex2.model.openai_compatible import OpenAICompatibleModelAdapter
from minicodex2.model.openai_compatible import _retry_after_seconds
from minicodex2.project.detector import ProjectDetector
from minicodex2.tools.command_runner import CommandResult
from minicodex2.tools.path_safety import PathSafety
from minicodex2.tools.permissions import PermissionPolicy
from minicodex2.verification.plan import VerificationStep
from minicodex2.verification.plan_builder import VerificationPlanBuilder


def _formatted_runtime_summary_text(
    summary: dict[str, object],
    *,
    token_budget: int = 40_000,
    section_token_budget: int = 20_000,
) -> str:
    """Return every formatted runtime section without requiring injection.

    Cache-friendly context construction keeps volatile runtime sections out of
    the main message prefix.  Tests that care about formatting should inspect
    the formatter output directly instead of asserting that every section is
    injected into `build_context().messages` on every model call.
    """

    return ContextManager()._format_summary(
        summary,
        token_budget=token_budget,
        section_token_budget=section_token_budget,
    )


def test_policy_decision_allow_ask_deny_blocked() -> None:
    assert PolicyDecision("allow", "ok").allowed
    assert not PolicyDecision("ask", "review").allowed
    assert not PolicyDecision("deny", "no").allowed
    assert not PolicyDecision("blocked", "bad").allowed


def test_runtime_logger_rotates_large_session_logs(tmp_path: Path) -> None:
    from minicodex2.agent.logging import RuntimeLogger

    logger = RuntimeLogger(tmp_path, name="session_test", max_bytes=80, backup_count=2)
    logger.write("first " + "x" * 90)
    logger.write("second")

    current = tmp_path / "logs" / "session_test.log"
    rotated = tmp_path / "logs" / "session_test.log.1"
    assert current.exists()
    assert rotated.exists()
    assert "first" in rotated.read_text(encoding="utf-8")
    assert "second" in current.read_text(encoding="utf-8")


def test_context_buffer_store_persists_model_messages(tmp_path: Path) -> None:
    store = ContextBufferStore(tmp_path / ".minicodex2")
    messages = [
        ChatMessage(role="runtime", content="[RUNTIME PROTOCOL]\nStable."),
        ChatMessage(role="user", content="hello"),
    ]

    path = store.save("session_test", messages, metadata={"reason": "initial"})
    loaded = store.load("session_test")

    assert path.exists()
    assert [(message.role, message.content) for message in loaded] == [
        ("runtime", "[RUNTIME PROTOCOL]\nStable."),
        ("user", "hello"),
    ]
    assert store.append("session_test", ChatMessage(role="assistant", content="ok"))
    assert [message.content for message in store.load("session_test")] == [
        "[RUNTIME PROTOCOL]\nStable.",
        "hello",
        "ok",
    ]


def test_context_buffer_append_preserves_existing_wire_bytes(tmp_path: Path) -> None:
    store = ContextBufferStore(tmp_path / ".minicodex2")
    messages = [
        ChatMessage(role="runtime", content="[RUNTIME PROTOCOL]\nStable."),
        ChatMessage(role="user", content="hello"),
    ]
    store.save("session_test", messages)
    wire_path = store.wire_path_for("session_test")
    before = wire_path.read_bytes()

    assert store.append("session_test", ChatMessage(role="assistant", content="ok"))

    after = wire_path.read_bytes()
    assert after.startswith(before)
    assert after != before
    assert [message.content for message in store.load("session_test")] == [
        "[RUNTIME PROTOCOL]\nStable.",
        "hello",
        "ok",
    ]


def test_context_buffer_repairs_missing_tool_result(tmp_path: Path) -> None:
    store = ContextBufferStore(tmp_path / ".minicodex2")
    store.save(
        "session_test",
        [
            ChatMessage(role="runtime", content="[RUNTIME PROTOCOL]\nStable."),
            ChatMessage(role="user", content="continue"),
            ChatMessage(
                role="assistant",
                content="",
                metadata={
                    "tool_calls": [
                        {
                            "id": "call_one",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "call_two",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": "{}"},
                        },
                    ]
                },
            ),
            ChatMessage(
                role="tool",
                content="first result",
                name="read_file",
                tool_call_id="call_one",
            ),
            ChatMessage(role="runtime", content="later control message"),
        ],
    )

    repaired = store.repair_tool_call_sequence("session_test")
    messages = store.load("session_test")

    assert repaired == 1
    assert [message.role for message in messages[2:6]] == ["assistant", "tool", "tool", "runtime"]
    assert messages[3].tool_call_id == "call_one"
    assert messages[4].tool_call_id == "call_two"
    assert messages[4].name == "write_file"
    assert "tool call result was missing" in messages[4].content


def test_context_buffer_append_repairs_existing_tool_sequence(tmp_path: Path) -> None:
    store = ContextBufferStore(tmp_path / ".minicodex2")
    store.save(
        "session_test",
        [
            ChatMessage(role="runtime", content="[RUNTIME PROTOCOL]\nStable."),
            ChatMessage(
                role="assistant",
                content="",
                metadata={
                    "tool_calls": [
                        {
                            "id": "call_missing",
                            "type": "function",
                            "function": {"name": "run_command", "arguments": "{}"},
                        }
                    ]
                },
            ),
        ],
    )

    assert store.append("session_test", ChatMessage(role="user", content="next"))
    messages = store.load("session_test")

    # Appending must not repair immediately. During a normal tool batch the
    # assistant tool_calls are appended before their tool results arrive; eager
    # repair would incorrectly synthesize "Recovered context buffer" tool
    # results and poison the next model turn. Repair is only safe at the final
    # send boundary, after the runtime has had a chance to append real tool
    # results for the whole batch.
    assert [message.role for message in messages] == ["runtime", "assistant", "user"]

    repaired = store.repair_tool_call_sequence("session_test")
    messages = store.load("session_test")

    assert repaired == 1
    assert [message.role for message in messages] == ["runtime", "assistant", "tool", "user"]
    assert messages[2].tool_call_id == "call_missing"
    assert messages[2].name == "run_command"


def test_main_context_converts_midstream_runtime_controls_to_synthetic_user() -> None:
    from minicodex2.agent.unified_session import _normalize_runtime_messages_for_main_context

    messages = [
        ChatMessage(role="runtime", content="[RUNTIME PROTOCOL]\nStable root protocol."),
        ChatMessage(role="user", content="start"),
        ChatMessage(role="runtime", content="[TOOL RESULT FACTS]\nvolatile derived facts"),
        ChatMessage(role="assistant", content="I'll inspect."),
        ChatMessage(role="system", content="[STATUS REVIEW]\nvolatile control prompt"),
        ChatMessage(role="tool", content="file content", name="read_file", tool_call_id="call_1"),
    ]

    filtered = _normalize_runtime_messages_for_main_context(messages)

    assert [message.role for message in filtered] == [
        "runtime",
        "user",
        "user",
        "assistant",
        "user",
        "tool",
    ]
    assert filtered[0].content.startswith("[RUNTIME PROTOCOL]")
    assert filtered[2].content.startswith("[RUNTIME OBSERVATION - synthetic")
    assert "[TOOL RESULT FACTS]" in filtered[2].content
    assert filtered[2].metadata["synthetic_runtime_observation"] is True
    assert "[STATUS REVIEW]" in filtered[4].content


def test_memory_extractor_parses_model_json() -> None:
    from minicodex2.agent.memory_extractor import parse_memory_extraction_response

    result = parse_memory_extraction_response(
        json.dumps(
            {
                "should_write": True,
                "rollout_summary": "Verified backend startup command.",
                "memories": [
                    {
                        "kind": "workflow",
                        "title": "backend startup",
                        "content": "Run python manage.py runserver 0.0.0.0:8000 from backend.",
                        "scope": "project",
                        "tags": ["startup", "django"],
                        "confidence": "high",
                        "reuse_rule": "Use before rediscovering backend startup.",
                        "evidence": "start_background_command succeeded.",
                        "cwd": "studentPet/backend",
                        "command": "venv/Scripts/python.exe manage.py runserver 0.0.0.0:8000",
                        "purpose": "startup",
                        "ready_check": "http://127.0.0.1:8000/",
                        "steps": ["start backend", "check port 8000"],
                        "ports": [8000],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )

    assert result.should_write
    assert result.rollout_summary == "Verified backend startup command."
    assert result.memories[0].kind == "workflow"
    assert result.memories[0].confidence == "high"
    assert result.memories[0].cwd == "studentPet/backend"
    assert result.memories[0].command.startswith("venv/Scripts/python.exe")
    assert result.memories[0].purpose == "startup"
    assert result.memories[0].ports == [8000]
    assert result.memories[0].steps == ["start backend", "check port 8000"]


def test_memory_extractor_keeps_pending_requirements() -> None:
    from minicodex2.agent.memory_extractor import parse_memory_extraction_response

    result = parse_memory_extraction_response(
        json.dumps(
            {
                "should_write": True,
                "rollout_summary": "User discussed second-phase task review requirements.",
                "memories": [
                    {
                        "kind": "pending_requirement",
                        "title": "task submission review workflow",
                        "content": (
                            "Student task submissions should support text/media evidence, teacher review, "
                            "student names in submission records, and completion-based scoring."
                        ),
                        "scope": "project",
                        "tags": ["studentpet", "requirement"],
                        "confidence": "medium",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )

    assert result.should_write
    assert result.memories[0].kind == "pending_requirement"
    assert "completion-based scoring" in result.memories[0].content


def test_turn_memory_store_records_model_memories(tmp_path: Path) -> None:
    from minicodex2.agent.project_memory import TurnMemoryStore

    store = TurnMemoryStore(tmp_path / ".minicodex2")
    records = store.record_model_memories(
        turn_id="turn_1",
        user_input="continue",
        rollout_summary="The turn found a stable project command.",
        memories=[
            {
                "kind": "repo_fact",
                "title": "project venv path",
                "content": "The backend uses backend/venv/Scripts/python.exe.",
                "scope": "project",
                "tags": ["venv", "python"],
                "confidence": "high",
                "reuse_rule": "Use before searching for Python again.",
                "evidence": "dir command found the executable.",
            }
        ],
    )

    assert len(records) == 2
    memory_text = store.memory_md_path.read_text(encoding="utf-8")
    summary_text = store.summary_path.read_text(encoding="utf-8")
    assert "project venv path" in memory_text
    assert "Use before searching for Python again." in memory_text
    assert "project venv path" in summary_text


def test_extracted_workflow_memory_is_promoted_to_workflow_store(tmp_path: Path) -> None:
    from minicodex2.agent.memory_extractor import ExtractedMemory, MemoryExtractionJob, MemoryExtractionResult
    from minicodex2.agent.project_memory import WorkflowMemoryStore
    from minicodex2.agent.unified_session import UnifiedAgentSession

    session = UnifiedAgentSession.__new__(UnifiedAgentSession)
    session.workflow_memory_store = WorkflowMemoryStore(tmp_path / ".minicodex2")
    job = MemoryExtractionJob(
        turn_id="turn_1",
        user_input="verify login",
        result_status="completed",
        metrics={},
        events=[],
    )
    extraction = MemoryExtractionResult(
        should_write=True,
        memories=[
            ExtractedMemory(
                kind="workflow",
                title="studentPet browser login smoke",
                content="Open frontend, log in as a seeded student, then verify the target route.",
                tags=["browser", "login"],
                confidence="high",
                cwd="studentPet/frontend",
                command="npm start",
                purpose="integration",
                ready_check="http://localhost:3000/",
                steps=["open http://localhost:3000/login", "submit seeded student credentials"],
                ports=[3000, 8000],
            )
        ],
    )

    records = UnifiedAgentSession._record_extracted_workflows(session, job, extraction)

    assert len(records) == 1
    assert records[0].title == "studentPet browser login smoke"
    assert records[0].cwd == "studentPet/frontend"
    assert records[0].ports == [3000, 8000]
    assert session.workflow_memory_store.search(query="login smoke")[0].id == records[0].id


def test_context_injects_turn_memory_summary() -> None:
    runtime_text = _formatted_runtime_summary_text(
        {
            "turn_memory_summary": "# Memory Summary\n- tm-1: backend startup works",
        }
    )
    assert "[TURN MEMORY SUMMARY]" in runtime_text
    assert "backend startup works" in runtime_text


def test_context_manager_builds_minimal_chat_context(tmp_path: Path) -> None:
    history = ChatHistory()
    history.add_user("hello")
    context = ContextManager().build_context(history=history, runtime_summary={"workspace": str(tmp_path)})
    assert context.messages[0].role == "runtime"
    assert "install_toolchain" in context.messages[0].content
    assert "execute local workspace commands through run_command" in context.messages[0].content
    assert "instead of claiming you cannot execute commands" in context.messages[0].content
    assert any(message.role == "user" and message.content == "hello" for message in context.messages)


def test_context_injects_environment_command_guidance() -> None:
    history = ChatHistory()
    history.add_user("run build")
    context = ContextManager().build_context(
        history=history,
        runtime_summary={
            "environment": {
                "os": "Windows",
                "platform": "Windows-11",
                "is_windows": True,
                "shell_family": "windows-cmd",
                "shell_path": "C:\\Windows\\System32\\cmd.exe",
                "shell_name": "cmd.exe",
                "path_separator": "\\",
                "command_notes": [
                    "run_command/start_background_command use the platform shell via subprocess shell=True; detected shell=C:\\Windows\\System32\\cmd.exe.",
                    "Do not use Unix-only helpers such as head, tail, grep, sed, awk, xargs, or /bin/sh unless they are explicitly available.",
                    "For output truncation on Windows cmd, prefer built-in MiniCodex tools or explicit powershell -NoProfile -Command \"... | Select-Object -First N\".",
                    "PowerShell cmdlets such as Test-Path, Get-ChildItem, Select-Object, and Where-Object must be run through explicit powershell -NoProfile -Command \"...\"; do not call them bare through run_command.",
                ],
            }
        },
    )

    runtime_text = context.messages[0].content
    assert "[ENVIRONMENT]" in runtime_text
    assert "shell_family=windows-cmd" in runtime_text
    assert "shell_name=cmd.exe" in runtime_text
    assert "Do not use Unix-only helpers such as head" in runtime_text
    assert "Select-Object -First" in runtime_text
    assert "Test-Path" in runtime_text
    assert "do not call them bare" in runtime_text


def test_diagnostic_policy_detects_runtime_error_report() -> None:
    from minicodex2.decision.diagnostic_policy import diagnose_user_input

    decision = diagnose_user_input("POST http://localhost:3001/api/auth/login/ 404 (Not Found)")

    assert decision.should_diagnose
    assert "HTTP" in decision.reason or "error" in decision.reason


def test_assistant_tool_calls_are_preserved_for_model_context() -> None:
    from minicodex2.model.messages import ToolCall

    call = ToolCall("call_1", "read_file", {"path": "README.md"})
    message = ChatMessage(
        role="assistant",
        content="",
        metadata={"tool_calls": [call.to_model_dict()]},
    )
    data = message.to_model_dict()
    assert data["tool_calls"][0]["id"] == "call_1"
    assert data["tool_calls"][0]["function"]["name"] == "read_file"


def test_user_image_message_serializes_to_openai_content_parts(tmp_path: Path) -> None:
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    )
    message = ChatMessage(
        role="user",
        content="what is in this image?",
        metadata={
            "image": {
                "path": str(image_path),
                "mime_type": "image/png",
                "detail": "low",
            }
        },
    )

    data = message.to_model_dict()

    assert isinstance(data["content"], list)
    assert data["content"][0] == {"type": "text", "text": "what is in this image?"}
    assert data["content"][1]["type"] == "image_url"
    assert data["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert data["content"][1]["image_url"]["detail"] == "low"


def test_chat_history_drop_commands_mutate_messages() -> None:
    history = ChatHistory()
    history.add_user("one")
    history.add_user("two")
    history.add_user("three")

    assert history.drop_first(1) == 1
    assert [message.content for message in history.messages()] == ["two", "three"]
    assert history.drop_last(1) == 1
    assert [message.content for message in history.messages()] == ["two"]
    assert history.clear() == 1
    assert history.messages() == []


def test_chat_history_drop_images_removes_image_metadata(tmp_path: Path) -> None:
    history = ChatHistory()
    history.add_user_image("look", path=str(tmp_path / "shot.png"), mime_type="image/png")

    assert history.drop_images() == 1
    message = history.messages()[0]
    assert "image" not in message.metadata
    assert "image dropped" in message.content


def test_chat_history_repairs_missing_tool_result() -> None:
    history = ChatHistory()
    history.add(
        ChatMessage(
            role="assistant",
            content="",
            metadata={
                "tool_calls": [
                    {
                        "id": "call_missing",
                        "type": "function",
                        "function": {"name": "run_command", "arguments": "{}"},
                    }
                ]
            },
        )
    )
    history.add_user("next message")

    repaired = history.repair_tool_call_sequence()
    messages = history.messages()

    assert repaired == 1
    assert messages[1].role == "tool"
    assert messages[1].tool_call_id == "call_missing"
    assert messages[1].name == "run_command"
    assert messages[2].role == "user"


def test_chat_history_repairs_orphan_tool_result() -> None:
    history = ChatHistory()
    history.add_tool_result("orphan", "late result", name="read_file")
    history.add_user("hello")

    repaired = history.repair_tool_call_sequence()
    messages = history.messages()

    assert repaired == 1
    assert messages[0].role == "runtime"
    assert "orphan tool result" in messages[0].content
    assert messages[1].role == "user"


def test_chat_history_compacts_large_tool_result() -> None:
    history = ChatHistory()

    history.add_tool_result("call_large", "a" * 10_000, name="read_file")
    message = history.messages()[0]

    assert len(message.content) < 4_500
    assert "tool output truncated" in message.content


def test_context_manager_clears_old_tool_results_only_in_active_context() -> None:
    history = ChatHistory()
    for index in range(5):
        history.add_tool_result(
            f"call_{index}",
            "command: python manage.py test\n"
            f"stderr: ERROR: old failure {index}\n"
            + ("raw-payload " * 120),
            name="run_command",
        )

    # _clear_old_tool_results now runs in _compress, so trigger compaction
    context = ContextManager().build_context(
        history=history,
        tool_result_raw_keep=2,
        tool_result_summary_chars=420,
        token_budget=800,
        compression_threshold=0.5,
    )
    tool_messages = [message for message in context.messages if message.role == "tool"]

    # Clearing now happens during compaction
    assert context.cleared_tool_results == 3
    assert tool_messages[0].metadata["tool_result_cleared"] is True
    assert "tool result cleared after model consumption" in tool_messages[0].content
    assert "python manage.py test" in tool_messages[0].content
    assert "old failure 0" in tool_messages[0].content
    assert "raw-payload " not in tool_messages[0].content
    assert "tool_result_cleared" not in tool_messages[-1].metadata
    assert "raw-payload " in tool_messages[-1].content
    assert "tool result cleared after model consumption" not in history.messages()[0].content


def test_context_manager_omits_older_images(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
    second.write_bytes(b"\x89PNG\r\n\x1a\nsecond")
    history = ChatHistory()
    history.add_user_image("first image", path=str(first), mime_type="image/png")
    history.add_user("middle")
    history.add_user_image("second image", path=str(second), mime_type="image/png")

    context = ContextManager().build_context(history=history)

    image_messages = [message for message in context.messages if "image" in message.metadata]
    assert len(image_messages) == 1
    assert image_messages[0].metadata["image"]["path"] == str(second)
    assert any("older image omitted" in message.content for message in context.messages)


def test_context_manager_compresses_old_tool_history() -> None:
    history = ChatHistory()
    history.add_user("build the project")
    for index in range(30):
        history.add_assistant("working")
        history.add_tool_result(
            f"call_{index}",
            "command: python manage.py test\n"
            "exit_code: 1\n"
            "stderr:\n"
            "Traceback\n"
            f"ERROR: failure {index}\n"
            + ("x" * 500),
            name="run_command",
        )
        history.add_tool_result(
            f"write_{index}",
            f"wrote app/module_{index}.py",
            name="write_file",
        )
    history.add_user("continue")

    context = ContextManager().build_context(
        history=history,
        runtime_summary={"workspace": "demo"},
        token_budget=2000,
        compression_threshold=0.50,
    )

    # Slightly higher because _clear_old_tool_results runs inside _compress
    assert context.estimated_tokens <= 2200
    assert context.messages[0].role == "runtime"
    assert "[RUNTIME PROTOCOL]" in context.messages[0].content
    runtime_text = "\n".join(message.content for message in context.messages if message.role == "runtime")
    assert "Context checkpoint handoff:" in runtime_text
    assert "app/module_" in runtime_text
    assert "python manage.py test" in runtime_text
    assert "Traceback" in runtime_text
    assert any(message.role == "user" and message.content == "continue" for message in context.messages)


def test_context_estimate_includes_static_tool_payload_and_utf8_bytes() -> None:
    history = ChatHistory()
    history.add_user("请读取项目并验证中文上下文估算不要过低")
    tool_payload = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a UTF-8 file. 读取文件并返回内容。",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]
    static_tokens = estimate_static_payload_tokens(tool_payload)

    context_without_tools = ContextManager().build_context(history=history)
    context_with_tools = ContextManager().build_context(
        history=history,
        static_overhead_tokens=static_tokens,
    )

    assert static_tokens > 20
    assert context_with_tools.static_overhead_tokens == static_tokens
    assert context_with_tools.message_estimated_tokens == context_without_tools.estimated_tokens
    assert context_with_tools.estimated_tokens == context_without_tools.estimated_tokens + static_tokens


def test_context_manager_uses_compaction_callback() -> None:
    history = ChatHistory()
    history.add_user("build the project")
    for index in range(20):
        history.add_assistant(f"working {index}")
        history.add_tool_result(
            f"call_{index}",
            f"command: pytest\nexit_code: 1\nERROR: failure {index}\n" + ("x" * 300),
            name="run_command",
        )
    history.add_user("continue")
    calls: list[tuple[int, int]] = []

    def compact(messages: list[ChatMessage], limit: int) -> str:
        calls.append((len(messages), limit))
        return (
            "[CONTEXT CHECKPOINT HANDOFF]\n"
            "[ROOT OBJECTIVE]\nBuild the project.\n"
            "[FAILURES AND EVIDENCE]\npytest failed.\n"
            "[NEXT ACTIONS]\nInspect failure and fix."
        )

    context = ContextManager().build_context(
        history=history,
        token_budget=1000,
        compression_threshold=0.50,
        compaction_callback=compact,
    )

    assert calls
    assert calls[0][1] <= 1000
    assert "[ROOT OBJECTIVE]" in context.messages[0].content
    assert "pytest failed" in context.messages[0].content
    assert context.messages[-1].content == "continue"


def test_context_manager_calls_compaction_callback_once_per_build() -> None:
    history = ChatHistory()
    history.add_user("build the project")
    for index in range(80):
        history.add_assistant(f"working {index} " + ("x" * 200))
        history.add_tool_result(
            f"call_{index}",
            f"command: pytest\nexit_code: 1\nERROR: failure {index}\n" + ("y" * 500),
            name="run_command",
        )
    history.add_user("continue")
    calls: list[tuple[int, int]] = []

    def compact(messages: list[ChatMessage], limit: int) -> str:
        calls.append((len(messages), limit))
        return (
            "[CONTEXT CHECKPOINT HANDOFF]\n"
            "[ROOT OBJECTIVE]\nBuild the project.\n"
            "[CURRENT PROGRESS]\n" + ("large summary " * 400)
        )

    context = ContextManager().build_context(
        history=history,
        token_budget=500,
        compression_threshold=0.50,
        compaction_callback=compact,
    )

    assert len(calls) == 1
    assert context.estimated_tokens <= 500
    assert "[CONTEXT CHECKPOINT HANDOFF]" in context.messages[0].content
    assert context.messages[-1].content == "continue"


def test_history_active_messages_rebuild_from_last_compaction_checkpoint() -> None:
    history = ChatHistory()
    history.add_user("old requirement")
    history.add_assistant("old answer")
    source = history.messages()
    replacement = [ChatMessage(role="runtime", content="[CONTEXT CHECKPOINT HANDOFF]\nsummary")]
    history.add_compaction_checkpoint(
        summary="[CONTEXT CHECKPOINT HANDOFF]\nsummary",
        replacement_history=replacement,
        source_messages=source,
    )
    history.add_user("new request")

    active = history.active_messages()

    assert [message.content for message in active] == [
        "[CONTEXT CHECKPOINT HANDOFF]\nsummary",
        "new request",
    ]
    checkpoint = next(
        message for message in history.messages() if message.metadata.get("kind") == "compaction_checkpoint"
    )
    assert checkpoint.metadata["source_message_ids"]
    assert checkpoint.metadata["replacement_history"]


def test_jsonl_history_store_preserves_compaction_checkpoint_active_context(tmp_path: Path) -> None:
    history = ChatHistory()
    history.add_user("old requirement")
    history.add_assistant("old answer")
    source = history.messages()
    history.add_compaction_checkpoint(
        summary="[CONTEXT CHECKPOINT HANDOFF]\nsummary",
        replacement_history=[ChatMessage(role="runtime", content="[CONTEXT CHECKPOINT HANDOFF]\nsummary")],
        source_messages=source,
    )
    history.add_user("new request")
    store = JsonlHistoryStore(tmp_path)
    store.save("session_test", history, {})

    loaded = store.load_history("session_test")

    assert [message.content for message in loaded.active_messages()] == [
        "[CONTEXT CHECKPOINT HANDOFF]\nsummary",
        "new request",
    ]


def test_compaction_manager_asks_model_for_handoff_summary() -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "[ROOT OBJECTIVE]\nFinish project.\n"
                "[USER CONSTRAINTS]\nDo not invent evidence.\n"
                "[CURRENT PROGRESS]\nOne file changed.\n"
                "[FILES AND COMMANDS]\npytest failed.\n"
                "[FAILURES AND EVIDENCE]\nTraceback.\n"
                "[OPEN QUESTIONS]\nNone.\n"
                "[NEXT ACTIONS]\nFix and verify."
            )
        ]
    )
    manager = CompactionManager(fake, "test-model")

    summary = manager.compact(
        [
            ChatMessage(role="user", content="make app"),
            ChatMessage(role="tool", content="command: pytest\nTraceback"),
        ],
        max_chars=2000,
    )

    assert fake.call_count == 1
    assert fake.requests[0].tools == []
    assert fake.requests[0].model == "test-model"
    assert "CONTEXT CHECKPOINT COMPACTION" in fake.requests[0].messages[0].content
    assert "Target summary size" in fake.requests[0].messages[1].content
    assert summary.startswith("[CONTEXT CHECKPOINT HANDOFF]")
    assert "[NEXT ACTIONS]" in summary


def test_compaction_prompt_preserves_operational_memory_policy() -> None:
    from minicodex2.agent.compaction import COMPACTION_PROMPT

    assert "Create a handoff summary for another LLM that will resume the task" in COMPACTION_PROMPT
    assert "Current progress and key decisions made" in COMPACTION_PROMPT
    assert "What remains to be done" in COMPACTION_PROMPT
    assert "Any critical data, examples, or references needed to continue" in COMPACTION_PROMPT
    assert "Be concise, structured, and focused" in COMPACTION_PROMPT
    assert "MiniCodex2 additional retention policy" in COMPACTION_PROMPT
    assert "Later explicit user statements override earlier statements" in COMPACTION_PROMPT
    assert "[PENDING REQUIREMENTS AND DESIGN DISCUSSION]" in COMPACTION_PROMPT
    assert "not yet promoted" in COMPACTION_PROMPT
    assert "active goal/work plan" in COMPACTION_PROMPT
    assert "usernames, passwords" in COMPACTION_PROMPT
    assert "ports, URLs, paths" in COMPACTION_PROMPT
    assert "[SUPERSEDED OR STALE CONTEXT]" in COMPACTION_PROMPT
    assert "Preserve failed attempts as failed attempts" in COMPACTION_PROMPT


def test_turn_memory_store_recovers_from_raw_log_when_index_is_corrupt(tmp_path: Path) -> None:
    from minicodex2.agent.project_memory import TurnMemoryStore

    store = TurnMemoryStore(tmp_path)
    store.record_model_memories(
        turn_id="turn_1",
        user_input="学生提交图片和视频要支持上传并在教师端查看",
        rollout_summary="Captured pending upload requirement.",
        memories=[
            {
                "kind": "pending_requirement",
                "title": "submission media upload",
                "content": "Student submissions must support image and video upload, and teachers must review them.",
                "tags": ["submission", "media"],
                "confidence": "high",
            }
        ],
    )
    store.records_path.write_text("{broken json", encoding="utf-8")

    recovered = TurnMemoryStore(tmp_path).important(kinds={"pending_requirement"}, limit=5)

    assert len(recovered) == 1
    assert recovered[0].kind == "pending_requirement"
    assert recovered[0].title == "submission media upload"
    assert "teachers must review" in recovered[0].content


def test_context_injects_pending_requirement_memory() -> None:
    history = ChatHistory()
    history.add_user("刚才我提的新需求是什么？")

    runtime_text = _formatted_runtime_summary_text(
        {
            "pending_turn_memories": [
                {
                    "id": "tm-upload",
                    "kind": "pending_requirement",
                    "title": "submission media upload",
                    "content": "Student submissions should support image and video upload in the project UI.",
                    "tags": ["submission", "media"],
                    "source_turn_id": "turn_abc",
                }
            ]
        }
    )
    assert "[PENDING REQUIREMENT MEMORY]" in runtime_text
    assert "submission media upload" in runtime_text
    assert "Student submissions should support image and video upload" in runtime_text


def test_compaction_preserves_recent_discussion_anchors() -> None:
    fake = FakeModelAdapter([FakeStep("[ROOT OBJECTIVE]\nContinue the project.")])
    manager = CompactionManager(fake, "test-model")

    summary = manager.compact(
        [
            ChatMessage(role="user", content="我想讨论二期功能：学生提交作业时要支持文字、图片和视频。"),
            ChatMessage(role="assistant", content="可以，建议拆成提交记录、附件存储、教师审核和按完成度给分四个步骤。"),
            ChatMessage(role="tool", content="large tool output " * 200),
        ],
        max_chars=1600,
    )

    assert "RECENT USER DISCUSSION ANCHORS" in summary
    assert "学生提交作业时要支持文字、图片和视频" in summary
    assert "提交记录、附件存储、教师审核" in summary


def test_token_usage_tracker_records_fake_usage() -> None:
    tracker = TokenUsageTracker()
    tracker.record(TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))
    assert tracker.total().total_tokens == 15


def test_goal_complete_requires_acceptance_evidence() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("build demo").ok

    incomplete = controller.update_goal("complete")
    assert not incomplete.ok
    assert not incomplete.blocked
    assert incomplete.block_reason == "missing acceptance evidence"
    assert incomplete.metadata["recoverable"] is True

    state.add_acceptance_evidence("command passed: npm run build")
    completed = controller.update_goal("complete")
    assert completed.ok


def test_work_plan_tools_manage_current_step_and_evidence() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("finish the demo project").ok

    result = controller.set_work_plan(
        [
            {
                "id": "auth",
                "title": "Finish auth flow",
                "status": "in_progress",
                "acceptance": "register and login smoke pass",
            },
            {"id": "tasks", "title": "Finish task list", "acceptance": "task list loads"},
        ]
    )

    assert result.ok
    assert state.current_step_id == "auth"
    assert [item.id for item in state.work_plan] == ["auth", "tasks"]

    record = state.add_evidence_record(
        kind="smoke",
        source_tool="http_request",
        summary="POST /api/auth/register returned 200",
        url="http://127.0.0.1/register",
    )
    updated = controller.update_work_step(
        "auth",
        "done",
        evidence_ids=[record.id],
    )

    assert updated.ok
    assert state.find_work_step("auth").status == "done"  # type: ignore[union-attr]
    assert state.find_work_step("auth").evidence_ids == [record.id]  # type: ignore[union-attr]
    assert state.current_step_id == "tasks"
    assert state.find_work_step("tasks").status == "pending"  # type: ignore[union-attr]


def test_set_work_plan_blocks_unsourced_replacement_of_document_plan() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("finish documented project").ok
    assert controller.merge_work_plan(
        [
            {
                "id": "profile",
                "title": "Build profile feature",
                "acceptance": "profile flow verified",
            }
        ],
        source_document="docs/plan.md",
    ).ok

    blocked = controller.set_work_plan(
        [
            {"id": "shell", "title": "Run shell check"},
            {"id": "python-helper", "title": "Run temporary Python helper"},
        ]
    )

    assert blocked.blocked
    assert blocked.block_reason == "document-sourced work plan replacement requires explicit override"
    assert [item.id for item in state.work_plan] == ["profile"]

    replaced = controller.set_work_plan(
        [{"id": "phase-2", "title": "Replace with phase 2 plan"}],
        replace_document_plan=True,
    )

    assert replaced.ok
    assert [item.id for item in state.work_plan] == ["phase-2"]


def test_work_step_done_requires_real_evidence_id() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("finish demo").ok
    assert controller.set_work_plan([{"id": "build", "title": "Build feature"}]).ok

    missing = controller.update_work_step("build", "done")
    assert missing.blocked
    assert missing.block_reason == "missing work step evidence"
    assert "Plan step ids are created before verification" in missing.content
    assert "available recent evidence_ids: <none>" in missing.content

    unknown = controller.update_work_step("build", "done", evidence_ids=["ev-missing"])
    assert unknown.blocked
    assert unknown.block_reason == "unknown evidence ids"

    record = state.add_evidence_record(
        kind="test",
        source_tool="run_command",
        summary="command passed: pytest -q",
        command="pytest -q",
    )
    updated = controller.update_work_step("build", "done", evidence_ids=[record.id])

    assert updated.ok
    assert state.find_work_step("build").status == "done"  # type: ignore[union-attr]
    assert state.find_work_step("build").evidence_ids == [record.id]  # type: ignore[union-attr]
    assert updated.metadata["step"]["id"] == "build"
    assert updated.metadata["step"]["title"] == "Build feature"
    assert updated.metadata["step"]["evidence_ids"] == [record.id]
    assert updated.metadata["evidence_records"][0]["id"] == record.id
    assert updated.metadata["evidence_records"][0]["summary"] == "command passed: pytest -q"


def test_accept_work_step_records_manual_acceptance() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("finish demo").ok
    assert controller.set_work_plan([{"id": "profile", "title": "Profile page"}]).ok

    accepted = controller.accept_work_step(
        "profile",
        "user confirmed this was already completed",
        source="user_confirmed",
    )

    assert accepted.ok
    item = state.find_work_step("profile")
    assert item is not None
    assert item.status == "done"
    assert item.evidence_ids == ["ev-1"]
    assert state.evidence_records[0].kind == "user_acceptance"
    assert state.evidence_records[0].source_tool == "accept_work_step"
    assert state.evidence_records[0].status == "user_confirmed"
    assert "user confirmed" in state.evidence_records[0].summary
    assert accepted.metadata["acceptance_source"] == "user_confirmed"
    assert accepted.metadata["acceptance_kind"] == "user_acceptance"


def test_accept_work_step_defaults_to_model_review_acceptance() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("finish demo").ok
    assert controller.set_work_plan([{"id": "profile", "title": "Profile page"}]).ok

    accepted = controller.accept_work_step("profile", "reviewed existing logs and screenshots")

    assert accepted.ok
    assert state.evidence_records[0].kind == "model_review_acceptance"
    assert state.evidence_records[0].status == "model_reviewed_existing_evidence"
    assert "model review acceptance" in state.evidence_records[0].summary
    assert accepted.metadata["acceptance_source"] == "model_reviewed_existing_evidence"


def test_merge_work_plan_preserves_existing_progress_and_adds_new_items() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("finish demo").ok
    assert controller.set_work_plan(
        [
            {"id": "auth", "title": "Auth flow"},
            {"id": "profile", "title": "Profile page"},
        ]
    ).ok
    record = state.add_evidence_record(
        kind="smoke",
        source_tool="http_request",
        summary="profile API returned 200",
    )
    assert controller.update_work_step("profile", "done", evidence_ids=[record.id]).ok

    merged = controller.merge_work_plan(
        [
            {"id": "profile", "title": "Profile page"},
            {"id": "pets", "title": "Pet dashboard"},
        ],
        source_document="PLAN.md",
    )

    assert merged.ok
    assert state.find_work_step("profile").status == "done"  # type: ignore[union-attr]
    assert state.find_work_step("profile").evidence_ids == [record.id]  # type: ignore[union-attr]
    assert state.find_work_step("pets") is not None
    assert state.find_work_step("pets").status == "pending"  # type: ignore[union-attr]


def test_merge_work_plan_document_done_creates_document_acceptance() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("finish demo").ok

    merged = controller.merge_work_plan(
        [{"id": "auth", "title": "Auth flow", "status": "done"}],
        source_document="PLAN.md",
    )

    assert merged.ok
    item = state.find_work_step("auth")
    assert item is not None
    assert item.status == "done"
    assert item.evidence_ids == ["ev-1"]
    assert state.evidence_records[0].kind == "document_acceptance"
    assert state.evidence_records[0].path == "PLAN.md"


def test_work_step_unknown_evidence_id_lists_recent_available_ids() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("finish demo").ok
    assert controller.set_work_plan([{"id": "build", "title": "Build feature"}]).ok
    record = state.add_evidence_record(
        kind="test",
        source_tool="run_command",
        summary="command passed: pytest -q",
        command="pytest -q",
    )

    unknown = controller.update_work_step("build", "done", evidence_ids=["ev-missing"])

    assert unknown.blocked
    assert unknown.block_reason == "unknown evidence ids"
    assert record.id in unknown.content
    assert "pytest -q" in unknown.content
    assert unknown.metadata["suggested_evidence_ids"] == [record.id]
    assert unknown.metadata["retry_arguments"] == {
        "step_id": "build",
        "status": "done",
        "evidence_ids": [record.id],
    }
    assert f"evidence_ids={[record.id]!r}" in unknown.content


def test_work_step_missing_evidence_suggests_recent_evidence_retry() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("finish demo").ok
    assert controller.set_work_plan([{"id": "notify", "title": "Task publish notification"}]).ok
    old_record = state.add_evidence_record(
        kind="smoke",
        source_tool="http_request",
        summary="older notification check passed",
    )
    latest_record = state.add_evidence_record(
        kind="smoke",
        source_tool="http_request",
        summary="latest task publish notification API returned 200",
    )

    missing = controller.update_work_step("notify", "done")

    assert missing.blocked
    assert missing.block_reason == "missing work step evidence"
    assert missing.metadata["suggested_evidence_ids"][:2] == [latest_record.id, old_record.id]
    assert missing.metadata["retry_arguments"]["evidence_ids"] == [latest_record.id]
    assert f"evidence_ids={[latest_record.id]!r}" in missing.content


def test_goal_complete_requires_required_steps_done_with_evidence() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("finish demo").ok
    assert controller.set_work_plan(
        [
            {"id": "required", "title": "Required step"},
            {"id": "optional", "title": "Optional step", "required": False},
        ]
    ).ok
    state.add_acceptance_evidence("command passed: pytest -q")

    incomplete = controller.update_goal("complete")
    assert not incomplete.ok
    assert not incomplete.blocked
    assert incomplete.block_reason == "required work steps not done"
    assert incomplete.metadata["recoverable"] is True
    assert incomplete.metadata["incomplete_steps"][0]["id"] == "required"

    record = state.add_evidence_record(
        kind="test",
        source_tool="run_command",
        summary="command passed: pytest -q",
        command="pytest -q",
    )
    assert controller.update_work_step("required", "done", evidence_ids=[record.id]).ok
    completed = controller.update_goal("complete")

    assert completed.ok
    assert state.goal_status == "complete"


def test_goal_complete_without_acceptance_evidence_is_recoverable() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("finish demo").ok

    result = controller.update_goal("complete")

    assert not result.ok
    assert not result.blocked
    assert result.block_reason == "missing acceptance evidence"
    assert result.metadata["recoverable"] is True
    assert state.goal_status == "active"


def test_goal_review_after_completed_work_plan_discourages_broad_rediscovery() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.goal_controller import GoalTurnState
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("finish demo").ok
    assert controller.set_work_plan([{"id": "auth", "title": "Auth flow"}]).ok
    record = state.add_evidence_record(
        kind="smoke",
        source_tool="http_request",
        summary="auth smoke passed",
    )
    assert controller.update_work_step("auth", "done", evidence_ids=[record.id]).ok

    decision = controller.plan_completion_review_decision(GoalTurnState(requested=True))

    assert decision is not None
    assert decision.expected_action == "review_goal_after_plan_done"
    assert "call update_goal(status='complete')" in decision.runtime_message
    assert "instead of starting broad rediscovery" in decision.runtime_message
    assert "do not loop over source files" in decision.runtime_message


def test_verification_progress_classification_excludes_pure_inspection() -> None:
    from minicodex2.agent.unified_session import _tool_result_counts_as_verification_progress
    from minicodex2.tools.results import ToolResult

    assert _tool_result_counts_as_verification_progress(
        "run_command",
        ToolResult(ok=True, content="command passed"),
    )
    assert _tool_result_counts_as_verification_progress(
        "http_request",
        ToolResult(ok=True, content="200 OK"),
    )
    assert _tool_result_counts_as_verification_progress(
        "read_file",
        ToolResult(ok=True, content="source"),
    ) is False
    assert _tool_result_counts_as_verification_progress(
        "edit_file",
        ToolResult(ok=True, content="changed", did_write=True),
    )


def test_completed_goal_archives_when_new_goal_is_created() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("phase one").ok
    assert controller.set_work_plan([{"id": "auth", "title": "Auth flow"}]).ok
    record = state.add_evidence_record(
        kind="test",
        source_tool="run_command",
        summary="phase one tests passed",
    )
    assert controller.update_work_step("auth", "done", evidence_ids=[record.id]).ok
    state.add_acceptance_evidence("phase one accepted")
    assert controller.update_goal("complete").ok

    created = controller.create_goal("phase two")

    assert created.ok
    assert state.active_goal == "phase two"
    assert state.goal_status == "active"
    assert state.work_plan == []
    assert state.archived_goals
    assert state.archived_goals[-1]["objective"] == "phase one"
    assert state.archived_goals[-1]["status"] == "complete"
    assert state.archived_goals[-1]["work_plan"][0]["id"] == "auth"


def test_create_goal_extends_unfinished_active_goal() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    assert controller.create_goal("finish phase one").ok
    assert controller.set_work_plan([{"id": "auth", "title": "Auth flow"}]).ok

    result = controller.create_goal("add image and video upload support")

    assert result.ok
    assert not result.blocked
    assert state.active_goal == "finish phase one"
    assert state.goal_status == "active"
    assert len(state.work_plan) == 2
    assert state.work_plan[-1].id.startswith("goal-extension-")
    assert "add image and video upload support" in state.work_plan[-1].title
    assert state.work_plan[-1].status == "pending"
    assert result.metadata["goal_event"] == "extended"


def test_goal_snapshot_round_trips_work_plan() -> None:
    from minicodex2.agent_os.goal_controller import GoalController
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    controller = GoalController(state)
    controller.create_goal("build demo", token_budget=123)
    controller.set_work_plan([{"id": "step-a", "title": "Do A", "status": "in_progress"}])
    state.add_acceptance_evidence("command passed: pytest")
    state.add_evidence_record(
        kind="test",
        source_tool="run_command",
        summary="command passed: pytest",
        command="pytest",
    )

    restored = SessionState()
    restored.load_goal_snapshot(state.goal_snapshot())

    assert restored.active_goal == "build demo"
    assert restored.goal_status == "active"
    assert restored.goal_token_budget == 123
    assert restored.current_step_id == "step-a"
    assert restored.work_plan[0].title == "Do A"
    assert restored.work_plan[0].evidence == ["command passed: pytest"]
    assert restored.evidence_records[0].summary == "command passed: pytest"


def test_goal_snapshot_round_trips_engineering_facts() -> None:
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    state.add_engineering_fact(
        type="config_ref",
        source="frontend/package.json",
        summary="package proxy targets http://127.0.0.1:8000",
        data={"target": "http://127.0.0.1:8000", "port": 8000},
        confidence="high",
    )

    restored = SessionState()
    restored.load_goal_snapshot(state.goal_snapshot())

    assert restored.engineering_facts[0].type == "config_ref"
    assert restored.engineering_facts[0].data["port"] == 8000
    assert restored._next_engineering_fact_index == 2


def test_goal_snapshot_round_trips_lessons() -> None:
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    state.add_lesson(
        summary="When a tool fails, inspect evidence before adding runtime special cases.",
        trigger="tool failure",
        scope="agent-runtime-boundary",
        evidence="read_file path missing",
    )

    restored = SessionState()
    restored.load_goal_snapshot(state.goal_snapshot())

    assert restored.lessons[0].id == "lesson-1"
    assert restored.lessons[0].scope == "agent-runtime-boundary"
    assert restored.relevant_lessons("tool failure runtime")[0].summary.startswith("When a tool fails")


def test_context_injects_corrigibility_principle_and_lesson_memory() -> None:
    stable_context = ContextManager().build_context(history=ChatHistory(), runtime_summary={"workspace": "demo"})
    runtime_text = "\n".join(message.content for message in stable_context.messages)
    dynamic_text = _formatted_runtime_summary_text(
        {
            "lessons": [
                {
                    "id": "lesson-1",
                    "summary": "Prefer evidence collection over special-case semantic runtime code.",
                    "trigger": "frontend/backend debugging",
                    "scope": "agent-runtime-boundary",
                    "evidence": "directory and HTTP facts were enough for the model to reason",
                }
            ]
        }
    )

    assert "Corrigibility principle" in runtime_text
    assert "Verification cost rule" in runtime_text
    assert "Use UI/browser E2E to prove the user-facing path works" in runtime_text
    assert "smallest controllable fixture" in runtime_text
    assert "stop exploration and record it with update_work_step" in runtime_text
    assert "[LESSON MEMORY]" in dynamic_text
    assert "Prefer evidence collection" in dynamic_text


def test_project_memory_store_persists_searches_and_updates(tmp_path: Path) -> None:
    from minicodex2.agent.project_memory import ProjectMemoryStore

    store = ProjectMemoryStore(tmp_path / ".minicodex2")
    fact = store.remember(
        title="Backend startup command",
        content="Run studentPet/backend/venv/Scripts/python.exe manage.py runserver 0.0.0.0:8000 from studentPet/backend.",
        tags=["startup", "python", "backend"],
        confidence="high",
        source="verified run",
    )
    same = store.remember(
        title="Backend startup command",
        content="Use project-local backend venv before Django runserver.",
        tags=["startup", "python", "backend"],
        confidence="high",
    )

    reloaded = ProjectMemoryStore(tmp_path / ".minicodex2")
    matches = reloaded.search(query="django startup", tags=["backend"], limit=5)

    assert same.id == fact.id
    assert len(reloaded.all()) == 1
    assert matches[0].id == fact.id
    assert "backend venv" in matches[0].content

    updated = reloaded.update(fact.id, stale=True)
    assert updated is not None
    assert updated.stale is True
    assert reloaded.search(query="django startup") == []
    assert reloaded.search(query="django startup", include_stale=True)[0].id == fact.id


def test_context_injects_project_fact_memory_hot_and_index() -> None:
    runtime_text = _formatted_runtime_summary_text(
        {
            "project_fact_memory_hot": [
                {
                    "id": "mem-1",
                    "title": "Backend startup command",
                    "content": "Run project-local Python from backend/venv before manage.py runserver.",
                    "tags": ["startup", "backend"],
                    "scope": "studentPet",
                    "confidence": "high",
                    "stale": False,
                }
            ],
            "project_fact_memory_index": [
                {
                    "id": "mem-1",
                    "title": "Backend startup command",
                    "summary": "Run project-local Python from backend/venv.",
                    "tags": ["startup", "backend"],
                    "scope": "studentPet",
                    "confidence": "high",
                    "stale": False,
                },
                {
                    "id": "mem-2",
                    "title": "Frontend smoke command",
                    "summary": "Use npm run build in frontend.",
                    "tags": ["verify", "frontend"],
                    "scope": "studentPet",
                    "confidence": "medium",
                    "stale": False,
                },
            ],
        }
    )

    assert "[PROJECT FACT MEMORY]" in runtime_text
    assert "Run project-local Python" in runtime_text
    assert "mem-2: Frontend smoke command" in runtime_text
    assert "search_project_memory" in runtime_text


def test_project_memory_index_prunes_ignored_dependency_dirs(tmp_path: Path) -> None:
    from minicodex2.agent.project_memory import ProjectMemoryIndex

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "plan.md").write_text("# Real Plan\n", encoding="utf-8")
    ignored = tmp_path / "node_modules" / "pkg"
    ignored.mkdir(parents=True)
    (ignored / "README.md").write_text("# Dependency Readme\n", encoding="utf-8")

    indexed = ProjectMemoryIndex().build(tmp_path)
    paths = {item.path for item in indexed}

    assert "docs/plan.md" in paths
    assert "node_modules/pkg/README.md" not in paths


def test_project_memory_tools_persist_between_session_instances(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.config.settings import AppSettings
    from minicodex2.model.fake_adapter import FakeModelAdapter
    from minicodex2.tools.registry import ToolRegistry

    settings = AppSettings(workspace_root=tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=FakeModelAdapter(),
        tools=ToolRegistry(),
    )
    result = session.tools.execute(
        "remember_project_fact",
        {
            "title": "Project Python",
            "content": "Use .venv/Scripts/python.exe for local commands.",
            "tags": ["python", "toolchain"],
            "confidence": "high",
        },
    )
    assert result.ok

    resumed = UnifiedAgentSession(
        settings=settings,
        model=FakeModelAdapter(),
        tools=ToolRegistry(),
    )
    search = resumed.tools.execute("search_project_memory", {"query": "python local commands"})

    assert search.ok
    assert "Project Python" in search.content
    assert (tmp_path / ".minicodex2" / "memory" / "project_facts.json").exists()


def test_workflow_memory_store_persists_searches_and_updates(tmp_path: Path) -> None:
    from minicodex2.agent.project_memory import WorkflowMemoryStore

    store = WorkflowMemoryStore(tmp_path / ".minicodex2")
    workflow = store.remember(
        title="Start backend",
        cwd="studentPet/backend",
        command="venv\\Scripts\\python.exe manage.py runserver 0.0.0.0:8000",
        purpose="startup",
        ready_check="http://127.0.0.1:8000/",
        steps=["Start backend service", "Smoke http://127.0.0.1:8000/"],
        ports=[8000],
        tags=["backend", "server"],
        confidence="medium",
        notes="Use the project-local backend venv.",
    )
    same = store.remember(
        title="Start backend",
        cwd="studentPet/backend",
        command="venv\\Scripts\\python.exe manage.py runserver 0.0.0.0:8000",
        purpose="startup",
        tags=["backend"],
        confidence="high",
    )

    reloaded = WorkflowMemoryStore(tmp_path / ".minicodex2")
    matches = reloaded.search(query="backend server", purpose="startup", limit=5)

    assert same.id == workflow.id
    assert len(reloaded.all()) == 1
    assert matches[0].id == workflow.id
    assert matches[0].confidence == "high"
    assert matches[0].steps == ["Start backend service", "Smoke http://127.0.0.1:8000/"]

    updated = reloaded.update(workflow.id, success_delta=2)
    assert updated is not None
    assert updated.success_count == 2
    assert updated.confidence == "high"

    stale = reloaded.update(workflow.id, failure_delta=4)
    assert stale is not None
    assert stale.stale is True
    assert reloaded.search(query="backend server") == []
    assert reloaded.search(query="backend server", include_stale=True)[0].id == workflow.id


def test_context_injects_workflow_memory_hot_and_index() -> None:
    runtime_text = _formatted_runtime_summary_text(
        {
            "workflow_memory_hot": [
                {
                    "id": "wf-1",
                    "title": "Start backend",
                    "cwd": "studentPet/backend",
                    "command": "venv\\Scripts\\python.exe manage.py runserver 0.0.0.0:8000",
                    "purpose": "startup",
                    "ready_check": "http://127.0.0.1:8000/",
                    "steps": ["Start backend", "GET /api/profile/ with token"],
                    "ports": [8000],
                    "tags": ["backend"],
                    "confidence": "high",
                    "stale": False,
                    "success_count": 2,
                    "failure_count": 0,
                    "notes": "Use project-local backend venv.",
                }
            ],
            "workflow_memory_index": [
                {
                    "id": "wf-1",
                    "title": "Start backend",
                    "cwd": "studentPet/backend",
                    "command": "venv\\Scripts\\python.exe manage.py runserver 0.0.0.0:8000",
                    "purpose": "startup",
                    "confidence": "high",
                    "stale": False,
                },
                {
                    "id": "wf-2",
                    "title": "Build frontend",
                    "cwd": "studentPet/frontend",
                    "command": "npm run build",
                    "purpose": "build",
                    "confidence": "medium",
                    "stale": False,
                },
            ],
        }
    )

    assert "[WORKFLOW MEMORY]" in runtime_text
    assert "Start backend" in runtime_text
    assert "venv\\Scripts\\python.exe manage.py runserver" in runtime_text
    assert "GET /api/profile/ with token" in runtime_text
    assert "wf-2: Build frontend" in runtime_text
    assert "search_workflows" in runtime_text


def test_workflow_memory_tools_persist_between_session_instances(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.config.settings import AppSettings
    from minicodex2.model.fake_adapter import FakeModelAdapter
    from minicodex2.tools.registry import ToolRegistry

    settings = AppSettings(workspace_root=tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=FakeModelAdapter(),
        tools=ToolRegistry(),
    )
    result = session.tools.execute(
        "remember_workflow",
        {
            "title": "Frontend build",
            "cwd": "app/frontend",
            "command": "npm run build",
            "purpose": "build",
            "steps": ["Run frontend build"],
            "tags": ["frontend", "build"],
            "confidence": "high",
        },
    )
    assert result.ok

    resumed = UnifiedAgentSession(
        settings=settings,
        model=FakeModelAdapter(),
        tools=ToolRegistry(),
    )
    search = resumed.tools.execute("search_workflows", {"query": "frontend build"})

    assert search.ok
    assert "Frontend build" in search.content
    assert (tmp_path / ".minicodex2" / "memory" / "workflows.json").exists()


def test_goal_snapshot_drops_legacy_http_passed_acceptance() -> None:
    from minicodex2.agent_os.state import SessionState

    restored = SessionState()
    restored.load_goal_snapshot(
        {
            "active_goal": "finish integration",
            "goal_status": "active",
            "acceptance_evidence": [
                "http_request passed: http://127.0.0.1:3001/api/auth/login/ status=200",
                "command passed: npm run build",
            ],
            "work_plan": [
                {
                    "id": "auth",
                    "title": "Finish auth",
                    "status": "done",
                    "evidence": [
                        "http_request passed: http://127.0.0.1:3001/api/auth/login/ status=200",
                        "command passed: npm run build",
                    ],
                }
            ],
        }
    )

    assert restored.acceptance_evidence == ["command passed: npm run build"]
    assert restored.work_plan[0].evidence == ["command passed: npm run build"]


def test_evidence_store_labels_http_as_diagnostic() -> None:
    from minicodex2.agent.chat_history import ChatHistory
    from minicodex2.agent.context import ContextManager

    runtime_text = _formatted_runtime_summary_text(
        {
            "evidence_records": [
                {
                    "id": "ev-1",
                    "kind": "smoke",
                    "source_tool": "http_request",
                    "summary": "http_request observed: http://127.0.0.1:3001/login status=200",
                    "url": "http://127.0.0.1:3001/login",
                }
            ]
        }
    )

    assert "HTTP observations are diagnostic evidence" in runtime_text
    assert "ev-1: smoke via http_request" in runtime_text


def test_goal_snapshot_round_trips_runtime_resource_map() -> None:
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    state.add_runtime_resource(
        kind="http",
        source_tool="http_request",
        url="http://127.0.0.1:8000/api/tasks",
        method="GET",
        status_code=404,
        status="failed",
        summary="HTTP GET http://127.0.0.1:8000/api/tasks status=404 ok=False",
        data={"error": "not found"},
    )

    restored = SessionState()
    restored.load_goal_snapshot(state.goal_snapshot())

    resource = restored.runtime_resources[0]
    assert resource.kind == "http"
    assert resource.url == "http://127.0.0.1:8000/api/tasks"
    assert resource.status_code == 404
    assert resource.data["error"] == "not found"


def test_goal_snapshot_round_trips_workspace_facts() -> None:
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    state.observe_workspace_entry(path="src/app.py", kind="file", source_tool="list_directory", size=120)
    state.record_read(
        path="src/app.py",
        source_tool="read_file",
        size=120,
        excerpt="def main(): pass",
        content_hash="abc123",
        mtime=123.5,
        line_count=4,
        start_line=1,
        end_line=2,
    )
    state.record_read(
        path="src/app.py",
        source_tool="read_file",
        size=120,
        excerpt="def main(): pass",
        content_hash="abc123",
        mtime=123.5,
        line_count=4,
        start_line=3,
        end_line=4,
    )
    state.record_change(path="src/app.py", source_tool="edit_file", action="edited", summary="edited src/app.py")

    restored = SessionState()
    restored.load_goal_snapshot(state.goal_snapshot())

    assert restored.workspace_entries[0].path == "src/app.py"
    assert restored.read_records[0].excerpt == "def main(): pass"
    assert restored.read_records[0].content_hash == "abc123"
    assert restored.read_records[0].mtime == 123.5
    assert restored.read_records[0].read_count == 2
    assert restored.read_records[0].line_count == 4
    assert restored.read_records[0].line_ranges == [[1, 4]]
    assert restored.read_records[0].full_read_count == 1
    assert restored.change_records[0].action == "edited"


def test_workspace_context_formats_working_set() -> None:
    from minicodex2.agent.chat_history import ChatHistory
    from minicodex2.agent.context import ContextManager

    runtime_text = _formatted_runtime_summary_text(
        {
            "read_records": [
                {
                    "path": "frontend/package.json",
                    "source_tool": "read_file",
                    "size": 500,
                    "content_hash": "abcdef1234567890",
                    "read_count": 3,
                    "line_count": 10,
                    "line_ranges": [[1, 4], [8, 10]],
                    "full_read_count": 0,
                    "last_start_line": 8,
                    "last_end_line": 10,
                    "excerpt": '{"proxy":"http://127.0.0.1:8000"}',
                    "observed_at": "2026-06-16T00:00:00+00:00",
                }
            ],
            "change_records": [
                {
                    "path": "frontend/package.json",
                    "source_tool": "edit_file",
                    "action": "edited",
                    "observed_at": "2026-06-16T00:01:00+00:00",
                }
            ],
        }
    )

    assert "WorkingSet recently read files" in runtime_text
    assert "hash=abcdef123456" in runtime_text
    assert "reads=3" in runtime_text
    assert "line_coverage=1-4,8-10" in runtime_text
    assert "line_count=10" in runtime_text
    assert "coverage_pct=70" in runtime_text
    assert "last_read=8-10" in runtime_text
    assert "changed-after-read=true" in runtime_text


def test_workspace_context_is_preserved_under_runtime_budget_pressure() -> None:
    from minicodex2.agent.chat_history import ChatHistory
    from minicodex2.agent.context import ContextManager

    runtime_text = _formatted_runtime_summary_text(
        {
            "read_records": [
                {
                    "path": "frontend/src/pages/TaskDetailPage.js",
                    "source_tool": "read_file",
                    "size": 39000,
                    "content_hash": "taskdetailhashabcdef",
                    "read_count": 8,
                    "line_count": 760,
                    "line_ranges": [[1, 180], [181, 360], [361, 560]],
                    "full_read_count": 0,
                    "last_start_line": 361,
                    "last_end_line": 560,
                    "excerpt": "Task detail page contains upload previews and media overlay code.",
                    "observed_at": "2026-06-16T00:00:00+00:00",
                }
            ],
            "evidence_pack": {
                "latest_tool_facts": [
                    "tool fact " + ("x" * 240)
                    for _ in range(30)
                ],
                "latest_failures": [
                    "failure " + ("y" * 240)
                    for _ in range(30)
                ],
            },
            "engineering_facts": [
                {
                    "id": f"fact-{index}",
                    "type": "runtime",
                    "source": "test",
                    "summary": "runtime fact " + ("z" * 240),
                    "confidence": "medium",
                }
                for index in range(30)
            ],
        },
        token_budget=1_200,
        section_token_budget=220,
    )

    assert "WorkingSet recently read files" in runtime_text
    assert "frontend/src/pages/TaskDetailPage.js" in runtime_text
    assert "line_coverage=1-180,181-360,361-560" in runtime_text


def test_read_coverage_resets_when_file_hash_changes() -> None:
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    state.record_read(
        path="src/app.py",
        source_tool="read_file",
        content_hash="oldhash",
        line_count=10,
        start_line=1,
        end_line=10,
    )
    state.record_read(
        path="src/app.py",
        source_tool="read_file",
        content_hash="newhash",
        line_count=10,
        start_line=8,
        end_line=10,
    )

    record = state.read_records[0]
    assert record.content_hash == "newhash"
    assert record.line_ranges == [[8, 10]]
    assert record.full_read_count == 0


def test_read_file_returns_fingerprint_metadata(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    result = runtime.read_file("app.py")

    assert result.ok
    assert result.metadata["path"] == "app.py"
    assert result.metadata["size"] == (tmp_path / "app.py").stat().st_size
    assert isinstance(result.metadata["mtime"], float)
    assert len(str(result.metadata["sha256"])) == 64
    assert result.metadata["truncated"] is False


def test_edit_file_falls_back_to_old_text_when_invalid_line_range_is_mixed_in(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    target = tmp_path / "app.js"
    target.write_text("const color = 'blue';\n", encoding="utf-8")
    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    result = runtime.edit_file(
        "app.js",
        old_text="const color = 'blue';",
        new_text="const color = 'green';",
        start_line=0,
        end_line=0,
        content="",
    )

    assert result.ok
    assert result.did_write
    assert target.read_text(encoding="utf-8") == "const color = 'green';\n"


def test_edit_file_invalid_line_range_reports_recovery_metadata(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "app.js").write_text("one\ntwo\n", encoding="utf-8")
    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    result = runtime.edit_file("app.js", start_line=0, end_line=0, content="zero\n")

    assert not result.ok
    assert result.blocked
    assert result.block_reason == "invalid line range"
    assert result.metadata["failure_kind"] == "invalid_tool_arguments"
    assert result.metadata["line_count"] == 2
    assert result.metadata["requested_start_line"] == 0
    assert result.metadata["valid_line_range"] == "1-2"


def test_list_directory_defaults_to_one_level_with_metadata(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    result = runtime.list_directory(".")

    assert result.ok
    entries = json.loads(result.content)
    paths = {entry["path"] for entry in entries}
    assert "src" in paths
    assert "README.md" in paths
    assert "src/app.py" not in paths
    readme = next(entry for entry in entries if entry["path"] == "README.md")
    assert readme["type"] == "file"
    assert readme["extension"] == ".md"
    assert isinstance(readme["size"], int)


def test_list_directory_can_return_bounded_recursive_tree(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (nested / "deep.py").write_text("print('deep')\n", encoding="utf-8")
    (tmp_path / ".secret").write_text("hidden\n", encoding="utf-8")
    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    result = runtime.list_directory(".", recursive=True, max_depth=2)

    assert result.ok
    entries = json.loads(result.content)
    paths = {entry["path"] for entry in entries}
    assert "src/app.py" in paths
    assert "src/pkg" in paths
    assert "src/pkg/deep.py" not in paths
    assert ".secret" not in paths


def test_list_directory_accepts_glob_filter(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    (tmp_path / "docs" / "notes.txt").write_text("notes\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    result = runtime.list_directory(".", recursive=True, max_depth=2, glob="*.md")

    assert result.ok
    entries = json.loads(result.content)
    paths = {entry["path"] for entry in entries}
    assert paths == {"docs/PLAN.md"}
    assert result.metadata["glob"] == "*.md"


def test_search_files_finds_text_without_scanning_dependency_dirs(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "models.py").write_text("class PointsLog:\n    pass\n", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.py").write_text("class PointsLog:\n    pass\n", encoding="utf-8")
    (tmp_path / "venv" / "Lib").mkdir(parents=True)
    (tmp_path / "venv" / "Lib" / "site.py").write_text("class PointsLog:\n    pass\n", encoding="utf-8")
    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    result = runtime.search_files("PointsLog", glob="*.py")

    assert result.ok
    payload = json.loads(result.content)
    paths = {item["path"] for item in payload["results"]}
    assert paths == {"src/models.py"}


def test_search_files_accepts_recurse_alias(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "src" / "pages").mkdir(parents=True)
    (tmp_path / "src" / "pages" / "TaskDetailPage.js").write_text("export const marker = 'TaskDetail';\n", encoding="utf-8")
    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    result = runtime.search_files("TaskDetail", root="src", glob="*.js", recurse=True)

    assert result.ok
    payload = json.loads(result.content)
    assert payload["recursive"] is True
    assert [item["path"] for item in payload["results"]] == ["src/pages/TaskDetailPage.js"]


def test_search_files_accepts_path_alias_for_root(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "src" / "pages").mkdir(parents=True)
    (tmp_path / "src" / "pages" / "TaskDetailPage.js").write_text("export const marker = 'TaskDetail';\n", encoding="utf-8")
    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    result = runtime.search_files("TaskDetail", path="src", glob="*.js")

    assert result.ok
    payload = json.loads(result.content)
    assert payload["root"] == "src"
    assert [item["path"] for item in payload["results"]] == ["src/pages/TaskDetailPage.js"]


def test_search_files_accepts_path_alias_for_single_file(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "src" / "pages").mkdir(parents=True)
    (tmp_path / "src" / "pages" / "TaskDetailPage.js").write_text(
        "const [videoModalUrl, setVideoModalUrl] = useState(null);\n",
        encoding="utf-8",
    )
    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    result = runtime.search_files("videoModalUrl", path="src/pages/TaskDetailPage.js")

    assert result.ok
    payload = json.loads(result.content)
    assert payload["root"] == "src/pages/TaskDetailPage.js"
    assert payload["results"] == [
        {
            "path": "src/pages/TaskDetailPage.js",
            "line": 1,
            "text": "const [videoModalUrl, setVideoModalUrl] = useState(null);",
        }
    ]


def test_search_files_emits_diagnostic_events_for_single_file(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "src" / "pages").mkdir(parents=True)
    (tmp_path / "src" / "pages" / "TaskDetailPage.js").write_text(
        "const [videoModalUrl, setVideoModalUrl] = useState(null);\n"
        "const other = true;\n",
        encoding="utf-8",
    )
    runtime = RuntimeTools(ConfigLoader().load(tmp_path))
    events: list[tuple[str, dict[str, object]]] = []
    runtime.progress_sink = lambda event_type, payload: events.append((event_type, payload))

    result = runtime.search_files("videoModalUrl", path="src/pages/TaskDetailPage.js")

    assert result.ok
    assert events[0][0] == "search_files_started"
    assert events[0][1]["root_kind"] == "file"
    assert events[0][1]["candidate_files"] == 1
    assert events[1][0] == "search_files_finished"
    assert events[1][1]["match_count"] == 1
    assert events[1][1]["matched_lines"][0]["line"] == 1


def test_search_files_can_disable_recursion(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "src" / "pages").mkdir(parents=True)
    (tmp_path / "src" / "root.js").write_text("const marker = 'TaskDetail';\n", encoding="utf-8")
    (tmp_path / "src" / "pages" / "TaskDetailPage.js").write_text("const marker = 'TaskDetail';\n", encoding="utf-8")
    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    result = runtime.search_files("TaskDetail", root="src", glob="*.js", recursive=False)

    assert result.ok
    payload = json.loads(result.content)
    assert payload["recursive"] is False
    assert [item["path"] for item in payload["results"]] == ["src/root.js"]


def test_goal_snapshot_load_drops_legacy_service_ready_acceptance() -> None:
    from minicodex2.agent_os.state import SessionState

    restored = SessionState()
    restored.load_goal_snapshot(
        {
            "objective": "finish app",
            "status": "active",
            "acceptance_evidence": [
                "service ready: python manage.py runserver 127.0.0.1:3001 on port 3001",
                "command passed: npm run build",
            ],
            "evidence_records": [
                {
                    "id": "ev-1",
                    "kind": "service",
                    "source_tool": "start_background_command",
                    "summary": "service ready: python manage.py runserver 127.0.0.1:3001 on port 3001",
                },
                {
                    "id": "ev-2",
                    "kind": "build",
                    "source_tool": "run_command",
                    "summary": "command passed: npm run build",
                    "command": "npm run build",
                },
            ],
            "work_plan": [
                {
                    "id": "step-1",
                    "title": "Verify app",
                    "evidence": [
                        "service ready: python manage.py runserver 127.0.0.1:3001 on port 3001",
                        "command passed: npm run build",
                    ],
                    "evidence_ids": ["ev-1", "ev-2"],
                }
            ],
        }
    )

    assert restored.acceptance_evidence == ["command passed: npm run build"]
    assert [record.id for record in restored.evidence_records] == ["ev-2"]
    assert restored.work_plan[0].evidence == ["command passed: npm run build"]
    assert restored.work_plan[0].evidence_ids == ["ev-2"]


def test_context_injects_structured_work_memory() -> None:
    manager = ContextManager()
    history = ChatHistory()
    history.add_user("continue")

    context = manager.build_context(
        history=history,
        runtime_summary={
            "active_goal": "finish demo",
            "goal_status": "active",
            "current_step_id": "tasks",
            "work_plan": [
                {
                    "id": "auth",
                    "title": "Finish auth",
                    "status": "done",
                    "evidence": ["register 200"],
                    "evidence_ids": ["ev-1"],
                },
                {
                    "id": "tasks",
                    "title": "Finish tasks",
                    "status": "in_progress",
                    "acceptance": "GET /api/tasks returns data",
                    "verification_hint": "HTTP smoke task list",
                    "source_document": "docs/milestones.md",
                },
            ],
            "evidence_records": [
                {
                    "id": "ev-1",
                    "kind": "smoke",
                    "source_tool": "http_request",
                    "summary": "http_request passed: /register status=200",
                    "url": "http://127.0.0.1/register",
                }
            ],
        },
    )

    runtime = "\n".join(message.content for message in context.messages if message.role == "runtime")
    assert "[WORK MEMORY]" not in runtime
    assert "RootObjective: finish demo" not in runtime
    assert "[EVIDENCE TAIL]" not in runtime


def test_context_hints_goal_closure_when_required_work_plan_is_done() -> None:
    manager = ContextManager()
    history = ChatHistory()
    history.add_user("continue")

    runtime = _formatted_runtime_summary_text(
        {
            "active_goal": "finish demo",
            "goal_status": "active",
            "work_plan": [
                {
                    "id": "auth",
                    "title": "Finish auth",
                    "status": "done",
                    "evidence": ["register 200"],
                    "evidence_ids": ["ev-1"],
                },
                {
                    "id": "tasks",
                    "title": "Finish tasks",
                    "status": "done",
                    "evidence": ["tasks 200"],
                    "evidence_ids": ["ev-2"],
                },
            ],
        }
    )

    assert "required_done=2; required_pending=0; required_blocked=0; missing_evidence=0" in runtime
    assert "WorkPlanClosureHint: all required WorkPlan steps are done with evidence" in runtime
    assert "prefer update_goal(status='complete')" in runtime


def test_context_instructs_model_to_align_verification_with_changed_files() -> None:
    manager = ContextManager()
    history = ChatHistory()
    history.add_user("continue")

    context = manager.build_context(
        history=history,
        runtime_summary={
            "changed_files": ["demo/frontend/src/App.js"],
            "workspace_entries": [
                {
                    "path": "demo/frontend/package.json",
                    "kind": "file",
                    "source_tool": "list_directory",
                },
                {
                    "path": "demo/backend/requirements.txt",
                    "kind": "file",
                    "source_tool": "list_directory",
                },
            ],
        },
    )

    runtime = "\n".join(message.content for message in context.messages if message.role == "runtime")
    assert "compare changed_files, nearby directory/file facts, and the proposed command" in runtime
    assert "Do not treat an unrelated syntax/build check as acceptance evidence" in runtime
    assert "Use read_background_log" in runtime


def test_context_declares_runtime_messages_as_prompt_protocol() -> None:
    manager = ContextManager()
    history = ChatHistory()
    history.add_user("continue")

    context = manager.build_context(
        history=history,
        runtime_summary={
            "active_goal": "finish demo",
            "goal_status": "active",
            "evidence_records": [{"id": "ev-1", "summary": "build passed"}],
        },
    )

    runtime = "\n".join(message.content for message in context.messages if message.role == "runtime")
    assert "Prompt protocol rule" in runtime
    assert "natural-language control protocol between Runtime and model" in runtime
    assert "Tool schemas are the typed API surface" in runtime
    assert "Runtime supplies perception, action, memory, evidence, and constraints" in runtime
    assert "CONTEXT CHECKPOINT HANDOFF as structured operating context" in runtime


def test_context_injects_engineering_facts_before_runtime_resources() -> None:
    manager = ContextManager()
    history = ChatHistory()
    history.add_user("continue")

    runtime = _formatted_runtime_summary_text(
        {
            "engineering_facts": [
                {
                    "id": "fact-1",
                    "type": "runtime_resource",
                    "source": "rr-1",
                    "summary": "historical process used 3001",
                    "data": {"command": "python manage.py runserver 127.0.0.1:3001", "port": 3001},
                    "confidence": "medium",
                    "stale": True,
                },
                {
                    "id": "fact-2",
                    "type": "config_ref",
                    "source": "frontend/package.json",
                    "summary": "proxy targets http://127.0.0.1:8000",
                    "data": {"target": "http://127.0.0.1:8000", "port": 8000},
                    "confidence": "high",
                    "stale": False,
                },
            ],
            "runtime_resources": [
                {
                    "id": "rr-1",
                    "kind": "background_process",
                    "command": "python manage.py runserver 127.0.0.1:3001",
                    "port": 3001,
                    "observed_status": "stale",
                }
            ],
        }
    )

    assert "[ENGINEERING FACTS]" in runtime
    assert "config_ref" in runtime
    assert "port=8000" in runtime
    assert runtime.index("fact-2") < runtime.index("fact-1")
    assert runtime.index("[ENGINEERING FACTS]") < runtime.index("[RUNTIME RESOURCE MAP]")


def test_context_injects_workspace_facts_and_evidence_pack() -> None:
    manager = ContextManager()
    history = ChatHistory()
    history.add_user("continue")

    context = manager.build_context(
        history=history,
        runtime_summary={
            "workspace_entries": [
                {
                    "path": "src/app.py",
                    "kind": "file",
                    "source_tool": "list_directory",
                    "size": 120,
                }
            ],
            "read_records": [
                {
                    "path": "src/app.py",
                    "source_tool": "read_file",
                    "size": 120,
                    "excerpt": "def main(): pass",
                }
            ],
            "change_records": [
                {
                    "path": "src/app.py",
                    "source_tool": "edit_file",
                    "action": "edited",
                    "summary": "changed main",
                }
            ],
            "evidence_pack": {
                "latest_commands": ["command observed: pytest"],
                "latest_http": ["HTTP GET http://127.0.0.1:8000 status=200 ok=True"],
                "latest_changes": ["src/app.py edited"],
            },
        },
    )

    runtime = "\n".join(message.content for message in context.messages if message.role == "runtime")
    assert "[WORKSPACE FACTS]" not in runtime
    assert "WorkspaceMap recent entries" not in runtime
    assert "[ACTIONABLE EVIDENCE TAIL]" not in runtime


def test_context_injects_project_guidance_and_memory_index() -> None:
    manager = ContextManager()
    history = ChatHistory()
    history.add_user("continue")

    context = manager.build_context(
        history=history,
        runtime_summary={
            "project_guidance": [
                {
                    "path": "AGENTS.md",
                    "content": "Run pytest before completing coding tasks.",
                    "truncated": False,
                }
            ],
            "project_memory_index": [
                {
                    "path": "docs/milestones.md",
                    "title": "Milestones",
                    "headings": ["M1 Core", "M2 TUI"],
                    "excerpt": "M1 builds the runtime core.",
                }
            ],
        },
    )

    runtime = context.messages[0].content
    assert "[PROJECT GUIDANCE]" in runtime
    assert "Run pytest before completing coding tasks." in runtime
    assert "[PROJECT MEMORY INDEX]" in runtime
    assert "docs/milestones.md" in runtime
    assert "sync_work_plan_from_document" in runtime


def test_runtime_context_omits_dynamic_tail_from_model_context() -> None:
    manager = ContextManager()
    history = ChatHistory()
    history.add_user("continue")

    context = manager.build_context(
        history=history,
        runtime_summary={
            "active_goal": "finish demo",
            "goal_status": "active",
            "workspace": "demo",
            "project_guidance": [
                {
                    "path": "AGENTS.md",
                    "content": "Run tests before completing work.",
                }
            ],
        },
    )

    assert context.messages[0].role == "runtime"
    assert "[RUNTIME PROTOCOL]" in context.messages[0].content
    assert "[PROJECT GUIDANCE]" in context.messages[0].content
    assert "[WORK MEMORY]" not in context.messages[0].content
    assert "[EXECUTION STATE]" not in context.messages[0].content
    runtime_text = "\n".join(message.content for message in context.messages if message.role == "runtime")
    assert "[WORK MEMORY]" not in runtime_text
    assert "[EXECUTION STATE]" not in runtime_text


def test_runtime_summary_respects_token_budgets() -> None:
    manager = ContextManager()
    history = ChatHistory()
    history.add_user("continue")

    context = manager.build_context(
        history=history,
        runtime_summary={
            "project_guidance": [
                {
                    "path": "AGENTS.md",
                    "content": "\n".join(f"guidance line {i}: " + ("x" * 120) for i in range(80)),
                }
            ],
            "project_memory_index": [
                {
                    "path": f"docs/plan-{i}.md",
                    "title": "Plan",
                    "headings": [f"Heading {j}" for j in range(8)],
                    "excerpt": "y" * 300,
                }
                for i in range(40)
            ],
            "evidence_pack": {
                "latest_commands": [f"command {i}: " + ("z" * 300) for i in range(40)],
                "latest_http": [f"http {i}: " + ("q" * 300) for i in range(40)],
            },
            "engineering_facts": [
                {
                    "id": f"fact-{i}",
                    "type": "config_ref",
                    "source": "package.json",
                    "summary": "proxy target " + ("a" * 300),
                    "confidence": "high",
                    "data": {"target": "http://127.0.0.1:8000", "port": 8000},
                }
                for i in range(50)
            ],
        },
        runtime_summary_token_budget=1_200,
        runtime_section_token_budget=250,
    )

    assert context.runtime_summary_tokens <= 1_200
    assert context.runtime_section_tokens
    assert context.runtime_section_tokens["RUNTIME PROTOCOL"] <= 250
    assert "omitted by runtime context budget" in context.messages[0].content


def test_sync_work_plan_from_document_tool(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.registry import ToolRegistry

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "milestones.md").write_text(
        "# Plan\n\n"
        "- Build core runtime\n"
        "- Add API server\n"
        "- Add TUI smoke test\n",
        encoding="utf-8",
    )
    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    assert session.goal_controller.create_goal("finish project").ok

    result = session.tools.execute(
        "sync_work_plan_from_document",
        {"path": "docs/milestones.md"},
    )

    assert result.ok
    assert "synced 3 work-plan steps" in result.content
    assert [item.title for item in session.state.work_plan] == [
        "Build core runtime",
        "Add API server",
        "Add TUI smoke test",
    ]
    assert session.state.current_step_id == "build-core-runtime"
    assert session.state.work_plan[0].source_document == "docs/milestones.md"
    assert session.state.work_plan_sources[0].path == "docs/milestones.md"


def test_get_work_plan_tool_returns_steps_and_source_status(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.registry import ToolRegistry

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "milestones.md").write_text(
        "# Plan\n\n"
        "- Build core runtime\n"
        "- Add API server\n",
        encoding="utf-8",
    )
    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    assert session.goal_controller.create_goal("finish project").ok
    assert session.tools.execute(
        "sync_work_plan_from_document",
        {"path": "docs/milestones.md"},
    ).ok
    evidence = session.state.add_evidence_record(
        kind="smoke",
        source_tool="run_command",
        summary="core runtime smoke passed",
    )
    assert session.goal_controller.update_work_step(
        "build-core-runtime",
        "done",
        evidence_ids=[evidence.id],
    ).ok

    result = session.tools.execute("get_work_plan", {})

    assert result.ok
    payload = json.loads(result.content)
    assert payload["objective"] == "finish project"
    assert payload["current_step_id"] == "add-api-server"
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["done_count"] == 1
    assert payload["summary"]["open_count"] == 1
    assert payload["page"]["status"] == "open"
    assert len(payload["work_plan"]) == 1
    assert payload["work_plan"][0]["id"] == "add-api-server"
    assert payload["work_plan_sources"][0]["path"] == "docs/milestones.md"
    assert payload["work_plan_source_status"][0]["stale"] is False

    all_result = session.tools.execute("get_work_plan", {"status": "all", "limit": 1, "offset": 1})
    assert all_result.ok
    all_payload = json.loads(all_result.content)
    assert all_payload["page"]["status"] == "all"
    assert all_payload["page"]["returned"] == 1
    assert all_payload["page"]["total_filtered"] == 2
    assert all_payload["work_plan"][0]["id"] == "add-api-server"


def test_get_work_plan_detail_levels_keep_default_compact(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.registry import ToolRegistry

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    assert session.goal_controller.create_goal("finish project").ok
    assert session.goal_controller.set_work_plan(
        [{"id": "qa", "title": "QA gate", "acceptance": "tests pass"}]
    ).ok
    evidence = session.state.add_evidence_record(
        kind="browser_smoke",
        source_tool="browser_test",
        summary="browser login reached /tasks",
        detail="large html " * 500,
        url="http://localhost:3000/login",
    )
    assert session.goal_controller.update_work_step("qa", "done", evidence_ids=[evidence.id]).ok

    compact = json.loads(session.tools.execute("get_work_plan", {"status": "all"}).content)
    compact_step = compact["work_plan"][0]
    assert compact["page"]["detail"] == "compact"
    assert compact_step["id"] == "qa"
    assert compact_step["evidence_count"] == 1
    assert "evidence" not in compact_step
    assert "evidence_ids" not in compact_step
    assert "evidence_records" not in compact_step

    standard = json.loads(
        session.tools.execute("get_work_plan", {"status": "all", "include_evidence": True}).content
    )
    assert standard["page"]["detail"] == "standard"
    assert standard["work_plan"][0]["evidence_ids"] == [evidence.id]
    assert "evidence_records" not in standard["work_plan"][0]

    full = json.loads(
        session.tools.execute(
            "get_work_plan",
            {"step_id": "qa", "status": "all", "detail": "full"},
        ).content
    )
    full_step = full["work_plan"][0]
    assert full["page"]["detail"] == "full"
    assert full_step["evidence_ids"] == [evidence.id]
    assert full_step["evidence_records"][0]["id"] == evidence.id
    assert "large html" in full_step["evidence_records"][0]["detail"]


def test_sync_work_plan_reuses_existing_verification_evidence(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.registry import ToolRegistry

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "qa.md").write_text(
        "# QA Plan\n\n"
        "- Automated integration and end-to-end tests cover API and UI flows\n"
        "- Frontend browser workflow test is automated\n",
        encoding="utf-8",
    )
    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    assert session.goal_controller.create_goal("finish QA").ok
    http_evidence = session.state.add_evidence_record(
        kind="http_smoke",
        source_tool="http_request",
        summary="GET /api/tasks/ returned status=200",
        url="http://localhost:3000/api/tasks/",
    )
    browser_evidence = session.state.add_evidence_record(
        kind="browser_smoke",
        source_tool="browser_test",
        summary="browser login workflow reached /tasks",
        url="http://localhost:3000/login",
    )

    result = session.tools.execute(
        "sync_work_plan_from_document",
        {"path": "docs/qa.md"},
    )

    assert result.ok
    assert "evidence-reconciled" in result.content
    first, second = session.state.work_plan
    assert first.status == "done"
    assert {http_evidence.id, browser_evidence.id} <= set(first.evidence_ids)
    assert second.status == "done"
    assert browser_evidence.id in second.evidence_ids


def test_get_work_plan_reconciles_existing_pending_quality_gate_steps(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.registry import ToolRegistry

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    assert session.goal_controller.create_goal("finish QA").ok
    assert session.goal_controller.set_work_plan(
        [
            {
                "id": "qa",
                "title": "Automated tests cover unit, integration, and end-to-end flows",
                "acceptance": "Existing verification evidence covers command/API/UI checks.",
            },
            {
                "id": "frontend-e2e",
                "title": "Frontend browser workflow test is automated",
                "acceptance": "Browser workflow has been exercised.",
            },
        ]
    ).ok
    command_evidence = session.state.add_evidence_record(
        kind="verification",
        source_tool="run_command",
        summary="command passed: npm test",
        command="npm test",
    )
    http_evidence = session.state.add_evidence_record(
        kind="http_smoke",
        source_tool="http_request",
        summary="GET /api/tasks/ returned status=200",
        url="http://localhost:3000/api/tasks/",
    )
    browser_evidence = session.state.add_evidence_record(
        kind="browser_smoke",
        source_tool="browser_test",
        summary="browser login workflow reached /tasks",
        url="http://localhost:3000/login",
    )

    result = session.tools.execute("get_work_plan", {})

    assert result.ok
    payload = json.loads(result.content)
    assert payload["evidence_reconciliation"]["reconciled_count"] == 2
    first, second = session.state.work_plan
    assert first.status == "done"
    assert {command_evidence.id, http_evidence.id, browser_evidence.id} <= set(first.evidence_ids)
    assert second.status == "done"
    assert browser_evidence.id in second.evidence_ids


def test_runtime_summary_marks_work_plan_source_stale_when_document_changes(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.registry import ToolRegistry

    docs = tmp_path / "docs"
    docs.mkdir()
    plan_path = docs / "milestones.md"
    plan_path.write_text(
        "# Plan\n\n"
        "- Build core runtime\n",
        encoding="utf-8",
    )
    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    assert session.goal_controller.create_goal("finish project").ok
    result = session.tools.execute(
        "sync_work_plan_from_document",
        {"path": "docs/milestones.md"},
    )
    assert result.ok

    plan_path.write_text(
        "# Plan\n\n"
        "- Build core runtime\n"
        "- Add browser smoke\n",
        encoding="utf-8",
    )

    summary = session._runtime_summary()
    statuses = summary["work_plan_source_status"]
    assert isinstance(statuses, list)
    assert statuses
    assert statuses[0]["path"] == "docs/milestones.md"
    assert statuses[0]["stale"] is True


def test_sync_work_plan_from_document_filters_prd_outline_and_adds_acceptance(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.registry import ToolRegistry

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "prd.md").write_text(
        "# Student Pet PRD\n\n"
        "## 项目简介\n"
        "这是一个学习项目。\n\n"
        "## 用户角色\n"
        "- 学生：完成任务、获得积分、照顾宠物。\n"
        "- 教师：发布任务、查看完成情况。\n\n"
        "## 功能模块\n"
        "- 用户注册与登录\n"
        "- 任务列表浏览及详情查看\n"
        "- 宠物领养、喂养和升级\n"
        "- 排行榜页面\n"
        "- 通知页面\n",
        encoding="utf-8",
    )
    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    assert session.goal_controller.create_goal("finish project").ok

    result = session.tools.execute(
        "sync_work_plan_from_document",
        {"path": "docs/prd.md"},
    )

    assert result.ok
    titles = [item.title for item in session.state.work_plan]
    assert "项目简介" not in titles
    assert "用户角色" not in titles
    assert "功能模块" not in titles
    assert "用户注册与登录" in titles
    assert "宠物领养、喂养和升级" in titles
    assert all(item.acceptance for item in session.state.work_plan)
    assert all(item.verification_hint for item in session.state.work_plan)
    login_step = next(item for item in session.state.work_plan if item.title == "用户注册与登录")
    assert "choose the method from project evidence" in login_step.verification_hint


def test_config_loads_agent_verified_step_budget(tmp_path: Path) -> None:
    (tmp_path / "minicodex2.toml").write_text(
        "[agent]\nmax_verified_steps_per_turn = 12\n",
        encoding="utf-8",
    )

    settings = ConfigLoader().load(tmp_path)

    assert settings.agent.max_verified_steps_per_turn == 12


def test_manual_command_acceptance_evidence_requires_structured_purpose(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import ToolRegistry
    from minicodex2.tools.results import ToolResult

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    result = ToolResult(ok=True, content="ok", metadata={"purpose": "generic"})

    session._record_tool_acceptance_evidence(
        "run_command",
        {"command": "npm run build"},
        result,
    )
    assert session.state.acceptance_evidence == []

    session._record_tool_acceptance_evidence(
        "run_command",
        {"command": "npm run build", "purpose": "build"},
        result,
    )
    assert session.state.acceptance_evidence == ["command passed: npm run build"]


def test_failure_target_extraction_is_not_web_specific() -> None:
    from minicodex2.agent.unified_session import _extract_failure_reproduction_targets

    targets = _extract_failure_reproduction_targets(
        "POST http://localhost:3000/api/auth/register/ 500\n"
        "command: pytest tests/test_api.py failed"
    )

    assert {
        "kind": "http",
        "entrypoint": "http://localhost:3000/api/auth/register/",
        "source": "user_report",
        "method": "POST",
    } in targets
    assert {"kind": "command", "entrypoint": "pytest tests/test_api.py", "source": "user_report"} in targets


def test_ui_formatter_localizes_toolchain_install_failure() -> None:
    from minicodex2.agent.events import AgentEvent

    event = AgentEvent(
        type="tool_call_finished",
        session_id="session_test",
        payload={
            "name": "install_toolchain",
            "ok": False,
            "content_excerpt": "msiexec failed with exit code 1603",
        },
    )

    message = UiMessageFormatter("zh-CN").format_event(event)

    assert message is not None
    assert message.level == "error"
    assert "工具链" in message.title
    assert "失败" in message.title
    assert "没有成功完成" in message.message


def test_ui_formatter_supports_english_locale() -> None:
    from minicodex2.agent.events import AgentEvent

    event = AgentEvent(
        type="model_call_started",
        session_id="session_test",
        payload={"model": "gpt-test"},
    )

    message = UiMessageFormatter("en-US").format_event(event)

    assert message is not None
    assert message.title == "Calling Model"
    assert "gpt-test" in message.message


def test_ui_formatter_hides_internal_noise_events() -> None:
    from minicodex2.agent.events import AgentEvent

    event = AgentEvent(type="token_usage_recorded", session_id="session_test")

    assert UiMessageFormatter("zh-CN").format_event(event) is None


def test_config_loader_reads_ui_locale(tmp_path: Path) -> None:
    (tmp_path / "minicodex2.toml").write_text('[ui]\nlocale = "en-US"\n', encoding="utf-8")

    settings = ConfigLoader().load(tmp_path)

    assert settings.ui.locale == "en-US"


def test_config_loader_selects_complete_model_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_TEST_KEY", "test-deepseek-key")
    (tmp_path / "minicodex2.toml").write_text(
        'default_model_profile = "deepseek_flash"\n'
        "\n"
        "[model]\n"
        'model = "legacy-default"\n'
        'base_url = "https://legacy.example/v1"\n'
        "\n"
        "[model_profiles.deepseek_flash]\n"
        'name = "DeepSeek Flash"\n'
        'provider = "deepseek"\n'
        'model = "deepseek-v4-flash"\n'
        'base_url = "https://api.deepseek.com"\n'
        'wire_api = "chat"\n'
        'api_key_env = "DEEPSEEK_TEST_KEY"\n'
        'timeout_seconds = 77\n',
        encoding="utf-8",
    )

    settings = ConfigLoader().load(tmp_path)

    assert settings.model.profile == "deepseek_flash"
    assert settings.model.name == "DeepSeek Flash"
    assert settings.model.provider == "deepseek"
    assert settings.model.model == "deepseek-v4-flash"
    assert settings.model.base_url == "https://api.deepseek.com"
    assert settings.model.wire_api == "chat"
    assert settings.model.api_key == "test-deepseek-key"
    assert settings.model.timeout_seconds == 77


def test_config_loader_model_profile_can_be_selected_by_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINICODEX2_MODEL_PROFILE", "aihub_gpt55")
    (tmp_path / "minicodex2.toml").write_text(
        "[model_profiles.deepseek_flash]\n"
        'model = "deepseek-v4-flash"\n'
        'base_url = "https://api.deepseek.com"\n'
        "\n"
        "[model_profiles.aihub_gpt55]\n"
        'model = "gpt-5.5"\n'
        'base_url = "https://aihub2api.cloud"\n'
        'wire_api = "responses"\n'
        'reasoning_effort = "medium"\n',
        encoding="utf-8",
    )

    settings = ConfigLoader().load(tmp_path)

    assert settings.model.profile == "aihub_gpt55"
    assert settings.model.model == "gpt-5.5"
    assert settings.model.base_url == "https://aihub2api.cloud"
    assert settings.model.wire_api == "responses"
    assert settings.model.reasoning_effort == "medium"


def test_config_loader_cli_model_overrides_selected_profile(tmp_path: Path) -> None:
    (tmp_path / "minicodex2.toml").write_text(
        'default_model_profile = "deepseek_flash"\n'
        "\n"
        "[model_profiles.deepseek_flash]\n"
        'model = "deepseek-v4-flash"\n'
        'base_url = "https://api.deepseek.com"\n',
        encoding="utf-8",
    )

    settings = ConfigLoader().load(
        tmp_path,
        model="override-model",
        base_url="https://override.example/v1",
        wire_api="chat",
        api_key="override-key",
    )

    assert settings.model.profile == "deepseek_flash"
    assert settings.model.model == "override-model"
    assert settings.model.base_url == "https://override.example/v1"
    assert settings.model.api_key == "override-key"


def test_default_context_budget_balances_cost_and_continuity(tmp_path: Path) -> None:
    settings = ConfigLoader().load(tmp_path)

    assert settings.context.trigger_auto_compact_limit_tokens == 50000
    assert settings.context.post_compact_ratio == 0.60
    assert settings.context.baseline_ratio == 0.25
    assert settings.context.model_context_limit_tokens == 128000
    assert settings.context.tool_result_raw_keep == 8
    assert settings.context.tool_result_summary_chars == 700
    assert not settings.agent.context_dump_enabled


def test_config_loader_reads_context_settings(tmp_path: Path) -> None:
    (tmp_path / "minicodex2.toml").write_text(
        "[context]\n"
        "trigger_auto_compact_limit_tokens = 24000\n"
        "post_compact_ratio = 0.5\n"
        "baseline_ratio = 0.25\n"
        "model_context_limit_tokens = 50000\n"
        "tool_result_raw_keep = 3\n"
        "tool_result_summary_chars = 900\n"
        "runtime_summary_token_budget = 1800\n"
        "runtime_section_token_budget = 400\n",
        encoding="utf-8",
    )

    settings = ConfigLoader().load(tmp_path)

    assert settings.context.budget_tokens == 24000
    assert settings.context.compression_threshold == 0.5
    assert settings.context.trigger_auto_compact_limit_tokens == 24000
    assert settings.context.request_soft_limit_tokens == 24000
    assert settings.context.post_compact_ratio == 0.5
    assert settings.context.baseline_ratio == 0.25
    assert settings.context.model_context_limit_tokens == 50000
    assert settings.context.tool_result_raw_keep == 3
    assert settings.context.tool_result_summary_chars == 900
    assert settings.context.runtime_summary_token_budget == 1800
    assert settings.context.runtime_section_token_budget == 400


def test_config_loader_reads_agent_context_dump_settings(tmp_path: Path) -> None:
    (tmp_path / "minicodex2.toml").write_text(
        "[agent]\n"
        "context_dump_enabled = false\n"
        "context_dump_max_chars_per_message = 1234\n",
        encoding="utf-8",
    )

    settings = ConfigLoader().load(tmp_path)

    assert not settings.agent.context_dump_enabled
    assert settings.agent.context_dump_max_chars_per_message == 1234


def test_config_loader_keeps_legacy_context_setting_aliases(tmp_path: Path) -> None:
    (tmp_path / "minicodex2.toml").write_text(
        "[context]\n"
        "request_soft_limit_tokens = 4200\n"
        "compression_threshold = 0.4\n",
        encoding="utf-8",
    )

    settings = ConfigLoader().load(tmp_path)

    assert settings.context.trigger_auto_compact_limit_tokens == 4200
    assert settings.context.request_soft_limit_tokens == 4200
    assert settings.context.post_compact_ratio == 0.4


def test_config_loader_keeps_budget_tokens_alias(tmp_path: Path) -> None:
    (tmp_path / "minicodex2.toml").write_text(
        "[context]\n"
        "budget_tokens = 4300\n",
        encoding="utf-8",
    )

    settings = ConfigLoader().load(tmp_path)

    assert settings.context.trigger_auto_compact_limit_tokens == 4300
    assert settings.context.budget_tokens == 4300


def test_context_budget_plan_uses_soft_limit_as_trigger() -> None:
    plan = plan_context_budget(token_budget=6_000, compression_threshold=0.45)

    assert plan.trigger_tokens == 6_000
    assert plan.runtime_summary_tokens <= 1_680
    assert plan.checkpoint_target_chars <= 4_000
    assert plan.reserve_tokens >= 2_000


def test_context_budget_plan_scales_reserve_without_unbounded_checkpoint_growth() -> None:
    small = plan_context_budget(token_budget=4_200, compression_threshold=0.45)
    medium = plan_context_budget(token_budget=6_000, compression_threshold=0.45)
    large = plan_context_budget(token_budget=12_000, compression_threshold=0.45)

    assert small.trigger_tokens == 4_200
    assert medium.trigger_tokens == 6_000
    assert large.trigger_tokens == 12_000
    assert large.runtime_summary_tokens > medium.runtime_summary_tokens
    assert large.runtime_section_tokens > medium.runtime_section_tokens
    assert large.checkpoint_target_chars > 3_600
    assert large.checkpoint_target_chars <= 8_800
    assert large.checkpoint_target_chars >= small.checkpoint_target_chars
    assert medium.reserve_tokens > small.reserve_tokens
    assert large.reserve_tokens > medium.reserve_tokens
    assert large.recent_target_tokens >= small.recent_target_tokens


def test_workspace_escape_is_rejected(tmp_path: Path) -> None:
    safety = PathSafety(tmp_path)
    try:
        safety.resolve_workspace_path(tmp_path.parent / "outside.txt")
    except ValueError as exc:
        assert "outside workspace" in str(exc)
    else:
        raise AssertionError("expected workspace escape rejection")


def test_permission_policy_auto_allows_workspace_write() -> None:
    decision = PermissionPolicy("auto").check_write("file.txt")
    assert decision.action == "allow"


def test_permission_policy_auto_allows_workspace_delete() -> None:
    decision = PermissionPolicy("auto").check_delete("file.txt")
    assert decision.action == "allow"


def test_runtime_tools_guarded_write_creates_permission_request(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path, permission_mode="guarded")
    runtime = RuntimeTools(settings)
    result = runtime.write_file("x.txt", "hello")
    assert result.blocked
    assert result.permission_request_id
    assert runtime.permission_store.pending()[0].id == result.permission_request_id


def test_runtime_tools_auto_delete_removes_workspace_file(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    target = tmp_path / "old.txt"
    target.write_text("old", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path, permission_mode="auto")
    runtime = RuntimeTools(settings)

    deleted = runtime.delete_file("old.txt")
    assert deleted.ok
    assert not target.exists()


def test_runtime_tools_guarded_delete_creates_permission_request(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    target = tmp_path / "old.txt"
    target.write_text("old again", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path, permission_mode="guarded")
    runtime = RuntimeTools(settings)

    blocked = runtime.delete_file("old.txt")
    assert blocked.blocked
    assert blocked.permission_request_id
    assert target.exists()


def test_write_file_records_diff_summary(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    result = runtime.write_file("calc.py", "def add(a, b):\n    return a + b\n")

    assert result.ok
    diff = result.metadata["diff"]
    assert diff["path"] == "calc.py"
    assert diff["added_lines"] == 2
    assert diff["removed_lines"] == 0
    assert "+def add(a, b):" in diff["lines"]


def test_run_command_treats_zero_discovered_tests_as_failure(tmp_path: Path, monkeypatch) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    def fake_run(command: str, cwd: Path, timeout_seconds: int = 30, progress_callback=None) -> CommandResult:
        return CommandResult(
            command=command,
            cwd=str(cwd),
            exit_code=0,
            stdout="Found 0 test(s).\nSystem check identified no issues.\n",
            stderr="NO TESTS RAN\n",
        )

    monkeypatch.setattr(runtime.command_runner, "run", fake_run)

    result = runtime.run_command("python manage.py test")

    assert not result.ok
    assert result.metadata["semantic_failure"] == "test command completed without executing any tests"
    assert "semantic_failure" in result.content


def test_run_command_detects_workspace_file_changes(tmp_path: Path, monkeypatch) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    def fake_run(command: str, cwd: Path, timeout_seconds: int = 30, progress_callback=None) -> CommandResult:
        (cwd / "generated.py").write_text("VALUE = 1\n", encoding="utf-8")
        return CommandResult(
            command=command,
            cwd=str(cwd),
            exit_code=0,
            stdout="generated\n",
            stderr="",
        )

    monkeypatch.setattr(runtime.command_runner, "run", fake_run)

    result = runtime.run_command("python generate.py")

    assert result.ok
    assert result.did_write
    assert result.changed_files == ["generated.py"]


def test_run_command_separates_runtime_data_changes(tmp_path: Path, monkeypatch) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    def fake_run(command: str, cwd: Path, timeout_seconds: int = 30, progress_callback=None) -> CommandResult:
        (cwd / "db.sqlite3").write_text("runtime data\n", encoding="utf-8")
        return CommandResult(
            command=command,
            cwd=str(cwd),
            exit_code=0,
            stdout="seeded\n",
            stderr="",
        )

    monkeypatch.setattr(runtime.command_runner, "run", fake_run)

    result = runtime.run_command("python seed_data.py")

    assert result.ok
    assert not result.did_write
    assert result.changed_files == []
    assert result.metadata["runtime_data_changed_files"] == ["db.sqlite3"]
    assert result.metadata["observed_changed_files"] == ["db.sqlite3"]


def test_run_python_reports_source_changes_but_not_runtime_data(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    result = runtime.run_python(
        "from pathlib import Path\n"
        "Path('generated.py').write_text('VALUE = 1\\n', encoding='utf-8')\n"
        "Path('db.sqlite3').write_text('runtime data\\n', encoding='utf-8')\n"
    )

    assert result.ok
    assert result.did_write
    assert result.changed_files == ["generated.py"]
    assert result.metadata["runtime_data_changed_files"] == ["db.sqlite3"]
    assert result.metadata["observed_changed_files"] == ["db.sqlite3", "generated.py"]


def test_python_failure_details_detect_django_settings_mismatch(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import (
        _python_command_failure_details,
        _python_script_failure_details,
    )

    project = tmp_path / "studentPet"
    backend = project / "backend"
    backend.mkdir(parents=True)
    (backend / "manage.py").write_text(
        "import os\n"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')\n",
        encoding="utf-8",
    )
    code = (
        "import os\n"
        "os.environ['DJANGO_SETTINGS_MODULE'] = 'studentPet.settings'\n"
        "import django; django.setup()\n"
    )

    details = _python_script_failure_details(
        code=code,
        stdout="",
        stderr="ModuleNotFoundError: No module named 'studentPet'",
        cwd=backend,
    )

    assert details["failure_kind"] == "django_settings_mismatch"
    assert details["configured_settings_module"] == "studentPet.settings"
    assert details["detected_manage_py_settings_module"] == "backend.settings"

    command_details = _python_command_failure_details(
        command=(
            f"cd /d {backend} && python -c \"import os; "
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE','studentPet.settings'); "
            "import django; django.setup()\""
        ),
        stdout="",
        stderr="ModuleNotFoundError: No module named 'studentPet'",
        cwd=tmp_path,
    )
    assert command_details["failure_kind"] == "django_settings_mismatch"


def test_python_failure_details_suggest_http_request_for_missing_requests(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import _python_script_failure_details

    details = _python_script_failure_details(
        code="import requests\nprint(requests.get('http://localhost:8000/api/health/').status_code)\n",
        stdout="",
        stderr="ModuleNotFoundError: No module named 'requests'",
        cwd=tmp_path,
    )

    assert details["failure_kind"] == "python_dependency_missing"
    assert details["missing_python_module"] == "requests"
    assert details["suggested_tool"] == "http_request"
    assert "http_request" in str(details["failure_summary"])


def test_python_failure_details_detect_django_helper_wrong_cwd(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import _python_script_failure_details

    project = tmp_path / "studentPet"
    backend = project / "backend"
    backend.mkdir(parents=True)
    (backend / "manage.py").write_text(
        "import os\n"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')\n",
        encoding="utf-8",
    )

    details = _python_script_failure_details(
        code=(
            "import os, sys, django\n"
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')\n"
            "sys.path.insert(0, 'studentPet/backend')\n"
            "django.setup()\n"
        ),
        stdout="",
        stderr="ModuleNotFoundError: No module named 'backend.settings'",
        cwd=project,
    )

    assert details["failure_kind"] == "django_helper_wrong_cwd_or_pythonpath"
    assert details["configured_settings_module"] == "backend.settings"
    assert details["detected_manage_py_settings_module"] == "backend.settings"
    assert details["detected_manage_py_dir"] == str(backend)


def test_command_failure_details_detects_bare_powershell_cmdlet(monkeypatch) -> None:
    from minicodex2.tools.runtime_tools import _command_failure_details

    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    details = _command_failure_details(
        command="Test-Path node_modules",
        stdout="",
        stderr="'Test-Path' is not recognized as an internal or external command",
        cwd=Path("."),
    )

    assert details["failure_kind"] == "powershell_cmdlet_used_in_cmd_shell"
    assert details["missing_shell_command"] == "Test-Path"
    assert details["expected_shell"] == "powershell"
    assert "powershell -NoProfile -Command" in str(details["failure_summary"])


def test_openai_retry_after_parses_body_delay() -> None:
    class DummyHttpError(Exception):
        headers = {}

    delay = _retry_after_seconds(
        DummyHttpError(),  # type: ignore[arg-type]
        "Rate limit reached. Please try again in 2.922s.",
        0,
    )

    assert 3.0 <= delay <= 3.3


class _DummyRequestsResponse:
    def __init__(self, body: dict[str, object], status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, object]:
        return self._body


def test_openai_adapter_retries_transient_url_error(monkeypatch) -> None:
    calls = {"count": 0}

    adapter = OpenAICompatibleModelAdapter(
        base_url="https://example.test/v1",
        api_key="key",
        model="model",
        max_retries=1,
    )

    def fake_post(url: str, *, data: bytes, timeout: int):
        calls["count"] += 1
        if calls["count"] == 1:
            import requests

            raise requests.ConnectionError("temporary eof")
        return _DummyRequestsResponse(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    monkeypatch.setattr(adapter._session, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    response = adapter.complete(
        ModelRequest(
            messages=[ChatMessage(role="user", content="hi")],
            tools=[],
            model="model",
        )
    )

    assert response.message.content == "ok"
    assert calls["count"] == 2


def test_openai_adapter_records_deepseek_prompt_cache_usage(monkeypatch) -> None:
    adapter = OpenAICompatibleModelAdapter(
        base_url="https://example.test/v1",
        api_key="key",
        model="model",
    )

    monkeypatch.setattr(
        adapter._session,
        "post",
        lambda url, *, data, timeout: _DummyRequestsResponse(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 5,
                    "total_tokens": 105,
                    "prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 20,
                },
            }
        ),
    )

    response = adapter.complete(
        ModelRequest(messages=[ChatMessage(role="user", content="hi")], tools=[], model="model")
    )

    assert response.usage.cache_hit_prompt_tokens == 80
    assert response.usage.cache_miss_prompt_tokens == 20
    assert response.usage.cached_prompt_tokens == 80
    assert response.usage.cache_hit_ratio == 0.8


def test_openai_adapter_records_prompt_details_cached_tokens(monkeypatch) -> None:
    adapter = OpenAICompatibleModelAdapter(
        base_url="https://example.test/v1",
        api_key="key",
        model="model",
    )

    monkeypatch.setattr(
        adapter._session,
        "post",
        lambda url, *, data, timeout: _DummyRequestsResponse(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 5,
                    "total_tokens": 105,
                    "prompt_tokens_details": {"cached_tokens": 30},
                },
            }
        ),
    )

    response = adapter.complete(
        ModelRequest(messages=[ChatMessage(role="user", content="hi")], tools=[], model="model")
    )

    assert response.usage.cached_prompt_tokens == 30
    assert response.usage.cache_hit_prompt_tokens == 30
    assert response.usage.cache_miss_prompt_tokens == 70


def test_openai_adapter_records_responses_input_details_cached_tokens(monkeypatch) -> None:
    adapter = OpenAICompatibleModelAdapter(
        base_url="https://example.test/v1",
        api_key="key",
        model="model",
        wire_api="responses",
    )

    monkeypatch.setattr(
        adapter._session,
        "post",
        lambda url, *, data, timeout: _DummyRequestsResponse(
            {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 5,
                    "total_tokens": 105,
                    "input_tokens_details": {"cached_tokens": 40},
                },
            }
        ),
    )

    response = adapter.complete(
        ModelRequest(messages=[ChatMessage(role="user", content="hi")], tools=[], model="model")
    )

    assert response.usage.prompt_tokens == 100
    assert response.usage.completion_tokens == 5
    assert response.usage.cached_prompt_tokens == 40
    assert response.usage.cache_hit_prompt_tokens == 40
    assert response.usage.cache_miss_prompt_tokens == 60


def test_openai_adapter_serializes_stable_tools_before_append_only_messages(monkeypatch) -> None:
    captured: dict[str, object] = {}

    adapter = OpenAICompatibleModelAdapter(
        base_url="https://example.test/v1",
        api_key="key",
        model="model",
    )

    def fake_send_with_retries(data: bytes) -> dict[str, object]:
        payload = json.loads(data.decode("utf-8"))
        captured["payload"] = payload
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(adapter, "_send_with_retries", fake_send_with_retries)

    adapter.complete(
        ModelRequest(
            messages=[ChatMessage(role="user", content="hi")],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            model="default",
        )
    )

    keys = list(captured["payload"].keys())  # type: ignore[index, union-attr]
    assert keys.index("tools") < keys.index("messages")
    assert keys.index("tool_choice") < keys.index("messages")


def test_openai_adapter_responses_wire_serializes_input_and_tools(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    adapter = OpenAICompatibleModelAdapter(
        base_url="https://example.test/v1",
        api_key="key",
        model="model",
        wire_api="responses",
    )

    def fake_post(url: str, *, data: bytes, timeout: int):
        body = json.loads(data.decode("utf-8"))
        calls.append((url, body))
        return _DummyRequestsResponse(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }
        )

    monkeypatch.setattr(adapter._session, "post", fake_post)
    response = adapter.complete(
        ModelRequest(
            messages=[
                ChatMessage(role="system", content="rules"),
                ChatMessage(role="runtime", content="facts"),
                ChatMessage(role="user", content="hi"),
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file.",
                        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    },
                }
            ],
            model="default",
        )
    )

    assert response.message.content == "ok"
    assert calls[0][0] == "https://example.test/v1/responses"
    payload = calls[0][1]
    assert payload["model"] == "model"
    assert payload["input"] == [
        {"role": "system", "content": "rules"},
        {"role": "system", "content": "facts"},
        {"role": "user", "content": "hi"},
    ]
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    assert payload["tool_choice"] == "auto"


def test_openai_adapter_responses_wire_parses_function_calls(monkeypatch) -> None:
    adapter = OpenAICompatibleModelAdapter(
        base_url="https://example.test/v1",
        api_key="key",
        model="model",
        wire_api="responses",
    )

    monkeypatch.setattr(
        adapter._session,
        "post",
        lambda url, *, data, timeout: _DummyRequestsResponse(
            {
                "status": "requires_action",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "list_directory",
                        "arguments": '{"path":"."}',
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
        ),
    )

    response = adapter.complete(
        ModelRequest(messages=[ChatMessage(role="user", content="inspect")], tools=[], model="default")
    )

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == [ToolCall(id="call_1", name="list_directory", arguments={"path": "."})]
    assert response.usage.prompt_tokens == 10


def test_openai_adapter_responses_wire_serializes_tool_outputs(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    adapter = OpenAICompatibleModelAdapter(
        base_url="https://example.test/v1",
        api_key="key",
        model="model",
        wire_api="responses",
    )

    def fake_post(url: str, *, data: bytes, timeout: int):
        body = json.loads(data.decode("utf-8"))
        calls.append(body)
        return _DummyRequestsResponse(
            {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    monkeypatch.setattr(adapter._session, "post", fake_post)
    adapter.complete(
        ModelRequest(
            messages=[
                ChatMessage(
                    role="assistant",
                    content="",
                    metadata={
                        "tool_calls": [
                            ToolCall(id="call_1", name="read_file", arguments={"path": "README.md"}).to_model_dict()
                        ]
                    },
                ),
                ChatMessage(role="tool", content="hello", name="read_file", tool_call_id="call_1"),
            ],
            tools=[],
            model="default",
        )
    )

    assert calls[0]["input"] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": '{"path": "README.md"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "hello"},
    ]


def test_openai_adapter_roundtrips_deepseek_reasoning_content(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    adapter = OpenAICompatibleModelAdapter(
        base_url="https://api.deepseek.com/v1",
        api_key="key",
        model="deepseek-v4-flash",
    )

    def fake_post(url: str, *, data: bytes, timeout: int):
        body = json.loads(data.decode("utf-8"))
        calls.append(body)
        return _DummyRequestsResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "ok",
                            "reasoning_content": "private chain state",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    monkeypatch.setattr(adapter._session, "post", fake_post)

    first = adapter.complete(
        ModelRequest(messages=[ChatMessage(role="user", content="hi")], tools=[], model="default")
    )
    second = adapter.complete(
        ModelRequest(
            messages=[
                ChatMessage(role="user", content="hi"),
                first.message,
                ChatMessage(role="user", content="continue"),
            ],
            tools=[],
            model="default",
        )
    )

    assert first.message.metadata["reasoning_content"] == "private chain state"
    assert second.message.content == "ok"
    assert calls[0]["messages"][0] == {"role": "user", "content": "hi"}
    second_messages = calls[1]["messages"]
    assert isinstance(second_messages, list)
    assert second_messages[1]["role"] == "assistant"
    assert second_messages[1]["reasoning_content"] == "private chain state"


def test_openai_adapter_omits_reasoning_content_for_non_deepseek_provider(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    adapter = OpenAICompatibleModelAdapter(
        base_url="https://api.openai.com/v1",
        api_key="key",
        model="gpt-test",
    )

    def fake_post(url: str, *, data: bytes, timeout: int):
        calls.append(json.loads(data.decode("utf-8")))
        return _DummyRequestsResponse(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    monkeypatch.setattr(adapter._session, "post", fake_post)

    adapter.complete(
        ModelRequest(
            messages=[
                ChatMessage(
                    role="assistant",
                    content="ok",
                    metadata={"reasoning_content": "provider-private"},
                )
            ],
            tools=[],
            model="default",
        )
    )

    assert "reasoning_content" not in calls[0]["messages"][0]


def test_openai_adapter_repairs_legacy_deepseek_history_without_reasoning_content(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, *, data: bytes, timeout: int):
        body = json.loads(data.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return _DummyRequestsResponse(
                {
                    "error": {
                        "message": "The `reasoning_content` in the thinking mode must be passed back to the API.",
                        "type": "invalid_request_error",
                        "code": "invalid_request_error",
                    }
                },
                status_code=400,
            )
        return _DummyRequestsResponse(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    adapter = OpenAICompatibleModelAdapter(
        base_url="https://api.deepseek.com/v1",
        api_key="key",
        model="deepseek-v4-flash",
    )
    monkeypatch.setattr(adapter._session, "post", fake_post)
    response = adapter.complete(
        ModelRequest(
            messages=[
                ChatMessage(role="user", content="hi"),
                ChatMessage(
                    role="assistant",
                    content="I will inspect the project",
                    metadata={
                        "tool_calls": [
                            ToolCall(id="call_1", name="list_directory", arguments={"path": "."}).to_model_dict()
                        ]
                    },
                ),
                ChatMessage(
                    role="tool",
                    content="README.md\nsrc",
                    name="list_directory",
                    tool_call_id="call_1",
                ),
                ChatMessage(role="user", content="continue"),
            ],
            tools=[],
            model="default",
        )
    )

    assert response.message.content == "ok"
    assert len(calls) == 2
    repaired_messages = calls[1]["messages"]
    assert isinstance(repaired_messages, list)
    assert not any(message.get("role") == "tool" for message in repaired_messages)
    assert any(
        message.get("role") == "system" and "provider history repair" in str(message.get("content"))
        for message in repaired_messages
    )


def test_openai_adapter_retries_remote_disconnected(monkeypatch) -> None:
    import json
    import requests

    calls = {"count": 0}

    def fake_post(url: str, *, data: bytes, timeout: int):
        calls["count"] += 1
        if calls["count"] == 1:
            raise requests.ConnectionError("Remote end closed connection without response")
        return _DummyRequestsResponse(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    monkeypatch.setattr("time.sleep", lambda seconds: None)

    adapter = OpenAICompatibleModelAdapter(
        base_url="https://example.test/v1",
        api_key="key",
        model="model",
        max_retries=1,
    )
    monkeypatch.setattr(adapter._session, "post", fake_post)
    response = adapter.complete(
        ModelRequest(
            messages=[ChatMessage(role="user", content="hi")],
            tools=[],
            model="model",
        )
    )

    assert response.message.content == "ok"
    assert calls["count"] == 2


def test_openai_adapter_falls_back_to_text_only_when_backend_rejects_image_messages(
    monkeypatch, tmp_path: Path
) -> None:
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"fakepng")
    calls: list[dict[str, object]] = []

    def fake_post(url: str, *, data: bytes, timeout: int):
        body = json.loads(data.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return _DummyRequestsResponse(
                {
                    "error": {
                        "message": "Failed to deserialize the JSON body into the target type: messages[0]: unknown variant `image_url`, expected `text`",
                        "type": "invalid_request_error",
                        "code": "invalid_request_error",
                    }
                },
                status_code=400,
            )
        return _DummyRequestsResponse(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    adapter = OpenAICompatibleModelAdapter(
        base_url="https://example.test/v1",
        api_key="key",
        model="model",
        max_retries=0,
    )
    monkeypatch.setattr(adapter._session, "post", fake_post)
    response = adapter.complete(
        ModelRequest(
            messages=[
                ChatMessage(
                    role="user",
                    content="[view_image attached shot.png]",
                    metadata={"image": {"path": str(image_path), "mime_type": "image/png", "detail": "high"}},
                )
            ],
            tools=[],
            model="model",
        )
    )

    assert response.message.content == "ok"
    assert len(calls) == 2
    assert isinstance(calls[0]["messages"][0]["content"], list)
    assert isinstance(calls[1]["messages"][0]["content"], str)
    assert "[image attached:" in calls[1]["messages"][0]["content"]


def test_openai_adapter_retries_retryable_http_5xx(monkeypatch) -> None:
    import json

    calls = {"count": 0}

    def fake_post(url: str, *, data: bytes, timeout: int):
        calls["count"] += 1
        if calls["count"] == 1:
            return _DummyRequestsResponse({"error": {"message": "temporary upstream failure"}}, status_code=502)
        return _DummyRequestsResponse(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    monkeypatch.setattr("time.sleep", lambda seconds: None)

    adapter = OpenAICompatibleModelAdapter(
        base_url="https://example.test/v1",
        api_key="key",
        model="model",
        max_retries=1,
    )
    monkeypatch.setattr(adapter._session, "post", fake_post)
    response = adapter.complete(
        ModelRequest(
            messages=[ChatMessage(role="user", content="hi")],
            tools=[],
            model="model",
        )
    )

    assert response.message.content == "ok"
    assert calls["count"] == 2


def test_edit_file_records_diff_summary(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    result = runtime.edit_file("calc.py", "return a - b", "return a + b")

    assert result.ok
    diff = result.metadata["diff"]
    assert diff["path"] == "calc.py"
    assert diff["added_lines"] == 1
    assert diff["removed_lines"] == 1
    assert "-    return a - b" in diff["lines"]
    assert "+    return a + b" in diff["lines"]


def test_edit_file_accepts_crlf_old_text_on_normalized_read(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    (tmp_path / "calc.py").write_bytes(b"def add(a, b):\r\n    return a - b\r\n")

    result = runtime.edit_file(
        "calc.py",
        "def add(a, b):\r\n    return a - b\r\n",
        "def add(a, b):\r\n    return a + b\r\n",
    )

    assert result.ok
    assert "return a + b" in (tmp_path / "calc.py").read_text(encoding="utf-8")


def test_edit_file_accepts_extra_blank_line_mismatch(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    (tmp_path / "orders.py").write_text(
        "import json\n\n\ndef total_prices(raw):\n    return 0\n",
        encoding="utf-8",
    )

    result = runtime.edit_file(
        "orders.py",
        "import json\n\ndef total_prices(raw):\n    return 0\n",
        "import json\n\n\ndef total_prices(raw):\n    return sum(item['price'] for item in json.loads(raw))\n",
    )

    assert result.ok
    assert "json.loads" in (tmp_path / "orders.py").read_text(encoding="utf-8")


def test_edit_file_replaces_line_range_without_old_text(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    (tmp_path / "page.js").write_text(
        "const a = 1;\n"
        "function render() {\n"
        "  return <span>old</span>;\n"
        "}\n"
        "export default render;\n",
        encoding="utf-8",
    )

    result = runtime.edit_file(
        "page.js",
        start_line=2,
        end_line=4,
        content="function render() {\n  return <span>new</span>;\n}",
    )

    assert result.ok
    text = (tmp_path / "page.js").read_text(encoding="utf-8")
    assert "old" not in text
    assert "return <span>new</span>;" in text
    assert "export default render;" in text


def test_edit_file_requires_a_supported_edit_mode(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    (tmp_path / "page.js").write_text("const a = 1;\n", encoding="utf-8")

    result = runtime.edit_file("page.js")

    assert not result.ok
    assert result.blocked
    assert result.metadata["failure_kind"] == "invalid_tool_arguments"


def test_missing_read_file_returns_parent_siblings(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    pages = tmp_path / "frontend" / "src" / "pages"
    pages.mkdir(parents=True)
    (pages / "LoginPage.js").write_text("export default function LoginPage() {}\n", encoding="utf-8")

    result = runtime.read_file("frontend/src/pages/Login.js")

    assert not result.ok
    assert result.blocked
    payload = json.loads(result.content)
    assert payload["requested_path"] == "frontend/src/pages/Login.js"
    assert payload["parent"] == "frontend/src/pages"
    assert {"name": "LoginPage.js", "type": "file"} in payload["existing_siblings"]


def test_blocked_path_payload_is_recoverable_even_with_short_block_reason() -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.results import ToolResult

    result = ToolResult(
        ok=False,
        blocked=True,
        block_reason="studentPet/pets",
        content=json.dumps(
            {
                "error": "studentPet/pets",
                "tool": "list_directory",
                "requested_path": "studentPet/pets",
                "hint": "The requested path does not exist or cannot be read.",
                "parent": "studentPet",
                "parent_exists": True,
                "existing_siblings": [{"name": "backend", "type": "dir"}],
            }
        ),
    )

    assert UnifiedAgentSession._is_recoverable_tool_error("list_directory", result)


def test_runtime_tool_registry_exposes_port_tools(tmp_path: Path) -> None:
    from minicodex2.tools.registry import build_runtime_tool_registry
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    registry = build_runtime_tool_registry(RuntimeTools(settings))
    schemas = {schema["function"]["name"]: schema["function"] for schema in registry.schemas()}
    assert "find_images" in schemas
    assert "view_image" in schemas
    assert "inspect_port" in schemas
    assert "release_port" in schemas
    assert "inspect_toolchain" in schemas
    assert "install_toolchain" in schemas
    assert "search_files" in schemas
    assert "run_python" in schemas
    assert "http_request" in schemas
    assert "browser_test" in schemas
    assert "cleanup_background_processes" in schemas
    assert "recursive" in schemas["list_directory"]["parameters"]["properties"]
    assert "max_depth" in schemas["list_directory"]["parameters"]["properties"]
    assert "max_entries" in schemas["list_directory"]["parameters"]["properties"]
    assert "glob" in schemas["list_directory"]["parameters"]["properties"]
    assert "max_bytes" in schemas["read_file"]["parameters"]["properties"]
    assert "offset" in schemas["read_file"]["parameters"]["properties"]
    assert "limit" in schemas["read_file"]["parameters"]["properties"]
    assert "path" in schemas["search_files"]["parameters"]["properties"]
    assert "glob" in schemas["search_files"]["parameters"]["properties"]
    assert "recursive" in schemas["search_files"]["parameters"]["properties"]
    assert "recurse" in schemas["search_files"]["parameters"]["properties"]
    assert "query" in schemas["search_files"]["parameters"]["required"]
    assert "timeout_seconds" in schemas["run_command"]["parameters"]["properties"]
    assert "timeout_seconds" in schemas["run_python"]["parameters"]["properties"]
    assert "purpose" in schemas["run_command"]["parameters"]["properties"]
    assert "port" in schemas["browser_test"]["parameters"]["properties"]
    assert "path" in schemas["browser_test"]["parameters"]["properties"]
    assert "url" not in schemas["browser_test"]["parameters"].get("required", [])
    assert "values" in schemas["browser_test"]["parameters"]["properties"]["actions"]["items"]["properties"]
    assert "media_diagnostics" in schemas["browser_test"]["description"]
    assert "minicodex-browser plugin" in schemas["browser_test"]["description"]
    assert "restart_port" in schemas["start_background_command"]["parameters"]["properties"]


def test_builtin_browser_plugin_declares_tool_registrar() -> None:
    from minicodex2.plugins.registry import load_builtin_plugins

    plugins = {plugin.name: plugin for plugin in load_builtin_plugins()}

    assert "minicodex-browser" in plugins
    browser = plugins["minicodex-browser"]
    assert browser.source == "builtin"
    assert browser.trust_level == "trusted"
    assert browser.tools == ("browser_test",)
    assert browser.tool_registrar == "tools.py:register_browser_tools"


def test_tool_registry_reports_missing_required_arguments(tmp_path: Path) -> None:
    from minicodex2.tools.registry import build_runtime_tool_registry
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    registry = build_runtime_tool_registry(RuntimeTools(settings))

    result = registry.execute(
        "edit_file",
        {"old_text": "before", "new_text": "after"},
    )

    assert not result.ok
    assert result.blocked
    assert result.block_reason == "missing required tool arguments: path"
    assert result.metadata["failure_kind"] == "invalid_tool_arguments"
    assert result.metadata["missing_arguments"] == ["path"]


def test_read_file_supports_offset_and_limit(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "large.txt").write_text("0123456789abcdef", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    result = runtime.read_file("large.txt", offset=4, limit=6)

    assert result.ok
    assert result.content == "456789"
    assert result.metadata["offset"] == 4
    assert result.metadata["limit"] == 6
    assert result.metadata["returned_bytes"] == 6
    assert result.metadata["next_offset"] == 10
    assert result.metadata["truncated"] is True


def test_read_file_supports_line_ranges(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "code.py").write_text("one\nTwo\nthree\nfour\n", encoding="utf-8", newline="")
    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    result = runtime.read_file("code.py", start_line=2, end_line=3)

    assert result.ok
    assert result.content == "Two\nthree\n"
    assert result.metadata["start_line"] == 2
    assert result.metadata["end_line"] == 3
    assert result.metadata["line_count"] == 4
    assert result.metadata["returned_lines"] == 2
    assert result.metadata["next_start_line"] == 4


def test_http_request_returns_structured_status(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"server failed" * 200)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = ConfigLoader().load(tmp_path)
        runtime = RuntimeTools(settings)
        result = runtime.http_request(
            f"http://127.0.0.1:{server.server_port}/api",
            method="POST",
            body="{}",
        )
    finally:
        server.shutdown()
        server.server_close()

    assert not result.ok
    assert result.metadata["status_code"] == 500
    assert result.metadata["body_excerpt"].startswith("server failed")
    assert len(result.metadata["body_excerpt"]) <= 1200


def test_http_request_rejects_non_object_headers_without_crashing(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    result = runtime.http_request(
        "http://127.0.0.1:9/api",
        headers="Authorization: token",  # type: ignore[arg-type]
    )

    assert not result.ok
    assert result.blocked
    assert result.metadata["failure_kind"] == "invalid_tool_arguments"
    assert "headers must be an object" in result.content


def test_http_request_accepts_json_body_object(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            captured["content_type"] = self.headers.get("Content-Type", "")
            captured["body"] = self.rfile.read(length).decode("utf-8")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = ConfigLoader().load(tmp_path)
        runtime = RuntimeTools(settings)
        result = runtime.http_request(
            f"http://127.0.0.1:{server.server_port}/api",
            method="POST",
            body={"role": "student"},
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.ok
    assert captured["content_type"] == "application/json"
    assert '"role": "student"' in captured["body"]


def test_http_request_timeout_is_recoverable_tool_feedback(tmp_path: Path, monkeypatch) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    def fake_urlopen(request, timeout=120):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    result = runtime.http_request("http://127.0.0.1:3000/api/tasks/", timeout_seconds=1)

    assert not result.ok
    assert not result.blocked
    assert result.block_reason == "http_timeout"
    assert result.metadata["failure_kind"] == "http_timeout"
    assert result.metadata["recoverable"] is True
    assert "diagnostic_hint" in result.metadata


def test_find_images_returns_workspace_candidates(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    (tmp_path / "ScreenShot2.jpg").write_bytes(b"\xff\xd8\xff\xe0image")
    (tmp_path / "notes.txt").write_text("not image", encoding="utf-8")

    result = runtime.find_images(query="screenshot", max_results=5)

    assert result.ok
    assert "ScreenShot2.jpg" in result.content
    assert "notes.txt" not in result.content


def test_find_images_latest_can_ignore_unmatched_query(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    older = tmp_path / "old.png"
    latest = tmp_path / "latest.png"
    older.write_bytes(b"\x89PNG\r\n\x1a\nold")
    latest.write_bytes(b"\x89PNG\r\n\x1a\nnew")
    os.utime(older, (1000, 1000))
    os.utime(latest, (2000, 2000))

    result = runtime.find_images(query="\u622a\u56fe", latest_only=True)

    assert result.ok
    assert "latest.png" in result.content
    assert "old.png" not in result.content


def test_view_image_returns_attach_metadata(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    result = runtime.view_image("shot.png", detail="high")

    assert result.ok
    assert result.metadata["image"]["path"] == str(image.resolve())
    assert result.metadata["image"]["mime_type"] == "image/png"
    assert result.metadata["image"]["detail"] == "high"


def test_run_command_rejects_long_running_server(tmp_path: Path) -> None:
    from minicodex2.tools.command_runner import CommandRunner

    result = CommandRunner().run("python -m http.server", tmp_path)
    assert result.blocked
    assert "long-running" in (result.block_reason or "")


def test_run_command_rejects_runserver(tmp_path: Path) -> None:
    from minicodex2.tools.command_runner import CommandRunner

    result = CommandRunner().run("python manage.py runserver 127.0.0.1:8000", tmp_path)
    assert result.blocked
    assert "long-running" in (result.block_reason or "")


def test_command_runner_timeout_kills_process_tree(tmp_path: Path) -> None:
    from minicodex2.tools.command_runner import CommandRunner

    progress: list[dict[str, object]] = []

    result = CommandRunner().run(
        f'"{sys.executable}" -c "import time; time.sleep(5)"',
        tmp_path,
        timeout_seconds=1,
        progress_callback=progress.append,
    )

    assert result.timed_out
    assert "process tree was killed" in result.stderr
    assert any(event["stage"] == "started" for event in progress)
    assert any(event["stage"] == "timed_out" for event in progress)


def test_runtime_install_command_uses_longer_default_timeout(tmp_path: Path, monkeypatch) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    seen: dict[str, object] = {}

    def fake_run(command: str, cwd: Path, timeout_seconds: int = 30, progress_callback=None) -> CommandResult:
        seen["timeout_seconds"] = timeout_seconds
        return CommandResult(command=command, cwd=str(cwd), exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(runtime.command_runner, "run", fake_run)

    result = runtime.run_command("npm install")

    assert result.ok
    assert seen["timeout_seconds"] == 300


def test_runtime_run_command_includes_block_reason_for_long_running_command(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    result = runtime.run_command("npm run dev")

    assert result.blocked
    assert result.block_reason == "command looks long-running; use start_background_command"
    assert "block_reason: command looks long-running" in result.content


def test_session_treats_long_running_run_command_as_recoverable(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import ToolRegistry
    from minicodex2.tools.results import ToolResult

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    result = ToolResult(
        ok=False,
        content="block_reason: command looks long-running; use start_background_command",
        blocked=True,
        block_reason="command looks long-running; use start_background_command",
    )

    assert session._is_recoverable_tool_error("run_command", result)


def test_tool_result_fact_classifies_http_404_as_route_missing(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import ToolRegistry
    from minicodex2.tools.results import ToolResult

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    result = ToolResult(
        ok=False,
        content='{"status_code": 404, "error": "HTTP Error 404: Not Found"}',
        metadata={
            "url": "http://127.0.0.1:3000/api/auth/profile/",
            "status_code": 404,
            "error": "HTTP Error 404: Not Found",
            "body_excerpt": "Cannot GET /api/auth/profile/",
        },
    )

    fact = session._tool_result_fact("http_request", {"url": "http://127.0.0.1:3000/api/auth/profile/"}, result)

    assert "failure_kind=route_missing" in fact


def test_tool_result_fact_classifies_http_connection_refused_as_service_unreachable(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import ToolRegistry
    from minicodex2.tools.results import ToolResult

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    result = ToolResult(
        ok=False,
        content="connection failed",
        metadata={
            "url": "http://127.0.0.1:8000/api/tasks/",
            "status_code": None,
            "error": "[WinError 10061] connection refused",
        },
    )

    fact = session._tool_result_fact("http_request", {"url": "http://127.0.0.1:8000/api/tasks/"}, result)

    assert "failure_kind=service_unreachable" in fact


def test_tool_result_fact_classifies_background_startup_failed_with_log_tail(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import ToolRegistry
    from minicodex2.tools.results import ToolResult

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    result = ToolResult(
        ok=False,
        content="background failed",
        metadata={
            "command": "python manage.py runserver 0.0.0.0:8000",
            "cwd": "studentPet/backend",
            "port": 8000,
            "ready": False,
            "exit_code": 1,
            "log_tail": "The system cannot find the path specified.",
            "command_normalization": "removed leading cd",
        },
    )

    fact = session._tool_result_fact("start_background_command", {}, result)

    assert "failure_kind=startup_failed" in fact
    assert "exit_code=1" in fact
    assert "log_tail=The system cannot find the path specified." in fact
    assert "command_normalization=removed leading cd" in fact


def test_tool_result_fact_marks_project_memory_candidate_for_normalized_command(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import ToolRegistry
    from minicodex2.tools.results import ToolResult

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    result = ToolResult(
        ok=True,
        content="started",
        metadata={
            "command": f"{tmp_path}\\studentPet\\backend\\venv\\Scripts\\python.exe manage.py runserver 0.0.0.0:8000",
            "cwd": "studentPet/backend",
            "port": 8000,
            "ready": True,
            "command_normalization": "converted workspace-relative executable path to an absolute path",
        },
    )

    fact = session._tool_result_fact("start_background_command", {}, result)

    assert "memory_candidate=project command/path convention observed" in fact
    assert "remember_workflow" in fact


def test_tool_result_fact_marks_project_memory_candidate_for_nearby_venv(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import ToolRegistry
    from minicodex2.tools.results import ToolResult

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    result = ToolResult(
        ok=False,
        content="ModuleNotFoundError: No module named 'django'",
        metadata={
            "failure_kind": "python_dependency_missing",
            "nearby_python_venvs": ["studentPet/backend/venv/Scripts/python.exe"],
        },
    )

    fact = session._tool_result_fact("start_background_command", {}, result)

    assert "memory_candidate=project Python environment candidate observed" in fact
    assert "studentPet/backend/venv/Scripts/python.exe" in fact


def test_tool_result_fact_marks_workflow_candidate_for_verified_http(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import ToolRegistry
    from minicodex2.tools.results import ToolResult

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    result = ToolResult(
        ok=True,
        content="ok",
        metadata={
            "url": "http://localhost:8000/api/profile/",
            "method": "GET",
            "status_code": 200,
            "purpose": "verify",
        },
    )

    fact = session._tool_result_fact(
        "http_request",
        {
            "url": "http://localhost:8000/api/profile/",
            "headers": {"Authorization": "Token secret"},
            "purpose": "verify",
        },
        result,
    )

    assert "verified reusable HTTP/API workflow step observed" in fact
    assert "remember_workflow" in fact
    assert "auth_header_present=True" in fact
    assert "Token secret" not in fact


def test_tool_result_fact_marks_workflow_candidate_for_browser_test(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import ToolRegistry
    from minicodex2.tools.results import ToolResult

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    result = ToolResult(
        ok=True,
        content="ok",
        metadata={"requested_url": "http://localhost:3000/login"},
    )

    fact = session._tool_result_fact(
        "browser_test",
        {
            "url": "http://localhost:3000/login",
            "actions": [
                {"type": "fill", "selector": "input[name=username]", "value": "student"},
                {"type": "fill", "selector": "input[name=password]", "value": "secret"},
                {"type": "click", "selector": "button[type=submit]"},
            ],
            "purpose": "verify",
        },
        result,
    )

    assert "browser/UI workflow step observed" in fact
    assert "remember_workflow" in fact
    assert "action_types=['fill', 'fill', 'click']" in fact
    assert "secret" not in fact


def test_tool_result_fact_summarizes_browser_output_without_html_dump(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import ToolRegistry
    from minicodex2.tools.results import ToolResult

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    result = ToolResult(
        ok=True,
        content=json.dumps(
            {
                "ok": True,
                "requested_url": "http://localhost:3000/login",
                "final_url": "http://localhost:3000/tasks",
                "html_excerpt": "<html>" + ("x" * 2000) + "</html>",
                "text_excerpt": "lots of changing page text",
                "screenshot_path": ".minicodex2/browser_runs/run_20260618_010203/final.png",
            }
        ),
        metadata={
            "requested_url": "http://localhost:3000/login",
            "final_url": "http://localhost:3000/tasks",
            "title": "studentPet",
            "screenshot_path": ".minicodex2/browser_runs/run_20260618_010203/final.png",
            "network_responses": [
                {
                    "method": "POST",
                    "url": "http://localhost:3000/api/auth/login/",
                    "resource_type": "fetch",
                    "status": 200,
                    "body_excerpt": '{"token":"secret"}',
                }
            ],
        },
    )

    fact = session._tool_result_fact(
        "browser_test",
        {"url": "http://localhost:3000/login", "purpose": "verify"},
        result,
    )

    assert "requested_url=http://localhost:3000/login" in fact
    assert "final_url=http://localhost:3000/tasks" in fact
    assert "network=POST http://localhost:3000/api/auth/login/ status=200" in fact
    assert "html_excerpt" not in fact
    assert "browser_runs/run_" not in fact
    assert "secret" not in fact


def test_tool_result_fact_classifies_recovered_history_as_interrupted_batch(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import ToolRegistry
    from minicodex2.tools.results import ToolResult

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=None,  # type: ignore[arg-type]
        tools=ToolRegistry(),
    )
    result = ToolResult(
        ok=False,
        content="Recovered history: this tool call did not finish before the session was interrupted or restarted.",
        metadata={},
    )

    fact = session._tool_result_fact("http_request", {"url": "http://127.0.0.1:3000/api/tasks/"}, result)

    assert "failure_kind=interrupted_batch" in fact


def test_browser_failure_summary_includes_recent_network_hint() -> None:
    from minicodex2.tools.browser_automation import _browser_failure_details

    details = _browser_failure_details(
        {
            "action_results": [
                {
                    "type": "assert_text",
                    "selector": "body",
                    "ok": False,
                    "error": 'Error: expected text "Dashboard" was not found',
                }
            ],
            "network_responses": [
                {
                    "url": "http://localhost:3000/api/auth/login/",
                    "method": "POST",
                    "resource_type": "fetch",
                    "status": 400,
                    "body_excerpt": '{"detail":"bad password"}',
                }
            ],
        },
        "http://localhost:3000/login",
    )

    assert details["failure_kind"] == "browser_action_failed"
    assert "recent_network=POST http://localhost:3000/api/auth/login/ status=400" in details[
        "failure_summary"
    ]
    assert "bad password" in details["failure_summary"]


def test_run_command_redirects_raw_port_cleanup_to_port_tools(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    result = runtime.run_command("taskkill /PID 12345 /F")

    assert not result.ok
    assert not result.blocked
    assert "release_port" in result.content
    assert result.metadata["suggested_tool"] == "inspect_port/release_port"


def test_run_command_redirects_raw_port_inspection_to_port_tools(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    result = runtime.run_command("netstat -ano | findstr :5000")

    assert not result.ok
    assert not result.blocked
    assert "inspect_port" in result.content


def test_run_python_executes_helper_script_and_logs_it(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    result = runtime.run_python(
        "from pathlib import Path\nprint(len(Path('data.txt').read_text().splitlines()))\n"
    )

    assert result.ok
    assert "stdout:\n2" in result.content
    script_path = Path(result.metadata["script_path"])
    assert script_path.exists()
    assert script_path.parent.name == "python_tools"


def test_run_python_prefers_project_virtualenv(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    project = tmp_path / "project"
    project.mkdir()
    if os.name == "nt":
        python_dir = project / "venv" / "Scripts"
        python_exe = python_dir / "python.exe"
    else:
        python_dir = project / "venv" / "bin"
        python_exe = python_dir / "python"
    python_dir.mkdir(parents=True)
    shutil.copy2(sys.executable, python_exe)
    (project / "venv" / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).parent}\ninclude-system-site-packages = true\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        python_exe.chmod(0o755)

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    result = runtime.run_python(
        "import sys\nprint(sys.executable)\n",
        cwd="project",
    )

    assert result.ok
    assert str(python_exe) in result.content


def test_run_python_times_out(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    result = runtime.run_python("import time\ntime.sleep(5)\n", timeout_seconds=1)

    assert not result.ok
    command_result = result.metadata["command_result"]
    assert command_result.timed_out


def test_browser_test_returns_structured_result(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    runtime.browser.run = lambda **kwargs: {  # type: ignore[method-assign]
        "ok": True,
        "requested_url": kwargs["url"],
        "final_url": kwargs["url"],
        "title": "Demo",
        "text_excerpt": "Hello browser",
        "page_errors": [],
        "failed_requests": [{"source": "response", "url": "http://127.0.0.1:3000/favicon.ico", "status": 404}],
        "console_errors": [],
        "console_event_details": [{"type": "error", "text": "Failed to load resource", "url": "http://127.0.0.1:3000/register"}],
        "action_results": [{"type": "wait_for", "selector": "#app", "ok": True}],
        "storage_snapshot": {"local_storage": {"token": "abc"}, "session_storage": {}},
        "screenshot_path": ".minicodex2/browser_runs/run_001/final.png",
    }

    result = runtime.browser_test(
        "http://127.0.0.1:3000",
        actions=[{"type": "wait_for", "selector": "#app"}],
    )

    assert result.ok
    assert result.metadata["title"] == "Demo"
    assert result.metadata["failed_requests"][0]["status"] == 404
    assert result.metadata["storage_snapshot"]["local_storage"]["token"] == "abc"
    assert "Hello browser" in result.content


def test_browser_test_requires_url_or_port_before_node_check(tmp_path: Path, monkeypatch) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    result = runtime.browser_test()

    assert not result.ok
    assert result.blocked
    assert result.metadata["failure_kind"] == "invalid_tool_arguments"
    assert result.metadata["missing_arguments"] == ["url"]
    assert "url or port" in result.block_reason


def test_browser_test_builds_local_url_from_port_and_path(tmp_path: Path, monkeypatch) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    monkeypatch.setattr(shutil, "which", lambda name: f"C:/fake/{name}.exe")

    runtime.browser.run = lambda **kwargs: {  # type: ignore[method-assign]
        "ok": True,
        "requested_url": kwargs["url"],
        "final_url": kwargs["url"],
        "title": "Login",
        "text_excerpt": "Login page",
        "page_errors": [],
        "failed_requests": [],
        "console_errors": [],
        "action_results": [],
    }

    result = runtime.browser_test(port="3000", path="login")

    assert result.ok
    assert result.metadata["requested_url"] == "http://localhost:3000/login"


def test_browser_test_converts_same_origin_page_errors_into_failure(tmp_path: Path) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    runtime.browser.run = lambda **kwargs: {  # type: ignore[method-assign]
        "ok": True,
        "requested_url": kwargs["url"],
        "final_url": kwargs["url"],
        "title": "Broken page",
        "text_excerpt": "Oops",
        "page_errors": ["ReferenceError: apiBase is not defined"],
        "failed_requests": [],
        "console_errors": [],
        "action_results": [],
    }

    result = runtime.browser_test("http://127.0.0.1:3000/login")

    assert not result.ok
    assert "semantic_failure" in result.metadata


def test_browser_test_timeout_is_recoverable_tool_feedback(tmp_path: Path, monkeypatch) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    monkeypatch.setattr(shutil, "which", lambda name: f"C:/fake/{name}.exe")

    runtime.browser.run = lambda **kwargs: {  # type: ignore[method-assign]
        "ok": False,
        "requested_url": kwargs["url"],
        "final_url": kwargs["url"],
        "title": "",
        "text_excerpt": "",
        "page_errors": [],
        "failed_requests": [],
        "console_errors": [],
        "action_results": [{"type": "wait_for_load_state", "ok": True}],
        "error": "browser test timed out after 90s",
    }

    result = runtime.browser_test("http://127.0.0.1:3000/login", timeout_seconds=90)

    assert not result.ok
    assert not result.blocked
    assert result.metadata["failure_kind"] == "browser_timeout"
    assert "usually recoverable" in result.metadata["recovery_hint"]


def test_browser_test_blocks_without_node_or_npm(tmp_path: Path, monkeypatch) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    result = runtime.browser_test("http://127.0.0.1:3000")

    assert not result.ok
    assert result.blocked
    assert result.block_reason == "missing node or npm"


def test_nearest_detected_project_root_prefers_deeper_subproject(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import _nearest_detected_project_root
    from minicodex2.project.detector import ProjectDetector

    workspace = tmp_path / "workspace"
    frontend = workspace / "studentPet" / "frontend"
    pages = frontend / "src" / "pages"
    pages.mkdir(parents=True)
    (frontend / "package.json").write_text('{"name":"frontend","scripts":{"build":"vite build"}}', encoding="utf-8")
    changed_file = pages / "RegisterPage.js"
    changed_file.write_text("export default function RegisterPage() { return null; }\n", encoding="utf-8")

    detector = ProjectDetector()
    root = _nearest_detected_project_root(changed_file, workspace, detector.detect)

    assert root == frontend


def test_verification_builder_defers_node_subproject_to_model_with_hints(tmp_path: Path) -> None:
    from minicodex2.project.detector import ProjectDetector
    from minicodex2.verification.plan_builder import VerificationPlanBuilder

    workspace = tmp_path / "workspace"
    backend = workspace / "studentPet" / "backend"
    frontend = workspace / "studentPet" / "frontend"
    backend.mkdir(parents=True)
    frontend.mkdir(parents=True)
    (backend / "manage.py").write_text("print('django')\n", encoding="utf-8")
    (frontend / "package.json").write_text('{"name":"frontend","scripts":{"build":"vite build"}}', encoding="utf-8")

    settings = ConfigLoader().load(workspace)
    builder = VerificationPlanBuilder(settings)
    detector = ProjectDetector()
    profile = detector.detect(frontend)

    plan = builder.build(frontend, profile, ["studentPet/frontend/src/pages/RegisterPage.js"])

    assert plan.requires_model_decision
    assert not plan.steps
    assert "changed_files=studentPet/frontend/src/pages/RegisterPage.js" in plan.reason
    assert "detected_types=node" in plan.reason
    assert "package.json" in plan.reason


def test_verification_builder_defers_pytest_to_model_with_local_test_hints(tmp_path: Path) -> None:
    from minicodex2.project.detector import ProjectDetector
    from minicodex2.verification.plan_builder import VerificationPlanBuilder

    backend = tmp_path / "backend"
    tests_dir = backend / "tests"
    tests_dir.mkdir(parents=True)
    (backend / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tests_dir / "test_api.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    settings = ConfigLoader().load(tmp_path)
    builder = VerificationPlanBuilder(settings)
    detector = ProjectDetector()
    profile = detector.detect(backend)

    plan = builder.build(backend, profile, ["backend/users/models.py"])

    assert plan.requires_model_decision
    assert not plan.steps
    assert "test_signals=pytest" in plan.reason
    assert "tests_dir=tests" in plan.reason


def test_verification_builder_defers_service_dependent_pytest_to_model(tmp_path: Path) -> None:
    from minicodex2.project.detector import ProjectDetector
    from minicodex2.verification.plan_builder import VerificationPlanBuilder

    backend = tmp_path / "backend"
    tests_dir = backend / "tests"
    tests_dir.mkdir(parents=True)
    (backend / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tests_dir / "test_api_integrate.py").write_text(
        "import requests\nBASE_URL='http://localhost:8000/api'\n"
        "def test_login():\n    assert requests.get(BASE_URL).status_code == 200\n",
        encoding="utf-8",
    )

    settings = ConfigLoader().load(tmp_path)
    builder = VerificationPlanBuilder(settings)
    detector = ProjectDetector()
    profile = detector.detect(backend)

    plan = builder.build(backend, profile, ["backend/users/models.py"])

    assert plan.requires_model_decision
    assert not plan.steps
    assert "tests_hint=service-dependent or integration-like tests detected" in plan.reason


def test_toolchain_inspect_reports_missing_go(monkeypatch) -> None:
    from minicodex2.tools.toolchain import ToolchainManager

    monkeypatch.setattr("shutil.which", lambda command: None)
    monkeypatch.setattr("os.name", "posix")

    info = ToolchainManager().inspect("go")

    assert info["name"] == "go"
    assert info["available"] is False
    assert info["installers"] == []


def test_toolchain_inspect_offers_official_go_msi_without_winget(monkeypatch) -> None:
    from minicodex2.tools.toolchain import ToolchainManager

    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr("shutil.which", lambda command: None)

    info = ToolchainManager().inspect("go")

    assert info["available"] is False
    assert info["installers"] == ["official_msi"]


def test_toolchain_install_go_with_winget(monkeypatch) -> None:
    from minicodex2.tools.toolchain import ToolchainManager

    calls = []

    def fake_which(command: str):
        calls.append(("which", command))
        if command == "winget":
            return "C:/Windows/System32/winget.exe"
        if command == "go" and any(item[0] == "run" for item in calls):
            return "C:/Program Files/Go/bin/go.exe"
        return None

    def fake_run(command, **kwargs):
        calls.append(("run", command))
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr("subprocess.run", fake_run)

    info = ToolchainManager().install("go")

    assert info["installed"] is True
    assert info["installer"] == "winget"
    assert "GoLang.Go" in info["command"]
    assert info["after"]["available"] is True


def test_toolchain_install_go_with_official_msi(tmp_path: Path, monkeypatch) -> None:
    from minicodex2.tools.toolchain import ToolchainManager

    calls = []
    releases = (
        b'[{"stable":true,"files":[{"filename":"go1.2.3.windows-amd64.msi",'
        b'"os":"windows","arch":"amd64","kind":"installer"}]}]'
    )
    installer_bytes = b"msi"

    class FakeResponse:
        def __init__(self, data: bytes):
            self._data = data
            self._offset = 0
            self.headers = {"Content-Length": str(len(data))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size: int = -1):
            if size is None or size < 0:
                size = len(self._data) - self._offset
            chunk = self._data[self._offset : self._offset + size]
            self._offset += len(chunk)
            return chunk

    def fake_urlopen(url: str, timeout: int):
        calls.append(("urlopen", url, timeout))
        data = releases if url.endswith("?mode=json") else installer_bytes
        return FakeResponse(data)

    def fake_run(command, **kwargs):
        calls.append(("run", command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def fake_which(command: str):
        if command == "go" and any(call[0] == "run" for call in calls):
            return "C:/Program Files/Go/bin/go.exe"
        return None

    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("subprocess.run", fake_run)

    progress_events = []

    info = ToolchainManager().install(
        "go",
        artifact_root=tmp_path,
        progress=lambda stage, payload: progress_events.append((stage, payload)),
    )

    assert info["installed"] is True
    assert info["installer"] == "official_msi"
    assert "msiexec" in info["command"]
    assert info["after"]["available"] is True
    assert any(stage == "download_progress" for stage, _ in progress_events)
    assert progress_events[-1][0] == "finished"


def test_runtime_install_toolchain_reports_missing_installer(tmp_path: Path, monkeypatch) -> None:
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    monkeypatch.setattr("shutil.which", lambda command: None)
    monkeypatch.setattr("os.name", "posix")

    result = runtime.install_toolchain("go")

    assert not result.ok
    assert result.blocked
    assert "No supported installer" in (result.block_reason or "")


def test_background_command_refuses_preoccupied_port(tmp_path: Path) -> None:
    from minicodex2.tools.command_runner import BackgroundCommandManager

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = int(sock.getsockname()[1])
        info = BackgroundCommandManager().start(
            "python -c \"import time; time.sleep(30)\"",
            tmp_path,
            tmp_path / "server.log",
            port=port,
            ready_timeout_seconds=1,
        )
    assert info["ready"] is False
    assert info["pid"] is None
    assert "already in use" in str(info["error"])


def test_start_background_command_strips_duplicate_cd_matching_cwd(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    project = tmp_path / "studentPet" / "backend"
    project.mkdir(parents=True)
    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    captured: dict[str, object] = {}

    def fake_start(command, cwd, log_path, *, port=None, ready_timeout_seconds=15, restart_port=False):
        captured["command"] = command
        captured["cwd"] = cwd
        return {
            "pid": 123,
            "command": command,
            "port": port,
            "ready": False,
            "log_path": str(log_path),
            "exit_code": 1,
            "log_tail": "The system cannot find the path specified.",
        }

    runtime.background.start = fake_start  # type: ignore[method-assign]

    result = runtime.start_background_command(
        "cd studentPet\\backend && python manage.py runserver 0.0.0.0:8000",
        cwd="studentPet/backend",
        port=8000,
        restart_port=True,
    )

    assert result.ok is False
    assert captured["command"] == "python manage.py runserver 0.0.0.0:8000"
    assert result.metadata["original_command"].startswith("cd studentPet")
    assert "removed leading cd" in result.metadata["command_normalization"]


def test_start_background_command_normalizes_workspace_relative_executable_under_cwd(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    project = tmp_path / "studentPet" / "backend"
    executable = project / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    captured: dict[str, object] = {}

    def fake_start(command, cwd, log_path, *, port=None, ready_timeout_seconds=15, restart_port=False):
        captured["command"] = command
        captured["cwd"] = cwd
        return {
            "pid": 123,
            "command": command,
            "port": port,
            "ready": True,
            "log_path": str(log_path),
            "exit_code": None,
            "log_tail": "ready",
        }

    runtime.background.start = fake_start  # type: ignore[method-assign]

    result = runtime.start_background_command(
        "studentPet/backend/venv/bin/python manage.py runserver 0.0.0.0:8000",
        cwd="studentPet/backend",
        port=8000,
    )

    assert result.ok is True
    assert captured["command"] == f'"{executable.resolve()}" manage.py runserver 0.0.0.0:8000'
    assert "converted workspace-relative executable path" in result.metadata["command_normalization"]


def test_run_command_normalizes_workspace_relative_executable_under_cwd(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    project = tmp_path / "studentPet" / "backend"
    executable = project / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    captured: dict[str, object] = {}

    def fake_run(command, cwd, timeout_seconds, progress_callback=None):
        captured["command"] = command
        captured["cwd"] = cwd
        return CommandResult(command=command, cwd=str(cwd), exit_code=0, stdout="ok", stderr="")

    runtime.command_runner.run = fake_run  # type: ignore[method-assign]

    result = runtime.run_command(
        "studentPet/backend/venv/bin/python -c \"print('ok')\"",
        cwd="studentPet/backend",
    )

    assert result.ok is True
    assert captured["command"] == f'"{executable.resolve()}" -c "print(\'ok\')"'
    assert "converted workspace-relative executable path" in result.metadata["command_normalization"]


def test_start_background_command_classifies_missing_python_dependency(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    project = tmp_path / "studentPet" / "backend"
    project.mkdir(parents=True)
    venv_python = project / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    def fake_start(command, cwd, log_path, *, port=None, ready_timeout_seconds=15, restart_port=False):
        return {
            "pid": 123,
            "command": command,
            "port": port,
            "ready": False,
            "log_path": str(log_path),
            "exit_code": 1,
            "log_tail": "ModuleNotFoundError: No module named 'django'",
        }

    runtime.background.start = fake_start  # type: ignore[method-assign]

    result = runtime.start_background_command(
        "C:\\agent\\.venv\\Scripts\\python.exe manage.py runserver 0.0.0.0:8000",
        cwd="studentPet/backend",
        port=8000,
    )

    assert not result.ok
    assert result.metadata["failure_kind"] == "python_dependency_missing"
    assert result.metadata["missing_python_module"] == "django"
    assert str(venv_python) in result.metadata["nearby_python_venvs"]
    assert "project's nearby virtualenv" in result.metadata["recovery_hint"]


def test_start_background_command_reports_startup_diagnosis(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    project = tmp_path / "studentPet" / "frontend"
    (project / "node_modules").mkdir(parents=True)
    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)

    def fake_start(command, cwd, log_path, *, port=None, ready_timeout_seconds=15, restart_port=False):
        return {
            "pid": 123,
            "command": command,
            "port": port,
            "ready": False,
            "log_path": str(log_path),
            "exit_code": None,
            "log_tail": "> studentpet@1.0.0 start\n> react-scripts start\n",
        }

    runtime.background.start = fake_start  # type: ignore[method-assign]

    result = runtime.start_background_command(
        "npm start",
        cwd="studentPet/frontend",
        port=3000,
    )

    assert result.ok is True
    assert result.metadata["startup_state"] == "running_not_ready"
    assert result.metadata["ready"] is False
    diagnosis = result.metadata["diagnosis"]
    assert diagnosis["status"] == "process_running_port_not_ready"
    assert "react_scripts_start_seen" in diagnosis["log_signals"]
    assert "node_modules" in diagnosis["dependency_dirs_present"]
    assert any("dependencies" in action for action in diagnosis["suggested_next_actions"])


def test_port_manager_inspects_and_releases_port(tmp_path: Path) -> None:
    from minicodex2.tools.command_runner import PortManager, find_free_port, is_port_open

    port = find_free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port)],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not is_port_open(port):
            time.sleep(0.1)
        manager = PortManager()
        inspected = manager.inspect(port)
        assert inspected["pids"]
        released = manager.release(port)
        assert released["released"]
        assert released["killed_pids"]
    finally:
        if process.poll() is None:
            process.kill()


def test_windows_kill_process_tree_reports_taskkill_failure(monkeypatch) -> None:
    from minicodex2.tools.command_runner import PortManager

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=5,
            stdout="",
            stderr="Access is denied.",
        )

    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr("subprocess.run", fake_run)

    try:
        PortManager.kill_process_tree(12345)
    except RuntimeError as exc:
        assert "taskkill failed" in str(exc)
        assert "Access is denied" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected taskkill failure")


def test_windows_release_includes_descendant_processes(monkeypatch) -> None:
    from minicodex2.tools.command_runner import PortManager

    manager = PortManager()
    calls = {"pids_using_port": 0, "killed": []}

    def fake_pids_using_port(port: int) -> list[int]:
        calls["pids_using_port"] += 1
        if calls["pids_using_port"] == 1:
            return [26548]
        return []

    def fake_kill_process_tree(pid: int) -> None:
        calls["killed"].append(pid)
        if pid == 26548:
            raise RuntimeError("taskkill failed for PID 26548: not found")

    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr(manager, "pids_using_port", fake_pids_using_port)
    monkeypatch.setattr(manager, "kill_process_tree", fake_kill_process_tree)
    monkeypatch.setattr(manager, "_windows_child_pids", lambda pid: [28764] if pid == 26548 else [])

    result = manager.release(5000, timeout_seconds=0)

    assert result["released"] is True
    assert calls["killed"] == [26548, 28764]
    assert result["killed_pids"] == [28764]
    assert "26548" in result["errors"][0]


def test_project_detector_python_and_c(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "main.c").write_text("int main(){return 0;}\n", encoding="utf-8")
    profile = ProjectDetector().detect(tmp_path)
    assert profile.has_type("python")
    assert profile.has_type("c")
    assert "pytest" in profile.test_signals


def test_python_pytest_plan(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, [])
    assert plan.requires_model_decision
    assert not plan.steps
    assert "test_signals=pytest" in plan.reason


def test_c_main_compile_plan(tmp_path: Path) -> None:
    (tmp_path / "main.c").write_text("int main(){return 0;}\n", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, [])
    assert plan.requires_model_decision
    assert not plan.steps
    assert "detected_types=c" in plan.reason


def test_node_package_plan_prefers_test(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"node test.js","build":"vite build"}}',
        encoding="utf-8",
    )
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, [])
    assert plan.requires_model_decision
    assert not plan.steps
    assert "detected_types=node" in plan.reason
    assert "package.json" in plan.reason


def test_node_package_plan_defers_build_when_test_script_looks_interactive(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"react-scripts test","build":"react-scripts build"}}',
        encoding="utf-8",
    )
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, [])
    assert plan.requires_model_decision
    assert not plan.steps
    assert "detected_types=node" in plan.reason


def test_node_package_plan_defers_interactive_test_without_build(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"react-scripts test"}}',
        encoding="utf-8",
    )
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, [])
    assert plan.requires_model_decision
    assert not plan.steps
    assert "detected_types=node" in plan.reason


def test_node_tests_directory_does_not_imply_pytest(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"node tests/run.js"}}',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "run.js").write_text("console.log('ok')\n", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, [])
    assert "pytest" not in profile.test_signals
    assert plan.requires_model_decision
    assert "detected_types=node" in plan.reason


def test_rust_plan(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, [])
    assert plan.requires_model_decision
    assert "detected_types=rust" in plan.reason


def test_go_plan(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, [])
    assert plan.requires_model_decision
    assert "detected_types=go" in plan.reason


def test_go_single_file_plan_defers_to_model_from_changed_file(tmp_path: Path) -> None:
    (tmp_path / "hello.go").write_text(
        'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("Hello") }\n',
        encoding="utf-8",
    )
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, ["hello.go"])
    assert plan.requires_model_decision
    assert not plan.steps
    assert "changed_files=hello.go" in plan.reason


def test_documentation_only_plan(tmp_path: Path) -> None:
    (tmp_path / "design.md").write_text("# Design\n", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, ["design.md"])
    assert not plan.requires_model_decision
    assert plan.reason == "documentation-only changes"
    assert plan.steps[0].step_type == "document_check"


def test_documentation_only_plan_wins_inside_python_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "design.md").write_text("# Design\n", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)

    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, ["design.md"])

    assert not plan.requires_model_decision
    assert plan.reason == "documentation-only changes"
    assert [step.step_type for step in plan.steps] == ["document_check"]


def test_mixed_documentation_and_code_does_not_use_documentation_plan(tmp_path: Path) -> None:
    (tmp_path / "design.md").write_text("# Design\n", encoding="utf-8")
    (tmp_path / "hello.go").write_text(
        'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("Hello") }\n',
        encoding="utf-8",
    )
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, ["design.md", "hello.go"])
    assert plan.requires_model_decision
    assert "changed_files=design.md, hello.go" in plan.reason


def test_cmake_plan_defers_to_model(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.10)\n", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, [])
    assert plan.requires_model_decision
    assert "detected_types=cmake" in plan.reason


def test_python_web_plan_defers_to_model_with_web_hints(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n", encoding="utf-8")
    settings = ConfigLoader().load(tmp_path)
    profile = ProjectDetector().detect(tmp_path)
    plan = VerificationPlanBuilder(settings).build(tmp_path, profile, [])
    assert plan.requires_model_decision
    assert "detected_types=python_web" in plan.reason
    assert "web_signals=flask" in plan.reason


def test_failure_classifier_missing_dependency() -> None:
    classifier = FailureClassifier()
    result = CommandResult(
        command="python app.py",
        cwd=".",
        exit_code=1,
        stdout="",
        stderr="ModuleNotFoundError: No module named 'flask'",
    )
    classification = classifier.classify(
        step=VerificationStep(name="run", step_type="run", command="python app.py"),
        command_result=result,
    )
    assert classification.failure_type == "missing_dependency"
    assert classification.scope == "task_critical"


def test_failure_classifier_missing_go_toolchain_on_windows() -> None:
    classifier = FailureClassifier()
    result = CommandResult(
        command="go test ./...",
        cwd=".",
        exit_code=1,
        stdout="",
        stderr="'go' 不是内部或外部命令，也不是可运行的程序",
    )
    classification = classifier.classify(
        step=VerificationStep(name="go test", step_type="test", command="go test ./..."),
        command_result=result,
    )
    assert classification.failure_type == "missing_toolchain"
    assert classification.scope == "global"
    assert not classification.can_retry
    assert "go" in classification.first_blocking_line


def test_failure_classifier_does_not_treat_script_file_not_found_as_missing_toolchain() -> None:
    classifier = FailureClassifier()
    result = CommandResult(
        command="python tests/smoke.py",
        cwd=".",
        exit_code=1,
        stdout="",
        stderr=(
            "Traceback (most recent call last):\n"
            "  File \"tests/smoke.py\", line 10, in <module>\n"
            "FileNotFoundError: [Errno 2] No such file or directory: 'data.txt'\n"
        ),
    )

    classification = classifier.classify(
        step=VerificationStep(name="smoke", step_type="test", command="python tests/smoke.py"),
        command_result=result,
    )

    assert classification.failure_type == "runtime_error"
    assert classification.scope == "task_critical"
    assert classification.can_retry


def test_failure_pack_suggests_installing_missing_toolchain() -> None:
    from minicodex2.agent.failure_pack import FailurePack
    from minicodex2.verification.plan import VerificationPlan
    from minicodex2.verification.result import VerificationResult, VerificationStepResult

    step = VerificationStep(name="go test", step_type="test", command="go test ./...")
    command_result = CommandResult(
        command="go test ./...",
        cwd=".",
        exit_code=1,
        stdout="",
        stderr="'go' is not recognized as an internal or external command",
    )
    verification = VerificationResult(
        plan=VerificationPlan(reason="go.mod found", steps=[step]),
        step_results=[VerificationStepResult(step=step, command_result=command_result, ok=False)],
        passed=False,
    )

    pack = FailurePack.from_verification(verification)

    assert pack.failure_type == "missing_toolchain"
    assert pack.suggested_next_action
    assert "PATH" in pack.suggested_next_action


def test_dependency_policy_project_declared_install_allowed(tmp_path: Path) -> None:
    from minicodex2.decision.dependency_policy import DependencyInstallPolicy

    (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
    decision = DependencyInstallPolicy(allow_project_install=True).decide_missing_python_dependency(
        tmp_path,
        "ModuleNotFoundError: No module named 'flask'",
    )
    assert decision.action == "allow"


def test_dependency_policy_undeclared_dependency_blocks(tmp_path: Path) -> None:
    from minicodex2.decision.dependency_policy import DependencyInstallPolicy

    decision = DependencyInstallPolicy(allow_project_install=True).decide_missing_python_dependency(
        tmp_path,
        "ModuleNotFoundError: No module named 'flask'",
    )
    assert decision.action == "blocked"


def test_failure_classifier_compile_error() -> None:
    classifier = FailureClassifier()
    result = CommandResult(
        command="gcc main.c -o main",
        cwd=".",
        exit_code=1,
        stdout="",
        stderr="main.c:1:1: error: expected ';'",
    )
    classification = classifier.classify(
        step=VerificationStep(name="compile", step_type="compile", command="gcc main.c -o main"),
        command_result=result,
    )
    assert classification.failure_type == "compile_error"


def test_continuation_planner_global_blocker_blocks() -> None:
    from minicodex2.agent.failure_pack import FailurePack

    pack = FailurePack("command_blocked", "global", "cmd", None, "blocked", "", "sig")
    decision = ContinuationPlanner().decide(
        failure_pack=pack,
        repair_round=0,
        max_repair_rounds=3,
        previous_signature=None,
    )
    assert decision.action == "blocked"


def test_continuation_planner_repeated_signature_allows_first_repair() -> None:
    from minicodex2.agent.failure_pack import FailurePack

    pack = FailurePack("test_failure", "task_critical", "pytest failed", None, "pytest failed", "", "sig")
    decision = ContinuationPlanner().decide(
        failure_pack=pack,
        repair_round=0,
        max_repair_rounds=3,
        previous_signature="sig",
    )
    assert decision.action == "repair_now"


def test_continuation_planner_repeated_signature_continues_until_repair_limit() -> None:
    from minicodex2.agent.failure_pack import FailurePack

    pack = FailurePack("test_failure", "task_critical", "pytest failed", None, "pytest failed", "", "sig")
    decision = ContinuationPlanner().decide(
        failure_pack=pack,
        repair_round=1,
        max_repair_rounds=3,
        previous_signature="sig",
    )
    assert decision.action == "repair_now"


def test_continuation_planner_blocks_at_repair_limit() -> None:
    from minicodex2.agent.failure_pack import FailurePack

    pack = FailurePack("test_failure", "task_critical", "pytest failed", None, "pytest failed", "", "sig")
    decision = ContinuationPlanner().decide(
        failure_pack=pack,
        repair_round=3,
        max_repair_rounds=3,
        previous_signature="sig",
    )
    assert decision.action == "blocked"


def test_failure_classifier_normalizes_dynamic_http_ports() -> None:
    result_one = CommandResult(
        command="HTTP GET http://127.0.0.1:51661/",
        exit_code=1,
        stdout="",
        stderr="HTTP Error 404: NOT FOUND",
        cwd=Path("."),
    )
    result_two = CommandResult(
        command="HTTP GET http://127.0.0.1:51879/",
        exit_code=1,
        stdout="",
        stderr="HTTP Error 404: NOT FOUND",
        cwd=Path("."),
    )
    step = VerificationStep(
        name="http smoke",
        step_type="http_smoke",
        command=None,
    )
    classifier = FailureClassifier()

    first = classifier.classify(step=step, command_result=result_one)
    second = classifier.classify(step=step, command_result=result_two)

    assert first.signature == second.signature


def test_collaboration_advisor_complex_request_without_keyword(tmp_path: Path) -> None:
    advice = CollaborationAdvisor().evaluate(
        "Create a local code agent with CLI, API, TUI, permissions, tests and final acceptance",
        workspace_root=tmp_path,
    )
    assert advice.should_advise
    assert advice.suggested_mode == "design_first"
    assert advice.score >= 6


def test_history_store_redacts_api_key(tmp_path: Path) -> None:
    history = ChatHistory()
    history.add(ChatMessage(role="runtime", content="secret", metadata={"api_key": "abc"}))
    store = JsonlHistoryStore(tmp_path)
    jsonl, meta = store.save("session_test", history, {"api_key": "abc"})
    assert "abc" not in jsonl.read_text(encoding="utf-8")
    assert "abc" not in meta.read_text(encoding="utf-8")


def test_history_store_latest_session_roundtrip(tmp_path: Path) -> None:
    history = ChatHistory()
    history.add_user("hello")
    store = JsonlHistoryStore(tmp_path)
    store.save("session_one", history, {"result_state": "completed"})
    assert store.latest_session_id() == "session_one"
    loaded = store.load_history("session_one")
    assert loaded.messages()[0].content == "hello"


def test_runtime_logger_writes_and_redacts(tmp_path: Path) -> None:
    from minicodex2.agent.logging import RuntimeLogger

    logger = RuntimeLogger(tmp_path, "test")
    logger.write("api sk-secret-value")
    text = (tmp_path / "logs" / "test.log").read_text(encoding="utf-8")
    assert "<redacted-secret>" in text
    assert "sk-secret-value" not in text


def test_metrics_text_reports_tool_failures() -> None:
    from minicodex2.agent.unified_session import _format_metrics_text

    text = _format_metrics_text(
        {
            "turns": 2,
            "model_calls": 4,
            "tool_calls": 8,
            "tool_failures": 3,
            "browser_failures": 2,
            "verification_runs": 1,
            "verification_failures": 1,
            "context_compactions": 1,
            "cache_hit_ratio": 0.25,
            "tool_calls_by_name": {"browser_test": 3, "run_command": 2},
            "tool_failures_by_name": {"browser_test": 2, "run_command": 1},
            "required_retries": {"verify_after_write": 1},
        }
    )

    assert "browser_test=2" in text
    assert "25.0%" in text
    assert "verify_after_write=1" in text


def test_browser_schema_mentions_text_click_and_timeout(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.registry import build_runtime_tool_registry
    from minicodex2.tools.runtime_tools import RuntimeTools

    settings = ConfigLoader().load(tmp_path)
    registry = build_runtime_tool_registry(RuntimeTools(settings))
    schema = next(item for item in registry.schemas() if item["function"]["name"] == "browser_test")
    description = schema["function"]["description"]

    assert "click_text" in description
    assert "wait_for_timeout" in description


def test_turn_scope_gate_asks_model_for_archived_project_increment_only() -> None:
    from minicodex2.agent.unified_session import _should_classify_turn_scope
    from minicodex2.agent_os.state import SessionState

    state = SessionState()
    state.archived_goals.append(
        {
            "objective": "finish studentPet",
            "status": "complete",
            "work_plan_sources": [{"path": "studentPet/studentPet_design_document.md"}],
        }
    )

    assert not _should_classify_turn_scope(
        "hello",
        state=state,
        expected_action=None,
        diagnostic_decision=None,
    )
    assert _should_classify_turn_scope(
        "Update the design doc and implement direct image/video upload for student submissions",
        state=state,
        expected_action=None,
        diagnostic_decision=None,
    )


def test_modify_current_goal_can_remain_engaged_for_implementation() -> None:
    from minicodex2.agent.unified_session import _parse_turn_scope_decision

    decision = _parse_turn_scope_decision(
        '{"turn_scope":"modify_current_goal","goal_engagement":"engaged",'
        '"reason":"same project design document gained an implementation requirement"}'
    )

    assert decision.turn_scope == "modify_current_goal"
    assert decision.goal_engagement == "engaged"
    assert decision.engages_goal


def test_web_search_disabled_by_default(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    result = runtime.web_search("MiniCodex2")

    assert not result.ok
    assert result.blocked
    assert result.metadata["failure_kind"] == "web_search_disabled"


def test_web_search_mock_provider_returns_results(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "minicodex2.toml").write_text(
        """
[web_search]
enabled = true
provider = "mock"
cache_ttl_seconds = 60
""".strip(),
        encoding="utf-8",
    )
    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    first = runtime.web_search("agent runtime", max_results=3)
    second = runtime.web_search("agent runtime", max_results=3)

    assert first.ok
    assert "Mock result for agent runtime" in first.content
    assert not first.metadata["cache_hit"]
    assert second.ok
    assert second.metadata["cache_hit"]


def test_duckduckgo_html_parser_extracts_results() -> None:
    from minicodex2.web.tools import _parse_duckduckgo_html

    html = """
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdoc">Example &amp; Doc</a>
    <a class="result__snippet">A small <b>snippet</b>.</a>
    """

    results = _parse_duckduckgo_html(html, 5)

    assert len(results) == 1
    assert results[0].title == "Example & Doc"
    assert results[0].url == "https://example.com/doc"
    assert results[0].snippet == "A small snippet ."


def test_fetch_web_page_blocks_localhost(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.runtime_tools import RuntimeTools

    runtime = RuntimeTools(ConfigLoader().load(tmp_path))

    result = runtime.fetch_web_page("http://127.0.0.1:8000")

    assert not result.ok
    assert result.blocked
    assert result.metadata["failure_kind"] == "blocked_private_or_invalid_url"


def test_registry_includes_public_web_tools(tmp_path: Path) -> None:
    from minicodex2.config.settings import ConfigLoader
    from minicodex2.tools.registry import build_runtime_tool_registry
    from minicodex2.tools.runtime_tools import RuntimeTools

    registry = build_runtime_tool_registry(RuntimeTools(ConfigLoader().load(tmp_path)))
    names = {item["function"]["name"] for item in registry.schemas()}

    assert "web_search" in names
    assert "fetch_web_page" in names


def test_skill_registry_defaults_to_core_minicodex_code_skill(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    settings = ConfigLoader().load(tmp_path)
    registry = SkillRegistry(settings)
    summary = registry.summary()

    assert summary["code_guidance_mode"] == "external"
    assert summary["active_code_skill"] == "minicodex-code"
    assert summary["selection"]["active_by_domain"]["code"] == "minicodex-code"
    assert summary["legacy_code_guidance_enabled"] is False
    skill_names = {item["name"] for item in summary["skills"]}
    assert "legacy-builtin-code" in skill_names
    assert "minicodex-code" in skill_names
    minicodex_skill = next(item for item in summary["skills"] if item["name"] == "minicodex-code")
    assert "references/integration-debug.md" in minicodex_skill["references"]


def test_skill_registry_legacy_mode_can_reenable_builtin_code(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    (tmp_path / "minicodex2.toml").write_text(
        """
[skills]
code_guidance_mode = "legacy"
active_code_skill = "legacy-builtin-code"
""".strip(),
        encoding="utf-8",
    )

    summary = SkillRegistry(ConfigLoader().load(tmp_path)).summary()

    assert summary["selection"]["active_by_domain"]["code"] == "legacy-builtin-code"
    assert summary["legacy_code_guidance_enabled"] is True


def test_skill_registry_summary_is_stable_within_session(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    registry = SkillRegistry(ConfigLoader().load(tmp_path))

    first = json.dumps(registry.summary(), ensure_ascii=False, separators=(",", ":"))
    second = json.dumps(registry.summary(), ensure_ascii=False, separators=(",", ":"))

    assert first == second


def test_skill_registry_external_mode_selects_configured_code_skill(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    (tmp_path / "minicodex2.toml").write_text(
        """
[skills]
code_guidance_mode = "external"
active_code_skill = "minicodex-code"
""".strip(),
        encoding="utf-8",
    )

    settings = ConfigLoader().load(tmp_path)
    registry = SkillRegistry(settings)
    summary = registry.summary()

    assert summary["code_guidance_mode"] == "external"
    assert summary["selection"]["active_by_domain"]["code"] == "minicodex-code"
    assert "legacy-builtin-code" in summary["selection"]["references_by_domain"]["code"]


def test_minicodex_code_skill_loads_boundary_guidance(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    registry = SkillRegistry(ConfigLoader().load(tmp_path))

    result = registry.load_skill("minicodex-code")

    assert result.ok
    assert "Agent OS" in result.content
    assert "代码 Skill" in result.content
    assert "职责边界" in result.content
    assert "references/integration-debug.md" in result.content
    assert "路径安全" in result.content


def test_minicodex_code_skill_reference_loads_on_demand(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    registry = SkillRegistry(ConfigLoader().load(tmp_path))

    result = registry.load_skill("minicodex-code", "references/integration-debug.md")

    assert result.ok
    assert "联调调试" in result.content
    assert "browser_test" in result.content
    assert result.metadata["reference"] == "references/integration-debug.md"


def test_load_skill_reports_available_references_on_missing_reference(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    registry = SkillRegistry(ConfigLoader().load(tmp_path))

    result = registry.load_skill("minicodex-code", "references/missing.md")

    assert not result.ok
    assert result.blocked
    assert result.metadata["failure_kind"] == "skill_load_failed"
    assert "references/integration-debug.md" in result.metadata["available_references"]


def test_trusted_builtin_code_skill_python_hook_runs_with_agentos_api(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    registry = SkillRegistry(ConfigLoader().load(tmp_path))

    text = registry.render_hook(
        domain="code",
        hook="project_preflight",
        legacy_content="legacy",
        variables={"workspace_root": str(tmp_path), "expected_action": "modify_and_verify"},
    )

    assert "[PYTHON SKILL HOOK: code.project_preflight]" in text
    assert "ActiveSkill: minicodex-code" in text
    assert "Inspect the workspace before editing" in text
    assert "references/project-inspection.md" in text
    assert str(tmp_path) in text


def test_config_loader_resolves_relative_skill_dirs_from_workspace(tmp_path: Path) -> None:
    (tmp_path / "minicodex2.toml").write_text(
        """
[skills]
external_dirs = ["skills-local"]
""".strip(),
        encoding="utf-8",
    )

    settings = ConfigLoader().load(tmp_path)

    assert settings.skills.external_dirs == [(tmp_path / "skills-local").resolve()]


def test_skill_registry_blocks_external_primary_by_default(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    skill_dir = tmp_path / "skills-local" / "browser-primary"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Browser Primary\n", encoding="utf-8")
    (skill_dir / "skill.toml").write_text(
        """
[skill]
name = "browser-primary"
domain = "browser"
role = "primary"
priority = 999
description = "A third-party browser workflow."
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "minicodex2.toml").write_text(
        """
[skills]
external_dirs = ["skills-local"]
""".strip(),
        encoding="utf-8",
    )

    registry = SkillRegistry(ConfigLoader().load(tmp_path))
    selection = registry.summary()["selection"]

    assert selection["active_by_domain"]["browser"] == "minicodex-computer-use"
    assert "browser-primary" in selection["references_by_domain"]["browser"]
    assert any("was not activated" in warning for warning in selection["warnings"])


def test_skill_registry_allows_external_primary_when_enabled(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    skill_dir = tmp_path / "skills-local" / "browser-primary"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Browser Primary\n", encoding="utf-8")
    (skill_dir / "skill.toml").write_text(
        """
[skill]
name = "browser-primary"
domain = "browser"
role = "primary"
priority = 999
description = "A third-party browser workflow."
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "minicodex2.toml").write_text(
        """
[skills]
external_dirs = ["skills-local"]
allow_third_party_primary = true
""".strip(),
        encoding="utf-8",
    )

    registry = SkillRegistry(ConfigLoader().load(tmp_path))
    selection = registry.summary()["selection"]

    assert selection["active_by_domain"]["browser"] == "browser-primary"


def test_skill_registry_warns_on_invalid_manifest_reference(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    skill_dir = tmp_path / "skills-local" / "custom-code"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Custom Code\n", encoding="utf-8")
    (skill_dir / "skill.toml").write_text(
        """
[skill]
name = "custom-code"
domain = "code"
role = "reference"
description = "A custom code workflow."
references = ["references/ok.md", "../escape.md"]
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "minicodex2.toml").write_text(
        """
[skills]
external_dirs = ["skills-local"]
""".strip(),
        encoding="utf-8",
    )

    registry = SkillRegistry(ConfigLoader().load(tmp_path))
    custom = next(item for item in registry.summary()["skills"] if item["name"] == "custom-code")

    assert custom["references"] == ["references/ok.md"]
    assert any("invalid skill reference path" in warning for warning in custom["warnings"])


def test_skill_registry_external_code_primary_requires_explicit_config(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    skill_dir = tmp_path / "skills-local" / "custom-code"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Custom Code\n", encoding="utf-8")
    (skill_dir / "skill.toml").write_text(
        """
[skill]
name = "custom-code"
domain = "code"
role = "primary"
priority = 999
description = "A custom code workflow."
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "minicodex2.toml").write_text(
        """
[skills]
code_guidance_mode = "external"
active_code_skill = "custom-code"
external_dirs = ["skills-local"]
""".strip(),
        encoding="utf-8",
    )

    registry = SkillRegistry(ConfigLoader().load(tmp_path))
    selection = registry.summary()["selection"]

    assert selection["active_by_domain"]["code"] == "custom-code"
    assert "legacy-builtin-code" in selection["references_by_domain"]["code"]
    assert any("explicitly configured as primary" in warning for warning in selection["warnings"])


def test_skill_registry_blocks_reference_path_escape(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    registry = SkillRegistry(ConfigLoader().load(tmp_path))

    result = registry.load_skill("minicodex-code", "../registry.py")

    assert not result.ok
    assert result.blocked
    assert "failed to load skill" in result.content


def test_context_includes_stable_skill_context(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    registry = SkillRegistry(ConfigLoader().load(tmp_path))
    context = ContextManager().build_context(
        history=ChatHistory(),
        runtime_summary={"skill_context": registry.summary()},
    )
    text = "\n".join(message.content for message in context.messages)

    assert "[SKILL CONTEXT]" in text
    assert "legacy-builtin-code" in text
    assert "load_skill" in text


def test_skill_context_is_catalog_not_full_skill_body(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    registry = SkillRegistry(ConfigLoader().load(tmp_path))
    text = _formatted_runtime_summary_text({"skill_context": registry.summary()})

    assert "[SKILL CONTEXT]" in text
    assert "minicodex-code" in text
    assert "load_skill" in text
    assert "references/integration-debug.md" in text
    assert "职责边界" not in text


def test_external_code_skill_disables_legacy_engineering_guidance(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    (tmp_path / "minicodex2.toml").write_text(
        """
[skills]
code_guidance_mode = "external"
active_code_skill = "minicodex-code"
""".strip(),
        encoding="utf-8",
    )
    registry = SkillRegistry(ConfigLoader().load(tmp_path))
    text = _formatted_runtime_summary_text({"skill_context": registry.summary()})

    assert "Engineering skill rule:" not in text


def test_overlay_code_skill_keeps_legacy_engineering_guidance(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    (tmp_path / "minicodex2.toml").write_text(
        """
[skills]
code_guidance_mode = "overlay"
active_code_skill = "minicodex-code"
""".strip(),
        encoding="utf-8",
    )
    registry = SkillRegistry(ConfigLoader().load(tmp_path))
    text = _formatted_runtime_summary_text({"skill_context": registry.summary()})

    assert "Engineering skill rule:" in text
    assert "Legacy built-in code workflow guidance is disabled" not in text


def test_external_code_skill_disables_legacy_project_preflight_rules(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import build_runtime_tool_registry
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "minicodex2.toml").write_text(
        """
[skills]
code_guidance_mode = "external"
active_code_skill = "minicodex-code"
""".strip(),
        encoding="utf-8",
    )
    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=FakeModelAdapter(),
        tools=build_runtime_tool_registry(RuntimeTools(settings)),
    )

    text = session._project_preflight_message()

    assert "[PYTHON SKILL HOOK: code.project_preflight]" in text
    assert "ActiveSkill: minicodex-code" in text
    assert "LoadGuidance: call load_skill" in text
    assert "references/project-inspection.md" in text
    assert "Inspect the workspace before editing" in text


def test_overlay_code_skill_keeps_legacy_project_preflight_rules(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import build_runtime_tool_registry
    from minicodex2.tools.runtime_tools import RuntimeTools

    (tmp_path / "minicodex2.toml").write_text(
        """
[skills]
code_guidance_mode = "overlay"
active_code_skill = "minicodex-code"
""".strip(),
        encoding="utf-8",
    )
    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=FakeModelAdapter(),
        tools=build_runtime_tool_registry(RuntimeTools(settings)),
    )

    text = session._project_preflight_message()

    assert "Inspect the workspace before editing" in text
    assert "[SKILL HOOK: code.project_preflight]" not in text


def test_untrusted_external_python_hook_is_not_executed_by_default(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    skill_dir = tmp_path / "skills-local" / "custom-code"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Custom Code\n", encoding="utf-8")
    (skill_dir / "hooks.py").write_text(
        "def project_preflight(ctx):\n    return 'UNTRUSTED_CODE_EXECUTED'\n",
        encoding="utf-8",
    )
    (skill_dir / "skill.toml").write_text(
        """
[skill]
name = "custom-code"
domain = "code"
role = "primary"
description = "A custom code workflow."

[skill.hooks.project_preflight]
description = "Custom preflight."
python = "hooks.py:project_preflight"
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "minicodex2.toml").write_text(
        """
[skills]
code_guidance_mode = "external"
active_code_skill = "custom-code"
external_dirs = ["skills-local"]
""".strip(),
        encoding="utf-8",
    )
    registry = SkillRegistry(ConfigLoader().load(tmp_path))

    text = registry.render_hook(
        domain="code",
        hook="project_preflight",
        legacy_content="legacy",
    )

    assert "UNTRUSTED_CODE_EXECUTED" not in text
    assert "[SKILL HOOK: code.project_preflight]" in text
    assert "ActiveSkill: custom-code" in text


def test_builtin_code_skill_python_hooks_cover_diagnostic_repair_and_verification(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    registry = SkillRegistry(ConfigLoader().load(tmp_path))

    diagnostic = registry.render_hook(
        domain="code",
        hook="diagnostic_first",
        legacy_content="legacy",
        variables={"reason": "reported error", "user_report": "login fails"},
    )
    repair = registry.render_hook(
        domain="code",
        hook="tool_failure_repair",
        legacy_content="legacy",
        variables={"tool": "run_command", "failure_kind": "timeout"},
    )
    verification = registry.render_hook(
        domain="code",
        hook="verification_guidance",
        legacy_content="legacy",
        variables={"plan_reason": "changed Python files", "requires_model_decision": True},
    )

    assert "[PYTHON SKILL HOOK: code.diagnostic_first]" in diagnostic
    assert "references/integration-debug.md" in diagnostic
    assert "[PYTHON SKILL HOOK: code.tool_failure_repair]" in repair
    assert "references/code-change-loop.md" in repair
    assert "[PYTHON SKILL HOOK: code.verification_guidance]" in verification
    assert "references/verification-strategy.md" in verification


def test_builtin_computer_use_skill_registers_browser_hooks(tmp_path: Path) -> None:
    from minicodex2.skills.registry import SkillRegistry

    registry = SkillRegistry(ConfigLoader().load(tmp_path))
    summary = registry.summary()
    computer_skill = next(
        item for item in summary["skills"]
        if item["name"] == "minicodex-computer-use"
    )

    assert summary["selection"]["active_by_domain"]["browser"] == "minicodex-computer-use"
    assert "visual_observation" in computer_skill["capabilities"]
    assert "references/visual-action-loop.md" in computer_skill["references"]
    assert any(hook["name"] == "visual_task_preflight" for hook in computer_skill["hooks"])

    preflight = registry.render_hook(
        domain="browser",
        hook="visual_task_preflight",
        legacy_content="legacy",
        variables={"task": "log in through the browser", "url": "http://localhost:3000"},
    )
    repair = registry.render_hook(
        domain="browser",
        hook="action_repair",
        legacy_content="legacy",
        variables={"tool": "browser_test", "failure_kind": "selector_missing"},
    )

    assert "[PYTHON SKILL HOOK: browser.visual_task_preflight]" in preflight
    assert "Observe first" in preflight
    assert "http://localhost:3000" in preflight
    assert "[PYTHON SKILL HOOK: browser.action_repair]" in repair
    assert "Re-observe the UI before retrying" in repair


def test_session_uses_code_skill_hooks_for_diagnostic_failure_and_verification(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.registry import build_runtime_tool_registry
    from minicodex2.tools.runtime_tools import RuntimeTools
    from minicodex2.tools.results import ToolResult
    from minicodex2.verification.plan import VerificationPlan, VerificationStep

    settings = ConfigLoader().load(tmp_path)
    session = UnifiedAgentSession(
        settings=settings,
        model=FakeModelAdapter(),
        tools=build_runtime_tool_registry(RuntimeTools(settings)),
    )

    diagnostic = session._diagnostic_first_message("login fails", "reported error")
    session._handle_tool_failure(
        "run_command",
        {"command": "npm test"},
        ToolResult(ok=False, content="timed out", metadata={"failure_kind": "timeout"}),
        "turn_test",
    )
    repair = session.history.active_messages()[-1].content
    verification = session._verification_guidance_message(
        verification_root=tmp_path,
        changed_files=["app.py"],
        plan=VerificationPlan(
            steps=[
                VerificationStep(
                    name="pytest",
                    step_type="test",
                    command="python -m pytest",
                )
            ],
            reason="changed Python files",
            requires_model_decision=True,
        ),
    )

    assert "[PYTHON SKILL HOOK: code.diagnostic_first]" in diagnostic
    assert "[PYTHON SKILL HOOK: code.tool_failure_repair]" in repair
    assert "[PYTHON SKILL HOOK: code.verification_guidance]" in verification
    hook_events = [
        event.payload for event in session.event_bus.events()
        if event.type == "skill_hook_rendered"
    ]
    assert {event["hook"] for event in hook_events} >= {
        "diagnostic_first",
        "tool_failure_repair",
        "verification_guidance",
    }
    assert all(event["mode"] == "python_hook" for event in hook_events)
    assert all(event["active_skill"] == "minicodex-code" for event in hook_events)

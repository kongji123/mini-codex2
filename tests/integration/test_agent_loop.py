from __future__ import annotations

import json
from pathlib import Path

from minicodex2.agent.chat_history import ChatHistory
from minicodex2.agent.context_buffer import ContextBufferStore
from minicodex2.agent.unified_session import UnifiedAgentSession
from minicodex2.config.settings import ConfigLoader
from minicodex2.model.fake_adapter import FakeModelAdapter, FakeStep
from minicodex2.model.messages import ToolCall
from minicodex2.tools.results import CommandResult, ToolResult
from minicodex2.tools.registry import RegisteredTool, build_runtime_tool_registry
from minicodex2.tools.runtime_tools import RuntimeTools


def make_session(tmp_path: Path, fake: FakeModelAdapter) -> UnifiedAgentSession:
    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    return UnifiedAgentSession(
        settings=settings,
        model=fake,
        tools=build_runtime_tool_registry(runtime),
        history=ChatHistory(),
    )


def make_session_with_http_stub(
    tmp_path: Path,
    fake: FakeModelAdapter,
    *,
    responses: list[tuple[bool, int | None, str | None]] | None = None,
) -> UnifiedAgentSession:
    settings = ConfigLoader().load(tmp_path)
    runtime = RuntimeTools(settings)
    registry = build_runtime_tool_registry(runtime)
    remaining = list(responses or [(True, 200, None)])

    def http_request(url: str, method: str = "GET", headers=None, body=None, timeout_seconds=None, purpose: str = "smoke"):
        ok, status_code, error = remaining.pop(0) if remaining else (True, 200, None)
        payload = {
            "url": url,
            "method": method,
            "status_code": status_code,
            "ok": ok,
            "headers": {},
            "body_excerpt": "ok" if ok else "failure",
            "error": error,
            "purpose": purpose,
        }
        return ToolResult(ok=ok, content=str(payload), metadata=payload)

    registry.register(
        RegisteredTool(
            "http_request",
            "stub",
            {"type": "object", "properties": {}, "required": []},
            http_request,
        )
    )
    return UnifiedAgentSession(
        settings=settings,
        model=fake,
        tools=registry,
        history=ChatHistory(),
    )


def test_chat_no_tools_one_model_call(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("hello")])
    session = make_session(tmp_path, fake)
    result = session.run_turn("hi")
    assert result.status == "completed"
    assert fake.call_count == 1


def test_main_model_context_buffer_is_persisted_and_used(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("hello")])
    session = make_session(tmp_path, fake)

    result = session.run_turn("hi", allow_advice=False)

    assert result.status == "completed"
    store = ContextBufferStore(tmp_path / ".minicodex2")
    buffered = store.load(session.session_id)
    assert buffered
    request_messages = fake.requests[0].messages
    assert [message.content for message in buffered[: len(request_messages)]] == [
        message.content for message in request_messages
    ]
    assert buffered[-1].role == "assistant"
    assert buffered[-1].content == "hello"


def test_session_writes_readable_model_context_dump(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("hello")])
    session = make_session(tmp_path, fake)
    session.settings.agent.context_dump_enabled = True

    result = session.run_turn("hi")

    assert result.status == "completed"
    latest = tmp_path / ".minicodex2" / "logs" / "context" / "latest.md"
    assert latest.exists()
    text = latest.read_text(encoding="utf-8")
    assert "# MiniCodex2 Model Context Dump" in text
    assert "role=runtime model_role=system" in text
    assert "role=user model_role=user" in text
    assert "hi" in text


def test_context_dump_command_toggles_model_context_dump(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("first"), FakeStep("second")])
    session = make_session(tmp_path, fake)

    first = session.run_turn("hi")
    assert first.status == "completed"
    latest = tmp_path / ".minicodex2" / "logs" / "context" / "latest.md"
    assert not latest.exists()

    enabled = session.run_turn("/context-dump on")
    assert enabled.status == "completed"
    assert "enabled" in enabled.response
    assert fake.call_count == 1

    second = session.run_turn("hello again")
    assert second.status == "completed"
    assert latest.exists()
    assert "hello again" in latest.read_text(encoding="utf-8")

    disabled = session.run_turn("/context-dump off")
    assert disabled.status == "completed"
    assert "disabled" in disabled.response
    assert fake.call_count == 2


def test_idle_tick_injects_ephemeral_runtime_message_without_history_pollution(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep("I should keep going."),
            FakeStep(
                "inspect workspace",
                [ToolCall("call_1", "read_file", {"path": "README.md"})],
            ),
            FakeStep("Idle reflection complete."),
        ]
    )
    session = make_session(tmp_path, fake)
    session.state.active_goal = "Finish the project"
    session.state.goal_status = "active"
    session.history.add_user("continue the project")
    session.settings.agent.idle_tick_interval_seconds = 0

    result = session.run_idle_tick()

    assert result is not None
    assert result.status == "completed"
    assert fake.call_count == 3
    assert any(
        message.role == "runtime" and "[IDLE REFLECTION]" in message.content
        for request in fake.requests
        for message in request.messages
    )
    assert not any(
        message.role == "runtime" and "[IDLE REFLECTION]" in message.content
        for message in session.history.messages()
    )
    event_types = [event.type for event in session.event_bus.events()]
    assert "idle_tick_started" in event_types
    assert "idle_tick_finished" in event_types


def test_idle_tick_stops_after_max_ticks(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("idle one"), FakeStep("idle two")])
    session = make_session(tmp_path, fake)
    session.state.active_goal = "Finish the project"
    session.state.goal_status = "active"
    session.history.add_user("hello")
    session.state.result_state = "completed"
    session.settings.agent.idle_tick_interval_seconds = 0
    session.settings.agent.idle_tick_max = 2

    first = session.run_idle_tick()
    second = session.run_idle_tick()
    calls_before_third = fake.call_count
    third = session.run_idle_tick()

    assert first is not None
    assert second is not None
    assert third is None
    assert fake.call_count == calls_before_third


def test_idle_tick_requires_active_goal(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("idle should not run")])
    session = make_session(tmp_path, fake)
    session.history.add_user("hello")
    session.state.result_state = "completed"
    session.settings.agent.idle_tick_interval_seconds = 0

    result = session.run_idle_tick()

    assert result is None
    assert fake.call_count == 0


def test_user_turn_resets_idle_tick_budget(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep("idle one"),
            FakeStep("idle two"),
            FakeStep("user reply"),
            FakeStep("idle after user"),
            FakeStep("idle after user again"),
        ]
    )
    session = make_session(tmp_path, fake)
    session.state.active_goal = "Finish the project"
    session.state.goal_status = "active"
    session.history.add_user("hello")
    session.state.result_state = "completed"
    session.settings.agent.idle_tick_interval_seconds = 0
    session.settings.agent.idle_tick_max = 2

    assert session.run_idle_tick() is not None
    assert session.run_idle_tick() is not None
    assert session.run_idle_tick() is None

    user_result = session.run_turn("new user message")

    assert user_result.status == "completed"
    assert session.run_idle_tick() is not None
    assert session.run_idle_tick() is not None
    assert session.run_idle_tick() is None


def test_toolchain_install_request_retries_when_model_does_not_call_tools(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda command: None)
    fake = FakeModelAdapter(
        [
            FakeStep("I cannot install software."),
            FakeStep(
                "checking toolchain",
                [ToolCall("call_1", "inspect_toolchain", {"name": "go"})],
            ),
            FakeStep("Go is missing."),
        ]
    )
    session = make_session_with_http_stub(tmp_path, fake)

    result = session.run_turn("install go", allow_advice=False)

    assert result.status == "completed"
    assert fake.call_count == 3
    assert any(message.role == "tool" and '"name": "go"' in message.content for message in session.history.messages())
    event_types = [event.type for event in session.event_bus.events()]
    assert "required_tool_retry" in event_types


def test_toolchain_install_request_ignores_previous_turn_write_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda command: None)
    fake = FakeModelAdapter(
        [
            FakeStep(
                "checking toolchain",
                [ToolCall("call_1", "inspect_toolchain", {"name": "go"})],
            ),
            FakeStep("Go is missing."),
            FakeStep("I still need to install it."),
        ]
    )
    session = make_session_with_http_stub(tmp_path, fake)
    session.state.did_write = True
    session.state.changed_files = ["previous.go"]

    result = session.run_turn("please install go", allow_advice=False)

    assert result.status == "completed"
    assert fake.call_count == 3
    event_types = [event.type for event in session.event_bus.events()]
    assert "required_tool_retry" in event_types
    assert "verification_started" not in event_types


def test_modify_contract_retries_when_model_does_not_call_tools(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep("Here is how you could fix it."),
            FakeStep(
                "now editing",
                [
                    ToolCall(
                        "call_1",
                        "write_file",
                        {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                    )
                ],
            ),
            FakeStep("fixed"),
        ]
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    session = make_session_with_http_stub(tmp_path, fake)
    result = session.run_turn(
        "fix add",
        allow_advice=False,
        expected_action="modify_and_verify",
    )
    assert result.status == "passed"
    assert fake.call_count == 2
    event_types = [event.type for event in session.event_bus.events()]
    assert "project_preflight_created" in event_types
    assert "required_tool_retry" in event_types


def test_recoverable_missing_file_tool_error_keeps_loop_alive(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "guess wrong file",
                [ToolCall("call_1", "read_file", {"path": "missing.py"})],
            ),
            FakeStep(
                "inspect after failure",
                [ToolCall("call_2", "list_directory", {"path": "."})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session_with_http_stub(tmp_path, fake)
    result = session.run_turn(
        "inspect project",
        allow_advice=False,
    )
    assert result.status == "completed"
    assert fake.call_count == 3
    event_types = [event.type for event in session.event_bus.events()]
    assert "recoverable_tool_error" in event_types


def test_recoverable_edit_old_text_not_found_keeps_loop_alive(tmp_path: Path) -> None:
    (tmp_path / "minicodex2.toml").write_text(
        '[verification]\ncommands = ["python -m py_compile app.py"]\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(
        "if __name__ == '__main__':\n"
        "    app.run(debug=True, port=5000)\n",
        encoding="utf-8",
    )
    fake = FakeModelAdapter(
        [
            FakeStep(
                "try stale edit",
                [
                    ToolCall(
                        "call_1",
                        "edit_file",
                        {
                            "path": "app.py",
                            "old_text": "app.run(debug=True)",
                            "new_text": "app.run(debug=True, host='127.0.0.1', port=5000)",
                        },
                    )
                ],
            ),
            FakeStep("inspect file", [ToolCall("call_2", "read_file", {"path": "app.py"})]),
            FakeStep(
                "retry exact edit",
                [
                    ToolCall(
                        "call_3",
                        "edit_file",
                        {
                            "path": "app.py",
                            "old_text": "app.run(debug=True, port=5000)",
                            "new_text": "app.run(debug=True, host='127.0.0.1', port=5000)",
                        },
                    )
                ],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("fix server port", allow_advice=False)

    assert result.status == "passed"
    assert fake.call_count >= 4
    assert "host='127.0.0.1'" in (tmp_path / "app.py").read_text(encoding="utf-8")
    event_types = [event.type for event in session.event_bus.events()]
    assert "recoverable_tool_error" in event_types


def test_failed_tool_result_retries_with_failure_context(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "try wrong command",
                [ToolCall("call_1", "run_command", {"command": "python missing.py"})],
            ),
            FakeStep(
                "retry corrected command",
                [ToolCall("call_2", "run_command", {"command": "python scripts/ok.py"})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("run script", allow_advice=False)

    assert result.status == "completed"
    assert fake.call_count == 3
    assert any(
        message.role == "runtime" and "The previous tool call failed" in message.content
        for message in session.history.messages()
    )
    event_types = [event.type for event in session.event_bus.events()]
    assert "tool_failure_recovery_started" in event_types


def test_tool_results_are_visible_to_next_model_call_without_facts_injection(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "run command",
                [ToolCall("call_1", "run_command", {"command": "python scripts/ok.py", "purpose": "smoke"})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("run smoke", allow_advice=False)

    assert result.status == "completed"
    assert fake.call_count == 2
    second_request_text = "\n".join(message.content for message in fake.requests[1].messages)
    assert "[TOOL RESULT FACTS]" not in second_request_text
    assert "ok" in second_request_text
    assert any(event.type == "tool_result_facts_recorded" for event in session.event_bus.events())


def test_read_file_fact_keeps_metadata_not_body(tmp_path: Path) -> None:
    from minicodex2.agent.unified_session import UnifiedAgentSession
    from minicodex2.tools.results import ToolResult

    session = UnifiedAgentSession.__new__(UnifiedAgentSession)
    result = ToolResult(
        ok=True,
        content="# Huge design document\n" + "important body " * 200,
        metadata={
            "path": "studentPet/studentPet_design_document.md",
            "size": 5000,
            "line_count": 120,
            "start_line": 1,
            "end_line": 40,
            "returned_lines": 40,
            "returned_bytes": 1800,
            "next_start_line": 41,
            "truncated": True,
            "sha256": "abcdef1234567890",
        },
    )

    fact = session._tool_result_fact("read_file", {"path": "studentPet/studentPet_design_document.md"}, result)

    assert "tool=read_file" in fact
    assert "path=studentPet/studentPet_design_document.md" in fact
    assert "line_count=120" in fact
    assert "next_start_line=41" in fact
    assert "sha256=abcdef123456" in fact
    assert "Huge design document" not in fact
    assert "important body" not in fact


def test_model_can_record_lesson_and_lesson_is_visible_next_call(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "store reusable lesson",
                [
                    ToolCall(
                        "call_lesson",
                        "record_lesson",
                        {
                            "summary": "When a path is missing, inspect the parent directory before guessing.",
                            "trigger": "missing path",
                            "scope": "tool-recovery",
                            "evidence": "read_file returned path not found",
                        },
                    )
                ],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("remember this debugging lesson", allow_advice=False)

    assert result.status == "completed"
    assert session.state.lessons[0].scope == "tool-recovery"
    second_request_text = "\n".join(message.content for message in fake.requests[1].messages)
    assert "[LESSON MEMORY]" in second_request_text
    assert "inspect the parent directory" in second_request_text
    assert any(event.type == "lesson_recorded" for event in session.event_bus.events())


def test_failed_tool_result_records_failed_evidence(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "fail command",
                [ToolCall("call_1", "run_command", {"command": "python missing.py"})],
            ),
            FakeStep(
                "recover",
                [ToolCall("call_2", "run_command", {"command": "python -c \"print('ok')\""})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("run script", allow_advice=False)

    assert result.status == "completed"
    failed_records = [
        record for record in session.state.evidence_records
        if record.kind == "tool_failure" and record.status == "failed"
    ]
    assert failed_records
    assert "python missing.py" in failed_records[0].summary
    second_request_text = "\n".join(message.content for message in fake.requests[1].messages)
    assert "[EVIDENCE EVENT]" in second_request_text
    assert "tool_failure" in second_request_text


def test_unresolved_tool_failure_requests_corrigibility_reflection(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "fail command",
                [ToolCall("call_1", "run_command", {"command": "python missing.py"})],
            ),
            FakeStep("manual instruction instead of retry"),
            FakeStep(
                "retry corrected command",
                [ToolCall("call_2", "run_command", {"command": "python -c \"print('ok')\""})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("run script", allow_advice=False)

    assert result.status == "completed"
    third_request_text = "\n".join(message.content for message in fake.requests[2].messages)
    assert "[CORRIGIBILITY REFLECTION]" in third_request_text
    assert "What assumption may be wrong?" in third_request_text
    assert any(event.type == "reflection_requested" for event in session.event_bus.events())


def test_workspace_facts_are_visible_to_next_model_call(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "inspect",
                [
                    ToolCall("call_list", "list_directory", {"path": "src"}),
                    ToolCall("call_read", "read_file", {"path": "src/app.py"}),
                ],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("inspect and update app", allow_advice=False)

    assert result.status == "completed"
    second_request_text = "\n".join(message.content for message in fake.requests[1].messages)
    assert "[TOOL RESULT FACTS]" not in second_request_text
    assert "src/app.py" in second_request_text


def test_http_observation_is_visible_to_next_model_call(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep("scope"),
            FakeStep(
                "probe",
                [ToolCall("call_http", "http_request", {"url": "http://127.0.0.1:8000/api/tasks"})],
            ),
            FakeStep(
                "recover",
                [ToolCall("call_http_ok", "http_request", {"url": "http://127.0.0.1:8000/api/tasks"})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session_with_http_stub(
        tmp_path,
        fake,
        responses=[(False, 404, None), (True, 200, None)],
    )

    result = session.run_turn("debug API 404", allow_advice=False)

    assert result.status == "completed"
    second_request_text = "\n".join(message.content for message in fake.requests[2].messages)
    assert "[TOOL RESULT FACTS]" not in second_request_text
    assert "url=http://127.0.0.1:8000/api/tasks" in second_request_text
    assert "status_code=404" in second_request_text
    assert "[EVIDENCE EVENT]" in second_request_text
    assert "http_observation" in second_request_text


def test_distinct_tool_failures_do_not_share_repair_limit(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "fail one",
                [ToolCall("call_1", "run_command", {"command": "python missing_one.py"})],
            ),
            FakeStep(
                "fail two",
                [ToolCall("call_2", "run_command", {"command": "python missing_two.py"})],
            ),
            FakeStep(
                "fail three",
                [ToolCall("call_3", "run_command", {"command": "python missing_three.py"})],
            ),
            FakeStep(
                "recover",
                [ToolCall("call_4", "run_command", {"command": "python -c \"print('ok')\""})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("run several commands", allow_advice=False)

    assert result.status == "completed"
    assert fake.call_count == 5
    assert [
        event.payload.get("round")
        for event in session.event_bus.events()
        if event.type == "tool_failure_recovery_started"
    ] == [1, 1, 1]


def test_repeated_failed_tool_result_retries_until_repair_limit(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "try wrong command",
                [ToolCall("call_1", "run_command", {"command": "python missing.py"})],
            ),
            FakeStep(
                "repeat wrong command",
                [ToolCall("call_2", "run_command", {"command": "python missing.py"})],
            ),
            FakeStep(
                "repeat wrong command again",
                [ToolCall("call_3", "run_command", {"command": "python missing.py"})],
            ),
        ]
    )
    session = make_session(tmp_path, fake)
    session.settings.max_repair_rounds = 2

    result = session.run_turn("run script", allow_advice=False)

    assert result.status == "blocked"
    assert "tool repair limit reached" in result.response
    event_types = [event.type for event in session.event_bus.events()]
    assert event_types.count("tool_failure_recovery_started") == 2
    assert "blocked" in event_types


def test_unresolved_tool_failure_is_not_hidden_by_verification(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "write then fail",
                [
                    ToolCall("call_write", "write_file", {"path": "app.py", "content": "print('ok')\n"}),
                    ToolCall("call_bad", "run_command", {"command": "python missing.py"}),
                ],
            ),
            FakeStep("manual instruction instead of retry"),
            FakeStep(
                "retry corrected command",
                [ToolCall("call_good", "run_command", {"command": "python app.py"})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("write and verify", allow_advice=False)

    assert result.status == "passed"
    assert fake.call_count == 4
    event_payloads = [
        event.payload
        for event in session.event_bus.events()
        if event.type == "required_tool_retry"
    ]
    assert {"expected_action": "resolve_tool_failure"} in event_payloads


def test_missing_tool_argument_is_recoverable(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('before')\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "bad edit",
                [ToolCall("call_bad", "edit_file", {"old_text": "before", "new_text": "after"})],
            ),
            FakeStep(
                "fixed edit",
                [
                    ToolCall(
                        "call_good",
                        "edit_file",
                        {"path": "app.py", "old_text": "before", "new_text": "after"},
                    )
                ],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("edit app", allow_advice=False)

    assert result.status == "passed"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('after')\n"
    assert any(
        event.type == "recoverable_tool_error"
        and event.payload.get("reason") == "missing required tool arguments: path"
        for event in session.event_bus.events()
    )


def test_unresolved_tool_failure_blocks_after_retry_prompt(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "write then fail",
                [
                    ToolCall("call_write", "write_file", {"path": "app.py", "content": "print('ok')\n"}),
                    ToolCall("call_bad", "run_command", {"command": "python missing.py"}),
                ],
            ),
            FakeStep("manual instruction instead of retry"),
            FakeStep("still no retry"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("write and verify", allow_advice=False)

    assert result.status == "blocked"
    assert "recoverable tool failure" in result.response


def test_assistant_question_after_write_retries_instead_of_waiting_for_user(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "write first file",
                [ToolCall("call_1", "write_file", {"path": "app.py", "content": "print('one')\n"})],
            ),
            FakeStep("I wrote the first file. Continue like this?"),
            FakeStep(
                "continue with tools",
                [ToolCall("call_2", "write_file", {"path": "more.py", "content": "print('two')\n"})],
            ),
            FakeStep(
                "verify with a model-selected command",
                [
                    ToolCall(
                        "call_3",
                        "run_command",
                        {
                            "command": "python -m py_compile app.py more.py",
                            "purpose": "test",
                        },
                    )
                ],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("continue development", allow_advice=False)

    assert result.status == "passed"
    assert fake.call_count == 5
    assert (tmp_path / "more.py").exists()
    event_payloads = [
        event.payload
        for event in session.event_bus.events()
        if event.type == "required_tool_retry"
    ]
    assert any(
        payload.get("expected_action") == "continue_after_deferred_work"
        for payload in event_payloads
    )


def test_assistant_promise_to_continue_after_write_retries_with_tools(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "write first module",
                [ToolCall("call_1", "write_file", {"path": "pets.py", "content": "class Pet:\n    pass\n"})],
            ),
            FakeStep("I completed the pets module. I will continue with rankings and notifications next."),
            FakeStep(
                "continue with tools",
                [ToolCall("call_2", "write_file", {"path": "rankings.py", "content": "def rank():\n    return []\n"})],
            ),
            FakeStep(
                "verify with a model-selected command",
                [
                    ToolCall(
                        "call_3",
                        "run_command",
                        {
                            "command": "python -m py_compile pets.py rankings.py",
                            "purpose": "test",
                        },
                    )
                ],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("build the project", allow_advice=False)

    assert result.status == "passed"
    assert fake.call_count == 5
    assert (tmp_path / "rankings.py").exists()
    event_payloads = [
        event.payload
        for event in session.event_bus.events()
        if event.type == "required_tool_retry"
    ]
    assert any(
        payload.get("expected_action") == "continue_after_deferred_work"
        for payload in event_payloads
    )


def test_multiple_deferred_work_messages_continue_in_same_turn(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "write first module",
                [ToolCall("call_1", "write_file", {"path": "one.py", "content": "print('one')\n"})],
            ),
            FakeStep("I completed the first module. I will continue with the second module next."),
            FakeStep(
                "write second module",
                [ToolCall("call_2", "write_file", {"path": "two.py", "content": "print('two')\n"})],
            ),
            FakeStep("The second module is done. I will continue with the third module next."),
            FakeStep(
                "write third module",
                [ToolCall("call_3", "write_file", {"path": "three.py", "content": "print('three')\n"})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("build all modules", allow_advice=False)

    assert result.status == "passed"
    assert fake.call_count == 6
    assert (tmp_path / "three.py").exists()
    retry_payloads = [
        event.payload
        for event in session.event_bus.events()
        if event.type == "required_tool_retry"
        and event.payload.get("expected_action") == "continue_after_deferred_work"
    ]
    assert [payload.get("count") for payload in retry_payloads] == [1, 2]


def test_read_project_uses_list_and_read(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "I will inspect.",
                [
                    ToolCall("call_1", "list_directory", {"path": "."}),
                    ToolCall("call_2", "read_file", {"path": "README.md"}),
                ],
            ),
            FakeStep("Project contains README."),
        ]
    )
    session = make_session(tmp_path, fake)
    result = session.run_turn("read project", allow_advice=False)
    assert result.status == "completed"
    assert fake.call_count == 2
    assert any(m.role == "tool" and "README" in m.content for m in session.history.messages())
    event_types = [event.type for event in session.event_bus.events()]
    assert "session_created" in event_types
    assert "model_call_started" in event_types
    assert "token_usage_recorded" in event_types
    assert event_types.count("tool_call_started") == 2
    assert event_types.count("tool_call_finished") == 2


def test_history_clear_command_does_not_call_model(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("should not be used")])
    session = make_session(tmp_path, fake)
    session.history.add_user("old")
    session.history.add_assistant("old response")

    result = session.run_turn("/clear", allow_advice=False)

    assert result.status == "completed"
    assert "Cleared 2 history messages" in result.response
    assert session.history.messages() == []
    assert fake.call_count == 0
    assert any(event.type == "history_changed" for event in session.event_bus.events())


def test_history_drop_last_command_does_not_call_model(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("should not be used")])
    session = make_session(tmp_path, fake)
    session.history.add_user("one")
    session.history.add_assistant("two")
    session.history.add_user("three")

    result = session.run_turn("/drop-last 2", allow_advice=False)

    assert result.status == "completed"
    assert [message.content for message in session.history.messages()] == ["one"]
    assert fake.call_count == 0


def test_session_persists_context_compaction_checkpoint(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "[ROOT OBJECTIVE]\nBuild project.\n[CURRENT PROGRESS]\nOlder history summarized.\n[NEXT ACTIONS]\nContinue."
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)
    session.settings.context.budget_tokens = 500
    session.settings.context.compression_threshold = 0.50
    session.history.add_user("build project")
    for index in range(30):
        session.history.add_assistant(f"old assistant {index} " + ("x" * 200))
        session.history.add_tool_result(
            f"call_{index}",
            "command: pytest\nexit_code: 1\nERROR: old failure\n" + ("y" * 300),
            name="run_command",
        )

    result = session.run_turn("continue", allow_advice=False)

    assert result.status == "completed"
    assert fake.call_count == 2
    checkpoints = [
        message
        for message in session.history.messages()
        if message.metadata.get("kind") == "compaction_checkpoint"
    ]
    assert len(checkpoints) == 1
    assert checkpoints[0].metadata["replacement_history"]
    assert session.history.active_messages()[0].content.startswith("[CONTEXT CHECKPOINT HANDOFF]")


def test_write_triggers_verification_and_repair(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    fake = FakeModelAdapter(
        [
            FakeStep(
                "write bad implementation",
                [
                    ToolCall(
                        "call_1",
                        "write_file",
                        {"path": "calc.py", "content": "def add(a, b):\n    return a - b\n"},
                    )
                ],
            ),
            FakeStep(
                "repair implementation",
                [
                    ToolCall(
                        "call_2",
                        "edit_file",
                        {
                            "path": "calc.py",
                            "old_text": "return a - b",
                            "new_text": "return a + b",
                        },
                    )
                ],
            ),
            FakeStep("fixed"),
        ]
    )
    session = make_session(tmp_path, fake)
    result = session.run_turn("implement add", allow_advice=False)
    assert result.status == "passed"
    assert "calc.py" in result.changed_files
    assert fake.call_count == 2
    event_types = [event.type for event in session.event_bus.events()]
    assert "file_changed" in event_types
    assert "verification_step_started" in event_types
    assert "verification_step_finished" in event_types
    assert "failure_pack_created" in event_types
    assert "repair_round_started" in event_types
    assert "verification_passed" in event_types
    assert all(len(str(event.payload)) < 5000 for event in session.event_bus.events())


def test_missing_declared_python_dependency_installs_once_and_retries(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import ok\n\n"
        "def test_ok():\n"
        "    assert ok() is True\n",
        encoding="utf-8",
    )
    fake = FakeModelAdapter(
        [
            FakeStep(
                "write app with declared dependency",
                [
                    ToolCall(
                        "call_1",
                        "write_file",
                        {
                            "path": "requirements.txt",
                            "content": "demo_framework\n",
                        },
                    ),
                    ToolCall(
                        "call_2",
                        "write_file",
                        {
                            "path": "app.py",
                            "content": "import demo_framework\n\n"
                            "def ok():\n"
                            "    return demo_framework.ok()\n",
                        },
                    ),
                ],
            ),
        ]
    )
    session = make_session(tmp_path, fake)

    def fake_install(command: str, cwd: Path, timeout_seconds: int = 30) -> CommandResult:
        assert command == "python -m pip install -r requirements.txt"
        (cwd / "demo_framework.py").write_text("def ok():\n    return True\n", encoding="utf-8")
        return CommandResult(command=command, cwd=str(cwd), exit_code=0, stdout="installed", stderr="")

    monkeypatch.setattr(session.dependency_command_runner, "run", fake_install)

    result = session.run_turn("create app", allow_advice=False)

    assert result.status == "passed"
    event_types = [event.type for event in session.event_bus.events()]
    assert "dependency_install_started" in event_types
    assert "dependency_install_finished" in event_types
    assert event_types.count("dependency_install_started") == 1
    assert event_types.count("verification_started") == 2
    assert fake.call_count == 1


def test_write_batch_verifies_before_model_can_continue_reading(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "def test_smoke():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    fake = FakeModelAdapter(
        [
            FakeStep(
                "write implementation",
                [
                    ToolCall(
                        "call_1",
                        "write_file",
                        {"path": "app.py", "content": "print('ok')\n"},
                    )
                ],
            ),
            FakeStep(
                "would keep reading without runtime guard",
                [ToolCall("call_2", "read_file", {"path": "app.py"})],
            ),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("create app", allow_advice=False)

    assert result.status == "passed"
    assert fake.call_count == 1
    event_types = [event.type for event in session.event_bus.events()]
    assert "verification_started" in event_types
    retry_payloads = [
        event.payload
        for event in session.event_bus.events()
        if event.type == "required_tool_retry"
    ]
    assert {"expected_action": "verify_after_write"} in retry_payloads


def test_script_static_failure_triggers_repair(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "write interactive helper script",
                [
                    ToolCall(
                        "call_1",
                        "write_file",
                        {
                            "path": "start_server.bat",
                            "content": "@echo off\nflask run\npause\n",
                        },
                    )
                ],
            ),
            FakeStep(
                "remove interactive pause",
                [
                    ToolCall(
                        "call_2",
                        "edit_file",
                        {
                            "path": "start_server.bat",
                            "old_text": "\npause\n",
                            "new_text": "\n",
                        },
                    )
                ],
            ),
            FakeStep("fixed"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("create a server startup script", allow_advice=False)

    assert result.status == "passed"
    assert "pause" not in (tmp_path / "start_server.bat").read_text(encoding="utf-8").lower()
    event_types = [event.type for event in session.event_bus.events()]
    assert "failure_pack_created" in event_types
    assert "repair_round_started" in event_types
    assert "verification_passed" in event_types
    created_plan = [
        event
        for event in session.event_bus.events()
        if event.type == "verification_plan_created"
    ][0]
    assert created_plan.payload["reason"] == "changed script files need static verification"


def test_documentation_only_write_passes_static_verification(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "write design doc",
                [
                    ToolCall(
                        "call_1",
                        "write_file",
                        {"path": "studentPet/design.md", "content": "# studentPet\n\nDesign notes.\n"},
                    )
                ],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("save the design document", allow_advice=False)

    assert result.status == "passed"
    event_types = [event.type for event in session.event_bus.events()]
    assert "verification_passed" in event_types
    created_plan = [
        event
        for event in session.event_bus.events()
        if event.type == "verification_plan_created"
    ][0]
    assert created_plan.payload["reason"] == "documentation-only changes"


def test_goal_tool_request_retries_when_model_only_answers(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "create goal",
                [ToolCall("call_goal", "create_goal", {"objective": "build and verify the demo project"})],
            ),
            FakeStep("I will start."),
            FakeStep(
                "write implementation",
                [
                    ToolCall(
                        "call_1",
                        "write_file",
                        {"path": "app.py", "content": "print('hello')\n"},
                    )
                ],
            ),
            FakeStep("done"),
            FakeStep(
                "mark complete",
                [ToolCall("call_goal_done", "update_goal", {"status": "complete"})],
            ),
            FakeStep("goal complete"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("build this project in order and notify me after tests pass", allow_advice=False)

    assert result.status == "completed"
    assert fake.call_count == 6
    assert (tmp_path / "app.py").exists()
    assert "app.py" in result.changed_files
    assert session.state.goal_status == "complete"
    event_payloads = [
        event.payload
        for event in session.event_bus.events()
        if event.type == "required_tool_retry"
    ]
    assert {"expected_action": "continue_project_build"} in event_payloads


def test_goal_tool_continues_after_document_only_write(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "create goal",
                [ToolCall("call_goal", "create_goal", {"objective": "build the studentPet project"})],
            ),
            FakeStep(
                "write readme",
                [
                    ToolCall(
                        "call_1",
                        "write_file",
                        {"path": "studentPet/README.md", "content": "# studentPet\n"},
                    )
                ],
            ),
            FakeStep("readme done"),
            FakeStep(
                "write runnable app",
                [
                    ToolCall(
                        "call_2",
                        "write_file",
                        {
                            "path": "studentPet/pyproject.toml",
                            "content": "[project]\nname = \"student-pet\"\n",
                        },
                    ),
                    ToolCall(
                        "call_3",
                        "write_file",
                        {"path": "studentPet/app.py", "content": "print('studentPet')\n"},
                    )
                ],
            ),
            FakeStep("implementation done"),
            FakeStep(
                "mark complete",
                [ToolCall("call_goal_done", "update_goal", {"status": "complete"})],
            ),
            FakeStep("goal complete"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("那开始吧", allow_advice=False)

    assert result.status == "completed"
    assert fake.call_count == 7
    assert session.state.goal_status == "complete"
    assert set(result.changed_files) == {
        "studentPet/README.md",
        "studentPet/pyproject.toml",
        "studentPet/app.py",
    }
    assert (tmp_path / "studentPet" / "app.py").exists()
    event_payloads = [
        event.payload
        for event in session.event_bus.events()
        if event.type == "required_tool_retry"
    ]
    assert {"expected_action": "continue_project_build_after_docs"} in event_payloads
    assert {"expected_action": "continue_project_build_after_verified_step"} in event_payloads
    assert any(
        payload.get("expected_action")
        in {"reconcile_active_goal_after_verified_progress", "review_active_goal"}
        for payload in event_payloads
    )


def test_active_goal_can_continue_past_four_verified_write_batches(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "create goal",
                [ToolCall("call_goal", "create_goal", {"objective": "build several project modules"})],
            ),
            *[
                FakeStep(
                    f"write module {index}",
                    [
                        ToolCall(
                            f"call_{index}",
                            "write_file",
                            {
                                "path": f"module_{index}.py",
                                "content": f"VALUE = {index}\n",
                            },
                        )
                    ],
                )
                for index in range(1, 6)
            ],
            FakeStep(
                "mark complete",
                [ToolCall("call_goal_done", "update_goal", {"status": "complete"})],
            ),
            FakeStep("goal complete"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("build several modules", allow_advice=False)

    assert result.status == "completed"
    assert session.state.goal_status == "complete"
    assert (tmp_path / "module_5.py").exists()
    assert fake.call_count == 8
    retry_payloads = [
        event.payload
        for event in session.event_bus.events()
        if event.type == "required_tool_retry"
    ]
    verified_step_retries = [
        payload
        for payload in retry_payloads
        if payload.get("expected_action") == "continue_project_build_after_verified_step"
    ]
    assert len(verified_step_retries) >= 5


def test_active_goal_reviews_work_plan_when_model_stops_after_planning(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "create goal and plan",
                [
                    ToolCall("call_goal", "create_goal", {"objective": "build the planned demo"}),
                    ToolCall(
                        "call_plan",
                        "set_work_plan",
                        {
                            "steps": [
                                {
                                    "id": "app",
                                    "title": "Create runnable app",
                                    "acceptance": "app.py exists and static verification passes",
                                }
                            ]
                        },
                    ),
                ],
            ),
            FakeStep("plan is ready"),
            FakeStep("current plan is done"),
            FakeStep(
                "write app",
                [ToolCall("call_write", "write_file", {"path": "app.py", "content": "print('demo')\n"})],
            ),
            FakeStep(
                "mark step done",
                [
                    ToolCall(
                        "call_step_done",
                        "update_work_step",
                        {
                            "step_id": "app",
                            "status": "done",
                            "evidence": "verification passed: changed script files need static verification",
                        },
                    )
                ],
            ),
            FakeStep(
                "mark goal complete",
                [ToolCall("call_goal_done", "update_goal", {"status": "complete"})],
            ),
            FakeStep("goal complete"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("build the planned demo", allow_advice=False)

    assert result.status == "completed"
    assert session.state.goal_status == "complete"
    assert (tmp_path / "app.py").exists()
    retry_payloads = [
        event.payload
        for event in session.event_bus.events()
        if event.type == "required_tool_retry"
    ]
    assert any(payload.get("expected_action") == "continue_project_build" for payload in retry_payloads)
    assert any(payload.get("expected_action") == "continue_open_work_plan" for payload in retry_payloads)


def test_runtime_does_not_create_goal_from_natural_language_keywords(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("I can help.")])
    session = make_session(tmp_path, fake)

    result = session.run_turn("build this project in order and notify me after tests pass", allow_advice=False)

    assert result.status == "completed"
    assert fake.call_count == 1
    assert session.state.active_goal is None
    assert session.state.goal_status == "none"
    assert not any(event.type.startswith("goal_") for event in session.event_bus.events())


def test_active_goal_does_not_force_plain_chat_into_project_loop(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                '{"turn_scope":"chat_only","goal_engagement":"background_only","reason":"plain greeting"}'
            ),
            FakeStep("hello, how can I help?"),
        ]
    )
    session = make_session(tmp_path, fake)
    session.state.active_goal = "finish the demo project"
    session.state.goal_status = "active"

    result = session.run_turn("你好", allow_advice=False)

    assert result.status == "completed"
    assert result.response == "hello, how can I help?"
    assert fake.call_count == 2
    assert session.state.goal_status == "active"
    assert any(
        event.type == "turn_scope_classified"
        and event.payload.get("goal_engagement") == "background_only"
        for event in session.event_bus.events()
    )
    assert not any(
        event.type == "required_tool_retry"
        and event.payload.get("expected_action") == "continue_project_build"
        for event in session.event_bus.events()
    )


def test_active_goal_allows_project_question_without_continuing_goal(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# Guidance\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                '{"turn_scope":"answer_with_tools","goal_engagement":"background_only",'
                '"reason":"user asks whether a file exists"}'
            ),
            FakeStep(
                "check files",
                [ToolCall("call_ls", "list_directory", {"path": "."})],
            ),
            FakeStep("Yes, AGENTS.md exists."),
        ]
    )
    session = make_session(tmp_path, fake)
    session.state.active_goal = "finish the demo project"
    session.state.goal_status = "active"

    result = session.run_turn("Does this project have an AGENTS.md file?", allow_advice=False)

    assert result.status == "completed"
    assert fake.call_count == 3
    assert "AGENTS.md" in result.response
    assert not any(
        event.type == "required_tool_retry"
        and str(event.payload.get("expected_action", "")).startswith("continue_project_build")
        for event in session.event_bus.events()
    )


def test_active_goal_status_review_uses_tools_but_does_not_resume_implementation(tmp_path: Path) -> None:
    (tmp_path / "studentPet_design_document.md").write_text("# Design\n\n- Profile page\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                '{"turn_scope":"status_review","goal_engagement":"background_only",'
                '"reason":"user asks for project progress against the design document"}'
            ),
            FakeStep(
                "inspect progress",
                [
                    ToolCall("call_plan", "get_work_plan", {}),
                    ToolCall("call_doc", "read_file", {"path": "studentPet_design_document.md"}),
                ],
            ),
            FakeStep("Current progress: plan exists, design doc was reviewed, and implementation should be summarized before any further build work."),
        ]
    )
    session = make_session(tmp_path, fake)
    session.state.active_goal = "finish the demo project"
    session.state.goal_status = "active"
    assert session.goal_controller.set_work_plan(
        [{"id": "profile", "title": "Finish profile page", "status": "in_progress"}]
    ).ok

    result = session.run_turn("对照设计文档说下现在项目进度如何", allow_advice=False)

    assert result.status == "completed"
    assert "Current progress:" in result.response
    assert any(
        event.type == "turn_scope_classified"
        and event.payload.get("turn_scope") == "status_review"
        and event.payload.get("goal_engagement") == "background_only"
        for event in session.event_bus.events()
    )
    assert not any(
        event.type == "required_tool_retry"
        and str(event.payload.get("expected_action", "")).startswith("continue_project_build")
        for event in session.event_bus.events()
    )


def test_status_review_may_realign_work_plan_without_resuming_implementation(tmp_path: Path) -> None:
    (tmp_path / "studentPet_design_document.md").write_text("# Design\n\n- Profile page\n- Notification page\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                '{"turn_scope":"status_review","goal_engagement":"background_only",'
                '"reason":"user asks for project progress and plan alignment"}'
            ),
            FakeStep(
                "realign plan",
                [
                    ToolCall("call_plan", "get_work_plan", {}),
                    ToolCall(
                        "call_realign",
                        "set_work_plan",
                        {
                            "steps": [
                                {"id": "profile", "title": "Finish profile page", "status": "done"},
                                {"id": "notify", "title": "Finish notification page", "status": "pending"},
                            ]
                        },
                    ),
                ],
            ),
            FakeStep("Current progress: profile appears done, notification remains pending, and the stored plan was realigned to match the observed implementation."),
        ]
    )
    session = make_session(tmp_path, fake)
    session.state.active_goal = "finish the demo project"
    session.state.goal_status = "active"
    assert session.goal_controller.set_work_plan(
        [{"id": "profile", "title": "Finish profile page", "status": "pending"}]
    ).ok

    result = session.run_turn("compare design doc with current code, report progress, and sync the plan", allow_advice=False)

    assert result.status == "completed"
    assert "Current progress:" in result.response
    assert [item.id for item in session.state.work_plan] == ["profile", "notify"]
    assert session.state.find_work_step("profile").status == "done"  # type: ignore[union-attr]
    assert not any(
        event.type == "required_tool_retry"
        and str(event.payload.get("expected_action", "")).startswith("continue_project_build")
        for event in session.event_bus.events()
    )


def test_active_goal_continuation_scope_allows_project_retry(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                '{"turn_scope":"continue_current_goal","goal_engagement":"engaged",'
                '"reason":"user explicitly asks to continue"}'
            ),
            FakeStep("I will continue."),
            FakeStep(
                "write app",
                [ToolCall("call_write", "write_file", {"path": "app.py", "content": "print('demo')\n"})],
            ),
            FakeStep(
                "mark complete",
                [ToolCall("call_goal_done", "update_goal", {"status": "complete"})],
            ),
            FakeStep("goal complete"),
        ]
    )
    session = make_session(tmp_path, fake)
    session.state.active_goal = "finish the demo project"
    session.state.goal_status = "active"

    result = session.run_turn("Continue the current project goal.", allow_advice=False)

    assert result.status == "completed"
    assert fake.call_count == 5
    assert session.state.goal_status == "complete"
    assert (tmp_path / "app.py").exists()
    assert any(
        event.type == "required_tool_retry"
        and event.payload.get("expected_action") == "continue_project_build"
        for event in session.event_bus.events()
    )


def test_active_goal_warns_but_does_not_block_after_repeated_stagnant_inspection_batches(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "create goal",
                [ToolCall("call_goal", "create_goal", {"objective": "finish the demo project"})],
            ),
            FakeStep("inspect 1", [ToolCall("call_1", "read_file", {"path": "README.md"})]),
            FakeStep("inspect 2", [ToolCall("call_2", "read_file", {"path": "README.md"})]),
            FakeStep("inspect 3", [ToolCall("call_3", "read_file", {"path": "README.md"})]),
            FakeStep("inspect 4", [ToolCall("call_4", "read_file", {"path": "README.md"})]),
            FakeStep("inspect 5", [ToolCall("call_5", "read_file", {"path": "README.md"})]),
            FakeStep("complete", [ToolCall("call_done", "update_goal", {"status": "complete"})]),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("finish the project", allow_advice=False)

    assert result.status == "blocked"
    assert "missing acceptance evidence" in result.response
    assert "inspection-only loop exceeded" not in result.response
    assert fake.call_count == 7
    events = session.event_bus.events()
    guidance_payloads = [
        event.payload
        for event in events
        if event.type == "inspection_guidance_added"
    ]
    assert any(
        payload.get("batches") == 3
        and "read_file:README.md" in payload.get("targets", [])
        for payload in guidance_payloads
    )
    assert len(guidance_payloads) == 1
    assert not any(event.type == "reflection_requested" for event in events)
    runtime_messages = [message.content for message in session.history.messages() if message.role == "runtime"]
    assert any(
        "Inspection progress note" in content
        and "Use this as a soft checkpoint, not a blocker" in content
        for content in runtime_messages
    )


def test_active_goal_allows_extended_inspection_when_targets_keep_expanding(tmp_path: Path) -> None:
    for index in range(1, 6):
        (tmp_path / f"file{index}.md").write_text(f"# file {index}\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "create goal",
                [ToolCall("call_goal", "create_goal", {"objective": "understand the project"})],
            ),
            FakeStep("inspect 1", [ToolCall("call_1", "read_file", {"path": "file1.md"})]),
            FakeStep("inspect 2", [ToolCall("call_2", "read_file", {"path": "file2.md"})]),
            FakeStep("inspect 3", [ToolCall("call_3", "read_file", {"path": "file3.md"})]),
            FakeStep("inspect 4", [ToolCall("call_4", "read_file", {"path": "file4.md"})]),
            FakeStep("inspect 5", [ToolCall("call_5", "read_file", {"path": "file5.md"})]),
            FakeStep("complete", [ToolCall("call_done", "update_goal", {"status": "complete"})]),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("understand the project", allow_advice=False)

    assert result.status == "blocked"
    assert "missing acceptance evidence" in result.response
    assert not any(event.type == "inspection_guidance_added" for event in session.event_bus.events())


def test_non_inspection_failure_resets_active_goal_inspection_budget(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "create goal",
                [ToolCall("call_goal", "create_goal", {"objective": "finish the demo project"})],
            ),
            FakeStep("inspect 1", [ToolCall("call_1", "read_file", {"path": "README.md"})]),
            FakeStep("inspect 2", [ToolCall("call_2", "list_directory", {"path": "."})]),
            FakeStep("inspect 3", [ToolCall("call_3", "read_file", {"path": "README.md"})]),
            FakeStep("inspect 4", [ToolCall("call_4", "read_file", {"path": "README.md"})]),
            FakeStep(
                "verify endpoint",
                [
                    ToolCall(
                        "call_http",
                        "http_request",
                        {
                            "method": "GET",
                            "url": "http://127.0.0.1:9999/health",
                            "purpose": "verify",
                        },
                    )
                ],
            ),
            FakeStep("diagnose after failure", [ToolCall("call_5", "read_file", {"path": "README.md"})]),
            FakeStep(
                "verify recovered path",
                [
                    ToolCall(
                        "call_verify",
                        "run_command",
                        {"command": "python -c \"print('ok')\"", "purpose": "verify"},
                    )
                ],
            ),
            FakeStep("complete goal", [ToolCall("call_done", "update_goal", {"status": "complete"})]),
        ]
    )
    session = make_session_with_http_stub(
        tmp_path,
        fake,
        responses=[(False, None, "connection refused")],
    )

    result = session.run_turn("finish the project", allow_advice=False)

    assert result.status == "completed"
    assert "inspection-only loop exceeded" not in result.response
    event_types = [event.type for event in session.event_bus.events()]
    assert "tool_failure_recovery_started" in event_types
    assert event_types.count("blocked") == 0


def test_background_command_records_runtime_resource_not_acceptance(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "start service",
                [
                    ToolCall(
                        "call_1",
                        "start_background_command",
                        {"command": "python server.py", "cwd": "app", "port": 8123},
                    )
                ],
            ),
            FakeStep("service is running"),
        ]
    )
    session = make_session(tmp_path, fake)

    def start_background_command(command: str, cwd: str = ".", port: int | None = None, restart_port: bool = False):
        return ToolResult(
            ok=True,
            content="ready",
            metadata={
                "command": command,
                "cwd": cwd,
                "port": port,
                "pid": 12345,
                "ready": True,
                "log_path": "logs/bg.log",
            },
        )

    session.tools.register(
        RegisteredTool(
            "start_background_command",
            "stub background service",
            {"type": "object", "properties": {}, "required": []},
            start_background_command,
        )
    )

    result = session.run_turn("start service", allow_advice=False)

    assert result.status == "completed"
    assert len(session.state.runtime_resources) == 1
    resource = session.state.runtime_resources[0]
    assert resource.command == "python server.py"
    assert resource.port == 8123
    assert resource.ready is True
    assert session.state.acceptance_evidence == []


def test_read_background_log_reads_latest_resource_without_guessing_path(tmp_path: Path) -> None:
    fake = FakeModelAdapter([])
    session = make_session(tmp_path, fake)
    log_path = tmp_path / ".minicodex2" / "logs" / "background" / "bg_2.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("line one\nline two\nline three\n", encoding="utf-8")
    resource = session.state.add_runtime_resource(
        kind="background_process",
        source_tool="start_background_command",
        command="python server.py",
        cwd="app",
        port=8123,
        pid=12345,
        ready=True,
        log_path=str(log_path),
    )

    latest = session.tools.execute("read_background_log", {"max_lines": 2})
    by_id = session.tools.execute("read_background_log", {"resource_id": resource.id})
    by_port = session.tools.execute("read_background_log", {"port": 8123})

    assert latest.ok
    assert latest.content == "line two\nline three"
    assert latest.metadata["resource_id"] == resource.id
    assert latest.metadata["log_path"] == ".minicodex2/logs/background/bg_2.log"
    assert by_id.ok
    assert by_port.ok


def test_read_background_log_blocks_outside_workspace_log_path(tmp_path: Path) -> None:
    fake = FakeModelAdapter([])
    session = make_session(tmp_path, fake)

    result = session.tools.execute(
        "read_background_log",
        {"log_path": str(tmp_path.parent / "outside.log")},
    )

    assert result.ok is False
    assert result.blocked is True
    assert result.metadata["failure_kind"] == "background_log_outside_workspace"


def test_inspect_project_startup_discovers_entrypoints_and_proxy(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    backend = tmp_path / "backend"
    frontend.mkdir()
    backend.mkdir()
    (frontend / "package.json").write_text(
        json.dumps(
            {
                "proxy": "http://127.0.0.1:8000",
                "scripts": {
                    "start": "react-scripts start",
                    "build": "react-scripts build",
                },
            }
        ),
        encoding="utf-8",
    )
    (backend / "manage.py").write_text("# django entrypoint\n", encoding="utf-8")
    fake = FakeModelAdapter([])
    session = make_session(tmp_path, fake)
    session.state.add_runtime_resource(
        kind="background_process",
        source_tool="start_background_command",
        command="python manage.py runserver 127.0.0.1:1",
        cwd="backend",
        port=1,
        ready=True,
    )

    result = session.tools.execute("inspect_project_startup", {"root": "."})

    assert result.ok
    report = result.metadata
    commands = {candidate["command"] for candidate in report["candidates"]}
    assert "npm start" in commands
    assert "npm run build" in commands
    assert "python manage.py runserver 127.0.0.1:8000" in commands
    assert report["config_refs"][0]["port"] == 8000
    assert any(warning["kind"] == "possible_port_mismatch" for warning in report["warnings"])
    assert report["runtime_resources"][0]["observed_port_open"] is False
    assert report["runtime_resources"][0]["observed_status"] == "stale"


def test_inspect_project_startup_facts_are_structured_in_next_prompt(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    backend = tmp_path / "backend"
    frontend.mkdir()
    backend.mkdir()
    (frontend / "package.json").write_text(
        json.dumps(
            {
                "proxy": "http://127.0.0.1:8000",
                "scripts": {"start": "react-scripts start"},
            }
        ),
        encoding="utf-8",
    )
    (backend / "manage.py").write_text("# django entrypoint\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "inspect startup",
                [ToolCall("call_startup", "inspect_project_startup", {"root": "."})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("inspect startup facts", allow_advice=False)

    assert result.status == "completed"
    second_request_text = "\n".join(message.content for message in fake.requests[1].messages)
    assert "tool=inspect_project_startup" in second_request_text
    assert "command=npm start" in second_request_text
    assert "command=python manage.py runserver 127.0.0.1:8000" in second_request_text
    assert "target=http://127.0.0.1:8000" in second_request_text
    assert "long_running=True" in second_request_text


def test_runtime_summary_marks_stale_background_resources(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("ok")])
    session = make_session(tmp_path, fake)
    session.state.add_runtime_resource(
        kind="background_process",
        source_tool="start_background_command",
        command="python manage.py runserver 127.0.0.1:1",
        cwd="backend",
        port=1,
        ready=True,
    )

    summary = session._runtime_summary()

    resource = summary["runtime_resources"][0]
    assert resource["observed_port_open"] is False
    assert resource["observed_status"] == "stale"
    assert "restart the service" in resource["warning"]


def test_startup_facts_retry_after_inspection_only_batches(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (backend / "manage.py").write_text("# django entrypoint\n", encoding="utf-8")
    (frontend / "package.json").write_text(
        json.dumps({"proxy": "http://127.0.0.1:8000", "scripts": {"start": "react-scripts start"}}),
        encoding="utf-8",
    )
    fake = FakeModelAdapter(
        [
            FakeStep(
                "create goal",
                [ToolCall("call_goal", "create_goal", {"objective": "start and verify local service"})],
            ),
            FakeStep("inspect startup", [ToolCall("call_startup", "inspect_project_startup", {"root": "."})]),
            FakeStep("inspect again", [ToolCall("call_read", "read_file", {"path": "backend/manage.py"})]),
            FakeStep("start service", [ToolCall("call_start", "start_background_command", {})]),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)
    session.state.add_runtime_resource(
        kind="background_process",
        source_tool="start_background_command",
        command="python manage.py runserver 127.0.0.1:1",
        cwd="backend",
        port=1,
        ready=True,
    )

    def start_background_command() -> ToolResult:
        return ToolResult(
            ok=True,
            content=json.dumps({"pid": 1234, "port": 8000, "ready": True}),
            metadata={"pid": 1234, "port": 8000, "ready": True},
        )

    session.tools.register(
        RegisteredTool(
            "start_background_command",
            "stub start background command",
            {"type": "object", "properties": {}, "required": []},
            start_background_command,
        )
    )

    result = session.run_turn("continue the project until the service is verified", allow_advice=False)

    assert result.status == "completed"
    events = session.event_bus.events()
    retries = [
        event
        for event in events
        if event.type == "required_tool_retry"
        and event.payload.get("expected_action") == "act_on_startup_facts"
    ]
    assert retries
    runtime_messages = [message.content for message in session.history.messages() if message.role == "runtime"]
    assert any("Startup facts require a concrete action" in content for content in runtime_messages)
    assert any("observed_status=stale" in content for content in runtime_messages)
    startup_message = next(
        content for content in runtime_messages if "Startup facts require a concrete action" in content
    )
    assert "port=8000" in startup_message
    assert "historical facts, not preferred startup instructions" in startup_message
    assert startup_message.index("Configuration references") < startup_message.index("Observed stale")
    facts = [fact.to_dict() for fact in session.state.engineering_facts]
    assert any(fact["type"] == "config_ref" and fact["data"]["port"] == 8000 for fact in facts)
    assert any(fact["type"] == "runtime_resource" and fact["stale"] is True for fact in facts)


def test_missing_path_failure_is_promoted_to_runtime_facts(tmp_path: Path) -> None:
    pages = tmp_path / "frontend" / "src" / "pages"
    pages.mkdir(parents=True)
    (pages / "LoginPage.js").write_text("export default function LoginPage() {}\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep("read missing", [ToolCall("call_1", "read_file", {"path": "frontend/src/pages/Login.js"})]),
            FakeStep("read sibling", [ToolCall("call_2", "read_file", {"path": "frontend/src/pages/LoginPage.js"})]),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("inspect login page", allow_advice=False)

    assert result.status == "completed"
    runtime_messages = [message.content for message in session.history.messages() if message.role == "runtime"]
    path_message = next(content for content in runtime_messages if "Path lookup failed facts" in content)
    assert "missing_requested_path: frontend/src/pages/Login.js" in path_message
    assert "LoginPage.js (file)" in path_message
    assert "do not read the same missing path again" in path_message
    assert any(
        fact.type == "missing_path"
        and fact.data["requested_path"] == "frontend/src/pages/Login.js"
        and "LoginPage.js" in fact.data["siblings"]
        for fact in session.state.engineering_facts
    )
    events = session.event_bus.events()
    assert any(event.type == "path_failure_facts" for event in events)


def test_model_action_plan_and_progress_events_are_emitted(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "I will inspect the README first.",
                [ToolCall("call_1", "read_file", {"path": "README.md"})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("read the project", allow_advice=False)

    assert result.status == "completed"
    events = session.event_bus.events()
    progress = [event for event in events if event.type == "assistant_progress"]
    plans = [event for event in events if event.type == "model_action_plan"]
    assert progress
    assert progress[0].payload["content"] == "I will inspect the README first."
    assert plans
    assert plans[0].payload["actions"][0]["name"] == "read_file"
    assert plans[0].payload["actions"][0]["summary"] == "README.md"


def test_progress_visibility_uses_assistant_note_not_report_tool(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("ok")])
    session = make_session(tmp_path, fake)

    session.run_turn("debug startup", allow_advice=False)

    runtime_message = fake.requests[0].messages[0].content
    tool_names = {
        raw_schema["function"]["name"]
        for raw_schema in fake.requests[0].tools
        if raw_schema.get("type") == "function"
    }
    assert "Before a non-trivial tool batch" in runtime_message
    assert "include one short public assistant note" in runtime_message
    assert "report_progress" not in tool_names


def test_runtime_protocol_encourages_batched_integration_tools(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("ok")])
    session = make_session(tmp_path, fake)

    session.run_turn("verify the frontend/backend integration", allow_advice=False)

    runtime_message = fake.requests[0].messages[0].content
    assert "batch independent read/list/inspect/check/smoke tool calls" in runtime_message
    assert "Avoid one-tool-per-model-turn pacing" in runtime_message
    assert "start_background_command(restart_port=true)" in runtime_message
    assert "exit_code/log_tail/command_normalization" in runtime_message


def test_current_step_evidence_requires_work_step_reconciliation(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "create plan",
                [
                    ToolCall("call_goal", "create_goal", {"objective": "finish the demo"}),
                    ToolCall(
                        "call_plan",
                        "set_work_plan",
                        {
                            "steps": [
                                {
                                    "id": "check",
                                    "title": "Run project check",
                                    "status": "in_progress",
                                    "acceptance": "check command passes",
                                }
                            ]
                        },
                    ),
                ],
            ),
            FakeStep(
                "run check",
                [
                    ToolCall(
                        "call_1",
                        "run_command",
                        {
                            "command": "python -c \"print('ok')\"",
                            "purpose": "check",
                        },
                    )
                ],
            ),
            FakeStep(
                "mark step done",
                [
                    ToolCall(
                        "call_step",
                        "update_work_step",
                        {
                            "step_id": "check",
                            "status": "done",
                            "evidence_ids": ["ev-1"],
                        },
                    )
                ],
            ),
            FakeStep(
                "mark goal done",
                [ToolCall("call_goal_done", "update_goal", {"status": "complete"})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    result = session.run_turn("finish the demo", allow_advice=False)

    assert result.status == "completed"
    assert session.state.work_plan[0].status == "done"
    assert session.state.work_plan[0].evidence_ids == ["ev-1"]
    retry_payloads = [
        event.payload
        for event in session.event_bus.events()
        if event.type == "required_tool_retry"
    ]
    assert {"expected_action": "reconcile_work_step_evidence"} in retry_payloads
    assert any(
        "latest user message narrows, redirects, or corrects the active focus" in message.content
        for request in fake.requests
        for message in request.messages
    )


def test_error_report_forces_local_diagnosis_before_asking_user(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"build":"echo ok"}}\n', encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep("Please provide a browser screenshot and Network panel error."),
            FakeStep(
                "checking project",
                [ToolCall("call_1", "read_file", {"path": "package.json"})],
            ),
            FakeStep(
                "checking original entrypoint",
                [
                    ToolCall(
                        "call_2",
                        "http_request",
                        {
                            "url": "http://localhost:3001/api/auth/login/",
                            "method": "POST",
                            "purpose": "smoke",
                        },
                    )
                ],
            ),
            FakeStep("I inspected the local project and rechecked the reported entrypoint."),
        ]
    )
    session = make_session_with_http_stub(tmp_path, fake)

    result = session.run_turn(
        "POST http://localhost:3001/api/auth/login/ 404 (Not Found)",
        allow_advice=False,
    )

    assert result.status == "completed"
    assert fake.call_count == 4
    assert any(message.role == "tool" and "build" in message.content for message in session.history.messages())
    retries = [event for event in session.event_bus.events() if event.type == "required_tool_retry"]
    assert any(event.payload["expected_action"] == "diagnose_before_answer" for event in retries)
    assert any(event.payload["expected_action"] == "verify_reported_failure_target" for event in retries)


def test_reported_failure_target_must_be_reverified_before_completion(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "backend ok",
                [
                    ToolCall(
                        "call_1",
                        "run_command",
                        {
                            "command": "python -c \"print('http://localhost:8000/api/auth/register/')\"",
                            "purpose": "verify",
                        },
                    )
                ],
            ),
            FakeStep("The backend endpoint works."),
            FakeStep(
                "checking original entrypoint",
                [
                    ToolCall(
                        "call_2",
                        "http_request",
                        {
                            "url": "http://localhost:3000/api/auth/register/",
                            "method": "POST",
                            "purpose": "smoke",
                        },
                    )
                ],
            ),
            FakeStep("The original failure entrypoint works now."),
        ]
    )
    session = make_session_with_http_stub(tmp_path, fake)

    result = session.run_turn(
        "POST http://localhost:3000/api/auth/register/ 500",
        allow_advice=False,
    )

    assert result.status == "completed"
    assert fake.call_count == 4
    assert session.state.failure_targets[0].status == "verified"
    retries = [event for event in session.event_bus.events() if event.type == "required_tool_retry"]
    assert any(event.payload["expected_action"] == "verify_reported_failure_target" for event in retries)


def test_failing_reported_http_target_continues_to_repair(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "check original entrypoint",
                [
                    ToolCall(
                        "call_1",
                        "http_request",
                        {
                            "url": "http://localhost:3000/api/auth/register/",
                            "method": "POST",
                            "purpose": "smoke",
                        },
                    )
                ],
            ),
            FakeStep(
                "read proxy config",
                [ToolCall("call_2", "read_file", {"path": "package.json"})],
            ),
            FakeStep(
                "fix proxy config",
                [
                    ToolCall(
                        "call_3",
                        "write_file",
                        {
                            "path": "package.json",
                            "content": '{"proxy":"http://127.0.0.1:8000"}\n',
                        },
                    )
                ],
            ),
            FakeStep(
                "recheck original entrypoint",
                [
                    ToolCall(
                        "call_4",
                        "http_request",
                        {
                            "url": "http://localhost:3000/api/auth/register/",
                            "method": "POST",
                            "purpose": "smoke",
                        },
                    )
                ],
            ),
            FakeStep("fixed and verified"),
        ]
    )
    (tmp_path / "package.json").write_text('{"proxy":"http://localhost:8000"}\n', encoding="utf-8")
    session = make_session_with_http_stub(
        tmp_path,
        fake,
        responses=[(False, None, "connection refused"), (True, 200, None)],
    )

    result = session.run_turn(
        "POST http://localhost:3000/api/auth/register/ 500",
        allow_advice=False,
    )

    assert result.status == "passed"
    assert fake.call_count == 5
    assert session.state.failure_targets[0].status == "verified"
    assert "127.0.0.1" in (tmp_path / "package.json").read_text(encoding="utf-8")
    event_types = [event.type for event in session.event_bus.events()]
    assert "tool_failure_recovery_started" in event_types


def test_runtime_auto_rechecks_reported_http_target_when_model_avoids_it(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "checking backend instead",
                [
                    ToolCall(
                        "call_1",
                        "http_request",
                        {
                            "url": "http://localhost:8000/api/auth/register/",
                            "method": "POST",
                            "purpose": "smoke",
                        },
                    )
                ],
            ),
            FakeStep("backend is fine, asking user"),
            FakeStep(
                "read config after runtime reproduced target failure",
                [ToolCall("call_2", "read_file", {"path": "package.json"})],
            ),
            FakeStep(
                "fix config",
                [
                    ToolCall(
                        "call_3",
                        "write_file",
                        {
                            "path": "package.json",
                            "content": '{"proxy":"http://127.0.0.1:8000"}\n',
                        },
                    )
                ],
            ),
            FakeStep("fixed"),
        ]
    )
    (tmp_path / "package.json").write_text('{"proxy":"http://localhost:8000"}\n', encoding="utf-8")
    session = make_session_with_http_stub(
        tmp_path,
        fake,
        responses=[
            (True, 200, None),
            (False, 500, "proxy failed"),
            (True, 200, None),
        ],
    )

    result = session.run_turn(
        "POST http://localhost:3001/api/auth/register/ 500",
        allow_advice=False,
    )

    assert result.status == "passed"
    assert session.state.failure_targets[0].status == "verified"
    assert session.state.failure_targets[0].method == "POST"
    assert "127.0.0.1" in (tmp_path / "package.json").read_text(encoding="utf-8")
    event_types = [event.type for event in session.event_bus.events()]
    assert "failure_target_auto_verify_started" in event_types
    assert "tool_failure_recovery_started" in event_types


def test_large_project_request_gets_collaboration_advice(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("should not be called")])
    session = make_session(tmp_path, fake)
    result = session.run_turn(
        "Create a local code agent with CLI, API, TUI, permissions, tests and final acceptance"
    )
    assert result.status == "completed"
    assert "design-first" in result.response
    assert fake.call_count == 0


def test_build_session_can_resume_latest_history(tmp_path: Path) -> None:
    from minicodex2.cli.main import build_session, save_session

    session = make_session(tmp_path, FakeModelAdapter([FakeStep("hello")]))
    session.run_turn("hi", allow_advice=False)
    save_session(session)

    resumed = build_session(str(tmp_path), resume=True)
    assert resumed.session_id == session.session_id
    assert any(message.content == "hi" for message in resumed.history.messages())


def test_build_session_restores_active_goal_state(tmp_path: Path) -> None:
    from minicodex2.cli.main import build_session, save_session
    from minicodex2.agent_os.state import WorkPlanItem

    session = make_session(tmp_path, FakeModelAdapter([FakeStep("hello")]))
    session.state.active_goal = "build the demo project"
    session.state.goal_status = "active"
    session.state.goal_token_budget = 1234
    session.state.work_plan = [
        WorkPlanItem(
            id="auth",
            title="Finish auth",
            status="done",
            acceptance="register and login pass",
            evidence=["register 200"],
        ),
        WorkPlanItem(id="tasks", title="Finish tasks", status="in_progress"),
    ]
    session.state.current_step_id = "tasks"
    session.state.acceptance_evidence = ["register 200"]
    save_session(session)

    resumed = build_session(str(tmp_path), resume=True)

    assert resumed.session_id == session.session_id
    assert resumed.state.active_goal == "build the demo project"
    assert resumed.state.goal_status == "active"
    assert resumed.state.goal_token_budget == 1234
    assert resumed.state.current_step_id == "tasks"
    assert [item.id for item in resumed.state.work_plan] == ["auth", "tasks"]
    assert resumed.state.work_plan[0].evidence == ["register 200"]
    assert resumed.state.acceptance_evidence == ["register 200"]


def test_save_session_does_not_overwrite_goal_with_empty_transient_state(tmp_path: Path) -> None:
    from minicodex2.agent_os.state import WorkPlanItem
    from minicodex2.cli.main import build_session, save_session
    from minicodex2.history.jsonl_store import JsonlHistoryStore

    session = make_session(tmp_path, FakeModelAdapter([FakeStep("hello")]))
    session.state.active_goal = "finish the project"
    session.state.goal_status = "active"
    session.state.work_plan = [WorkPlanItem(id="step-1", title="Build feature", status="in_progress")]
    session.state.current_step_id = "step-1"
    save_session(session)

    store = JsonlHistoryStore(tmp_path)
    assert (store.sessions_dir / f"{session.session_id}.goal_snapshot.json").exists()
    store.save(
        session.session_id,
        session.history,
        {
            "workspace_root": str(tmp_path),
            "model": "fake",
            "permission_mode": "auto",
            "result_state": "completed",
            "objective": None,
            "active_goal": None,
            "goal_status": "none",
            "status": "none",
            "work_plan": [],
            "work_plan_sources": [],
            "current_step_id": None,
        },
    )

    transient = make_session(tmp_path, FakeModelAdapter([FakeStep("side task")]))
    transient.session_id = session.session_id
    transient.history.add_user("temporary login check")
    save_session(transient)

    metadata = store.load_metadata(session.session_id)

    assert metadata["objective"] == "finish the project"
    assert metadata["goal_status"] == "active"
    assert metadata["current_step_id"] == "step-1"
    assert metadata["work_plan"][0]["id"] == "step-1"


def test_session_autosave_checkpoints_user_message_before_model_returns(tmp_path: Path) -> None:
    from minicodex2.cli.main import build_session
    from minicodex2.history.jsonl_store import JsonlHistoryStore
    from minicodex2.model.adapter import ModelAdapter
    from minicodex2.model.messages import ModelRequest, ModelResponse

    class FailingModel(ModelAdapter):
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise RuntimeError("model stopped")

    session = build_session(str(tmp_path), resume=True, autosave=True)
    session.model = FailingModel()
    try:
        session.run_turn("persist me", allow_advice=False)
    except RuntimeError:
        pass
    saved = JsonlHistoryStore(tmp_path).load_history(session.session_id)
    assert any(message.role == "user" and message.content == "persist me" for message in saved.messages())


def test_cancel_after_model_response_prevents_tool_execution(tmp_path: Path) -> None:
    from minicodex2.model.adapter import ModelAdapter
    from minicodex2.model.messages import ChatMessage, ModelRequest, ModelResponse, TokenUsage

    session_holder: dict[str, UnifiedAgentSession] = {}

    class CancellingModel(ModelAdapter):
        def complete(self, request: ModelRequest) -> ModelResponse:
            session_holder["session"].cancel_current_turn(source="test")
            return ModelResponse(
                message=ChatMessage(role="assistant", content="write after cancel"),
                tool_calls=[
                    ToolCall(
                        "call_1",
                        "write_file",
                        {"path": "should_not_exist.txt", "content": "bad\n"},
                    )
                ],
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2, estimated=True),
            )

    session = make_session(tmp_path, CancellingModel())
    session_holder["session"] = session

    result = session.run_turn("write a file", allow_advice=False)

    assert result.status == "cancelled"
    assert not (tmp_path / "should_not_exist.txt").exists()
    assert not any(event.type == "tool_call_started" for event in session.event_bus.events())


def test_context_tells_model_to_recover_goal_from_continue_message(tmp_path: Path) -> None:
    fake = FakeModelAdapter([FakeStep("ok")])
    session = make_session(tmp_path, fake)

    session.run_turn("继续", allow_advice=False)

    runtime_message = fake.requests[0].messages[0].content
    assert "latest user message asks to continue/resume prior unfinished work" in runtime_message
    assert "call create_goal with that objective" in runtime_message
    assert "do not finish a turn by promising future work" in runtime_message
    assert "continue with concrete tool calls" in runtime_message
    assert "batch related write_file/edit_file calls" in runtime_message


def test_write_subproject_infers_verification_root(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "write flask app",
                [
                    ToolCall(
                        "call_1",
                        "write_file",
                        {
                            "path": "demo1/app.py",
                            "content": "from flask import Flask\napp = Flask(__name__)\n",
                        },
                    )
                ],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)
    session.run_turn("create flask app in demo1", allow_advice=False)
    created_plan = [
        event
        for event in session.event_bus.events()
        if event.type == "verification_plan_created"
    ][-1]
    assert created_plan.payload["reason"] == "Flask app signal"
    command = created_plan.payload["steps"][0]["command"]
    assert "python -m flask --app app run" in command
    assert created_plan.payload["steps"][0]["type"] == "background_server"

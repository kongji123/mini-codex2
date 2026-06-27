from __future__ import annotations

import os
from pathlib import Path

from minicodex2.agent.chat_history import ChatHistory
from minicodex2.agent.events import AgentEventBus
from minicodex2.agent.unified_session import UnifiedAgentSession
from minicodex2.config.settings import ConfigLoader
from minicodex2.model.fake_adapter import FakeModelAdapter, FakeStep
from minicodex2.model.messages import ToolCall
from minicodex2.tools.registry import build_runtime_tool_registry
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


def test_event_bus_records_events() -> None:
    bus = AgentEventBus("session_test")
    event = bus.emit("turn_started", {"user_input": "hi"}, "turn_test")
    assert event.id.startswith("event_")
    assert event.session_id == "session_test"
    assert event.turn_id == "turn_test"
    assert bus.events()[0].payload["user_input"] == "hi"


def test_tool_call_emits_events(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep("inspect", [ToolCall("call_1", "read_file", {"path": "README.md"})]),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)
    session.run_turn("read", allow_advice=False)
    event_types = [event.type for event in session.event_bus.events()]
    assert "tool_call_started" in event_types
    assert "tool_call_finished" in event_types


def test_verification_emits_events(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_smoke.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    fake = FakeModelAdapter(
        [
            FakeStep(
                "write",
                [ToolCall("call_1", "write_file", {"path": "created.py", "content": "VALUE = 1\n"})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)
    session.run_turn("write file", allow_advice=False)
    event_types = [event.type for event in session.event_bus.events()]
    assert "verification_started" in event_types
    assert "verification_plan_created" in event_types
    assert "verification_step_started" in event_types
    assert "verification_step_finished" in event_types
    assert "verification_passed" in event_types


def test_file_diff_event_is_emitted_for_writes(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "write",
                [ToolCall("call_1", "write_file", {"path": "calc.py", "content": "VALUE = 1\n"})],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)

    session.run_turn("write file", allow_advice=False)

    diff_events = [event for event in session.event_bus.events() if event.type == "file_diff"]
    assert len(diff_events) == 1
    assert diff_events[0].payload["path"] == "calc.py"
    assert "+VALUE = 1" in diff_events[0].payload["lines"]


def test_image_command_attaches_image_without_persisting_base64(tmp_path: Path) -> None:
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    )
    model = FakeModelAdapter([FakeStep("image understood")])
    session = make_session(tmp_path, model)

    result = session.run_turn(f"/image {image_path} describe it", allow_advice=False)

    assert result.status == "completed"
    image_events = [event for event in session.event_bus.events() if event.type == "image_attached"]
    assert len(image_events) == 1
    latest_user = session.history.messages()[-2]
    assert latest_user.content == "describe it"
    assert latest_user.metadata["image"]["path"] == str(image_path.resolve())
    assert "base64" not in str(latest_user.metadata)
    model_message = model.requests[0].messages[-1].to_model_dict()
    assert isinstance(model_message["content"], list)
    assert model_message["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_natural_language_image_reference_fuzzy_matches_workspace_image(tmp_path: Path) -> None:
    image_path = tmp_path / "ScreenShot.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 32)
    model = FakeModelAdapter([FakeStep("image understood")])
    session = make_session(tmp_path, model)

    result = session.run_turn(
        "\u5de5\u4f5c\u533a\u6709\u4e2a ScreenShot2.jpg\uff0c\u5e2e\u6211\u770b\u4e0b\u91cc\u9762\u7684\u9519\u8bef",
        allow_advice=False,
    )

    assert result.status == "completed"
    latest_user = session.history.messages()[-2]
    assert latest_user.metadata["image"]["path"] == str(image_path.resolve())
    assert "ScreenShot2.jpg" not in latest_user.metadata["image"]["path"]
    assert isinstance(model.requests[0].messages[-1].to_model_dict()["content"], list)


def test_natural_language_latest_screenshot_uses_recent_image(tmp_path: Path) -> None:
    older = tmp_path / "old.png"
    latest = tmp_path / "latest.png"
    older.write_bytes(b"\x89PNG\r\n\x1a\nold")
    latest.write_bytes(b"\x89PNG\r\n\x1a\nnew")
    os.utime(older, (1000, 1000))
    os.utime(latest, (2000, 2000))
    model = FakeModelAdapter([FakeStep("image understood")])
    session = make_session(tmp_path, model)

    result = session.run_turn("\u770b\u4e00\u4e0b\u6700\u65b0\u622a\u56fe\u91cc\u7684\u95ee\u9898", allow_advice=False)

    assert result.status == "completed"
    latest_user = session.history.messages()[-2]
    assert latest_user.metadata["image"]["path"] == str(latest.resolve())


def test_image_upload_requirement_does_not_attach_latest_image(tmp_path: Path) -> None:
    latest = tmp_path / "index_page.png"
    latest.write_bytes(b"\x89PNG\r\n\x1a\nnew")
    model = FakeModelAdapter([FakeStep("requirement understood")])
    session = make_session(tmp_path, model)

    result = session.run_turn(
        "\u5b66\u751f\u63d0\u4ea4\u56fe\u7247\u548c\u89c6\u9891\u8981\u6539\u4e0b\uff0c"
        "\u652f\u6301\u76f4\u63a5\u4e0a\u4f20\u56fe\u7247\u548c\u89c6\u9891\uff0c"
        "\u9700\u6c42\u4e5f\u66f4\u65b0\u5230\u8bbe\u8ba1\u6587\u6863",
        allow_advice=False,
    )

    assert result.status == "completed"
    assert not [event for event in session.event_bus.events() if event.type == "image_attached"]
    latest_user = session.history.messages()[-2]
    assert "image" not in latest_user.metadata
    assert isinstance(model.requests[0].messages[-1].to_model_dict()["content"], str)


def test_model_can_find_then_view_image(tmp_path: Path) -> None:
    image_path = tmp_path / "ScreenShot2.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 32)
    model = FakeModelAdapter(
        [
            FakeStep(
                "find image",
                [ToolCall("call_find", "find_images", {"query": "screenshot", "max_results": 5})],
            ),
            FakeStep(
                "view image",
                [ToolCall("call_view", "view_image", {"path": "ScreenShot2.jpg", "detail": "low"})],
            ),
            FakeStep("image analyzed"),
        ]
    )
    session = make_session(tmp_path, model)

    result = session.run_turn("inspect the visual artifact", allow_advice=False)

    assert result.status == "completed"
    assert model.call_count == 3
    second_request_messages = model.requests[2].messages
    image_messages = [
        message.to_model_dict()
        for message in second_request_messages
        if message.role == "user" and isinstance(message.to_model_dict()["content"], list)
    ]
    assert image_messages
    assert image_messages[-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_token_usage_emits_event(tmp_path: Path) -> None:
    session = make_session(tmp_path, FakeModelAdapter([FakeStep("hello")]))
    session.run_turn("hi", allow_advice=False)
    assert any(event.type == "token_usage_recorded" for event in session.event_bus.events())


def test_events_do_not_include_large_logs(tmp_path: Path) -> None:
    fake = FakeModelAdapter(
        [
            FakeStep(
                "run",
                [
                    ToolCall(
                        "call_1",
                        "run_command",
                        {"command": "python -c \"print('x' * 20000)\""},
                    )
                ],
            ),
            FakeStep("done"),
        ]
    )
    session = make_session(tmp_path, fake)
    session.run_turn("run noisy command", allow_advice=False)
    assert all(len(str(event.payload)) < 5000 for event in session.event_bus.events())

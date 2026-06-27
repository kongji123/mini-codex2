from __future__ import annotations

import asyncio
import html
from pathlib import Path

import minicodex2.tui.app as tui_app
from minicodex2.agent.chat_history import ChatHistory
from minicodex2.agent.unified_session import UnifiedAgentSession
from minicodex2.config.settings import ConfigLoader
from minicodex2.history.jsonl_store import JsonlHistoryStore
from minicodex2.model.fake_adapter import FakeModelAdapter, FakeStep
from minicodex2.model.messages import ToolCall
from minicodex2.tools.registry import build_runtime_tool_registry
from minicodex2.tools.runtime_tools import RuntimeTools
from minicodex2.tui.app import create_tui_app


def test_tui_pilot_can_submit_and_persist_message(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_tui_app(str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.click("#input")
            await pilot.press(*"hello tui")
            await pilot.press("enter")
            await pilot.pause(0.5)
            session_id = app.session.session_id
        history = JsonlHistoryStore(tmp_path).load_history(session_id)
        assert any(
            message.role == "user" and message.content == "hello tui"
            for message in history.messages()
        )

    asyncio.run(run())


def test_tui_pilot_renders_agent_process_steps(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "minicodex2.toml").write_text(
        '[verification]\ncommands = ["python check_calc.py"]\n',
        encoding="utf-8",
    )
    (tmp_path / "check_calc.py").write_text(
        "namespace = {}\n"
        "exec(open('calc.py', encoding='utf-8').read(), namespace)\n"
        "assert namespace['add'](2, 3) == 5\n",
        encoding="utf-8",
    )
    bad_source = "def add(a, b):\n    return a - b\n"
    good_source = "def add(a, b):\n    return a + b\n"

    def build_fake_session(
        workspace: str,
        *,
        api_key: str | None = None,
        model_name: str | None = None,
        base_url: str | None = None,
        resume: bool = False,
        autosave: bool = False,
    ) -> UnifiedAgentSession:
        settings = ConfigLoader().load(workspace, api_key=api_key, model=model_name, base_url=base_url)
        runtime = RuntimeTools(settings)
        model = FakeModelAdapter(
            [
                FakeStep(
                    "write broken implementation",
                    [ToolCall("call_1", "write_file", {"path": "calc.py", "content": bad_source})],
                ),
                FakeStep("ready for verification"),
                FakeStep(
                    "repair implementation",
                    [ToolCall("call_2", "write_file", {"path": "calc.py", "content": good_source})],
                ),
                FakeStep("fixed"),
            ]
        )
        return UnifiedAgentSession(
            settings=settings,
            model=model,
            tools=build_runtime_tool_registry(runtime),
            history=ChatHistory(),
        )

    monkeypatch.setattr(tui_app, "build_session", build_fake_session)

    async def run() -> None:
        app = create_tui_app(str(tmp_path))
        async with app.run_test(size=(140, 72)) as pilot:
            await pilot.click("#input")
            await pilot.press(*"fix add")
            await pilot.press("enter")
            for _ in range(60):
                await pilot.pause(0.2)
                event_types = [event.type for event in app.session.event_bus.events()]
                if (
                    not app._turn_running
                    and "verification_passed" in event_types
                    and "turn_finished" in event_types
                ):
                    break
            assert not app._turn_running
            app.refresh_runtime_state()
            await pilot.pause(0.1)
            exported = app.export_screenshot()
            event_types = [event.type for event in app.session.event_bus.events()]

        visible = html.unescape(exported).replace("\xa0", " ")
        assert "Running" in visible
        assert "write file" in visible or "\u5199\u5165\u6587\u4ef6" in visible
        assert "calc.py" in visible
        assert "Diff" in visible
        assert "return a + b" in visible
        assert "Running verification" in visible
        assert "Failed" in visible
        assert "Repair round" in visible
        assert "verification_passed" in event_types
        assert "turn_finished" in event_types

    asyncio.run(run())


def test_tui_diff_renderer_escapes_markup_characters(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_tui_app(str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            line = app._format_event_for_timeline(
                "file_diff",
                {
                    "path": "templates/page[demo].html",
                    "added_lines": 1,
                    "removed_lines": 1,
                    "lines": [
                        "--- a/templates/page[demo].html",
                        "+++ b/templates/page[demo].html",
                        "@@ -1 +1 @@",
                        "-<button>[old]</button>",
                        "+<button onclick=\"alert('[ok]')\">[new]</button>",
                    ],
                    "truncated": False,
                },
            )
            assert line is not None
            app.query_one("#conversation").write(line)

    asyncio.run(run())


def test_tui_background_ready_without_port_does_not_render_error(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_tui_app(str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            line = app._format_event_for_timeline(
                "background_command_ready",
                {
                    "name": "start web server",
                    "ok": True,
                    "command": "python -m flask --app app run",
                },
            )
            assert line is not None
            assert "Server ready" in line
            app.query_one("#conversation").write(line)

    asyncio.run(run())


def test_tui_renders_image_attached_event(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_tui_app(str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            line = app._format_event_for_timeline(
                "image_attached",
                {
                    "path": str(tmp_path / "shot[1].png"),
                    "mime_type": "image/png",
                    "detail": "low",
                },
            )
            assert line is not None
            assert "Attached image" in line
            app.query_one("#conversation").write(line)

    asyncio.run(run())


def test_tui_renders_browser_progress_event(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_tui_app(str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            line = app._format_event_for_timeline(
                "browser_progress",
                {
                    "stage": "browser_actions_planned",
                    "url": "http://127.0.0.1:3000/login",
                    "action_count": 3,
                    "actions": [
                        "wait_for #root",
                        "fill input[name=username]",
                        "click button[type=submit]",
                    ],
                },
            )
            assert line is not None
            assert "Browser actions" in line
            assert "fill input" in line
            app.query_one("#conversation").write(line)

    asyncio.run(run())


def test_tui_submit_preempts_idle_reflection(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        app = create_tui_app(str(tmp_path))
        cancelled: list[str] = []

        def fake_cancel_current_turn(*, source: str = "external") -> None:
            cancelled.append(source)

        app.session.cancel_current_turn = fake_cancel_current_turn  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            app._turn_running = True
            app._idle_tick_running = True
            app._submit_text("new user message", source="test")
            assert app._pending_submission == ("new user message", "test")
            assert cancelled == ["tui_user_preempt_idle"]

    asyncio.run(run())


def test_tui_toggle_idle_reflection_keeps_status_uncluttered(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_tui_app(str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            original = app.session.settings.agent.idle_tick_enabled
            app.action_toggle_idle_reflection()
            assert app.session.settings.agent.idle_tick_enabled is (not original)
            title = app.query_one("#title").render()
            status = app.query_one("#status").render()
            assert "goal-idle" not in str(title).lower()
            assert "idle" not in str(status).lower()
            assert "反思" not in str(status)

    asyncio.run(run())


def test_tui_idle_toggle_widget_click_updates_setting(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_tui_app(str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            original = app.session.settings.agent.idle_tick_enabled
            await pilot.click("#idle-toggle")
            await pilot.pause(0.1)
            assert app.session.settings.agent.idle_tick_enabled is (not original)
            idle_toggle = app.query_one("#idle-toggle").render()
            assert "idle reflection" in str(idle_toggle).lower()

    asyncio.run(run())


def test_tui_conversation_log_wraps_long_lines(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_tui_app(str(tmp_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.1)
            log = app.query_one("#conversation")
            assert log.wrap is True
            assert log.min_width == 1

    asyncio.run(run())


def test_tui_conversation_log_does_not_follow_when_user_scrolls_up(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_tui_app(str(tmp_path))
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause(0.1)
            log = app.query_one("#conversation")
            for index in range(80):
                log.write(f"line {index}")
            await pilot.pause(0.1)
            log.scroll_end(animate=False, immediate=True)
            await pilot.pause(0.1)
            bottom_y = log.scroll_y
            assert bottom_y > 0

            log.scroll_home(animate=False, immediate=True)
            await pilot.pause(0.1)
            assert log.scroll_y == 0
            log.write("new line while reading history")
            await pilot.pause(0.1)
            assert log.scroll_y == 0

            log.scroll_end(animate=False, immediate=True)
            await pilot.pause(0.1)
            assert log.scroll_y >= bottom_y
            log.write("new line at tail")
            await pilot.pause(0.1)
            assert log.is_vertical_scroll_end

    asyncio.run(run())


def test_tui_toggle_idle_reflection_persists_across_resume(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_tui_app(str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            app.action_toggle_idle_reflection()
            toggled = app.session.settings.agent.idle_tick_enabled
        resumed = create_tui_app(str(tmp_path))
        assert resumed.session.settings.agent.idle_tick_enabled is toggled

    asyncio.run(run())

from __future__ import annotations

import asyncio
from pathlib import Path

import minicodex2.tui.app as tui_app
from minicodex2.agent.chat_history import ChatHistory
from minicodex2.agent.unified_session import UnifiedAgentSession
from minicodex2.config.settings import ConfigLoader
from minicodex2.model.fake_adapter import FakeModelAdapter, FakeStep
from minicodex2.model.messages import ToolCall
from minicodex2.tools.registry import build_runtime_tool_registry
from minicodex2.tools.runtime_tools import RuntimeTools
from minicodex2.tui.app import create_tui_app


async def main() -> None:
    root = Path(__file__).resolve().parents[2]
    workspace = root / ".minicodex2" / "tui-visual-smoke-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    for stale_name in ("calc.py", "data.txt"):
        stale_path = workspace / stale_name
        if stale_path.exists():
            stale_path.unlink()
    (workspace / "minicodex2.toml").write_text(
        '[verification]\ncommands = ["python -m pytest -q"]\n',
        encoding="utf-8",
    )
    (workspace / "test_calc.py").write_text(
        "from calc import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    bad_source = "def add(a, b):\n    return a - b\n"
    good_source = "def add(a, b):\n    return a + b\n"

    def build_fake_session(
        workspace_arg: str,
        *,
        api_key: str | None = None,
        model_name: str | None = None,
        base_url: str | None = None,
        resume: bool = False,
        autosave: bool = False,
    ) -> UnifiedAgentSession:
        settings = ConfigLoader().load(
            workspace_arg,
            api_key=api_key,
            model=model_name,
            base_url=base_url,
        )
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

    original_build_session = tui_app.build_session
    tui_app.build_session = build_fake_session
    try:
        app = create_tui_app(str(workspace))
        async with app.run_test(size=(140, 72)) as pilot:
            await pilot.click("#input")
            await pilot.press(*"fix add")
            await pilot.press("enter")
            for _ in range(80):
                await pilot.pause(0.2)
                if not app._turn_running:
                    break
            await pilot.pause(0.5)
            svg = app.export_screenshot()
    finally:
        tui_app.build_session = original_build_session

    output = root / ".minicodex2" / "tui-visual-smoke.svg"
    output.write_text(svg, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

from pathlib import Path

from minicodex2.model.messages import ChatMessage
from minicodex2.tui.app import _build_plain_transcript


def test_cli_and_tui_modules_import() -> None:
    import minicodex2.cli.main
    import minicodex2.tui.app

    assert minicodex2.cli.main.main
    assert minicodex2.tui.app.run_tui


def test_docs_describe_core_topics() -> None:
    root = Path(__file__).resolve().parents[2]
    expected = [
        "docs/architecture.md",
        "docs/verification.md",
        "docs/context.md",
        "docs/failure-handling.md",
        "docs/collaboration-advisor.md",
        "docs/api.md",
        "docs/implementation-status.md",
    ]
    for relative in expected:
        assert (root / relative).exists(), relative


def test_tui_turns_run_on_background_thread() -> None:
    source = Path("src/minicodex2/tui/app.py").read_text(encoding="utf-8")
    assert "threading.Thread(target=self._run_turn_worker" in source
    assert "run_worker(" not in source


def test_tui_has_submit_fallbacks_and_autosave() -> None:
    source = Path("src/minicodex2/tui/app.py").read_text(encoding="utf-8")
    assert '"ctrl+enter", "submit_input"' in source
    assert '"ctrl+s", "submit_input"' in source
    assert '"ctrl+y", "copy_chat"' in source
    assert '"/copy"' in source
    assert '"/export-chat"' in source
    assert "autosave=True" in source
    assert "TUI submit received" in source


def test_tui_starts_worker_before_rendering_submitted_message() -> None:
    source = Path("src/minicodex2/tui/app.py").read_text(encoding="utf-8")
    assert source.index("worker.start()") < source.index(
        'convo.write(f"[bold cyan]>[/bold cyan] {escape(text)}")'
    )


def test_tui_renders_agent_status_in_conversation_timeline() -> None:
    source = Path("src/minicodex2/tui/app.py").read_text(encoding="utf-8")
    assert 'RichLog(id="conversation"' in source
    assert 'RichLog(id="event_log"' not in source
    assert "_format_event_for_timeline" in source
    assert 'convo.write(line)' in source


def test_tui_plain_transcript_is_copy_friendly() -> None:
    transcript = _build_plain_transcript(
        [
            ChatMessage(role="user", content="你好"),
            ChatMessage(role="assistant", content="可以，我来处理。"),
            ChatMessage(role="runtime", content="tool=read_file; status=ok"),
        ]
    )

    assert "user:\n你好" in transcript
    assert "assistant:\n可以，我来处理。" in transcript
    assert "runtime:\ntool=read_file; status=ok" in transcript

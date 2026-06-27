from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import platform
import subprocess
import threading
import time

from minicodex2.agent.result import AgentTurnResult
from minicodex2.cli.main import build_session, save_session
from minicodex2.display import UiMessageFormatter
from minicodex2.model.messages import ChatMessage


def run_tui(
    workspace: str = ".",
    *,
    api_key: str | None = None,
    model_profile: str | None = None,
    model_name: str | None = None,
    base_url: str | None = None,
    wire_api: str | None = None,
) -> None:
    create_tui_app(
        workspace,
        api_key=api_key,
        model_profile=model_profile,
        model_name=model_name,
        base_url=base_url,
        wire_api=wire_api,
    ).run()


def create_tui_app(
    workspace: str = ".",
    *,
    api_key: str | None = None,
    model_profile: str | None = None,
    model_name: str | None = None,
    base_url: str | None = None,
    wire_api: str | None = None,
):
    try:
        from rich.markup import escape
        from textual.app import App, ComposeResult
        from textual.widgets import Input, RichLog, Static
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Textual is not installed. Install minicodex2[tui].") from exc

    class IdleToggle(Static):
        def on_click(self, event) -> None:
            event.stop()
            self.app.action_toggle_idle_reflection()

    class ConversationLog(RichLog):
        """RichLog with terminal-style follow mode.

        Textual's default auto-scroll is great while watching live output, but
        painful when the user scrolls up to inspect earlier lines: every new
        event snaps the viewport back to the bottom. This widget follows only
        when the viewport was already at the bottom before the write. Scrolling
        back to the bottom naturally re-enables follow mode.
        """

        def write(
            self,
            content,
            width: int | None = None,
            expand: bool = False,
            shrink: bool = True,
            scroll_end: bool | None = None,
            animate: bool = False,
        ):
            if scroll_end is None:
                scroll_end = self._should_follow_tail()
            return super().write(
                content,
                width=width,
                expand=expand,
                shrink=shrink,
                scroll_end=scroll_end,
                animate=animate,
            )

        def _should_follow_tail(self) -> bool:
            try:
                max_scroll_y = int(self.max_scroll_y)
                scroll_y = int(self.scroll_y)
            except Exception:
                return True
            if max_scroll_y <= 0:
                return True
            return max_scroll_y - scroll_y <= 1

    class MiniCodexTui(App[None]):
        TITLE = "MiniCodex2"
        BINDINGS = [
            ("ctrl+enter", "submit_input", "Send"),
            ("ctrl+s", "submit_input", "Send"),
            ("ctrl+i", "toggle_idle_reflection", "Idle"),
            ("ctrl+y", "copy_chat", "Copy"),
            ("escape", "cancel_turn", "Cancel"),
        ]

        CSS = """
        Screen {
            background: #050505;
            color: #e6e6e6;
        }

        #title {
            height: 1;
            padding: 0 1;
            background: #050505;
            color: #f2f2f2;
            text-style: bold;
        }

        #conversation {
            height: 1fr;
            padding: 1 1;
            background: #050505;
            color: #e6e6e6;
        }

        #status {
            height: 2;
            padding: 0 1;
            background: #050505;
            color: #b8b8b8;
            border-top: solid #666666;
        }

        #idle-toggle {
            height: 1;
            padding: 0 1;
            background: #050505;
            color: #b8b8b8;
        }

        #idle-toggle:hover {
            background: #151515;
            color: #f2f2f2;
        }

        #input {
            height: 3;
            margin: 0 1 1 1;
            padding: 0 1;
            background: #101010;
            color: #f2f2f2;
            border: solid #3a3a3a;
        }

        #input:focus {
            border: solid #7a7a7a;
        }
        """

        def __init__(self) -> None:
            super().__init__()
            session_kwargs = {
                "api_key": api_key,
                "model_name": model_name,
                "base_url": base_url,
                "resume": True,
                "autosave": True,
            }
            if model_profile is not None:
                session_kwargs["model_profile"] = model_profile
            if wire_api is not None:
                session_kwargs["wire_api"] = wire_api
            self.session = build_session(workspace, **session_kwargs)
            self._last_event_count = 0
            self._turn_running = False
            self._idle_tick_running = False
            self._cancel_requested = False
            self._turn_generation = 0
            self._generation_modes: dict[int, str] = {}
            self._pending_submission: tuple[str, str] | None = None
            self._turn_started_at: float | None = None
            self._last_worked_seconds: float | None = None
            locale = os.environ.get("MINICODEX2_LOCALE") or self.session.settings.ui.locale
            self.formatter = UiMessageFormatter(locale)
            self.session.logger.write("TUI started")

        def compose(self) -> ComposeResult:
            yield Static(self._title_line(), id="title")
            yield ConversationLog(
                id="conversation",
                highlight=True,
                markup=True,
                wrap=True,
                auto_scroll=False,
                min_width=1,
            )
            yield Static(self._status_line("ready", 0), id="status")
            yield IdleToggle(self._idle_toggle_line(), id="idle-toggle")
            yield Input(
                placeholder="Message MiniCodex2, /image <path> <question>, /copy, /export-chat",
                id="input",
            )

        def on_mount(self) -> None:
            log = self.query_one("#conversation", RichLog)
            log.write(self._session_line())
            if self.session.history.messages():
                log.write("[dim]Recent session[/dim]")
            for message in self.session.history.messages()[-20:]:
                if message.role in {"user", "assistant", "runtime"}:
                    log.write(self._format_history_message(message.role, message.content))
            self.query_one("#input", Input).focus()
            self.set_interval(0.5, self.refresh_runtime_state)

        def on_unmount(self) -> None:
            runtime = self.session.runtime_tools
            if runtime is None:
                return
            try:
                result = runtime.cleanup_background_processes()
                terminated = result.metadata.get("terminated", [])
                count = sum(
                    1
                    for item in terminated
                    if isinstance(item, dict) and item.get("was_running")
                ) if isinstance(terminated, list) else 0
                if count:
                    self.session.logger.write(f"TUI cleanup terminated background_processes={count}")
            except Exception as exc:  # pragma: no cover - shutdown best effort
                self.session.logger.write(f"TUI cleanup failed error={exc!r}")

        def on_input_submitted(self, event: Input.Submitted) -> None:
            try:
                self._submit_text(event.value, source="enter")
                event.input.value = ""
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                self.session.logger.write(f"TUI submit handler exception: {exc!r}")
                self.query_one("#conversation", RichLog).write(f"[red]Submit error:[/red] {exc}")

        def action_submit_input(self) -> None:
            try:
                input_widget = self.query_one("#input", Input)
                self._submit_text(input_widget.value, source="binding")
                input_widget.value = ""
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                self.session.logger.write(f"TUI binding submit exception: {exc!r}")
                self.query_one("#conversation", RichLog).write(f"[red]Submit error:[/red] {exc}")

        def _submit_text(self, text: str, *, source: str) -> None:
            text = text.strip()
            if not text:
                return
            self.session.logger.write(f"TUI submit received source={source} text={text[:200]}")
            if self._handle_idle_toggle_command(text):
                return
            if self._handle_copy_command(text):
                return
            if self._handle_cleanup_command(text):
                return
            if self._handle_permission_command(text):
                return
            self.session.logger.write("TUI submit checking running state")
            if self._turn_running:
                self.session.logger.write("TUI submit rejected because turn is already running")
                convo = self.query_one("#conversation", RichLog)
                if self._idle_tick_running:
                    self._pending_submission = (text, source)
                    self._cancel_requested = True
                    self.session.cancel_current_turn(source="tui_user_preempt_idle")
                    convo.write("[yellow]> Interrupting idle reflection for your new message...[/yellow]")
                    self.query_one("#status", Static).update(self._status_line("running", None))
                    return
                if self._cancel_requested:
                    convo.write("[yellow]> Cancel is pending. Wait for the current turn to stop.[/yellow]")
                else:
                    convo.write("[yellow]> A turn is already running. Please wait.[/yellow]")
                return
            self._start_user_turn(text, source=source)

        def _start_user_turn(self, text: str, *, source: str) -> None:
            from rich.markup import escape

            self._turn_running = True
            self._idle_tick_running = False
            self._cancel_requested = False
            self._turn_started_at = time.monotonic()
            self._last_worked_seconds = None
            self._turn_generation += 1
            generation = self._turn_generation
            self._generation_modes[generation] = "user"
            self.session.logger.write(f"TUI submit marked running source={source}")
            worker = threading.Thread(target=self._run_turn_worker, args=(text, generation), daemon=True)
            self.session.logger.write("TUI submit starting worker thread")
            worker.start()
            self.session.logger.write("TUI submit worker thread started")
            convo = self.query_one("#conversation", RichLog)
            convo.write("")
            convo.write(f"[bold cyan]>[/bold cyan] {escape(text)}")
            self.query_one("#status", Static).update(self._status_line("running", None))
            self.session.logger.write("TUI submit UI updated")

        def action_toggle_idle_reflection(self) -> None:
            self._set_idle_reflection_enabled(
                not self.session.settings.agent.idle_tick_enabled,
                announce=True,
            )

        def _handle_idle_toggle_command(self, text: str) -> bool:
            normalized = " ".join(text.strip().lower().split())
            enabled_commands = {"/idle on", "idle on"}
            disabled_commands = {"/idle off", "idle off"}
            if normalized not in enabled_commands | disabled_commands:
                return False
            self._set_idle_reflection_enabled(normalized in enabled_commands, announce=True)
            return True

        def _set_idle_reflection_enabled(self, enabled: bool, *, announce: bool) -> None:
            self.session.settings.agent.idle_tick_enabled = enabled
            state = self.formatter.ui_text("on") if self.session.settings.agent.idle_tick_enabled else self.formatter.ui_text("off")
            if announce:
                self.query_one("#conversation", RichLog).write(
                    f"[yellow]> Idle reflection {state}[/yellow]"
                )
            self.query_one("#title", Static).update(self._title_line())
            self._refresh_idle_toggle()
            self.refresh_runtime_state()
            save_session(self.session)

        def action_copy_chat(self) -> None:
            self._copy_chat_to_clipboard(scope="full")

        def _handle_copy_command(self, text: str) -> bool:
            parts = text.strip().split()
            if not parts:
                return False
            command = parts[0].lower()
            if command not in {"/copy", "copy", "/export-chat", "export-chat"}:
                return False
            scope, recent_count = _parse_transcript_scope(parts[1:])
            transcript = _build_plain_transcript(
                self.session.history.messages(),
                active_messages=self.session.history.active_messages(),
                scope=scope,
                recent_count=recent_count,
            )
            if command in {"/export-chat", "export-chat"}:
                path = _write_transcript_export(
                    self.session.settings.workspace_root,
                    self.session.session_id,
                    transcript,
                )
                self.query_one("#conversation", RichLog).write(
                    f"[green]> Exported chat transcript[/green] {escape(str(path))}"
                )
                self.session.logger.write(f"TUI exported chat transcript path={path}")
                return True
            self._copy_chat_to_clipboard(
                scope=scope,
                recent_count=recent_count,
                transcript=transcript,
            )
            return True

        def _copy_chat_to_clipboard(
            self,
            *,
            scope: str = "full",
            recent_count: int | None = None,
            transcript: str | None = None,
        ) -> None:
            transcript = transcript or _build_plain_transcript(
                self.session.history.messages(),
                active_messages=self.session.history.active_messages(),
                scope=scope,
                recent_count=recent_count,
            )
            convo = self.query_one("#conversation", RichLog)
            try:
                _copy_text_to_clipboard(transcript)
            except Exception as exc:
                path = _write_transcript_export(
                    self.session.settings.workspace_root,
                    self.session.session_id,
                    transcript,
                )
                convo.write(
                    "[yellow]> Clipboard copy failed; exported chat transcript instead[/yellow] "
                    f"{escape(str(path))}"
                )
                self.session.logger.write(
                    f"TUI copy chat failed error={exc!r}; exported path={path}"
                )
                return
            convo.write(
                f"[green]> Copied chat transcript[/green] "
                f"[dim]({len(transcript)} chars, scope={escape(scope)})[/dim]"
            )
            self.session.logger.write(
                f"TUI copied chat transcript chars={len(transcript)} scope={scope}"
            )

        def _handle_cleanup_command(self, text: str) -> bool:
            normalized = " ".join(text.strip().lower().split())
            if normalized not in {"/cleanup", "cleanup", "/release", "release"}:
                return False
            result = self._cleanup_background_processes()
            self.query_one("#conversation", RichLog).write(result)
            self.refresh_runtime_state()
            save_session(self.session)
            return True

        def _cleanup_background_processes(self) -> str:
            runtime = self.session.runtime_tools
            if runtime is None:
                return "[dim]> No runtime cleanup is available.[/dim]"
            result = runtime.cleanup_background_processes()
            terminated = result.metadata.get("terminated") if isinstance(result.metadata, dict) else []
            running = [
                item
                for item in terminated
                if isinstance(item, dict) and item.get("was_running")
            ] if isinstance(terminated, list) else []
            if not running:
                return "[dim]> No MiniCodex2 background processes to clean up.[/dim]"
            pids = ", ".join(str(item.get("pid")) for item in running[:12])
            extra = " ..." if len(running) > 12 else ""
            return f"[yellow]> Cleaned up {len(running)} background process(es): {pids}{extra}[/yellow]"

        def action_cancel_turn(self) -> None:
            convo = self.query_one("#conversation", RichLog)
            if not self._turn_running:
                convo.write("[dim]> No running turn to cancel.[/dim]")
                return
            self._cancel_requested = True
            self.session.cancel_current_turn(source="tui_escape")
            self.session.state.result_state = "cancelled"
            self.session.event_bus.emit("turn_cancel_requested", {"source": "tui_escape"})
            self.session.logger.write("TUI cancel requested by escape")
            convo.write("[yellow]> Cancel requested. Waiting for the running turn to stop.[/yellow]")
            self.query_one("#status", Static).update(self._status_line("cancelled", None))
            save_session(self.session)

        def _handle_permission_command(self, text: str) -> bool:
            runtime = self.session.runtime_tools
            pending = runtime.permission_store.pending() if runtime is not None else []
            intent = self._permission_intent(text, has_pending=bool(pending))
            if intent is None:
                return False
            convo = self.query_one("#conversation", RichLog)
            if runtime is None:
                convo.write("[red]Permission store unavailable.[/red]")
                return True
            if not pending:
                convo.write("[yellow]No pending permission request.[/yellow]")
                self.refresh_runtime_state()
                return True
            request = pending[-1]
            approved = intent == "approve"
            runtime.permission_store.resolve(request.id, approved)
            self.session.event_bus.emit(
                "permission_resolved",
                {"permission_request_id": request.id, "approved": approved},
            )
            status = "approved" if approved else "denied"
            convo.write(f"[yellow]Permission {status}[/yellow] {escape(request.id)}")
            self.session.logger.write(
                f"TUI permission {status} id={request.id} action={request.action}"
            )
            if approved:
                self._execute_approved_permission(request)
            save_session(self.session)
            self.refresh_runtime_state()
            return True

        @staticmethod
        def _permission_intent(text: str, *, has_pending: bool) -> str | None:
            normalized = text.strip().lower()
            approve_words = {"/approve", "approve", "yes", "y", "ok", "确认", "同意", "授权", "允许"}
            deny_words = {"/deny", "deny", "no", "n", "拒绝", "取消", "不同意"}
            if normalized in approve_words:
                return "approve"
            if normalized in deny_words:
                return "deny"
            if has_pending and any(word in text for word in ("确认", "同意", "授权", "允许", "可以")):
                return "approve"
            if has_pending and any(word in text for word in ("拒绝", "取消", "不允许")):
                return "deny"
            return None

        def _execute_approved_permission(self, request) -> None:
            convo = self.query_one("#conversation", RichLog)
            summary = str(request.payload.get("path") or request.payload.get("command") or request.action)
            self.session.event_bus.emit(
                "tool_call_started",
                {"name": request.action, "summary": summary},
            )
            result = self.session.tools.execute(request.action, request.payload)
            self.session.event_bus.emit(
                "tool_call_finished",
                {
                    "name": request.action,
                    "summary": summary,
                    "ok": result.ok,
                    "blocked": result.blocked,
                    "content_excerpt": result.content[:500],
                },
            )
            if result.did_write:
                self.session.state.did_write = True
                self.session.state.changed_files.extend(result.changed_files)
                for changed_file in result.changed_files:
                    self.session.event_bus.emit(
                        "file_changed",
                        {"path": changed_file, "tool": request.action},
                    )
                diff = result.metadata.get("diff")
                if isinstance(diff, dict):
                    self.session.event_bus.emit("file_diff", diff)
            if result.ok:
                convo.write(f"[green]Permission action completed[/green] {escape(result.content)}")
                self.session.state.result_state = "completed"
            else:
                convo.write(f"[red]Permission action failed[/red] {escape(result.content)}")
                self.session.state.result_state = "blocked" if result.blocked else "completed"

        def _run_turn_worker(self, text: str, generation: int) -> None:
            self.session.logger.write(f"TUI worker started generation={generation}")
            try:
                result = self.session.run_turn(text)
                save_session(self.session)
                self.session.logger.write(f"TUI worker finished status={result.status}")
                self.call_from_thread(self._show_result, result, generation)
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                self.session.logger.write(f"TUI worker exception: {exc!r}")
                self.call_from_thread(self._show_runtime_error, exc, generation)

        def _run_idle_tick_worker(self, generation: int) -> None:
            self.session.logger.write(f"TUI idle worker started generation={generation}")
            try:
                result = self.session.run_idle_tick()
                if result is None:
                    self.call_from_thread(self._finish_idle_tick_without_result, generation)
                    return
                save_session(self.session)
                self.session.logger.write(f"TUI idle worker finished status={result.status}")
                self.call_from_thread(self._show_result, result, generation)
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                self.session.logger.write(f"TUI idle worker exception: {exc!r}")
                self.call_from_thread(self._show_runtime_error, exc, generation)

        def _finish_idle_tick_without_result(self, generation: int) -> None:
            if generation != self._turn_generation:
                return
            worked = self._finish_work_timer()
            self._turn_running = False
            self._idle_tick_running = False
            self._cancel_requested = False
            self._generation_modes.pop(generation, None)
            self.query_one("#conversation", RichLog).write(
                f"[dim]• Idle reflection finished, worked for {_format_duration(worked)}[/dim]"
            )
            self.refresh_runtime_state()
            self._drain_pending_submission()

        def _show_runtime_error(self, exc: Exception, generation: int) -> None:
            if generation != self._turn_generation:
                return
            mode = self._generation_modes.pop(generation, "user")
            worked = self._finish_work_timer()
            self._turn_running = False
            self._idle_tick_running = False
            self._cancel_requested = False
            if mode == "idle" and self._pending_submission is not None:
                self.query_one("#conversation", RichLog).write(
                    f"[yellow]> Idle reflection stopped for your new message. Worked for {_format_duration(worked)}.[/yellow]"
                )
            else:
                self.query_one("#conversation", RichLog).write(
                    f"[red]Runtime error[/red] {exc}\n[dim]worked for {_format_duration(worked)}[/dim]"
                )
            self.refresh_runtime_state()
            self._drain_pending_submission()

        def _show_result(self, result: AgentTurnResult, generation: int) -> None:
            if generation != self._turn_generation:
                self.session.logger.write(
                    f"TUI ignored completed result for stale generation={generation}"
                )
                return
            mode = self._generation_modes.pop(generation, "user")
            worked = self._finish_work_timer()
            self._turn_running = False
            self._idle_tick_running = False
            was_cancelled = self._cancel_requested or result.status == "cancelled"
            self._cancel_requested = False
            convo = self.query_one("#conversation", RichLog)
            if mode == "idle" and self._pending_submission is not None and was_cancelled:
                convo.write(
                    f"[yellow]> Idle reflection stopped for your new message. Worked for {_format_duration(worked)}.[/yellow]"
                )
                self.refresh_runtime_state()
                self._drain_pending_submission()
                return
            assistant = self.formatter.ui_text("assistant")
            label = self.formatter.status_label(result.status)
            if was_cancelled:
                convo.write(f"[yellow]{assistant}[/yellow] [dim]({label})[/dim]")
            else:
                convo.write(f"[bold green]{assistant}[/bold green] [dim]({label})[/dim]")
            if result.response:
                convo.write(escape(result.response))
            if result.failure_pack:
                failure_label = self.formatter.ui_text("failure_pack")
                convo.write(
                    f"[red]{failure_label}[/red] "
                    f"{escape(result.failure_pack.failure_type)} | "
                    f"{escape(result.failure_pack.first_blocking_failure)}"
                )
            convo.write(f"[dim]• Worked for {_format_duration(worked)}[/dim]")
            self._drain_pending_submission()

        def _drain_pending_submission(self) -> None:
            pending = self._pending_submission
            if pending is None or self._turn_running:
                return
            self._pending_submission = None
            text, source = pending
            self.session.logger.write("TUI draining queued user submission after idle preemption")
            self._start_user_turn(text, source=f"{source}:queued")

        def refresh_runtime_state(self) -> None:
            try:
                convo = self.query_one("#conversation", RichLog)
                status = self.query_one("#status", Static)
            except Exception:
                return
            all_events = self.session.event_bus.events()
            for agent_event in all_events[self._last_event_count :]:
                try:
                    line = self._format_event_for_timeline(agent_event.type, agent_event.payload)
                    if line:
                        convo.write(line)
                except Exception as exc:
                    self.session.logger.write(
                        f"TUI event render exception type={agent_event.type} error={exc!r}"
                    )
                    convo.write(f"[red]• UI render error[/red] {escape(agent_event.type)}")
            self._last_event_count = len(all_events)
            total = self.session.token_usage.total()
            current_status = "running" if self._turn_running else self.session.state.result_state
            status.update(self._status_line(current_status, total))
            self._refresh_idle_toggle()
            if (
                not self._turn_running
                and not self._cancel_requested
                and self.session.should_run_idle_tick()
            ):
                self._turn_running = True
                self._idle_tick_running = True
                self._turn_started_at = time.monotonic()
                self._last_worked_seconds = None
                self._turn_generation += 1
                generation = self._turn_generation
                self._generation_modes[generation] = "idle"
                self.session.logger.write(
                    f"TUI scheduling idle tick generation={generation}"
                )
                worker = threading.Thread(
                    target=self._run_idle_tick_worker,
                    args=(generation,),
                    daemon=True,
                )
                worker.start()

        def _format_event_for_timeline(self, event_type: str, payload: dict) -> str | None:
            from minicodex2.agent.events import AgentEvent

            summary = str(payload.get("summary") or "")
            if event_type == "turn_started":
                return "[dim]• Started work[/dim]"
            if event_type == "idle_tick_started":
                tick = payload.get("tick")
                max_ticks = payload.get("max")
                return (
                    f"[dim]• Idle reflection[/dim] [cyan]({tick}/{max_ticks})[/cyan]\n"
                    "[dim]  checking whether any required work is still unfinished[/dim]"
                )
            if event_type == "idle_tick_finished":
                tick = payload.get("tick")
                status = self.formatter.status_label(str(payload.get("status") or "completed"))
                return f"[dim]• Idle reflection finished[/dim] [cyan]({tick})[/cyan] [bold]{status}[/bold]"
            if event_type == "turn_cancel_requested":
                return "[yellow]• Cancel requested[/yellow]"
            if event_type == "image_attached":
                path = escape(str(payload.get("path") or "image"))
                detail = escape(str(payload.get("detail") or "low"))
                return f"[cyan]• Attached image[/cyan] {path} [dim]detail={detail}[/dim]"
            if event_type == "history_changed":
                message = escape(str(payload.get("message") or "history updated"))
                return f"[yellow]History[/yellow] {message}"
            if event_type == "context_built":
                tokens = payload.get("estimated_tokens")
                return f"[dim]• Built context[/dim] [cyan]({tokens} tokens est.)[/cyan]"
            if event_type == "model_call_started":
                model = escape(str(payload.get("model") or "model"))
                return f"[dim]• Calling model[/dim] [bold]{model}[/bold]"
            if event_type == "model_call_finished":
                calls = payload.get("tool_calls", 0)
                return f"[dim]• Model returned[/dim] [cyan]({calls} tool calls)[/cyan]"
            if event_type == "token_usage_recorded":
                tokens = payload.get("total_tokens")
                cache_text = _cache_payload_text(payload)
                return f"[dim]• Token usage[/dim] [cyan]{tokens} tokens[/cyan] [dim]cache {cache_text}[/dim]"
            if event_type == "turn_metrics":
                elapsed = _format_duration(float(payload.get("elapsed_seconds") or 0))
                model_calls = int(payload.get("model_calls") or 0)
                tool_calls = int(payload.get("tool_calls") or 0)
                tool_failures = int(payload.get("tool_failures") or 0)
                browser_failures = int(payload.get("browser_failures") or 0)
                verification_failures = int(payload.get("verification_failures") or 0)
                compactions = int(payload.get("context_compactions") or 0)
                failures = payload.get("tool_failures_by_name") or {}
                failure_text = ""
                if isinstance(failures, dict) and failures:
                    top = ", ".join(
                        f"{escape(str(name))}={count}" for name, count in list(failures.items())[:3]
                    )
                    failure_text = f" [dim]top failures {top}[/dim]"
                return (
                    f"[dim]Metrics[/dim] worked [cyan]{elapsed}[/cyan], "
                    f"model [cyan]{model_calls}[/cyan], tools [cyan]{tool_calls}[/cyan], "
                    f"failed [red]{tool_failures}[/red], browser [red]{browser_failures}[/red], "
                    f"verify failed [red]{verification_failures}[/red], compact [cyan]{compactions}[/cyan]"
                    f"{failure_text}"
                )
            if event_type == "progress_reported":
                focus = escape(self._compact(str(payload.get("current_focus") or ""), limit=180))
                next_action = escape(self._compact(str(payload.get("next_action") or ""), limit=220))
                hypothesis = escape(self._compact(str(payload.get("hypothesis") or ""), limit=220))
                why = escape(self._compact(str(payload.get("why") or ""), limit=220))
                risk = escape(self._compact(str(payload.get("risk_or_blocker") or ""), limit=220))
                confidence = escape(str(payload.get("confidence") or "medium"))
                rendered = [f"[cyan]Agent progress[/cyan] [dim]confidence={confidence}[/dim]"]
                if focus:
                    rendered.append(f"[dim]  focus:[/dim] {focus}")
                facts = payload.get("observed_facts") or []
                if isinstance(facts, list):
                    for fact in facts[:4]:
                        fact_text = escape(self._compact(str(fact), limit=180))
                        if fact_text:
                            rendered.append(f"[dim]  fact:[/dim] {fact_text}")
                if hypothesis:
                    rendered.append(f"[dim]  hypothesis:[/dim] {hypothesis}")
                if why:
                    rendered.append(f"[dim]  why:[/dim] {why}")
                if risk:
                    rendered.append(f"[yellow]  risk:[/yellow] {risk}")
                if next_action:
                    rendered.append(f"[green]  next:[/green] {next_action}")
                return "\n".join(rendered)
            if event_type == "assistant_progress":
                content = escape(self._compact(str(payload.get("content") or ""), limit=500))
                if not content:
                    return None
                return f"[dim]• Model note[/dim] {content}"
            if event_type == "model_action_plan":
                actions = payload.get("actions") or []
                if not isinstance(actions, list) or not actions:
                    return None
                rendered = ["[dim]• Next actions[/dim]"]
                for raw_action in actions[:8]:
                    if not isinstance(raw_action, dict):
                        continue
                    name = self.formatter.tool_label(str(raw_action.get("name") or "tool"))
                    summary_text = str(raw_action.get("summary") or "")
                    summary_part = f" [bold]{escape(summary_text)}[/bold]" if summary_text else ""
                    rendered.append(f"[dim]  -[/dim] {name}{summary_part}")
                if payload.get("truncated"):
                    rendered.append("[dim]  - ...[/dim]")
                return "\n".join(rendered)
            if event_type == "tool_call_started":
                name = self.formatter.tool_label(str(payload.get("name") or "tool"))
                target = f" [bold]{escape(summary)}[/bold]" if summary else ""
                return f"[cyan]• Running[/cyan] {name}{target}"
            if event_type == "tool_call_finished":
                name = self.formatter.tool_label(str(payload.get("name") or "tool"))
                target = f" [bold]{escape(summary)}[/bold]" if summary else ""
                if payload.get("ok"):
                    return f"[green]• Ran[/green] {name}{target}"
                details = escape(self._compact(str(payload.get("content_excerpt") or "failed")))
                return f"[red]• Failed[/red] {name}{target}\n[red]  └ {details}[/red]"
            if event_type == "command_progress":
                stage = str(payload.get("stage") or "running")
                command = escape(str(payload.get("command") or "command"))
                elapsed = payload.get("elapsed_seconds")
                timeout = payload.get("timeout_seconds")
                if stage == "started":
                    return f"[cyan]  - Command started[/cyan] {command} [dim]timeout {timeout}s[/dim]"
                if stage == "running":
                    return f"[cyan]  - Command still running[/cyan] {command} [dim]{elapsed}s / {timeout}s[/dim]"
                if stage == "timed_out":
                    return f"[red]  - Command timed out[/red] {command} [dim]{elapsed}s / {timeout}s[/dim]"
                if stage == "finished":
                    return f"[green]  - Command finished[/green] {command} [dim]{elapsed}s[/dim]"
                return f"[dim]  - Command {escape(stage)}[/dim] {command}"
            if event_type == "browser_progress":
                stage = str(payload.get("stage") or "running")
                url = escape(str(payload.get("url") or "browser"))
                browser = escape(str(payload.get("browser") or "browser"))
                action_count = payload.get("action_count")
                if stage == "browser_prepare":
                    return f"[cyan]  - Browser prepare[/cyan] {url} [dim]{browser}[/dim]"
                if stage == "browser_bootstrap_started":
                    return "[cyan]  - Browser tooling install[/cyan] [dim]playwright-core[/dim]"
                if stage == "browser_bootstrap_finished":
                    return "[green]  - Browser tooling ready[/green] [dim]playwright-core[/dim]"
                if stage == "browser_actions_planned":
                    actions = payload.get("actions") or []
                    rendered = [f"[dim]  - Browser actions[/dim] [cyan]({action_count or len(actions)})[/cyan]"]
                    if isinstance(actions, list):
                        for item in actions[:8]:
                            rendered.append(f"[dim]    •[/dim] {escape(str(item))}")
                    return "\n".join(rendered)
                if stage == "browser_run_started":
                    count_text = f" [dim]{action_count} actions[/dim]" if action_count else ""
                    return f"[cyan]  - Browser started[/cyan] {url} [dim]{browser}[/dim]{count_text}"
                if stage == "browser_run_finished":
                    semantic_failure = escape(str(payload.get("semantic_failure") or ""))
                    ok = bool(payload.get("ok"))
                    if ok:
                        return f"[green]  - Browser finished[/green] {url} [dim]{browser}[/dim]"
                    if semantic_failure:
                        return f"[red]  - Browser failed[/red] {url}\n[red]    {semantic_failure}[/red]"
                    return f"[red]  - Browser failed[/red] {url} [dim]{browser}[/dim]"
                return f"[dim]  - Browser {escape(stage)}[/dim] {url}"
            if event_type == "toolchain_install_progress":
                return self._format_toolchain_install_progress(payload)
            if event_type == "file_diff":
                return self._format_file_diff(payload)
            if event_type == "file_changed":
                return f"[yellow]  └ Changed[/yellow] {escape(str(payload.get('path')))}"
            if event_type == "verification_started":
                return "[dim]• Running verification[/dim]"
            if event_type == "verification_plan_created":
                return f"[dim]  └ Plan[/dim] {escape(str(payload.get('reason')))}"
            if event_type == "verification_step_started":
                command = payload.get("command")
                detail = (
                    f" [bold]{escape(str(command))}[/bold]"
                    if command
                    else escape(str(payload.get("name") or ""))
                )
                return f"[cyan]  └ Verify[/cyan]{detail}"
            if event_type == "verification_step_finished":
                command = escape(str(payload.get("command") or payload.get("name") or "verification"))
                if payload.get("ok"):
                    return f"[green]  └ Passed[/green] {command}"
                details = self._compact(
                    str(payload.get("failure_summary") or payload.get("stderr_excerpt") or "failed")
                )
                details = escape(details)
                return f"[red]  └ Failed[/red] {command}\n[red]    {details}[/red]"
            if event_type == "verification_needs_model_decision":
                reason = escape(str(payload.get("reason") or "model should choose the next verification step"))
                return f"[yellow]• Verification paused[/yellow]\n[yellow]  └ {reason}[/yellow]"
            if event_type == "verification_failed":
                return "[red]• Verification failed[/red]"
            if event_type == "verification_passed":
                return "[green]• Verification passed[/green]"
            if event_type == "background_command_started":
                port = payload.get("port") or "unknown"
                command = escape(str(payload.get("command") or payload.get("name") or "server"))
                return f"[cyan]  └ Starting server[/cyan] {command} [dim]port {port}[/dim]"
            if event_type == "background_command_ready":
                port = payload.get("port") or "unknown"
                return f"[green]  └ Server ready[/green] [dim]port {port}[/dim]"
            if event_type == "background_command_failed":
                port = payload.get("port") or "unknown"
                return f"[red]  └ Server failed[/red] [dim]port {port}[/dim]"
            if event_type == "failure_pack_created":
                failure = escape(self._compact(str(payload.get("first_blocking_failure") or "blocked")))
                return f"[red]• Blocking failure[/red]\n[red]  └ {failure}[/red]"
            if event_type == "repair_round_started":
                return f"[yellow]• Repair round {payload.get('round')}[/yellow]"
            if event_type == "goal_step_updated":
                return self._format_goal_step_updated(payload)
            if event_type == "blocked":
                return f"[red]• Blocked[/red] {escape(str(payload.get('reason')))}"
            if event_type == "turn_finished":
                label = self.formatter.status_label(str(payload.get("status") or "completed"))
                return f"[green]• Turn finished[/green] [bold]{label}[/bold]"

            display = self.formatter.format_event(
                AgentEvent(
                    type=event_type,
                    session_id=self.session.session_id,
                    payload=payload,
                )
            )
            if display is None:
                return None
            style = {
                "info": "dim",
                "success": "green",
                "warning": "yellow",
                "error": "red",
            }.get(display.level, "dim")
            return f"[{style}]* {display.title}[/{style}] {display.message}"

        @staticmethod
        def _compact(text: str, limit: int = 220) -> str:
            compact = " ".join(text.split())
            if len(compact) <= limit:
                return compact
            return compact[: limit - 3] + "..."

        def _format_file_diff(self, payload: dict) -> str:
            path = escape(str(payload.get("path") or "file"))
            added = payload.get("added_lines", 0)
            removed = payload.get("removed_lines", 0)
            lines = payload.get("lines") or []
            if not isinstance(lines, list):
                lines = []
            rendered = [f"[yellow]  └ Diff[/yellow] {path} [green]+{added}[/green] [red]-{removed}[/red]"]
            for raw_line in lines[:40]:
                line = str(raw_line)
                escaped = escape(line)
                if line.startswith("+") and not line.startswith("+++"):
                    rendered.append(f"[green]    {escaped}[/green]")
                elif line.startswith("-") and not line.startswith("---"):
                    rendered.append(f"[red]    {escaped}[/red]")
                elif line.startswith("@@"):
                    rendered.append(f"[cyan]    {escaped}[/cyan]")
                else:
                    rendered.append(f"[dim]    {escaped}[/dim]")
            if payload.get("truncated"):
                rendered.append("[dim]    ... diff truncated[/dim]")
            return "\n".join(rendered)

        def _format_goal_step_updated(self, payload: dict) -> str:
            step = payload.get("step") or {}
            if not isinstance(step, dict):
                step = {}
            step_id = escape(str(step.get("id") or payload.get("current_step_id") or "unknown"))
            title = escape(self._compact(str(step.get("title") or ""), limit=160))
            status = escape(str(step.get("status") or "updated"))
            evidence_ids = step.get("evidence_ids") or []
            if not isinstance(evidence_ids, list):
                evidence_ids = []
            rendered = [
                f"[green]• Work step updated[/green] [bold]{step_id}[/bold] [dim]status={status}[/dim]"
            ]
            if title:
                rendered.append(f"[dim]  title:[/dim] {title}")
            if evidence_ids:
                rendered.append(
                    "[dim]  evidence_ids:[/dim] "
                    + ", ".join(escape(str(evidence_id)) for evidence_id in evidence_ids[:6])
                )
            records = payload.get("evidence_records") or []
            if isinstance(records, list):
                for record in records[:3]:
                    if not isinstance(record, dict):
                        continue
                    evidence_id = escape(str(record.get("id") or "evidence"))
                    kind = escape(str(record.get("kind") or "evidence"))
                    source = escape(str(record.get("source_tool") or "tool"))
                    summary = escape(self._compact(str(record.get("summary") or ""), limit=180))
                    rendered.append(
                        f"[dim]  -[/dim] {evidence_id} [cyan]{kind}[/cyan] "
                        f"[dim]via {source}[/dim] {summary}"
                    )
                if len(records) > 3:
                    rendered.append("[dim]  - ...[/dim]")
            return "\n".join(rendered)

        def _title_line(self) -> str:
            root = self.session.settings.workspace_root
            return f"MiniCodex2  {root}"

        def _idle_toggle_line(self) -> str:
            idle_state = self.formatter.ui_text("on") if self.session.settings.agent.idle_tick_enabled else self.formatter.ui_text("off")
            return f"Idle reflection: {idle_state}  (click to toggle, /idle on|off)"

        def _refresh_idle_toggle(self) -> None:
            try:
                self.query_one("#idle-toggle", Static).update(self._idle_toggle_line())
            except Exception:
                return

        def _session_line(self) -> str:
            root = self.session.settings.workspace_root
            return f"[bold]MiniCodex2[/bold] [dim]{self.session.session_id} | {root}[/dim]"

        def _format_history_message(self, role: str, content: str) -> str:
            if role == "user":
                return f"[bold cyan]>[/bold cyan] {escape(content)}"
            if role == "assistant":
                return f"[bold green]{self.formatter.ui_text('assistant')}[/bold green] {escape(content)}"
            return f"[dim]{escape(role)}[/dim] {escape(content)}"

        def _format_toolchain_install_progress(self, payload: dict) -> str:
            name = escape(str(payload.get("name") or "toolchain"))
            stage = str(payload.get("stage") or "progress")
            labels = {
                "inspect_before": "Checking toolchain",
                "already_available": "Toolchain already available",
                "select_installer": "Selected installer",
                "no_installer": "No installer available",
                "download_metadata": "Fetching download metadata",
                "download_installer": "Downloading installer",
                "download_progress": "Downloading installer",
                "download_failed": "Download failed",
                "run_installer": "Running installer",
                "inspect_after": "Checking install result",
                "finished": "Install finished",
            }
            label = labels.get(stage, stage)
            if stage == "download_progress":
                downloaded = int(payload.get("bytes_downloaded") or 0)
                total = payload.get("bytes_total")
                if isinstance(total, int) and total > 0:
                    percent = downloaded * 100 // total
                    return f"[cyan]  - {label}[/cyan] {name} [dim]{percent}%[/dim]"
                mb = downloaded / (1024 * 1024)
                return f"[cyan]  - {label}[/cyan] {name} [dim]{mb:.1f} MiB[/dim]"
            if stage == "download_failed":
                error = escape(self._compact(str(payload.get("error") or "download failed")))
                return f"[red]  - {label}[/red] {name}\n[red]    {error}[/red]"
            detail = (
                payload.get("selected_installer")
                or payload.get("url")
                or payload.get("command")
                or payload.get("message")
                or ""
            )
            suffix = f" [dim]{escape(str(detail))}[/dim]" if detail else ""
            color = "green" if stage in {"already_available", "finished"} else "cyan"
            return f"[{color}]  - {label}[/{color}] {name}{suffix}"

        def _finish_work_timer(self) -> float:
            started_at = self._turn_started_at
            if started_at is None:
                worked = self._last_worked_seconds or 0.0
            else:
                worked = max(0.0, time.monotonic() - started_at)
            self._turn_started_at = None
            self._last_worked_seconds = worked
            return worked

        def _status_line(self, status: str, usage) -> str:
            token_text, cache_text = _token_status_text(usage)
            changed = ", ".join(self.session.state.changed_files) or self.formatter.ui_text("none")
            runtime = self.session.runtime_tools
            if runtime is None:
                pending = []
            else:
                pending = [request.id for request in runtime.permission_store.pending()]
            permissions = ", ".join(str(item) for item in pending) or self.formatter.ui_text("none")
            if self._turn_running and self._turn_started_at is not None:
                status_text = f"working {_format_duration(time.monotonic() - self._turn_started_at)}"
            elif self._last_worked_seconds is not None:
                status_text = f"worked {_format_duration(self._last_worked_seconds)}"
            else:
                status_text = self.formatter.status_label(status)
            return (
                f"{self.formatter.ui_text('status')}: {status_text} | "
                f"{self.formatter.ui_text('tokens')}: {token_text} | "
                f"{self.formatter.ui_text('cache')}: {cache_text} | "
                f"{self.formatter.ui_text('changed')}: {changed} | "
                f"{self.formatter.ui_text('permissions')}: {permissions}"
            )

    return MiniCodexTui()


def _cache_status_text(usage) -> str:
    ratio = usage.cache_hit_ratio
    if ratio is None:
        return "n/a"
    hit = usage.cache_hit_prompt_tokens
    miss = usage.cache_miss_prompt_tokens
    return f"{_format_cache_ratio(ratio)} ({hit}/{hit + miss})"


def _format_duration(seconds: float | int | None) -> str:
    total = max(0, int(round(float(seconds or 0))))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _token_status_text(usage) -> tuple[str, str]:
    if usage is None:
        return "...", "n/a"
    if isinstance(usage, int):
        return str(usage), "n/a"
    return str(usage.total_tokens), _cache_status_text(usage)


def _cache_payload_text(payload: dict) -> str:
    hit = int(payload.get("cache_hit_prompt_tokens") or 0)
    miss = int(payload.get("cache_miss_prompt_tokens") or 0)
    observed = hit + miss
    if observed <= 0:
        return "n/a"
    return f"{_format_cache_ratio(hit / observed)} ({hit}/{observed})"


def _format_cache_ratio(ratio: float) -> str:
    ratio = max(0.0, min(1.0, float(ratio)))
    percent = ratio * 100
    if 0 < percent < 0.1:
        return "<0.1%"
    if 99.9 < percent < 100:
        return "<100%"
    return f"{percent:.1f}%"


def _parse_transcript_scope(args: list[str]) -> tuple[str, int | None]:
    if not args:
        return "full", None
    first = args[0].lower()
    if first == "active":
        return "active", None
    if first == "recent":
        count = _parse_positive_int(args[1] if len(args) > 1 else None, default=80)
        return "recent", count
    if first.isdigit():
        return "recent", _parse_positive_int(first, default=80)
    return "full", None


def _parse_positive_int(value: str | None, *, default: int) -> int:
    try:
        parsed = int(value or "")
    except ValueError:
        return default
    return max(1, parsed)


def _build_plain_transcript(
    messages: list[ChatMessage],
    *,
    active_messages: list[ChatMessage] | None = None,
    scope: str = "full",
    recent_count: int | None = None,
) -> str:
    if scope == "active":
        selected = active_messages if active_messages is not None else messages
    elif scope == "recent":
        selected = messages[-max(1, recent_count or 80) :]
    else:
        selected = messages
    rendered = [_format_transcript_message(message) for message in selected]
    return "\n\n".join(item for item in rendered if item).strip() + "\n"


def _format_transcript_message(message: ChatMessage) -> str:
    role = message.role
    name = f":{message.name}" if message.name else ""
    header = f"{role}{name}"
    content = message.content
    image = message.metadata.get("image")
    if isinstance(image, dict):
        image_path = image.get("path") or "unknown"
        content = f"{content}\n[image attached: {image_path}]"
    tool_calls = message.metadata.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        content = f"{content}\n[tool_calls: {len(tool_calls)}]"
    return f"{header}:\n{content.strip()}"


def _copy_text_to_clipboard(text: str) -> None:
    if platform.system().lower() == "windows":
        _copy_text_to_clipboard_windows(text)
        return
    _copy_text_to_clipboard_tk(text)


def _copy_text_to_clipboard_windows(text: str) -> None:
    # Tk handles Unicode well on Windows and avoids code-page surprises from clip.exe.
    try:
        _copy_text_to_clipboard_tk(text)
        return
    except Exception:
        pass
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Set-Clipboard -Value ([Console]::In.ReadToEnd())",
        ],
        input=text,
        text=True,
        capture_output=True,
        timeout=5,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Set-Clipboard failed")


def _copy_text_to_clipboard_tk(text: str) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
    finally:
        root.destroy()


def _write_transcript_export(workspace_root: str, session_id: str, transcript: str) -> Path:
    export_dir = Path(workspace_root) / ".minicodex2" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    safe_session_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in session_id)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = export_dir / f"chat_{safe_session_id}_{stamp}.txt"
    path.write_text(transcript, encoding="utf-8")
    return path

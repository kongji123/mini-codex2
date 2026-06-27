from __future__ import annotations

import json

from minicodex2.agent.chat_history import ChatHistory
from minicodex2.agent.unified_session import UnifiedAgentSession
from minicodex2.config.settings import ConfigLoader
from minicodex2.history.jsonl_store import JsonlHistoryStore
from minicodex2.model.fake_adapter import FakeModelAdapter
from minicodex2.model.openai_compatible import OpenAICompatibleModelAdapter
from minicodex2.tools.registry import build_runtime_tool_registry
from minicodex2.tools.runtime_tools import RuntimeTools


GOAL_METADATA_KEYS = {
    "active_goal",
    "goal_status",
    "goal_token_budget",
    "objective",
    "status",
    "token_budget",
    "acceptance_evidence",
    "evidence_records",
    "runtime_resources",
    "work_plan",
    "work_plan_sources",
    "current_step_id",
    "archived_goals",
    "engineering_facts",
    "lessons",
    "workspace_entries",
    "read_records",
    "change_records",
}


def build_session(
    workspace: str,
    *,
    api_key: str | None = None,
    model_profile: str | None = None,
    model_name: str | None = None,
    base_url: str | None = None,
    wire_api: str | None = None,
    resume: bool = False,
    autosave: bool = False,
) -> UnifiedAgentSession:
    settings = ConfigLoader().load(
        workspace,
        api_key=api_key,
        model_profile=model_profile,
        model=model_name,
        base_url=base_url,
        wire_api=wire_api,
    )
    runtime = RuntimeTools(settings)
    tools = build_runtime_tool_registry(runtime)
    if settings.model.api_key:
        model = OpenAICompatibleModelAdapter(
            base_url=settings.model.base_url,
            api_key=settings.model.api_key,
            model=settings.model.model,
            wire_api=settings.model.wire_api,
            timeout_seconds=settings.model.timeout_seconds,
        )
    else:
        model = FakeModelAdapter()
    history = ChatHistory()
    session_id = None
    metadata = None
    if resume:
        store = JsonlHistoryStore(settings.workspace_root)
        latest = store.latest_session_id()
        if latest:
            history = store.load_history(latest)
            metadata = store.load_metadata(latest)
            session_id = latest
    session = UnifiedAgentSession(
        settings=settings,
        model=model,
        tools=tools,
        history=history,
        session_id=session_id,
        checkpoint_callback=save_session if autosave else None,
        runtime_tools=runtime,
    )
    if metadata:
        if "idle_tick_enabled" in metadata:
            session.settings.agent.idle_tick_enabled = bool(metadata["idle_tick_enabled"])
        session.state.load_goal_snapshot(metadata)
    return session


def save_session(session: UnifiedAgentSession) -> None:
    metadata = {
        "workspace_root": str(session.settings.workspace_root),
        "model": session.settings.model.model,
        "permission_mode": session.settings.permission_mode,
        "result_state": session.state.result_state,
        "active_goal": session.state.active_goal,
        "goal_status": session.state.goal_status,
        "goal_token_budget": session.state.goal_token_budget,
        "objective": session.state.active_goal,
        "status": session.state.goal_status,
        "token_budget": session.state.goal_token_budget,
        "acceptance_evidence": session.state.acceptance_evidence,
        "evidence_records": [item.to_dict() for item in session.state.evidence_records],
        "runtime_resources": [item.to_dict() for item in session.state.runtime_resources],
        "work_plan": [item.to_dict() for item in session.state.work_plan],
        "work_plan_sources": [item.to_dict() for item in session.state.work_plan_sources],
        "current_step_id": session.state.current_step_id,
        "archived_goals": session.state.archived_goals,
        "engineering_facts": [item.to_dict() for item in session.state.engineering_facts],
        "lessons": [item.to_dict() for item in session.state.lessons],
        "workspace_entries": [item.to_dict() for item in session.state.workspace_entries],
        "read_records": [item.to_dict() for item in session.state.read_records],
        "change_records": [item.to_dict() for item in session.state.change_records],
        "idle_tick_enabled": session.settings.agent.idle_tick_enabled,
    }
    store = JsonlHistoryStore(session.settings.workspace_root)
    metadata = _preserve_existing_goal_metadata(store, session.session_id, metadata)
    store.save(
        session.session_id,
        session.history,
        metadata,
    )
    if _metadata_has_goal(metadata):
        _goal_snapshot_path(store, session.session_id).write_text(
            json.dumps({key: metadata.get(key) for key in GOAL_METADATA_KEYS if key in metadata}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _metadata_has_goal(metadata: dict[str, object]) -> bool:
    return bool(metadata.get("objective") or metadata.get("active_goal") or metadata.get("work_plan"))


def _preserve_existing_goal_metadata(
    store: JsonlHistoryStore,
    session_id: str,
    metadata: dict[str, object],
) -> dict[str, object]:
    """Avoid erasing durable goal memory when a transient empty session saves.

    A side task or resumed UI can legitimately have no goal loaded yet. That
    must not overwrite an existing active work plan unless a future explicit
    clear-goal operation records that intent.
    """
    if _metadata_has_goal(metadata):
        return metadata
    previous = _load_previous_goal_metadata(store, session_id)
    if not previous or not _metadata_has_goal(previous):
        return metadata
    merged = dict(metadata)
    for key in GOAL_METADATA_KEYS:
        if key in previous:
            merged[key] = previous[key]
    return merged


def _load_previous_goal_metadata(store: JsonlHistoryStore, session_id: str) -> dict[str, object] | None:
    candidates: list[dict[str, object]] = []
    try:
        candidates.append(store.load_metadata(session_id))
    except Exception:
        pass
    snapshot_path = _goal_snapshot_path(store, session_id)
    try:
        candidates.append(json.loads(snapshot_path.read_text(encoding="utf-8")))
    except Exception:
        pass
    for candidate in candidates:
        if _metadata_has_goal(candidate):
            return candidate
    return None


def _goal_snapshot_path(store: JsonlHistoryStore, session_id: str):
    return store.sessions_dir / f"{session_id}.goal_snapshot.json"


def main() -> None:
    try:
        import typer
    except ImportError:
        _argparse_main()
        return

    app = typer.Typer()
    benchmark_app = typer.Typer()

    @app.command()
    def run(
        message: str,
        workspace: str = typer.Option(".", "--workspace", "-w"),
        api_key: str | None = typer.Option(None, "--api-key"),
        model_profile: str | None = typer.Option(None, "--model-profile"),
        model: str | None = typer.Option(None, "--model"),
        base_url: str | None = typer.Option(None, "--base-url"),
        wire_api: str | None = typer.Option(None, "--wire-api"),
        allow_advice: bool = typer.Option(True, "--allow-advice/--no-advice"),
    ) -> None:
        session = build_session(
            workspace,
            api_key=api_key,
            model_profile=model_profile,
            model_name=model,
            base_url=base_url,
            wire_api=wire_api,
        )
        result = session.run_turn(message, allow_advice=allow_advice)
        typer.echo(f"[{result.status}] {result.response}")

    @app.command()
    def chat(
        workspace: str = typer.Option(".", "--workspace", "-w"),
        api_key: str | None = typer.Option(None, "--api-key"),
        model_profile: str | None = typer.Option(None, "--model-profile"),
        model: str | None = typer.Option(None, "--model"),
        base_url: str | None = typer.Option(None, "--base-url"),
        wire_api: str | None = typer.Option(None, "--wire-api"),
    ) -> None:
        session = build_session(
            workspace,
            api_key=api_key,
            model_profile=model_profile,
            model_name=model,
            base_url=base_url,
            wire_api=wire_api,
        )
        typer.echo(f"MiniCodex2 session {session.session_id}. Type 'exit' to quit.")
        while True:
            text = typer.prompt(">")
            if text.strip().lower() in {"exit", "quit"}:
                break
            result = session.run_turn(text)
            typer.echo(f"[{result.status}] {result.response}")
        save_session(session)

    @app.command()
    def tui(
        workspace: str = typer.Option(".", "--workspace", "-w"),
        api_key: str | None = typer.Option(None, "--api-key"),
        model_profile: str | None = typer.Option(None, "--model-profile"),
        model: str | None = typer.Option(None, "--model"),
        base_url: str | None = typer.Option(None, "--base-url"),
        wire_api: str | None = typer.Option(None, "--wire-api"),
    ) -> None:
        from minicodex2.tui.app import run_tui

        run_tui(
            workspace,
            api_key=api_key,
            model_profile=model_profile,
            model_name=model,
            base_url=base_url,
            wire_api=wire_api,
        )

    @benchmark_app.command("run")
    def benchmark_run(
        suite: str = typer.Option("smoke", "--suite"),
        output: str = typer.Option(".minicodex2/benchmarks/latest", "--output"),
        model_mode: str = typer.Option("fake", "--model-mode"),
        api_key: str | None = typer.Option(None, "--api-key"),
        model_profile: str | None = typer.Option(None, "--model-profile"),
        model: str | None = typer.Option(None, "--model"),
        base_url: str | None = typer.Option(None, "--base-url"),
        wire_api: str | None = typer.Option(None, "--wire-api"),
    ) -> None:
        from minicodex2.benchmark.cases import built_in_suite
        from minicodex2.benchmark.runner import BenchmarkRunner

        runner = BenchmarkRunner(
            output,
            model_mode=model_mode,  # type: ignore[arg-type]
            api_key=api_key,
            model_profile=model_profile,
            model_name=model,
            base_url=base_url,
            wire_api=wire_api,
        )
        try:
            result = runner.run_suite(suite, built_in_suite(suite))
        finally:
            runner.close()
        typer.echo(result.to_markdown())
        typer.echo(f"JSON: {runner.output_root / 'benchmark-report.json'}")
        typer.echo(f"Markdown: {runner.output_root / 'benchmark-report.md'}")

    app.add_typer(benchmark_app, name="benchmark")
    app()


def _argparse_main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("message", nargs="?", default=None)
    parser.add_argument("--workspace", "-w", default=".")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--model-profile", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--wire-api", default=None)
    args = parser.parse_args()
    session = build_session(
        args.workspace,
        api_key=args.api_key,
        model_profile=args.model_profile,
        model_name=args.model,
        base_url=args.base_url,
        wire_api=args.wire_api,
    )
    if args.message:
        print(session.run_turn(args.message).response)
        return
    print(f"MiniCodex2 session {session.session_id}. Type 'exit' to quit.")
    while True:
        text = input("> ")
        if text.strip().lower() in {"exit", "quit"}:
            break
        result = session.run_turn(text)
        print(f"[{result.status}] {result.response}")


if __name__ == "__main__":
    main()

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from minicodex2.agent.chat_history import ChatHistory
from minicodex2.agent.unified_session import UnifiedAgentSession
from minicodex2.config.settings import ConfigLoader
from minicodex2.model.fake_adapter import FakeModelAdapter
from minicodex2.model.openai_compatible import OpenAICompatibleModelAdapter
from minicodex2.tools.registry import build_runtime_tool_registry
from minicodex2.tools.runtime_tools import RuntimeTools


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, UnifiedAgentSession] = {}
        self.runtime_tools: dict[str, RuntimeTools] = {}

    def create_session(self, payload: dict[str, Any]) -> UnifiedAgentSession:
        model_config = payload.get("model") or {}
        settings = ConfigLoader().load(
            payload["workspace_root"],
            api_key=model_config.get("api_key"),
            model_profile=model_config.get("profile") or model_config.get("model_profile"),
            model=model_config.get("name") or model_config.get("model"),
            base_url=model_config.get("base_url"),
            wire_api=model_config.get("wire_api"),
            permission_mode=payload.get("permission_mode"),
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
        session = UnifiedAgentSession(
            settings=settings,
            model=model,
            tools=tools,
            history=ChatHistory(),
            runtime_tools=runtime,
        )
        self.sessions[session.session_id] = session
        self.runtime_tools[session.session_id] = runtime
        return session

    def get(self, session_id: str) -> UnifiedAgentSession:
        return self.sessions[session_id]

    def runtime_for_permission(self, request_id: str) -> RuntimeTools | None:
        for runtime in self.runtime_tools.values():
            if any(req.id == request_id for req in runtime.permission_store.pending()):
                return runtime
        return None

    def session_for_permission(self, request_id: str) -> UnifiedAgentSession | None:
        for session_id, runtime in self.runtime_tools.items():
            if any(req.id == request_id for req in runtime.permission_store.pending()):
                return self.sessions[session_id]
        return None


manager = SessionManager()


def create_app():
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("FastAPI is not installed. Install minicodex2[api].") from exc

    app = FastAPI(title="MiniCodex2 Local API")

    @app.post("/sessions")
    def create_session(payload: dict[str, Any]) -> dict[str, Any]:
        session = manager.create_session(payload)
        return {
            "session_id": session.session_id,
            "workspace_root": str(session.settings.workspace_root),
            "permission_mode": session.settings.permission_mode,
            "status": "ready",
        }

    @app.post("/sessions/{session_id}/messages")
    def send_message(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            session = manager.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        result = session.run_turn(payload["content"], allow_advice=payload.get("allow_advice", True))
        return {"status": result.status, "response": result.response}

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        try:
            session = manager.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        usage = session.token_usage.total()
        return {
            "session_id": session.session_id,
            "status": session.state.result_state,
            "messages": [m.to_model_dict() for m in session.history.messages()],
            "pending_permissions": [
                request.to_dict()
                for request in manager.runtime_tools[session_id].permission_store.pending()
            ],
            "token_usage": asdict(usage),
            "verification": {"status": session.state.result_state},
        }

    @app.get("/sessions/{session_id}/events")
    def get_events(session_id: str) -> dict[str, Any]:
        try:
            session = manager.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        return {"events": [event.to_dict() for event in session.event_bus.events()]}

    @app.post("/permissions/{request_id}/approve")
    def approve_permission(request_id: str) -> dict[str, Any]:
        session = manager.session_for_permission(request_id)
        runtime = manager.runtime_for_permission(request_id)
        if runtime is None:
            return {"request_id": request_id, "status": "not_found"}
        request = runtime.permission_store.resolve(request_id, True)
        if session:
            session.event_bus.emit(
                "permission_resolved",
                {"permission_request_id": request_id, "approved": True},
            )
        return request.to_dict()

    @app.post("/permissions/{request_id}/deny")
    def deny_permission(request_id: str) -> dict[str, Any]:
        session = manager.session_for_permission(request_id)
        runtime = manager.runtime_for_permission(request_id)
        if runtime is None:
            return {"request_id": request_id, "status": "not_found"}
        request = runtime.permission_store.resolve(request_id, False)
        if session:
            session.event_bus.emit(
                "permission_resolved",
                {"permission_request_id": request_id, "approved": False},
            )
        return request.to_dict()

    return app


app = create_app()

from __future__ import annotations

from pathlib import Path

import pytest


def test_api_create_session_and_send_message(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from minicodex2.api.server import create_app

    client = TestClient(create_app())
    response = client.post("/sessions", json={"workspace_root": str(tmp_path)})
    assert response.status_code == 200
    session_id = response.json()["session_id"]
    message = client.post(
        f"/sessions/{session_id}/messages",
        json={"content": "hello", "allow_advice": False},
    )
    assert message.status_code == 200
    state = client.get(f"/sessions/{session_id}").json()
    assert "token_usage" in state
    events = client.get(f"/sessions/{session_id}/events").json()
    assert events["events"]


def test_api_permission_approve_deny(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from minicodex2.api.server import create_app
    from minicodex2.model.fake_adapter import FakeStep
    from minicodex2.model.messages import ToolCall

    # Use manager internals to install a deterministic tool-calling model for this session.
    from minicodex2.api import server
    from minicodex2.model.fake_adapter import FakeModelAdapter

    client = TestClient(create_app())
    response = client.post(
        "/sessions",
        json={"workspace_root": str(tmp_path), "permission_mode": "guarded"},
    )
    session_id = response.json()["session_id"]
    server.manager.sessions[session_id].model = FakeModelAdapter(
        [FakeStep("write", [ToolCall("call_1", "write_file", {"path": "x.txt", "content": "x"})])]
    )
    message = client.post(
        f"/sessions/{session_id}/messages",
        json={"content": "write", "allow_advice": False},
    )
    assert message.status_code == 200
    state = client.get(f"/sessions/{session_id}").json()
    request_id = state["pending_permissions"][0]["id"]
    approved = client.post(f"/permissions/{request_id}/approve").json()
    assert approved["status"] == "approved"
    events = client.get(f"/sessions/{session_id}/events").json()["events"]
    assert any(
        event["type"] == "permission_resolved"
        and event["payload"]["permission_request_id"] == request_id
        and event["payload"]["approved"] is True
        for event in events
    )

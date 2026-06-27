# Local API Server

This document defines the first-version Local API Server for MiniCodex2.

本文档定义 MiniCodex2 第一版 Local API Server。

## Decision

The Local API Server is required in v0.1.

Local API Server 是 v0.1 必做模块。

It should be a thin entry layer over `UnifiedAgentSession`, not a duplicate implementation of the agent loop.

它应该是 `UnifiedAgentSession` 之上的薄入口层，而不是 Agent 主循环的重复实现。

## Why It Exists

Reasons:

原因：

- It provides a clean integration testing boundary.
  它提供清晰的集成测试边界。
- It makes TUI integration easier.
  它方便后续接入 TUI。
- It prepares for a future desktop UI.
  它为未来桌面 UI 做准备。
- It separates UI rendering from core agent execution.
  它将 UI 渲染和核心 Agent 执行解耦。

## First-Version Scope

Required endpoints:

必需接口：

```text
POST /sessions
POST /sessions/{id}/messages
GET  /sessions/{id}
GET  /sessions/{id}/events
POST /permissions/{request_id}/approve
POST /permissions/{request_id}/deny
```

## API Shape

### POST /sessions

Create a new local agent session.

创建新的本地 Agent session。

Request:

```json
{
  "workspace_root": "D:/path/to/project",
  "permission_mode": "auto",
  "max_repair_rounds": 3,
  "model": {
    "provider": "openai_compatible",
    "base_url": "https://api.openai.com/v1",
    "name": "default",
    "api_key": "<runtime-only-secret>"
  }
}
```

`model.api_key` is runtime-only and must not be persisted or emitted in events.

`model.api_key` 仅限运行时使用，不得持久化或写入 events。

For DeepSeek or another OpenAI-compatible service:

DeepSeek 或其他 OpenAI-compatible 服务示例：

```json
{
  "workspace_root": "D:/path/to/project",
  "model": {
    "provider": "openai_compatible",
    "base_url": "https://api.deepseek.com",
    "name": "deepseek-v4-flash",
    "api_key": "<runtime-only-secret>"
  }
}
```

Response:

```json
{
  "session_id": "session_123",
  "workspace_root": "D:/path/to/project",
  "permission_mode": "auto",
  "status": "ready"
}
```

### POST /sessions/{id}/messages

Send one user message.

发送一条用户消息。

Request:

```json
{
  "content": "Add a CLI option and run tests."
}
```

Response:

```json
{
  "turn_id": "turn_123",
  "status": "running"
}
```

The first implementation may run synchronously for tests or asynchronously for UI use. The response shape should leave room for both.

第一版实现可以为了测试同步运行，也可以为了 UI 异步运行。响应结构应为两种模式都保留空间。

### GET /sessions/{id}

Return current session state.

返回当前 session 状态。

Response should include:

响应应包含：

```json
{
  "session_id": "session_123",
  "status": "ready | running | waiting_for_permission | passed | blocked",
  "messages": [],
  "pending_permissions": [],
  "token_usage": {
    "prompt_tokens": 1000,
    "completion_tokens": 200,
    "total_tokens": 1200,
    "estimated": false
  },
  "verification": {
    "status": "not_run | running | passed | failed | blocked",
    "last_command": "python -m pytest"
  }
}
```

### GET /sessions/{id}/events

Return runtime events.

返回 Runtime events。

First version may use polling:

第一版可以使用轮询：

```json
{
  "events": [
    {
      "id": "event_1",
      "type": "tool_call_started",
      "timestamp": "2026-01-01T00:00:00Z",
      "payload": {}
    }
  ]
}
```

Later versions may add Server-Sent Events or WebSocket.

后续版本可以增加 Server-Sent Events 或 WebSocket。

### POST /permissions/{request_id}/approve

Approve a pending permission request.

批准一个待处理权限请求。

### POST /permissions/{request_id}/deny

Deny a pending permission request.

拒绝一个待处理权限请求。

## Event Types

Suggested event types:

建议事件类型：

```text
session_created
turn_started
model_call_started
model_call_finished
tool_call_started
tool_call_finished
permission_requested
permission_resolved
verification_started
verification_step_started
verification_step_finished
verification_passed
verification_failed
failure_pack_created
repair_round_started
turn_finished
blocked
```

## Tests

Required tests:

必需测试：

- `test_api_create_session`
- `test_api_send_message`
- `test_api_get_session_state`
- `test_api_get_events`
- `test_api_permission_approve_deny`
- `test_api_state_includes_token_usage`
- `test_api_state_includes_verification_status`

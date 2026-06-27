# Agent Events

This document defines the structured runtime event contract for MiniCodex2.

本文档定义 MiniCodex2 的结构化运行时事件契约。

## Decision

MiniCodex2 emits `AgentEvent` records from the core runtime. CLI, Local API, TUI, and future desktop UI should consume the same event stream instead of reconstructing state independently.

MiniCodex2 由核心 Runtime 发出 `AgentEvent` 记录。CLI、Local API、TUI 和未来桌面 UI 应消费同一套事件流，而不是各自重新推断状态。

## Event Shape

事件结构：

```json
{
  "id": "event_123",
  "session_id": "session_123",
  "turn_id": "turn_123",
  "type": "tool_call_started",
  "timestamp": "2026-01-01T00:00:00Z",
  "payload": {}
}
```

## Event Types

Implemented in v0.1:

v0.1 已实现：

```text
session_created
turn_started
context_built
model_call_started
model_call_finished
token_usage_recorded
tool_call_started
tool_call_finished
permission_requested
permission_resolved
file_changed
verification_started
verification_plan_created
verification_step_started
verification_step_finished
verification_passed
verification_failed
failure_pack_created
repair_round_started
background_command_started
background_command_ready
background_command_failed
turn_finished
blocked
collaboration_advice_created
```

Future event types may include finer-grained streaming model deltas, command stdout chunks, and desktop UI lifecycle events. These are intentionally not required for v0.1.

后续可以扩展模型流式输出、命令 stdout 分片、桌面 UI 生命周期等更细事件。v0.1 不强制要求这些事件。

## Payload Policy

Events must stay compact.

事件 payload 必须保持紧凑。

Allowed payload fields include:

允许的 payload 字段包括：

- Tool name.
  工具名称。
- Command string.
  命令字符串。
- Status and exit code.
  状态和退出码。
- Short output excerpt.
  短输出摘要。
- Changed files.
  变更文件。
- Token usage summary.
  token 用量摘要。
- Verification status.
  验证状态。
- Log path.
  日志路径。

Large raw logs must not be embedded in event payloads. Store logs under `.minicodex2/logs/` and put only paths or short excerpts in events.

大型原始日志不得直接塞入 event payload。日志应存入 `.minicodex2/logs/`，事件中只放路径或短摘要。

## UI Usage

The TUI should render the event stream directly:

TUI 应直接渲染事件流：

- `model_call_started` / `model_call_finished` show model activity.
  `model_call_started` / `model_call_finished` 展示模型调用状态。
- `tool_call_started` / `tool_call_finished` show tool progress.
  `tool_call_started` / `tool_call_finished` 展示工具执行进度。
- `verification_step_started` / `verification_step_finished` show validation progress.
  `verification_step_started` / `verification_step_finished` 展示验证进度。
- `background_command_ready` shows server readiness.
  `background_command_ready` 展示服务已就绪。
- `failure_pack_created` and `blocked` explain why the loop stopped.
  `failure_pack_created` 和 `blocked` 解释主循环停止原因。

## Tests

Required tests:

必须测试：

- `test_event_bus_records_events`
- `test_tool_call_emits_events`
- `test_verification_emits_events`
- `test_token_usage_emits_event`
- `test_events_do_not_include_large_logs`
- `test_api_permission_approve_deny`

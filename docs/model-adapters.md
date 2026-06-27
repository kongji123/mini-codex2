# Model Adapters

This document defines the model adapter strategy for MiniCodex2.

本文档定义 MiniCodex2 的模型适配器策略。

## Decision

MiniCodex2 should start with `ModelAdapter` plus `FakeModelAdapter`, then add a real model adapter after the deterministic core loop passes.

MiniCodex2 应先实现 `ModelAdapter` 和 `FakeModelAdapter`，在确定性核心闭环通过后，再接入真实模型 adapter。

The first real adapter should support OpenAI-compatible tool calling, while keeping the core `ModelAdapter` interface provider-neutral.

第一版真实 adapter 应支持 OpenAI-compatible tool calling，但核心 `ModelAdapter` 接口保持供应商无关。

Recommended first adapter name:

建议的第一版 adapter 名称：

```text
OpenAICompatibleModelAdapter
```

This adapter should support official OpenAI endpoints, DeepSeek-style OpenAI-compatible endpoints, and local OpenAI-compatible servers when their tool-calling behavior is compatible.

该 adapter 应支持官方 OpenAI endpoint、DeepSeek 类 OpenAI-compatible endpoint，以及工具调用行为兼容的本地 OpenAI-compatible server。

## Why FakeModelAdapter Comes First

`FakeModelAdapter` makes core behavior testable.

`FakeModelAdapter` 让核心行为可测试。

It should support scripted responses for:

它应支持脚本化响应：

- Normal chat with no tools.
  无工具的普通聊天。
- Tool calls such as `list_directory` and `read_file`.
  调用 `list_directory`、`read_file` 等工具。
- File writes.
  文件写入。
- Verification failure repair.
  验证失败后的修复。
- Blocked flows.
  blocked 流程。

This allows tests to verify the agent loop without depending on model nondeterminism.

这样测试可以验证 Agent 主循环，而不依赖真实模型的不确定性。

## Why Real Model Testing Is Still Required

Real models expose issues FakeModel cannot:

真实模型会暴露 FakeModel 无法暴露的问题：

- Tool schema ambiguity.
  工具 schema 表达不清。
- Context summary is too verbose or too sparse.
  上下文摘要过长或信息不足。
- Model chooses `run_command` for long-running servers.
  模型把长驻服务放进 `run_command`。
- Model fails to use `FailurePack` correctly.
  模型没有正确使用 `FailurePack`。
- Model needs clearer runtime instructions.
  模型需要更清晰的 Runtime 指令。

Therefore, once the fake-model suite passes, MiniCodex2 should run real-model smoke tests and feed the results back into the runtime design.

因此，在 FakeModel 测试通过后，MiniCodex2 应运行真实模型 smoke tests，并将发现的问题反馈到 Runtime 设计。

## Required Adapter Interface

The interface should hide provider details:

接口应隐藏供应商细节：

```text
ModelAdapter.complete(ModelRequest) -> ModelResponse
```

`ModelRequest` should include:

`ModelRequest` 应包含：

- Messages.
  消息。
- Tool schemas.
  工具 schema。
- Runtime context.
  Runtime 上下文。
- Model config.
  模型配置。

The model config should be resolved by `ConfigLoader`.

模型配置应由 `ConfigLoader` 解析。

API credentials should be supplied at runtime, preferably through CLI arguments or Local API session config in v0.1. Environment variables such as `OPENAI_API_KEY` can remain an optional fallback.

API 凭据应在运行时提供。v0.1 优先支持通过 CLI 参数或 Local API session config 传入；`OPENAI_API_KEY` 等环境变量可以作为可选 fallback。

Credentials must not be persisted in project config, history, logs, events, or failure packs.

凭据不得持久化到项目配置、历史、日志、事件或 FailurePack 中。

OpenAI-compatible config:

OpenAI-compatible 配置：

```text
provider: openai_compatible
base_url: configurable
model: configurable
api_key: runtime-only secret
```

The adapter should not assume that every OpenAI-compatible provider has identical behavior.

Adapter 不应假设所有 OpenAI-compatible provider 的行为完全一致。

Real-model smoke tests should detect:

真实模型 smoke tests 应检测：

- Tool-calling support.
  工具调用支持。
- Usage reporting availability.
  token usage 返回能力。
- Error response compatibility.
  错误响应兼容性。
- Parallel tool call behavior.
  并行工具调用行为。

`ModelResponse` should include:

`ModelResponse` 应包含：

- Assistant message.
  Assistant 消息。
- Tool calls.
  工具调用。
- Usage data.
  token 用量。
- Finish reason.
  结束原因。
- Raw provider metadata when useful.
  有用时保留原始供应商元数据。

## Test Strategy

Deterministic tests:

确定性测试：

- Must use `FakeModelAdapter`.
  必须使用 `FakeModelAdapter`。
- Must run in normal CI.
  必须在普通 CI 中运行。
- Must not require credentials.
  不需要凭据。

Real-model smoke tests:

真实模型 smoke tests：

- May require credentials.
  可以需要凭据。
- Should be skipped when credentials are unavailable.
  没有凭据时应跳过。
- Should cover chat, read tools, small edits, and failure repair.
  应覆盖聊天、读工具、小编辑和失败修复。
- Should be used to refine schemas and context.
  应用于改进 schema 和上下文。

## Implementation Order

1. Implement `ModelAdapter`.
   实现 `ModelAdapter`。
2. Implement `FakeModelAdapter`.
   实现 `FakeModelAdapter`。
3. Pass deterministic loop tests.
   通过确定性主循环测试。
4. Implement real model adapter.
   实现真实模型 adapter。
5. Run real-model smoke tests.
   运行真实模型 smoke tests。
6. Fix issues found by real-model behavior.
   修复真实模型行为暴露的问题。

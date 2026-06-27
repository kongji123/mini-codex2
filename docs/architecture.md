# MiniCodex2 Architecture

This document describes the v0.1 architecture and module boundaries.

本文档描述 MiniCodex2 v0.1 的架构和模块边界。

## Core Architecture

```text
CLI / TUI / Local API Server
        |
UnifiedAgentSession
        |
+--------------------------+
| Agent OS                 |
| - ChatHistory            |
| - ContextManager         |
| - TokenUsageTracker      |
| - GoalController         |
| - AgentEventBus          |
| - ToolRegistry           |
| - RuntimeTools           |
|                          |
| Code Skill               |
| - ProjectDetector        |
| - VerificationPlanBuilder|
| - VerificationRunner     |
| - FailurePack            |
|                          |
| Engineering Skill        |
| - CollaborationAdvisor   |
+--------------------------+
        |
Local Project Workspace
```

## Layered Runtime Direction

MiniCodex2 keeps one user-facing session loop, but the internals are now split by reuse boundary:

MiniCodex2 仍保持单一用户主会话循环，但内部按复用边界分层：

```text
Agent OS:
  session state, goal controller, history, context, events, token usage, tools, permissions

Code Skill:
  project detection, verification planning, verification execution, code failure packs

Engineering Skill:
  complexity advice, design-first guidance, future project planning and acceptance audit

Apps:
  CLI, TUI, Local API, future desktop client
```

```text
Agent OS:
  session state、goal controller、history、context、events、token usage、tools、permissions

Code Skill:
  项目检测、验证计划、验证执行、代码失败包

Engineering Skill:
  复杂度建议、设计优先提示、未来项目计划和验收审计

Apps:
  CLI、TUI、Local API、未来桌面客户端
```

The current implementation keeps compatibility wrappers in the older packages, but new generic runtime behavior should move toward `minicodex2.agent_os`, and code-domain behavior should move toward `minicodex2.skills.code`.

当前实现保留旧包路径兼容，但新的通用 Runtime 行为应逐步进入 `minicodex2.agent_os`，代码领域行为应逐步进入 `minicodex2.skills.code`。

## Goal Controller

The normal execution loop handles model calls, tool calls, and verification. Large project development needs goal control because "verification passed" only proves the current change is valid; it does not prove the whole project goal is complete.

普通执行循环处理模型调用、工具调用和验证。大项目开发需要 goal control，因为“验证通过”只能证明当前改动有效，不能证明整个项目目标完成。

This is not a second physical `while` loop. It is a controller called by the single execution loop to decide whether to continue, finish, or ask the model for concrete progress.

这不是第二个物理 `while` 循环，而是由单一执行循环调用的控制器，用来判断继续、结束，还是要求模型推进具体工作。

Goal state must be model-driven or user-command-driven, not keyword-driven:

Goal 状态必须由模型或用户显式命令驱动，不能由关键词驱动：

- The model may call `create_goal` when the prompt and user request indicate a durable objective.
- The model may call `get_goal` to inspect current structured goal state.
- The model may call `update_goal` only to mark a goal `complete` or genuinely `blocked`.
- The model may call `set_work_plan`, `update_work_step`, and `set_current_step` to maintain a structured work plan under the active goal.
- The CLI/TUI/API may later expose explicit user commands such as `/goal`.
- Runtime validates goal state transitions and persists structured goal state.
- Runtime must not infer goals by matching natural-language keywords such as "project", "continue", or "implement".

- 当 prompt 和用户请求表明存在持久目标时，模型可以调用 `create_goal`。
- 模型可以调用 `get_goal` 查看当前结构化 goal 状态。
- 模型只能通过 `update_goal` 将 goal 标记为 `complete` 或真正 `blocked`。
- 模型可以调用 `set_work_plan`、`update_work_step` 和 `set_current_step`，在 active goal 下维护结构化工作计划。
- CLI/TUI/API 后续可以暴露 `/goal` 这类用户显式命令。
- Runtime 负责校验 goal 状态转换并持久化结构化 goal 状态。
- Runtime 不得通过匹配“项目”、“继续”、“实现”等自然语言关键词来推断 goal。

Structured goal state has three layers:

结构化 goal 状态分为三层：

```text
RootObjective:
  The durable user objective. It is anchored by create_goal and should not be changed by the model without explicit user intent.

WorkPlan:
  Model-maintained steps under the root objective. Steps can be pending, in_progress, done, or blocked, with acceptance notes and evidence.

CurrentStep:
  The focused step for the current loop. Runtime injects it into context as working memory so the model does not depend on long chat history.
```

```text
RootObjective:
  持久用户目标。由 create_goal 锚定，模型不应在没有用户明确意图时修改。

WorkPlan:
  RootObjective 下由模型维护的步骤。步骤状态可以是 pending、in_progress、done 或 blocked，并带有验收说明和证据。

CurrentStep:
  当前循环聚焦的小步骤。Runtime 会把它作为工作记忆注入上下文，避免模型依赖冗长聊天历史。
```

This structure stores task memory. It does not replace model intelligence. If the state is complete and visible but the model chooses the wrong next step, that is a model-quality issue. If the state is missing, stale, or hidden by compression, that is a runtime issue.

这个结构用于存储任务记忆，不替代模型智能。如果状态完整且可见，但模型仍选择错误步骤，那是模型质量问题；如果状态缺失、过期或被压缩隐藏，那是 Runtime 问题。

### TurnScope And GoalEngagement

An active goal existing in the session is not the same thing as the latest user turn engaging that goal. MiniCodex2 separates these concepts with a small model-classified `TurnScopeDecision`.

会话中存在 active goal，不等于最新用户回合正在推进这个 goal。MiniCodex2 用一个由模型判定的小型 `TurnScopeDecision` 来分开这两个概念。

`TurnScopeDecision` is not an intent router. It does not choose business tools or replace the unified loop. It only tells the runtime whether the current turn may use goal-continuation gates.

`TurnScopeDecision` 不是 intent router。它不选择业务工具，也不替代统一主循环。它只告诉 Runtime：当前回合是否允许使用 goal continuation gate。

```text
turn_scope:
  chat_only
  answer_with_tools
  side_task
  continue_current_goal
  modify_current_goal
  create_new_goal
  pause_goal
  complete_goal

goal_engagement:
  none
  background_only
  engaged
  update_only
```

Runtime rule:

Runtime 规则：

```text
goal_status == active
  means: inject durable goal/work-plan state as background memory.

goal_engagement == engaged
  means: this turn may trigger continue_project_build, plan review, and verified-step continuation.
```

Normal project questions such as "does this project have AGENTS.md?" should be classified as `answer_with_tools + background_only`: tools may be used to answer the question, but the older goal must not steal the turn.

普通项目问题，例如“这个项目有没有 AGENTS.md？”，应被判定为 `answer_with_tools + background_only`：可以调用工具回答问题，但旧的长期目标不能抢走本轮。

Explicit continuation such as "continue the current project goal" should be classified as `continue_current_goal + engaged`: now the runtime may apply goal/work-plan continuation discipline.

明确继续，例如“继续当前项目目标”，应被判定为 `continue_current_goal + engaged`：此时 Runtime 才可以应用 goal/work-plan 的持续推进规则。

The classifier is model-driven. Runtime only validates the returned enum values and falls back conservatively when classification fails.

这个分类由模型负责。Runtime 只校验返回的枚举值，并在分类失败时保守降级。

This is intentionally smaller than a full project manager. Future versions can attach task lists, audit acceptance criteria, and expose richer goal operations through CLI/TUI/API.

这有意比完整项目管理器更小。后续版本可以绑定 task list、审计验收标准，并通过 CLI/TUI/API 暴露更完整的 goal 操作。

The implementation must follow MVP discipline: build a small formal slice, but do not use temporary special-case rules inside Agent OS boundaries.

实现必须遵守 MVP 纪律：可以先做小范围正式切片，但不能在 Agent OS 边界内使用临时特殊规则。

## Implementation Stack

Default v0.1 stack:

v0.1 默认技术栈：

```text
Runtime: Python
Package: pyproject.toml
Tests: pytest
Lint: ruff
Local API: FastAPI
CLI: Typer
TUI later: Textual
```

The implementation should prioritize Windows compatibility first while keeping path and process abstractions cross-platform.

实现应优先保证 Windows 可用，同时保持路径和进程抽象跨平台。

The primary control path is `UnifiedAgentSession`. The session gives the model access to tool schemas and runtime context. The model decides whether to call tools. The runtime executes tools safely and verifies code changes.

主控制路径是 `UnifiedAgentSession`。Session 将工具 schema 和 Runtime 上下文提供给模型。模型决定是否调用工具，Runtime 负责安全执行工具并验证代码修改。

## Single Session, Modular Internals

MiniCodex2 should use one primary user-facing session loop.

MiniCodex2 应使用单一的用户主会话循环。

All user turns enter `UnifiedAgentSession`.

所有用户回合都进入 `UnifiedAgentSession`。

Different behaviors emerge from model tool calls and runtime state:

不同行为由模型工具调用和 Runtime 状态自然产生：

```text
chat path:
  one model call, no tools, no verification

read path:
  model calls list/read tools, no verification

write path:
  model calls write/edit tools, automatic verification, repair loop
```

```text
聊天路径：
  一次模型调用，无工具，无验证

读取路径：
  模型调用 list/read 工具，无验证

写入路径：
  模型调用 write/edit 工具，自动验证，失败修复
```

Do not implement separate primary flows such as:

不要实现以下分裂的主流程：

```text
ChatSession
ReadProjectSession
EditSession
RepairSession
```

Internal modules should be modular and testable, but conversation state should not be split into separate session types.

内部模块应保持模块化和可测试，但对话状态不应拆成多个 session 类型。

Intent classification may be used as advisory metadata, but not as the main dispatch mechanism.

Intent 分类可以作为辅助元数据，但不能作为主分派机制。

This design is especially important for MiniCodex2 because the target workflow is continuous:

这个设计对 MiniCodex2 尤其重要，因为目标工作流是连续的：

```text
chat
-> read project
-> edit files
-> verify
-> repair
-> summarize
```

Router-based architectures may be valid for other agent products, but MiniCodex2 optimizes for one continuous local development loop.

Router 架构对其他 Agent 产品可能是合理的，但 MiniCodex2 优化目标是一条连续的本地开发闭环。

## Codex Source Reading Notes: Turn Follow-Up Semantics

This section records design lessons from reading Codex's turn loop. These notes are design input for MiniCodex2's next loop refactor.

本节记录阅读 Codex turn loop 后得到的设计启发，用于 MiniCodex2 后续主循环重构。

Relevant Codex files:

相关 Codex 文件：

- `core/src/session/turn.rs`
- `core/src/stream_events_utils.rs`
- `core/src/compact.rs`
- `core/src/context_manager/updates.rs`

Codex treats a user-facing turn as a sequence of sampling requests. A turn should continue when the model, runtime, or pending user input indicates that more model work is needed.

Codex 把一个用户可见回合视为一串模型采样请求。当模型、Runtime 或待处理用户输入表明还需要模型继续处理时，当前回合应继续。

MiniCodex2 should make `needs_follow_up` explicit and testable.

MiniCodex2 应把 `needs_follow_up` 变成显式、可测试的循环语义。

The following cases must produce follow-up model input instead of silently ending the turn:

以下情况必须形成下一轮模型输入，而不是静默结束当前回合：

- Tool call returned successfully and its result is needed for the next decision.
- Tool call failed with a recoverable error.
- Tool arguments were invalid but the error is recoverable.
- `end_turn=false` or equivalent model signal says the model intends to continue.
- Verification failed and produced a `FailurePack`.
- Automatic verification passed but an active goal or work-plan step still needs reconciliation.
- A background command started and returned readiness/log/port information needed by the model.
- A command timed out or was detected as long-running/interactive.
- A permission gate changed the executable action.
- User input arrived while a turn was still running.
- A context compaction or new context window was started mid-turn.

These cases should enter the next model sampling request, not only UI state, logs, or a final message.

这些情况应进入下一次模型采样，而不是只进入 UI 状态、日志或最终回答。

The loop should distinguish:

主循环需要区分：

```text
turn_completed:
  no tool calls pending
  no runtime failure requiring model action
  no active goal reconciliation needed
  no pending user input
  model has produced a final answer

needs_follow_up:
  model requested tools
  tool result needs interpretation
  runtime produced recoverable failure context
  verification produced blocking failure context
  goal/work-plan state requires next-step decision
  pending input must be drained

blocked:
  same blocker repeated beyond policy
  required permission or external dependency is unavailable
  model exhausted repair attempts without progress
```

MiniCodex2 should not rely on UI messages as the only carrier for runtime failures. Tool errors, validation errors, verification failures, permission denials, and context compaction results must be written into model-visible history or structured runtime context.

MiniCodex2 不应只把运行时失败显示在 UI 里。工具错误、参数错误、验证失败、权限拒绝和上下文压缩结果，都必须进入模型可见历史或结构化 runtime context。

### Planned Tests

后续需要补充测试：

- `test_tool_error_sets_needs_follow_up`
- `test_invalid_tool_arguments_set_needs_follow_up`
- `test_verification_failure_sets_needs_follow_up`
- `test_end_turn_false_continues_turn`
- `test_pending_user_input_continues_after_current_sampling`
- `test_goal_reconciliation_continues_after_verified_step`
- `test_context_compaction_continues_after_rebuilding_context`
- `test_runtime_failure_is_model_visible_not_ui_only`

## Module Boundaries

### ChatHistory

Responsibilities:

- Store current session messages.
- Preserve user, assistant, tool, and runtime messages.
- Provide history to `ModelAdapter`.
- Delegate compression and truncation decisions to `ContextManager`.

职责：

- 保存当前会话消息。
- 保留 user、assistant、tool、runtime message。
- 向 `ModelAdapter` 提供上下文。
- 将压缩和裁剪决策交给 `ContextManager`。

Not responsible for:

- Model calls.
- Tool execution.
- Persistence.
- Token budget decisions.

### ContextManager

Responsibilities:

- Build the final model context for each model call.
- Include compact runtime summaries such as `ProjectProfile`, changed files, active background services, and verification recommendations.
- Keep recent user and assistant messages verbatim when possible.
- Summarize older conversation history.
- Summarize large tool results.
- Truncate noisy command output before it reaches the model.
- Respect configured token budgets.

职责：

- 为每次模型调用构建最终上下文。
- 注入压缩后的 Runtime 摘要，例如 `ProjectProfile`、变更文件、后台服务和验证建议。
- 尽量保留最近的用户和助手消息原文。
- 摘要化较早的会话历史。
- 摘要化大型工具结果。
- 在命令输出进入模型前截断噪声。
- 遵守配置的 token 预算。

Not responsible for:

- Counting provider-specific exact token usage after a call.
- Executing tools.
- Persisting history.

Current context sections:

- `[RUNTIME PROTOCOL]`: model-independent operating rules for goals, tools, command execution, and guidance handling.
- `[PROJECT GUIDANCE]`: durable repo instructions loaded from `AGENTS.md`.
- `[WORK MEMORY]`: active `RootObjective`, `GoalStatus`, `WorkPlan`, `CurrentStep`, acceptance evidence, and blockers.
- `[PROJECT MEMORY INDEX]`: compact index of `README.md`, `AGENTS.md`, and `docs/*.md` so the model knows what project documents are available without receiving full text.
- `[EXECUTION STATE]`: changed files, verification evidence, failure targets, repair round, and result state.
- `Compressed older context`: summarized older chat/tool history when the context approaches budget.

Current context sections / 当前上下文分区：

- `[RUNTIME PROTOCOL]`: 与模型无关的运行协议，包括目标、工具、命令执行和项目规则处理。
- `[PROJECT GUIDANCE]`: 从 `AGENTS.md` 加载的仓库长期规则。
- `[WORK MEMORY]`: 当前 `RootObjective`、`GoalStatus`、`WorkPlan`、`CurrentStep`、验收证据和 blocker。
- `[PROJECT MEMORY INDEX]`: `README.md`、`AGENTS.md` 和 `docs/*.md` 的紧凑索引，让模型知道有哪些项目文档可读，但不直接塞全文。
- `[EXECUTION STATE]`: 已修改文件、验证证据、失败目标、修复轮次和结果状态。
- `Compressed older context`: 当上下文接近预算时，对较早聊天和工具历史进行摘要。

### ProjectGuidanceLoader

Responsibilities:

- Find `AGENTS.md` files under the workspace, excluding dependency/build/artifact directories.
- Load bounded content and mark truncation.
- Provide durable project instructions to `ContextManager` for every model call.
- Avoid treating guidance as the current task plan.

职责：

- 在 workspace 内查找 `AGENTS.md`，忽略依赖、构建和产物目录。
- 加载有长度边界的内容，并标记是否截断。
- 向 `ContextManager` 提供每轮模型调用都能看到的项目长期规则。
- 不把项目规则当成当前任务计划。

### ProjectMemoryIndex

Responsibilities:

- Index `README.md`, `AGENTS.md`, and documents under `docs/`.
- Extract title, headings, and compact excerpts.
- Let the model discover relevant planning/design documents without injecting full files.
- Pair with `sync_work_plan_from_document` when a document should become executable runtime work memory.

职责：

- 索引 `README.md`、`AGENTS.md` 和 `docs/` 下的文档。
- 提取标题、标题列表和紧凑摘要。
- 让模型知道有哪些相关计划/设计文档，而不是每轮注入全文。
- 当某个文档需要变成可执行运行时工作记忆时，配合 `sync_work_plan_from_document` 使用。

### TokenUsageTracker

Responsibilities:

- Estimate tokens before model calls when possible.
- Record actual prompt, completion, and total tokens returned by `ModelAdapter`.
- Track usage per model call, per turn, and per session.
- Expose usage summaries to CLI, TUI, logs, and tests.
- Warn when the context budget is close to the limit.

职责：

- 在可行时预估模型调用前的 token 数。
- 记录 `ModelAdapter` 返回的实际 prompt、completion 和 total tokens。
- 按模型调用、回合和会话统计 token 消耗。
- 向 CLI、TUI、日志和测试暴露用量摘要。
- 在上下文预算接近上限时发出提示。

Token tracking should be useful even when the provider does not return exact usage. In that case, the tracker can store estimated values and mark them as estimated.

即使模型供应商不返回精确 token 用量，统计模块也应该可用。这种情况下可以存储估算值，并标记为 estimated。

### ConfigLoader

Responsibilities:

- Load `minicodex2.toml` from the workspace root when present.
- Merge defaults, config file values, environment variables, CLI arguments, and API session overrides.
- Validate permission, model, verification, context, and artifact settings.
- Provide a resolved immutable session config to `UnifiedAgentSession`.

职责：

- 在 workspace root 中存在 `minicodex2.toml` 时加载它。
- 合并默认值、配置文件、环境变量、CLI 参数和 API session 覆盖。
- 校验权限、模型、验证、上下文和产物目录设置。
- 向 `UnifiedAgentSession` 提供解析后的不可变 session config。

### AgentEventBus

Responsibilities:

- Emit structured `AgentEvent` records.
- Store recent events for CLI/API/TUI consumption.
- Keep event payloads compact.
- Provide a common event boundary for Local API, TUI, and future desktop UI.

职责：

- 发出结构化 `AgentEvent` 记录。
- 保存近期事件，供 CLI/API/TUI 消费。
- 保持事件 payload 简洁。
- 为 Local API、TUI 和未来桌面 UI 提供统一事件边界。

Events describe what happened. They should not become a second control path.

事件描述发生了什么，不应变成第二套控制路径。

### DecisionSystem

Responsibilities:

- Provide shared decision structures such as `PolicyDecision`, `FailureClassification`, and `ContinuationDecision`.
- Keep engineering judgment in testable policies, strategies, classifiers, and planners.
- Avoid scattering language, permission, dependency, and continuation decisions across unrelated modules.
- Support partial progress without hiding unresolved blockers.

职责：

- 提供共享判断结构，例如 `PolicyDecision`、`FailureClassification` 和 `ContinuationDecision`。
- 将工程判断放入可测试的 policies、strategies、classifiers 和 planners。
- 避免将语言、权限、依赖和继续执行判断散落在无关模块中。
- 支持部分推进，同时不隐藏未解决 blockers。

Decision categories:

判断类别：

```text
Detector: identifies facts.
Strategy: translates facts into plans.
Policy: allows, asks, denies, or blocks actions.
Classifier: interprets failures.
Planner: decides next action.
```

```text
Detector: 识别事实。
Strategy: 将事实翻译成计划。
Policy: 允许、询问、拒绝或阻塞动作。
Classifier: 解释失败。
Planner: 决定下一步行动。
```

### CollaborationAdvisor

Responsibilities:

- Estimate task complexity from request shape and workspace state.
- Suggest design-first collaboration before coding when appropriate.
- Allow users to override and request direct implementation.
- Record MVP assumptions when direct implementation proceeds with incomplete details.
- Avoid repeated or annoying advice.

职责：

- 根据请求形态和 workspace 状态估计任务复杂度。
- 在合适时建议先进行 design-first 协作。
- 允许用户覆盖建议并要求直接实现。
- 当用户选择直接实现但细节不足时，记录 MVP 假设。
- 避免重复或打扰式提示。

This is an entry-layer collaboration aid, not the runtime's large-project planner.

这是入口层协作辅助，不是 Runtime 的大项目规划器。

It should use explainable complexity signals rather than requiring the user to say "large project".

它应使用可解释的复杂度信号，而不是要求用户明确说“大项目”。

### ModelAdapter

Responsibilities:

- Encapsulate model provider calls.
- Accept messages, tool schemas, and runtime context.
- Return assistant messages and tool calls.
- Hide provider-specific API differences.

职责：

- 封装模型供应商调用。
- 接收 messages、tool schemas 和 runtime context。
- 返回 assistant message 和 tool calls。
- 屏蔽不同模型供应商的 API 差异。

First version should include:

- `ModelAdapter` interface.
- `FakeModelAdapter` for deterministic tests.
- Real model adapter after the fake-model core loop is stable.

Implementation order:

实现顺序：

1. Build and test `ModelAdapter` interface.
   构建并测试 `ModelAdapter` 接口。
2. Build `FakeModelAdapter` to drive deterministic tool calls, failures, and repairs.
   构建 `FakeModelAdapter`，用于确定性驱动工具调用、失败和修复。
3. Complete core loop tests with `FakeModelAdapter`.
   使用 `FakeModelAdapter` 完成核心闭环测试。
4. Add real model adapter integration.
   增加真实模型 adapter 集成。
5. Run real-model smoke tests and fix runtime issues found there.
   运行真实模型 smoke tests，并修复其中暴露的 Runtime 问题。

### ToolRegistry

Responsibilities:

- Register tool definitions.
- Expose tool schemas to the model.
- Dispatch tool calls to runtime handlers.
- Validate basic argument shape.

职责：

- 注册工具定义。
- 向模型暴露工具 schema。
- 将 tool call 分发给 Runtime handler。
- 校验基础参数结构。

Not responsible for:

- Permission decisions.
- Path safety.
- Command execution internals.

### RuntimeTools

Required tools:

- `list_directory`
- `read_file`
- `write_file`
- `edit_file`
- `delete_file`
- `run_command`
- `start_background_command`

Edit behavior:

编辑行为：

- `write_file` creates or fully replaces a file.
  `write_file` 新建或全量覆盖文件。
- `edit_file` starts with exact string replacement.
  `edit_file` 第一版使用 exact string replace。
- Unified diff or patch application can be added later.
  unified diff 或 patch apply 后续再加。

Responsibilities:

- Keep all filesystem access inside the workspace root.
- Gate writes, edits, and deletes according to permission mode.
- Run commands with cwd restrictions, timeout, output truncation, and safety checks.
- Detect likely long-running commands in `run_command`.
- Start long-running services through `start_background_command`.
- Return structured tool results.

职责：

- 将所有文件系统访问限制在 workspace root 内。
- 根据权限模式对写入、编辑、删除执行 gate。
- 执行命令时限制 cwd、设置超时、截断输出并进行安全检查。
- 在 `run_command` 中识别可能长驻的命令。
- 通过 `start_background_command` 启动长驻服务。
- 返回结构化工具结果。

Default permission behavior should be automation-friendly:

默认权限行为应偏自动化：

- Read operations are allowed inside the workspace.
  workspace 内读取默认允许。
- Ordinary writes inside the workspace can be allowed in `auto` mode.
  `auto` 模式下，workspace 内普通写入可以默认允许。
- Verification commands can run without approval when they match trusted plans.
  匹配可信验证计划的验证命令可以不经审批运行。
- Destructive commands, workspace escapes, and deletes still require stronger gates.
  破坏性命令、越界路径和删除操作仍需要更强 gate。

### ProjectDetector

Responsibilities:

- Scan high-signal project files.
- Build `ProjectProfile`.
- Identify candidate project types without deep analysis.
- Produce compact summaries suitable for model context.

职责：

- 扫描高信号项目文件。
- 构建 `ProjectProfile`。
- 在不深度分析全项目的情况下识别候选项目类型。
- 生成适合放入模型上下文的压缩摘要。

High-signal files:

- `pyproject.toml`
- `requirements.txt`
- `pytest.ini`
- `package.json`
- `Makefile`
- `main.c`
- `CMakeLists.txt`
- `Cargo.toml`
- `go.mod`
- `README`
- `app.py`
- `vite.config.*`

### VerificationPlanBuilder

Responsibilities:

- Build high-confidence verification plans from `ProjectProfile`, changed files, and project scripts.
- Prefer project-defined scripts when available.
- Use standard language commands when confidence is high.
- Fall back to model decision when the project is ambiguous.
- Produce concise verification recommendations for model context.

职责：

- 根据 `ProjectProfile`、变更文件和项目脚本生成高置信验证计划。
- 优先使用项目自定义脚本。
- 在置信度高时使用语言标准命令。
- 项目不明确时交给模型读取项目文件后决定。
- 为模型上下文生成简洁的验证建议。

This module must remain conservative. It should not try to fully understand every build system.

Verification commands can be overridden by user request or `minicodex2.toml`.

验证命令可以被用户请求或 `minicodex2.toml` 覆盖。

Override priority:

覆盖优先级：

1. Explicit user instruction in the current turn.
   当前回合用户明确指令。
2. Session override from CLI or Local API.
   CLI 或 Local API 的 session 覆盖。
3. `minicodex2.toml` verification config.
   `minicodex2.toml` 中的验证配置。
4. Strategy-generated high-confidence defaults.
   Strategy 生成的高置信默认策略。
5. Model-selected commands after reading project files.
   模型读取项目文件后选择的命令。

Language-specific behavior should be isolated in small verification strategies, not spread across the runtime.

语言特殊逻辑应隔离在小型 verification strategies 中，而不是散落在整个 Runtime 中。

Suggested strategy interface:

建议的 strategy 接口：

```text
VerificationStrategy.matches(ProjectProfile, changed_files) -> MatchResult
VerificationStrategy.build_plan(ProjectProfile, changed_files) -> VerificationPlan
```

The main `VerificationPlanBuilder` should select the highest-confidence matching strategy and return a unified `VerificationPlan`.

主 `VerificationPlanBuilder` 应选择置信度最高的 strategy，并返回统一的 `VerificationPlan`。

This keeps language details testable while allowing `VerificationRunner` to remain language-agnostic.

这样既能让语言细节可测试，也能保持 `VerificationRunner` 语言无关。

### VerificationRunner

Responsibilities:

- Execute `VerificationPlan` steps.
- Stop on the first blocking failure.
- Collect command output, exit codes, durations, and smoke test results.
- Return `VerificationResult`.

职责：

- 执行 `VerificationPlan` 步骤。
- 在第一个 blocking failure 处停止。
- 收集命令输出、退出码、耗时和 smoke test 结果。
- 返回 `VerificationResult`。

`VerificationRunner` should not contain Python, C, Node, Rust, Go, or CMake-specific branching. It should execute generic verification steps.

`VerificationRunner` 不应包含 Python、C、Node、Rust、Go 或 CMake 的特殊分支。它只应执行通用验证步骤。

Generic step types:

通用步骤类型：

```text
command
compile
run
test
background_server
http_smoke
```

### FailurePack

Responsibilities:

- Extract the first blocking failure from a failed verification result.
- Keep output concise.
- Include command, exit code, failure type, relevant output, suspected files, and suggested next action.
- Feed structured failure context back to the model.

职责：

- 从失败的验证结果中提取第一个 blocking failure。
- 控制输出体积。
- 包含命令、退出码、失败类型、相关输出、疑似文件和建议下一步。
- 将结构化失败上下文回灌给模型。

### UnifiedAgentSession

Responsibilities:

- Own the one-turn main loop.
- Add user messages.
- Call the model.
- Dispatch tool calls.
- Track whether files changed.
- Trigger automatic verification after writes.
- Feed `FailurePack` back to the model after verification failure.
- Limit repair rounds.
- Return success or blocked.

职责：

- 拥有一回合主循环。
- 添加用户消息。
- 调用模型。
- 分发工具调用。
- 跟踪文件是否发生变更。
- 写入后触发自动验证。
- 验证失败后将 `FailurePack` 回灌给模型。
- 限制修复轮数。
- 返回 success 或 blocked。

Repair policy:

修复策略：

- Default `max_repair_rounds` is `3`.
  默认 `max_repair_rounds` 为 `3`。
- A repair round starts only after a failed verification creates a `FailurePack`.
  修复轮只在验证失败并生成 `FailurePack` 后开始。
- Verification must run after each repair attempt.
  每次修复尝试后必须重新运行验证。
- The session should stop early if the same blocking failure repeats twice.
  如果同一个 blocking failure 连续出现两次，Session 应提前停止。
- Environment failures, denied permissions, and ambiguous verification commands can produce immediate `blocked`.
  环境失败、权限拒绝和不明确的验证命令可以直接产生 `blocked`。

### CLI / TUI

First version may start with CLI, but v0.1 must include a first usable TUI after the core runtime loop is stable enough to exercise real tasks.

第一版可以先从 CLI 开始，但 v0.1 必须在核心 Runtime 闭环足够稳定、能跑真实任务后交付第一版可用 TUI。

Responsibilities:

- Accept user input.
- Display assistant messages.
- Display tool and verification status.
- Handle permission prompts.
- Select workspace root.

Planned TUI responsibilities:

- Show the active session transcript.
- Show tool calls and tool results.
- Show verification status and failure packs.
- Show background service status.
- Provide permission approval prompts.
- Provide a clearer bridge toward a future desktop experience.
- Make final user acceptance easier.

计划中的 TUI 职责：

- 展示当前会话记录。
- 展示工具调用和工具结果。
- 展示验证状态和 FailurePack。
- 展示后台服务状态。
- 提供权限确认交互。
- 为未来桌面端体验提供过渡形态。
- 让最终用户验收更方便。

### Desktop Experience

A desktop app similar to Codex Desktop is a later productization target.

类似 Codex Desktop 的桌面端是后续产品化目标。

It should not block the first core runtime delivery, but the architecture should avoid coupling the agent loop directly to CLI-only assumptions.

它不应阻塞第一阶段核心 Runtime 交付，但架构也不应把 Agent 主循环强绑定到 CLI 假设上。

### Local API Server

Required for v0.1 as a thin entry layer over `UnifiedAgentSession`.

v0.1 必须实现，作为 `UnifiedAgentSession` 之上的薄入口层。

Because TUI and desktop are planned, the API server should provide a stable event and session boundary.

由于 TUI 和桌面端已经进入路线规划，API Server 应提供稳定的事件和 session 边界。

The API server is also an integration testing boundary. Tests can exercise the agent without going through CLI rendering.

API Server 也是集成测试边界。测试可以不经过 CLI 渲染，直接驱动 Agent。

Possible endpoints:

```text
POST /sessions
POST /sessions/{id}/messages
GET  /sessions/{id}
GET  /sessions/{id}/events
POST /permissions/{request_id}/approve
POST /permissions/{request_id}/deny
```

First-version endpoint scope:

第一版接口范围：

```text
POST /sessions
  Create a session with workspace root and config.
  使用 workspace root 和配置创建 session。

POST /sessions/{id}/messages
  Send one user message and start or continue one agent turn.
  发送一条用户消息，启动或继续一个 Agent turn。

GET /sessions/{id}
  Return session state, latest messages, token usage, and verification status.
  返回 session 状态、最新消息、token 用量和验证状态。

GET /sessions/{id}/events
  Stream or poll runtime events.
  流式或轮询 Runtime events。

POST /permissions/{request_id}/approve
  Approve a pending permission request.
  批准待处理权限请求。

POST /permissions/{request_id}/deny
  Deny a pending permission request.
  拒绝待处理权限请求。
```

The first implementation may use polling for events. Server-sent events can be added when the TUI or desktop UI needs streaming.

第一版事件可以先使用轮询。等 TUI 或桌面 UI 需要流式体验时，再增加 Server-Sent Events。

The Local API should expose `AgentEventBus` events instead of reconstructing UI state independently.

Local API 应暴露 `AgentEventBus` 事件，而不是独立重建 UI 状态。

### HistoryStore

Responsibilities:

- Persist session history.
- Store tool call summaries.
- Store verification summaries.
- Store token usage summaries.
- Redact sensitive values.

First version recommendation:

- JSONL store for simplicity and testability.
- Default location: `<workspace_root>/.minicodex2/sessions/`.
- One session should have one `.jsonl` file and one `.meta.json` file.

第一版建议：

- 使用 JSONL，简单、易测试、易调试。
- 默认位置：`<workspace_root>/.minicodex2/sessions/`。
- 每个 session 使用一个 `.jsonl` 文件和一个 `.meta.json` 文件。

Sensitive values such as API keys must never be persisted.

API key 等敏感值不得持久化。

## One-Turn Main Loop Pseudocode

```python
def run_turn(user_input):
    history.add_user(user_input)
    wrote_files = False
    repair_round = 0
    max_repair_rounds = 3
    previous_failure_signature = None

    while True:
        context = context_manager.build_context(
            history=history,
            project_profile=project_detector.cached_profile(),
            verification_hint=verification_plan_builder.cached_hint(),
            token_budget=session_token_budget,
        )

        response = model.complete(
            messages=context.messages,
            tools=tool_registry.schemas(),
            runtime_context=session_context(),
        )

        token_usage_tracker.record(response.usage)

        history.add_assistant(response.message)

        if response.tool_calls:
            for call in response.tool_calls:
                result = tool_registry.execute(call)
                history.add_tool_result(call.id, result)

                if result.did_write:
                    wrote_files = True

                if result.blocked:
                    return blocked(result)

            continue

        if not wrote_files:
            return final_response(response)

        verification = verify_workspace()

        if verification.passed:
            return final_success(response, verification)

        failure_pack = FailurePack.from_verification(verification)

        if failure_pack.signature == previous_failure_signature:
            return blocked(failure_pack)

        if repair_round >= max_repair_rounds:
            return blocked(failure_pack)

        previous_failure_signature = failure_pack.signature
        repair_round += 1
        history.add_runtime_failure(failure_pack)
```

## Data Flows

### Normal Chat

```text
UserInput
-> ChatHistory.add_user
-> ModelAdapter.complete
-> no tool calls
-> final response
```

### Project Reading

```text
UserInput
-> ModelAdapter.complete
-> tool_call: list_directory/read_file
-> ToolRegistry dispatch
-> RuntimeTools read-only execution
-> tool result appended
-> ModelAdapter.complete
-> final summary
```

### File Change and Verification

```text
UserInput
-> ModelAdapter.complete
-> tool_call: write_file/edit_file
-> permission gate
-> RuntimeTools write
-> UnifiedAgentSession marks workspace dirty
-> VerificationPlanBuilder builds plan
-> VerificationRunner runs plan
-> pass: final success
-> fail: FailurePack -> ModelAdapter repair loop
```

### Web Service Verification

```text
File changes
-> VerificationPlanBuilder detects web candidate
-> start_background_command
-> wait for port readiness
-> HTTP smoke
-> pass or FailurePack
```

## Codex Source Reading Notes: Generic Runtime Over Special Cases

This section records design lessons from reading the local Codex source under
`playground/codex-main/codex-rs`. These notes are design input, not a claim that
MiniCodex2 already implements the same behavior.

本节记录阅读本地 Codex 源码 `playground/codex-main/codex-rs` 后得到的架构启发。这些内容是后续设计输入，不代表 MiniCodex2 当前已经实现同等能力。

Current source-reading conclusion:

当前源码阅读结论：

- Codex core does not appear to implement coding ability through a large language/framework verification router.
  Codex 核心看起来不是靠大型语言/框架验证 router 来实现代码能力。
- Searches in core did not show an obvious hard-coded matrix for `pytest`, `npm test`, `go test`, `cargo test`, `Flask`, `Vite`, `Django`, or frontend/backend topology.
  在 core 中搜索后，没有看到明显硬编码的 `pytest`、`npm test`、`go test`、`cargo test`、`Flask`、`Vite`、`Django` 或前后端拓扑矩阵。
- The stronger pattern is: stable tools, clear tool schemas, permission gates, model-visible tool results, model-visible tool errors, bounded context, and explicit context-window controls.
  更明显的模式是：稳定工具、清晰工具 schema、权限 gate、模型可见的工具结果、模型可见的工具错误、有界上下文，以及显式上下文窗口控制。
- The model remains responsible for deciding which project files, commands, and debugging steps matter.
  哪些项目文件、命令、调试步骤重要，仍然由模型判断。

Relevant Codex files:

相关 Codex 文件：

- `core/src/session/turn.rs`: sampling loop, tool calls, follow-up decisions.
- `core/src/tools/parallel.rs`: tool execution and conversion of tool errors into model-visible outputs.
- `core/src/tools/spec_plan.rs`: dynamic tool planning and exposure.
- `core/src/tools/handlers/plan.rs`: `update_plan` as a model-callable checklist/status tool.
- `core/src/tools/handlers/get_context_remaining.rs`: model-visible context budget.
- `core/src/tools/handlers/new_context_window.rs`: model-requested context reset.
- `core/src/context/environment_context.rs`: environment facts such as cwd, shell, date, workspace roots, and permissions.
- `core/src/realtime_context.rs`: bounded startup workspace tree, not a full always-on project index.

### RuntimeResourceMap

MiniCodex2 should add a durable runtime-resource/evidence map. This should be generic execution perception, not web-specific business logic.

MiniCodex2 应增加一个持久化的 runtime resource / evidence map。它应当是通用执行感知，而不是 Web 项目专用业务逻辑。

Suggested structure:

建议结构：

```text
RuntimeResourceMap:
  commands:
    - id
      command
      cwd
      started_at
      finished_at
      status
      exit_code
      process_id
      log_path
      stdout_excerpt
      stderr_excerpt
  active_processes:
    - process_id
      command
      cwd
      started_at
      log_path
      poll_hint
  observed_ports:
    - port
      pid
      process_name
      source
      observed_at
  http_evidence:
    - method
      url
      status_code
      body_excerpt
      error
      observed_at
  file_evidence:
    - path
      exists
      size
      modified_at
      source
```

Injection policy:

注入策略：

- Inject recent failed commands, active processes, observed ports, and HTTP evidence when the task involves running, verifying, or debugging software.
  当任务涉及运行、验证或调试软件时，注入最近失败命令、活跃进程、端口观察结果和 HTTP 证据。
- Do not label a process as frontend/backend/API unless the project config says so or the model explicitly forms that hypothesis.
  除非项目配置明确说明，或模型显式形成该假设，否则 runtime 不应把进程标记为 frontend/backend/API。
- Prefer raw facts over runtime conclusions.
  优先提供原始事实，而不是 runtime 结论。
- Keep excerpts small and preserve log paths so the model can request more detail when needed.
  摘要要小，但保留日志路径，让模型需要时继续读取细节。

### Generic Engineering Evidence

For integration debugging, MiniCodex2 should build an `EvidencePack` before asking the model to continue after a failure.

对于联调调试，MiniCodex2 应在失败后继续请求模型前构造 `EvidencePack`。

```text
EvidencePack:
  user_goal
  latest_assistant_plan
  commands_run
  tool_errors
  verification_errors
  observed_resources
  files_changed_since_last_evidence
  files_read_since_last_evidence
  suggested_next_observation_tools
```

This is different from a language/framework rule. It does not say "for Vite do X" or "for Django do Y". It says what the runtime has actually observed.

这和语言/框架规则不同。它不说“Vite 要做 X”或“Django 要做 Y”，只说 runtime 真实观察到了什么。

## Codex Source Reading Notes: Plan And Goal

This section records design lessons from reading Codex's `update_plan` and goal implementation. These notes are design input for MiniCodex2's work-memory and long-running objective design.

本节记录阅读 Codex `update_plan` 和 goal 实现后得到的设计启发，用于 MiniCodex2 后续工作记忆和长目标设计。

### Turn Plan

Codex `update_plan` is a model-callable, turn-level checklist/status tool.

Codex 的 `update_plan` 是模型可调用的、回合级 checklist/status 工具。

Relevant files:

相关文件：

- `core/src/tools/handlers/plan_spec.rs`
- `core/src/tools/handlers/plan.rs`
- `protocol/src/plan_tool.rs`
- `app-server/src/bespoke_event_handling.rs`
- `tui/src/chatwidget/turn_runtime.rs`
- `tui/src/history_cell/plans.rs`

Observed behavior:

观察到的行为：

- The model decides when to call `update_plan`.
  由模型决定何时调用 `update_plan`。
- The schema only contains `explanation` and `plan[]`, where each item has `step` and `status`.
  schema 只包含 `explanation` 和 `plan[]`，每个 item 只有 `step` 与 `status`。
- The handler does not execute work, verify steps, or drive continuation.
  handler 不执行工作、不验证步骤，也不驱动续跑。
- The handler emits `EventMsg::PlanUpdate` and returns a successful tool output: `Plan updated`.
  handler 发出 `EventMsg::PlanUpdate`，并向模型返回成功工具结果：`Plan updated`。
- App server converts it to `TurnPlanUpdatedNotification`.
  App server 将其转换成 `TurnPlanUpdatedNotification`。
- TUI renders it as an `Updated Plan` history cell and status/title progress.
  TUI 将其渲染成 `Updated Plan` 历史块，并更新状态栏/标题进度。
- A plan update is UI-visible progress, not proof of completion.
  plan update 是 UI 可见进度，不是完成证明。

Design implication for MiniCodex2:

对 MiniCodex2 的设计含义：

- Keep a lightweight `update_plan` style tool for visible turn planning.
  保留轻量级 `update_plan` 风格工具，用于可见的回合计划。
- Do not make `update_plan` itself responsible for durable memory, verification, or goal continuation.
  不要让 `update_plan` 本身承担持久记忆、验证或目标续跑职责。
- If a plan step is durable, copy or promote it into a separate work-memory structure with evidence.
  如果某个计划步骤需要持久化，应复制或提升到独立 work-memory 结构，并绑定 evidence。

### Thread Goal

Codex goal is a separate thread-level persisted objective system.

Codex goal 是独立的线程级持久目标系统。

Relevant files:

相关文件：

- `app-server-protocol/src/protocol/v2/thread.rs`
- `app-server/src/request_processors/thread_goal_processor.rs`
- `state/src/model/thread_goal.rs`
- `state/src/runtime/goals.rs`
- `ext/goal/src/spec.rs`
- `ext/goal/src/tool.rs`
- `ext/goal/src/runtime.rs`
- `ext/goal/src/extension.rs`
- `ext/goal/src/steering.rs`
- `ext/goal/templates/goals/continuation.md`
- `ext/goal/templates/goals/budget_limit.md`
- `ext/goal/templates/goals/objective_updated.md`

Observed goal state:

观察到的 goal 状态：

```text
ThreadGoal:
  thread_id
  goal_id
  objective
  status: active | paused | blocked | usage_limited | budget_limited | complete
  token_budget
  tokens_used
  time_used_seconds
  created_at
  updated_at
```

Observed behavior:

观察到的行为：

- Goal is persisted in SQLite state, not only in chat history.
  Goal 持久化在 SQLite state 中，而不是只存在聊天历史里。
- App/TUI can set, get, clear, pause, resume, and edit a goal through thread-level API.
  App/TUI 可通过 thread-level API 设置、读取、清除、暂停、恢复、编辑 goal。
- The model gets `get_goal`, `create_goal`, and `update_goal` tools.
  模型获得 `get_goal`、`create_goal`、`update_goal` 工具。
- `create_goal` is only for explicitly requested goals; ordinary tasks should not automatically become goals.
  `create_goal` 只用于显式要求的目标；普通任务不应自动变成 goal。
- `update_goal` can only mark complete or blocked. Pause/resume/budget/usage state is controlled by user or system.
  `update_goal` 只能标记 complete 或 blocked；pause/resume/budget/usage 状态由用户或系统控制。
- The goal extension accounts token and elapsed-time usage across turns/tools.
  goal extension 会统计跨 turn/tool 的 token 和耗时。
- If an active goal exists and the thread becomes idle, goal runtime can start a new automatic turn by injecting a hidden continuation item.
  如果存在 active goal 且线程空闲，goal runtime 可注入隐藏 continuation item，自动开启下一回合。
- If a turn is already running, goal runtime can inject steering into the active turn.
  如果 turn 正在运行，goal runtime 可向当前 turn 注入 steering。
- Goal continuation is blocked in Plan mode and when user-triggered work is queued.
  Plan mode 或用户触发任务排队时，goal continuation 会被拒绝。

### Goal Steering Prompt

Codex's continuation prompt is stronger than a simple "continue" instruction.

Codex 的 continuation prompt 比简单的“继续”更强。

It tells the model:

它要求模型：

- Preserve the full objective; do not shrink it to the current turn.
  保留完整目标，不要缩小成当前回合能做完的小目标。
- Make concrete progress if the whole objective cannot be finished now.
  如果当前无法完成整体目标，也要做具体进展。
- Work from current worktree and external state as authoritative evidence.
  以当前 worktree 和外部状态作为权威证据。
- Use `update_plan` when the next work is meaningfully multi-step.
  当下一步工作明显多步骤时，使用 `update_plan`。
- Do a requirement-by-requirement completion audit before marking complete.
  标记 complete 前，逐项审计需求和完成证据。
- Treat weak, indirect, or missing evidence as not complete.
  弱证据、间接证据或缺失证据都不能视为完成。
- Mark blocked only after the same blocker repeats for at least three consecutive goal turns.
  只有同一 blocker 连续至少三个 goal turn 重复后，才允许标记 blocked。

Design implication for MiniCodex2:

对 MiniCodex2 的设计含义：

- MiniCodex2 should separate:
  MiniCodex2 应拆分：

```text
VisibleTurnPlan:
  transient checklist shown in TUI
  model-updated
  no durable completion semantics by itself

DurableGoal:
  persistent root objective
  status/budget/usage
  survives context compaction and restarts
  can trigger automatic continuation when idle

WorkMemory:
  durable work plan/current step/evidence records
  bridges visible plans and goal completion audit
```

- The runtime should not decide whether the goal is complete, but it should force completion claims through structured evidence and the `update_goal` tool.
  Runtime 不应替模型判断目标是否完成，但应要求完成声明经过结构化 evidence 和 `update_goal` 工具。
- Automatic continuation should be an idle-turn injection mechanism, not an extra business loop nested inside the normal turn loop.
  自动续跑应是空闲时注入新 turn 的机制，而不是在普通 turn loop 内再嵌套一个业务循环。
- Goal continuation prompt should be explicit about preserving the original objective and auditing completion against current evidence.
  goal continuation prompt 应明确要求保留原始目标，并根据当前证据审计完成度。
## Codex Source Reading Notes: Tool Follow-Up And Integration Debugging

Codex does not appear to solve integration debugging by hard-coding every framework.
The stronger pattern is a generic evidence loop.

Codex 似乎不是靠为每个框架写死联调规则来解决问题，而是靠通用证据闭环。

- Strong autonomy prompt: for implementation tasks, keep going until the task is resolved end-to-end.
  强自主提示：对于实现任务，要求模型持续推进直到端到端解决。
- Any model tool call is persisted and sets `needs_follow_up=true`.
  任何模型工具调用都会持久化，并设置 `needs_follow_up=true`。
- Tool outputs and tool errors are converted into model-visible function-call output.
  工具输出和工具错误都会转换成模型可见的 function-call output。
- The next model request is rebuilt from full history after tool outputs are recorded.
  工具结果写入历史后，下一次模型请求会从完整历史重新构造。
- Long-running commands return a running session id instead of silently blocking.
  长驻命令返回运行中的 session id，而不是静默卡住。
- Command output includes structured facts: wall time, exit code, running session id, original token count, and truncated output.
  命令输出包含结构化事实：耗时、退出码、运行中 session id、原始 token 数和截断输出。
- Context compaction creates a handoff summary, not a blind truncation.
  上下文压缩生成交接摘要，而不是盲目截断。

Design implication for MiniCodex2:

MiniCodex2 的设计含义：

```text
ToolResultFact:
  tool_name
  ok
  blocked
  did_write
  changed_files
  command/url/path when applicable
  exit_code/status_code when applicable
  output_excerpt
  next_model_instruction

IssueEvidence:
  reported_target
  reproduction_attempt
  observed_failure
  hypothesis_source
  verification_attempt
  status
```

Implementation plan:

实现计划：

1. Record a compact `ToolResultFact` runtime message after every non-trivial tool batch.
   每个非平凡工具批次后记录紧凑的 `ToolResultFact` runtime 消息。
2. Record failed tool calls as `EvidenceRecord(status=failed)` so compression and work memory retain them.
   将失败工具调用记录为 `EvidenceRecord(status=failed)`，避免压缩后丢失失败链。
3. Keep the existing tool-call follow-up loop, but make follow-up reasons explicit in events and history.
   保留现有工具调用后的 follow-up 循环，但把 follow-up 原因显式写入事件和历史。
4. Add an integration-debugging runtime protocol: reproduce, collect evidence, form hypothesis, verify same path.
   增加联调调试 runtime 协议：复现、收集证据、建立假设、沿同一路径验证。
5. Replace fixed history truncation summaries with handoff-style summaries until model-based compaction is added.
   在接入模型压缩前，把固定截断摘要改成交接式摘要。

Acceptance tests:

验收测试：

- Tool success produces model-visible structured facts before the next model call.
  工具成功后，下一次模型调用前能看到结构化事实。
- Tool failure produces failed evidence and forces another model call unless the repeated-blocker limit is reached.
  工具失败后生成失败证据，并在未达到重复 blocker 阈值前强制下一次模型调用。
- Integration issue prompts include reproduce/evidence/hypothesis/same-path verification guidance.
  联调问题上下文包含复现、证据、假设和同路径验证提示。
- Compaction summary preserves objective, recent failures, changed files, evidence, and next actions.
  压缩摘要保留目标、最近失败、变更文件、证据和下一步。

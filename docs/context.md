# Context and Token Budget

This document defines how MiniCodex2 should prepare model context and track token usage.

本文档定义 MiniCodex2 如何准备模型上下文并统计 token 消耗。

## Decision

MiniCodex2 should explicitly provide compact project detection results and verification recommendations to the model.

MiniCodex2 应显式向模型提供压缩后的项目检测结果和验证建议。

The model should not need to rediscover obvious runtime facts repeatedly. However, the runtime must control size carefully.

模型不应反复重新发现明显的 Runtime 事实。但 Runtime 必须严格控制上下文体积。

## Persistent Project Memory

MiniCodex2 separates durable memory into three context tiers:

MiniCodex2 将持久项目记忆分成三层上下文：

- Hot Memory: a few high-confidence, frequently useful facts injected directly into every relevant model context.
  热记忆：少量高置信、常用事实，直接注入相关模型上下文。
- Memory Index: compact keys, titles, tags, confidence, and summaries so the model knows what can be retrieved.
  记忆索引：只注入 key、标题、标签、置信度和摘要，让模型知道哪些记忆可以检索。
- Cold Memory: full fact content stored on disk and loaded only through memory tools.
  冷记忆：完整事实内容存在磁盘，需要时通过记忆工具读取。

Runtime owns storage, deduplication, stale flags, search, and compact context injection. The model owns semantic judgment: what should be remembered, when to search, when to read full memory, and when a remembered fact is obsolete.

Runtime 负责存储、去重、过期标记、搜索和压缩注入；模型负责语义判断：什么值得记、什么时候查、什么时候读完整记忆、什么时候标记旧记忆失效。

The first project-memory tools are:

第一版项目记忆工具：

```text
remember_project_fact(title, content, tags, scope, confidence, source)
search_project_memory(query, tags, scope, include_stale, limit)
read_project_memory(memory_id)
update_project_memory(memory_id, title, content, tags, scope, confidence, stale)
forget_project_memory(memory_id)
```

The model should remember only observed or verified facts, such as startup commands, project-local tool paths, ports, verified test/build/smoke commands, and recurring failure fixes. It must not store secrets, guesses, ordinary chat, or one-off implementation details.

模型只能记住已经观察或验证过的事实，例如启动命令、项目本地工具路径、端口、已验证测试/构建/冒烟命令、反复出现故障的修复方法。不能存储密钥、猜测、普通聊天或一次性的实现细节。

### Workflow Memory

Workflow Memory is a narrower procedural layer for reusable project operations.
It stores "how to do the thing again" rather than broad project facts:

工作流程记忆是更窄的流程层，用来保存可复用的项目操作方式。
它记录的是“下次怎么再做这件事”，而不是宽泛项目事实：

```text
remember_workflow(title, cwd, command, purpose, ready_check, ports, tags, scope, confidence, source, notes)
search_workflows(query, tags, scope, purpose, include_stale, limit)
read_workflow(workflow_id)
update_workflow(workflow_id, ..., success_delta, failure_delta, stale)
forget_workflow(workflow_id)
```

Examples include stable startup commands, project-local virtual environments,
verified test/build/smoke commands, browser/API integration smoke flows, and
repeatable debug procedures. Runtime stores, deduplicates, ranks, injects, and
marks workflows stale; the model decides whether a newly observed command or
flow is reusable enough to remember.

例如：稳定的启动命令、项目本地虚拟环境、已验证的测试/构建/冒烟命令、
浏览器或 API 联调流程、可重复的调试流程。Runtime 负责存储、去重、排序、
注入上下文和标记过期；模型负责判断某个新观察到的命令或流程是否值得记忆。

Only a small hot set is injected directly into context. The full workflow list
stays on disk under `.minicodex2/memory/workflows.json` and can be searched with
`search_workflows`.

每次上下文只注入少量高价值热流程。完整流程列表保存在
`.minicodex2/memory/workflows.json`，需要时通过 `search_workflows` 查询。

## What Goes Into Model Context

Recommended runtime summary:

推荐注入的 Runtime 摘要：

```text
ProjectProfile:
  root: <workspace>
  detected_types: python, node, c, ...
  key_files: pyproject.toml, package.json, Makefile
  likely_entrypoints: app.py, main.c
  test_signals: tests/, pytest.ini, package.json scripts

VerificationHint:
  confidence: high | medium | low
  recommended_commands:
    - python -m pytest
  reason:
    - pytest.ini exists
    - tests/ directory exists

SessionState:
  changed_files:
    - src/example.py
  active_background_services:
    - port: 5000
      command: python app.py
  last_verification:
    status: passed | failed | blocked | not_run
```

## What Should Not Be Included Blindly

Do not blindly include:

不要盲目注入：

- Full project tree.
  完整项目树。
- Full file contents unless the model asked to read them.
  完整文件内容，除非模型明确读取。
- Full command logs.
  完整命令日志。
- Full test output.
  完整测试输出。
- Old conversation turns that are no longer relevant.
  已不相关的旧对话。

## Workspace Map, ReadSet, and ChangeSet

Code agents need a project-awareness layer before they can reliably edit multi-file projects. This layer is not business logic and should not decide what the bug means. It only gives the model a compact, current map of the workspace so the model can choose which files to inspect.

代码 Agent 需要一个项目感知层，才能稳定处理多文件项目。这个层不是业务判断，也不应该替模型决定 bug 的含义。它只负责把 workspace 的当前轮廓压缩后提供给模型，让模型知道项目大概在哪里、最近改了什么、哪些文件已经读过，然后由模型决定继续读哪些文件和怎么修。

MiniCodex2 should maintain three lightweight structures:

MiniCodex2 应维护三个轻量结构：

```text
WorkspaceMap:
  version: monotonically increasing workspace map version
  root: workspace root
  top_level_entries: compact root directory listing
  key_files: README, package.json, pyproject.toml, go.mod, Cargo.toml, Makefile, etc.
  source_roots: compact candidate source/test directories
  truncated: whether the map was truncated
  ignored_dirs: dependency/build/cache directories excluded from scanning

ReadSet:
  path
  read_at_workspace_version
  short_summary_or_excerpt_reference
  stale: true when file changed after read

ChangeSet:
  added_files
  modified_files
  deleted_files
  changed_by_tool_call
  changed_by_command_snapshot
```

Injection policy:

注入策略：

- Do not inject the full project tree every model call.
  不要每轮都把完整项目树塞进模型上下文。
- Inject a compact `WorkspaceMap` summary when the task involves code/project work.
  当任务涉及代码或项目操作时，注入压缩后的 `WorkspaceMap` 摘要。
- Always inject recent `ChangeSet` entries after file writes, edits, deletes, or command-detected file changes.
  写入、编辑、删除文件，或命令检测到文件变化后，应始终注入最近的 `ChangeSet`。
- Inject `ReadSet` so the model knows which files it has actually seen and which reads may be stale.
  注入 `ReadSet`，让模型知道哪些文件它真的读过，哪些读取可能已经过期。
- If the map is truncated, explicitly tell the model to use `list_directory`, `find_files`, `search_text`, or `read_file` instead of guessing paths.
  如果 map 被截断，要明确告诉模型继续用 `list_directory`、`find_files`、`search_text` 或 `read_file` 查找，而不是猜路径。

Codex source reading suggests a similar principle rather than a full always-on project index. Codex injects environment context such as cwd, shell, workspace roots, permissions, and dates. In realtime startup context it also builds a bounded workspace tree: depth 2, up to 20 entries per directory, excluding noisy directories such as `.git`, `node_modules`, `target`, `dist`, and `build`. It does not appear to blindly send a full repository tree each normal turn.

阅读 Codex 源码后，目前看到的是类似原则，而不是每轮注入完整项目索引。Codex 会注入 cwd、shell、workspace roots、权限、日期等环境上下文。在 realtime startup context 中，它还会构造一个有限 workspace tree：深度 2、每个目录最多 20 项，并排除 `.git`、`node_modules`、`target`、`dist`、`build` 等噪声目录。它看起来并不会在普通回合中盲目发送完整仓库树。

This means MiniCodex2 should not solve multi-file project repair by hard-coding framework-specific guesses. The runtime should provide perception primitives and compact facts; the model remains responsible for deciding which files matter.

这意味着 MiniCodex2 不应该通过硬编码具体框架猜测来解决多文件项目修复问题。Runtime 应提供感知器官和压缩事实；哪些文件关键、如何修复，仍由模型判断。

Runtime evidence should be tracked alongside workspace evidence:

Runtime 执行证据应和 workspace 证据一起维护：

```text
RuntimeResourceMap:
  commands: command/cwd/status/exit_code/log_path/stdout_excerpt/stderr_excerpt
  active_processes: pid/command/cwd/log_path/poll_hint
  observed_ports: port/pid/process_name/source/observed_at
  http_evidence: method/url/status_code/body_excerpt/error/observed_at
  file_evidence: path/exists/size/modified_at/source

EvidencePack:
  user_goal
  latest_plan
  commands_run
  tool_errors
  verification_errors
  observed_resources
  files_changed_since_last_evidence
  files_read_since_last_evidence
  suggested_next_observation_tools
```

This evidence is intentionally factual. It should not conclude that a process is "frontend", "backend", or "API" unless the project config or the model has made that explicit.

这些证据应保持事实性。除非项目配置或模型已经明确判断，否则 runtime 不应直接断言某个进程是“前端”“后端”或“API”。

## ContextManager

`ContextManager` builds the final messages passed to `ModelAdapter`.

`ContextManager` 负责构建传给 `ModelAdapter` 的最终 messages。

Responsibilities:

职责：

- Keep system instructions and current user request.
  保留系统指令和当前用户请求。
- Keep recent conversation turns.
  保留最近对话。
- Keep recent tool results when they are small.
  工具结果较小时保留原文。
- Summarize large tool results.
  摘要化大型工具结果。
- Keep the latest `FailurePack` mostly intact.
  尽量完整保留最新 `FailurePack`。
- Add compact `ProjectProfile` and `VerificationHint`.
  添加压缩后的 `ProjectProfile` 和 `VerificationHint`。
- Drop or summarize stale runtime details.
  丢弃或摘要化过期 Runtime 细节。

## History Compression Policy

Suggested compression order when approaching context budget:

接近上下文预算时，建议压缩顺序：

1. Truncate command stdout/stderr to relevant excerpts.
   截断命令 stdout/stderr，只保留相关片段。
2. Summarize old tool results.
   摘要化旧工具结果。
3. Summarize old assistant reasoning and status messages.
   摘要化旧 assistant 推理和状态消息。
4. Keep user requirements and explicit constraints.
   保留用户需求和明确约束。
5. Keep latest `FailurePack`.
   保留最新 `FailurePack`。
6. Keep changed file list.
   保留变更文件列表。

Never drop:

不要丢弃：

- Current user request.
  当前用户请求。
- Explicit user constraints.
  用户明确约束。
- Permission decisions.
  权限决策。
- Latest blocking failure.
  最新 blocking failure。
- Files changed in the current turn.
  当前回合变更文件。

## Codex Source Reading Notes: Context Compaction

This section records design lessons from reading the local Codex source under
`playground/codex-main/codex-rs`. These notes are design input, not a claim that
MiniCodex2 already implements the same behavior.

本节记录阅读本地 Codex 源码 `playground/codex-main/codex-rs` 后得到的上下文压缩设计启发。
这些内容是后续设计输入，不代表 MiniCodex2 当前已经实现同等能力。

Relevant Codex files:

相关 Codex 文件：

- `core/src/compact.rs`
- `core/src/compact_remote.rs`
- `core/src/compact_remote_v2.rs`
- `core/src/session/turn.rs`
- `prompts/templates/compact/prompt.md`
- `prompts/templates/compact/summary_prefix.md`

Codex has multiple compaction paths:

Codex 有多条压缩路径：

- Local compaction: run a special model call with a compaction prompt, then use the final assistant message as a handoff summary.
- Remote compaction: call OpenAI-side `/responses/compact`.
- Remote compaction v2: send history plus a compaction trigger and receive a structured compaction output item.

MiniCodex2 should not assume that Codex's remote compaction endpoint is available through generic OpenAI-compatible APIs. The portable implementation path is local model compaction:

MiniCodex2 不应假设通用 OpenAI-compatible API 一定提供 Codex 的远端压缩接口。可移植实现路径应是本地模型压缩：

```text
if context is near budget:
  build a special compaction request
  call the model with tools disabled
  ask for a structured handoff summary
  replace old chat/tool history with:
    - selected recent user messages
    - model-generated compaction summary
  preserve structured runtime state separately
  rebuild canonical runtime context on the next normal model call
```

Codex's local compaction prompt asks the model to preserve:

Codex 本地压缩提示词要求模型保留：

- current progress and key decisions
- important context, constraints, and user preferences
- clear next steps
- critical data, examples, and references needed to continue

MiniCodex2 should use a stronger structured schema for code-agent continuity:

MiniCodex2 应使用更适合代码 Agent 连续执行的结构化摘要：

```text
CompactionSummary:
  root_objective
  active_work_plan
  current_step
  completed_work
  changed_files
  commands_run
  verification_results
  unresolved_failures
  important_runtime_facts
  important_user_constraints
  next_concrete_actions
```

Structured runtime state must not rely only on compaction summaries.

结构化运行状态不能只依赖压缩摘要保存。

Keep these as durable runtime state and re-inject them after compaction:

这些内容应作为持久 runtime state 保存，并在压缩后重新注入：

- active goal
- work plan
- current step
- evidence records
- latest failure pack
- engineering facts / runtime observations
- runtime resources
- project guidance
- environment context
- permission and safety context

The compaction summary is for old chat/tool history semantics. Durable state is owned by runtime.

压缩摘要负责保留旧聊天和工具历史的语义；持久状态由 Runtime 管理。

### Trigger Policy

Codex triggers compaction from actual token usage and model metadata, not only a fixed percentage. It checks `model_auto_compact_token_limit` and the effective model context window.

Codex 主要根据真实 token usage 和模型元数据触发压缩，而不是只用固定百分比。它会检查 `model_auto_compact_token_limit` 和有效模型上下文窗口。

MiniCodex2 should evolve from estimated character-based budgeting toward:

MiniCodex2 后续应从字符估算逐步升级为：

- Use provider-returned usage whenever available.
- Keep estimated usage only as fallback.
- Make `context_budget_tokens` and `compression_threshold` configurable.
- Avoid overly early lossy compression.
- Emit context budget events at meaningful thresholds.

### Retention Policy

Codex retains recent user messages by token budget, not by a fixed message count. The local path retains up to about `20_000` tokens of user messages. Remote v2 retains selected user/developer/system messages up to about `64_000` tokens.

Codex 按 token 预算保留近期用户消息，而不是固定消息条数。本地压缩路径大约保留 `20_000` tokens 的用户消息；remote v2 约保留 `64_000` tokens 的 user/developer/system 消息。

MiniCodex2 should replace "keep last N messages" with token-budgeted retention:

MiniCodex2 应把“保留最近 N 条消息”改为按 token 预算保留：

- recent user intent
- latest assistant/tool exchange
- latest blocking failure
- recent changed-file evidence
- selected high-value older constraints

### Large Tool Outputs

Codex rewrites oversized function-call outputs before remote compaction so the compaction request itself can fit the model context. The replacement preserves that output existed but says it exceeded available context.

Codex 会在远端压缩前重写过大的工具输出，避免压缩请求本身超过上下文窗口。替换内容保留“输出存在且过大”的事实。

MiniCodex2 should store large command logs on disk and inject:

MiniCodex2 应把大型命令日志存盘，并只注入：

- command
- exit code
- first blocking failure
- short stdout/stderr excerpt
- log path
- whether output was truncated

### Required Tests

Additional tests required before this design is considered implemented:

在认为该设计已实现前，需要补充测试：

- `test_model_compaction_preserves_root_objective`
- `test_model_compaction_preserves_user_constraints`
- `test_model_compaction_preserves_latest_failure_pack`
- `test_model_compaction_preserves_runtime_facts_via_state_reinjection`
- `test_context_uses_actual_provider_usage_when_available`
- `test_large_tool_output_is_referenced_by_log_path`
- `test_compaction_rebuilds_canonical_runtime_context`

## TokenUsageTracker

`TokenUsageTracker` records usage per model call, turn, and session.

`TokenUsageTracker` 按模型调用、回合和会话记录 token 消耗。

Suggested fields:

建议字段：

```text
model_name
call_id
turn_id
prompt_tokens
completion_tokens
total_tokens
estimated
context_budget
timestamp
```

If exact usage is unavailable, estimates should be recorded and marked with `estimated: true`.

如果无法获得精确用量，应记录估算值并标记 `estimated: true`。

## Budget Defaults

Suggested defaults:

建议默认值：

```text
context_budget_tokens: provider_default_or_configured
warning_threshold: 0.80
compression_threshold: 0.70
hard_limit_threshold: 0.95
```

Behavior:

行为：

- At 70%, start summarizing older context.
  到 70% 时开始摘要旧上下文。
- At 80%, warn in runtime events.
  到 80% 时通过 Runtime event 警告。
- At 95%, aggressively compress or block with a clear context-budget error.
  到 95% 时激进压缩，或用清晰的上下文预算错误 blocked。

## Tests

Required tests:

必需测试：

- `test_context_includes_project_profile_summary`
- `test_context_includes_verification_hint`
- `test_context_does_not_include_full_large_logs`
- `test_context_keeps_latest_failure_pack`
- `test_history_compression_preserves_user_constraints`
- `test_token_usage_tracker_records_actual_usage`
- `test_token_usage_tracker_marks_estimates`

## Request Soft Limit And Compaction Budget

MiniCodex2 uses an API-cost-oriented context budget. The primary setting is not the model's
physical context window. It is the maximum request size we are willing to send before compacting.

MiniCodex2 使用面向 API 成本控制的上下文预算。核心配置不是模型物理上下文窗口，
而是“单次请求在多大 token 规模时开始压缩”。

```toml
[context]
trigger_auto_compact_limit_tokens = 8000
post_compact_ratio = 0.60
baseline_ratio = 0.25
model_context_limit_tokens = 50000
```

Field meaning:

字段含义：

- `trigger_auto_compact_limit_tokens`: soft cap for one model request. When the estimated request exceeds this value, compact old history.
  `trigger_auto_compact_limit_tokens`：单次模型请求软上限。估算请求超过该值时，触发自动压缩。
- `post_compact_ratio`: target size after compaction, relative to `trigger_auto_compact_limit_tokens`.
  `post_compact_ratio`：压缩后的目标规模，占 `trigger_auto_compact_limit_tokens` 的比例。
- `baseline_ratio`: budget reserved for durable context such as goal, plan, runtime state, workspace map, and compacted summary.
  `baseline_ratio`：给持久上下文预留的预算，例如 goal、plan、runtime state、workspace map、压缩摘要。
- `model_context_limit_tokens`: physical or provider-level maximum context limit. This is a safety guard, not the normal cost-control trigger.
  `model_context_limit_tokens`：模型或 provider 层面的物理上下文上限。这是安全保护，不是日常成本控制触发点。

The budget planner derives internal values from the small public configuration surface:

预算规划器从少量公开配置推导内部预算：

```text
trigger_tokens = trigger_auto_compact_limit_tokens
post_compact_target_tokens = trigger_auto_compact_limit_tokens * post_compact_ratio
baseline_target_tokens = trigger_auto_compact_limit_tokens * baseline_ratio
history_after_compact = post_compact_target_tokens - bounded_baseline_target_tokens
reserve_tokens = trigger_auto_compact_limit_tokens - post_compact_target_tokens
```

The key rule is that increasing `trigger_auto_compact_limit_tokens` should mostly increase usable recent
conversation and reserve capacity. It should not blindly make the long-term summary grow linearly
forever. Durable summaries need a cap; otherwise compaction can still return near the trigger size
and immediately compact again.

关键规则是：提高 `trigger_auto_compact_limit_tokens` 时，主要应该增加“可继续对话的空间”和
reserve，而不是让长期摘要无限线性膨胀。持久摘要必须有上限，否则压缩后仍接近触发线，
很快又会再次压缩。

Example derived budgets:

示例推导结果：

| `trigger_auto_compact_limit_tokens` | trigger | runtime summary | runtime section | compact summary chars | recent target | reserve |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4200 | 4200 | 1176 | 493 | 1000 | 464 | 2310 |
| 5000 | 5000 | 1400 | 588 | 1188 | 553 | 2750 |
| 8000 | 8000 | 2000 | 840 | 3600 | 1900 | 3200 |
| 12000 | 12000 | 2200 | 900 | 3600 | 2300 | 6600 |

### Comparison With Codex Source

### 与 Codex 源码机制对比

The local Codex source under `playground/codex-main/codex-rs` separates two concerns:

本地 Codex 源码 `playground/codex-main/codex-rs` 中把两个问题分开处理：

- `model_context_window`: the hard model context capacity.
  `model_context_window`：模型硬上下文容量。
- `model_auto_compact_token_limit`: the auto-compaction trigger.
  `model_auto_compact_token_limit`：自动压缩触发线。

Important source files:

关键源码文件：

- `core/src/session/turn.rs`
- `core/src/state/auto_compact_window.rs`
- `core/src/compact.rs`
- `core/src/compact_remote.rs`
- `core/src/compact_remote_v2.rs`
- `core/src/context_manager/history.rs`

Codex supports different auto-compact scopes. In `BodyAfterPrefix` mode, it subtracts a baseline
prefix (`prefill_input_tokens`) and triggers compaction based on growth after that prefix. In `Total`
mode, it can trigger based on total request size. It also independently checks whether the full model
context window is near exhaustion.

Codex 支持不同的自动压缩范围。在 `BodyAfterPrefix` 模式下，它会扣掉 baseline prefix
（`prefill_input_tokens`），根据 prefix 之后新增的 body token 增长触发压缩。在 `Total`
模式下，它也可以根据总请求大小触发。同时它还会独立检查是否接近模型物理上下文窗口。

MiniCodex2 deliberately uses a simpler first-version model:

MiniCodex2 第一版有意采用更简单的模型：

- `trigger_auto_compact_limit_tokens` is total request soft cap for API cost control.
  `trigger_auto_compact_limit_tokens` 是用于 API 成本控制的总请求软上限。
- `model_context_limit_tokens` is the physical safety guard.
  `model_context_limit_tokens` 是物理安全保护。
- `post_compact_ratio` controls how much room remains after compaction.
  `post_compact_ratio` 控制压缩后留下多少继续工作的空间。
- `baseline_ratio` controls durable runtime/context overhead.
  `baseline_ratio` 控制持久 runtime/context 信息占比。

This differs from Codex's `BodyAfterPrefix` growth-window model. The MiniCodex2 choice is better for
early API-key testing because the user cares about per-request token cost, not only model overflow.
If MiniCodex2 later has high context limits and stable pricing, it can add a Codex-like body-growth
scope while keeping the current total-request soft cap as a cost guard.

这和 Codex 的 `BodyAfterPrefix` 增量窗口模型不同。MiniCodex2 当前选择更适合早期 API key
测试，因为用户关心的是单次请求成本，而不只是模型是否溢出。后续如果 MiniCodex2 拥有更高
上下文限制和稳定价格，可以再增加类似 Codex 的 body-growth scope，同时保留当前总请求软上限
作为成本保护。

### Prompt Cache Layout

### Prompt Cache 布局

Provider-side prompt cache is usually prefix based. A code agent should therefore keep the earliest
model-context bytes as stable as possible. It should not mix frequently changing execution facts
into the first runtime message if those facts can be placed later.

Provider 侧 prompt cache 通常依赖稳定前缀。因此 Code Agent 应尽量让模型上下文最前面的内容保持稳定。
不要把每轮都会变化的执行状态、工具事实、端口观察、token 统计等内容混进第一条 runtime message 的前面部分。

MiniCodex2 now builds runtime context in two logical bands:

MiniCodex2 当前把 runtime context 分成两个逻辑带：

1. Stable prefix:
   `RUNTIME PROTOCOL`, `PROJECT GUIDANCE`, and `PROJECT MEMORY INDEX`.
   These sections should change rarely and should appear first.
2. Dynamic state:
   `WORK MEMORY`, `EVIDENCE STORE`, `WORKSPACE FACTS`, `EVIDENCE PACK`,
   `ENGINEERING FACTS`, `RUNTIME RESOURCE MAP`, and `EXECUTION STATE`.
   These sections may change every turn and should appear after the stable prefix.

This is not meant to hide information from the model. The model still receives the same kind of
facts, but the volatile facts start later in the serialized request. This gives cache providers a
longer reusable prefix while preserving model-visible runtime state.

这不是为了少给模型信息。模型仍然能看到同类事实，只是高频变化的事实更靠后。这样可以让 provider 复用更长的前缀，
同时不牺牲模型可见的 runtime state。

Tests:

测试：

- `test_runtime_context_keeps_dynamic_state_after_stable_prefix`
- `test_context_injects_structured_work_memory`
- `test_context_injects_engineering_facts_before_runtime_resources`

### Compaction Requirements

### 压缩实现要求

- Compaction must be a separate model call with a dedicated compression prompt.
  压缩必须是一次独立模型调用，使用专门的压缩提示词。
- The prompt must ask for a target size, not just "summarize".
  提示词必须给目标大小，而不是只说“总结”。
- The compacted summary must preserve root goal, user constraints, current plan, latest failures,
  changed files, unresolved risks, and next actionable work.
  压缩摘要必须保留根目标、用户约束、当前计划、最新失败、已改文件、未解决风险和下一步可执行工作。
- Durable runtime state should be re-injected from structured state, not only recovered from summary text.
  持久 runtime state 应从结构化状态重新注入，而不是只依赖摘要文本恢复。
- Oversized tool output should be replaced by a log reference before compaction.
  超大工具输出应在压缩前替换为日志引用。
## Operational Memory Policy

## 操作型记忆策略

Compaction is not only a way to shorten old chat history. It is a memory-consolidation step that
turns a long transcript into an operational handoff. MiniCodex2 should preserve the information a
future model needs to keep working, especially details that were scattered through normal
conversation.

压缩不只是把旧聊天变短，而是一次记忆整理。它要把长对话变成未来模型可以继续工作的
操作型交接记录，尤其要保留那些分散在普通聊天里的关键细节。

Rules:

规则：

- Later explicit user statements override earlier statements. Keep the current decision, and only
  mention the older decision as superseded context when useful.
- 用户后说的明确决定覆盖早期说法。压缩后应保留当前决定，必要时把旧决定放入“已被覆盖”的上下文。
- Preserve operational facts such as accounts, passwords when the user supplied them for local
  testing, ports, URLs, paths, venv locations, commands, test data, reproduction steps, and
  environment constraints.
- 保留操作事实，例如用户提供的本地测试账号、密码、端口、URL、路径、虚拟环境位置、命令、
  测试数据、复现步骤和环境限制。
- Separate observed facts, user-provided facts, assistant proposals, guesses, failures, and
  verified completions.
- 区分已观察事实、用户提供事实、助手方案、猜测、失败尝试和已验证完成项。
- Preserve failed attempts as failed attempts, so the next model does not repeat them as if they
  were successful.
- 失败尝试要以失败尝试的身份保留，避免后续模型把它当成成功方案重复执行。
- Preserve unresolved product or architecture discussion, including the user's latest question and
  the assistant's public proposal, even if no code was written yet.
- 保留未结束的产品或架构讨论，包括用户最近的问题和助手公开提出的方案，即使当时还没有写代码。

The desired output is a small but structured memory, not a free-form recap. It should contain root
objective, latest user intent, durable facts, current decisions, superseded context, progress,
files/commands/environment, failures/evidence, open questions, and next actions.

理想输出不是自由发挥的聊天摘要，而是小而结构化的记忆：根目标、最新用户意图、持久事实、
当前决定、已覆盖上下文、进度、文件/命令/环境、失败/证据、开放问题和下一步。
